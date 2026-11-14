"""The buying agent: request -> checks -> search -> price & stock -> options -> rules -> act.

Acts in exactly one of four ways (worst rule result decides, see rules.py):
  auto_order      every rule passed -> places a mock order by itself
  needs_approval  something needs a person -> waits on the dashboard
  ask_buyer       request unclear or unusually big -> asks the buyer, orders nothing
  cannot_fulfil   no option can be used at all -> says why, orders nothing

Every step is recorded in record["steps"] as one plain-language line with its source.
Every sentence is built from data or rule results; nothing is invented.
"""
from datetime import date, datetime, timezone

from . import ai, aimo, catalog, demo, log, orders, rules, store

MAX_SPLIT_PARTNERS = 8  # how many second products to try in a split


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _step(rec, step, msg, source, actor="agent", **data):
    """Record one step: in the request's own record AND in the audit log (data/log.jsonl)."""
    e = log.write(step, msg, source, actor=actor, request_id=rec["id"], data=data or None)
    rec["steps"].append({"log_id": e["id"], "time": e["time"], "actor": actor, "request_id": rec["id"],
                         "step": step, "message": msg, "source": source, "data": data or None})


def _name(pid):
    p = catalog.get(pid)
    return p["name_en"] or p["name_fi"]


def _as_much_as_possible(rec, req, scenario):
    """Work out the amount for 'as much as possible'. Returns a finished record if it has to ask, else None."""
    item = rules.find_usual_item(req["item_id"])
    pid, unit = item["usual_product"], item["unit"]
    p = catalog.get(pid)
    try:
        q = demo.apply_stock_overrides(aimo.quote([pid], req["deadline"]), scenario)["products"].get(pid) or {}
    except aimo.AimoError as e:
        q = {}
        _step(rec, "lookup", f"Could not ask Valio Aimo about {pid}: {e}", "Aimo API")
    stock = _usable_stock_units(q) if q else 0
    if q and q.get("deliverable") and q.get("available_date") and q["available_date"] > req["deadline"]:
        stock = 0
    per = catalog.content_per_sales_unit(p, unit)
    name = p["name_en"] or p["name_fi"]
    if stock is None:
        question = (f"Valio Aimo reports no stock limit for {name} ({pid}), so 'as much as possible' has no number. "
                    f"How much do you need? (Usual order: {rules.num(item['usual_amount'])} {unit.lower() if unit == 'KG' else unit}.)")
    elif not stock or not per:
        why = ("its order cut-off has passed" if q.get("cutoff_passed") else
               "it can't be delivered by then" if q.get("deliverable") else "Valio Aimo gave no delivery date")
        question = (f"None of the usual {name} ({pid}) can be ordered for {rules.nice_date(req['deadline'])}: {why}. "
                    f"Tell me an amount and I will look for other products, or choose a later date.")
    else:
        units = rules.whole_steps_within(p, stock)
        if req.get("max_amount"):  # follow-up: never more than was first asked for
            units = min(units, rules.units_for(pid, req["max_amount"], unit))
        req["amount"] = round(units * per, 3)
        req["buyer_confirmed_amount"] = True  # the buyer asked for everything: not a typo
        req["amount_reason"] = "everything available"
        rec["current_request"] = (f"{rules.num(req['amount'])} {unit.lower() if unit == 'KG' else unit} of {item['name'].lower()} "
                                  f"by {rules.nice_date(req['deadline'])} (everything available)")
        _step(rec, "lookup", f"'As much as possible': Valio Aimo has {rules.num(stock)} {p['sales_unit']} of {name} ({pid}) "
              f"for {rules.nice_date(req['deadline'])}" + (" (demo stock)" if q.get("demo_stock_override") else "")
              + f". The agent will ask for {rules.num(req['amount'])} {unit}.", "Aimo API", amount=req["amount"])
        return None
    rec["request_checks"] = []
    rec.update(status="ask_buyer", outcome="ask_buyer", options=[], chosen=None, ask_kind="missing", ask_fields=["amount"],
               question=question)
    _step(rec, "ask", f"Asked the buyer: {question}", "agent")
    return _finish(rec)


def _finish(rec):
    """Last thing on every path: write the short summary (from rule results only), then save."""
    rec["summary"] = ai.summarize(rec)
    return store.save(rec)


# ---------------------------------------------------------------- finding products

def _candidates(req):
    """Products that could fill the request, from the product file (no API calls)."""
    item = rules.find_usual_item(req.get("item_id")) if req.get("item_id") else None
    if req.get("only_products"):  # a substitute the chef picked: order exactly this product
        hits = [catalog.get(pid) for pid in req["only_products"] if catalog.get(pid)]
        labels = item["search"].get("labels", []) if item else []
        return item, [p for p in hits if catalog.content_per_sales_unit(p, req["unit"])], labels
    if item:
        s = item["search"]
        hits = catalog.search(s.get("words", ""), s.get("labels", []), s.get("category"), limit=200)
        labels = s.get("labels", [])
    else:
        hits = catalog.search(req["product"] or "", [], None, limit=200)
        labels = []
    # Only products whose pack size is known in the requested unit (litres or kilograms).
    usable = [p for p in hits if catalog.content_per_sales_unit(p, req["unit"])]
    return item, usable, labels


# ---------------------------------------------------------------- building options

def _line(pid, qty, role, quote, unit="L"):
    q = quote["products"].get(pid) or {}
    p = catalog.get(pid)
    per = catalog.content_per_sales_unit(p, unit)
    price = q.get("price_per_unit_with_tax")
    return {"product_id": pid, "name": _name(pid), "brand": p["brand"], "quantity": qty,
            "sales_unit": p["sales_unit"], "unit": unit, "litres": round(qty * (per or 0), 3),  # "litres" = amount in `unit`
            "price_per_unit_with_tax": price,
            "line_total_with_tax": round(qty * price, 2) if price is not None else None,
            "role": role,
            # facts for the dashboard, straight from the API answer
            "litres_per_sales_unit": per, "content_per_sales_unit": per,
            "stock_status": q.get("stock_status"), "stock_amount": q.get("stock_amount"),
            "demo_stock_override": bool(q.get("demo_stock_override")),
            "available_date": q.get("available_date"), "closing_time": q.get("closing_time"),
            "tax_rate": q.get("tax_rate"), "price_valid_to": q.get("price_valid_to"),
            "demo_price_override": bool(q.get("demo_price_override"))}


def _usable_stock_units(q):
    """How many sales units we can take from this product (None = no known limit)."""
    if not q or not q["deliverable"] or q["sales_block"] or q["cutoff_passed"] or not q["priced"]:
        return 0
    if q["stock_status"] == "known":
        return int(q["stock_amount"])
    return None


def _role(pid, usual):
    if usual and pid == usual["usual_product"]:
        return "usual product"
    if usual and any(s["product_id"] == pid for s in usual.get("recipe_swaps", [])):
        return "recipe swap"
    if usual and pid in (catalog.get(usual["usual_product"]) or {}).get("substitutes", []):
        return "official substitute"
    return "alternative"


def _price_per_litre(pid, quote, unit="L"):
    """Price per litre or per kilogram (whichever unit the request uses)."""
    q = quote["products"].get(pid) or {}
    per = catalog.content_per_sales_unit(catalog.get(pid), unit)
    if not q.get("priced") or not per:
        return float("inf")
    return q["price_per_unit_with_tax"] / per


def build_options(req, usual, cand_ids, quote):
    amount, unit = req["amount"], req["unit"]
    options = []
    # 1) One product covers everything.
    for pid in cand_ids:
        qty = rules.units_for(pid, amount, unit)
        if qty:
            options.append({"kind": "single", "lines": [_line(pid, qty, _role(pid, usual), quote, unit)]})

    # 2) Split (rule C4): only if no single product can cover the amount by the deadline.
    def covers(o):
        pid, qty = o["lines"][0]["product_id"], o["lines"][0]["quantity"]
        stock = _usable_stock_units(quote["products"].get(pid))
        return stock is None or stock >= qty if stock != 0 else False
    split_needed = not any(covers(o) for o in options)
    if split_needed:
        # Base: the usual product if it has some stock, else the cheapest product with some stock.
        base = None
        if usual and usual["usual_product"] in cand_ids and _usable_stock_units(quote["products"].get(usual["usual_product"])):
            base = usual["usual_product"]
        else:
            with_stock = [pid for pid in cand_ids if _usable_stock_units(quote["products"].get(pid))]
            base = min(with_stock, key=lambda x: _price_per_litre(x, quote, unit)) if with_stock else None
        if base:
            base_units = _usable_stock_units(quote["products"][base])
            per = catalog.content_per_sales_unit(catalog.get(base), unit)
            base_units = min(rules.whole_steps_within(catalog.get(base), base_units), rules.units_for(base, amount, unit))
            rest = amount - base_units * per
            subs = (catalog.get(usual["usual_product"]) or {}).get("substitutes", []) if usual else []
            partners = [pid for pid in cand_ids if pid != base and _usable_stock_units(quote["products"].get(pid)) != 0]
            partners.sort(key=lambda x: (x not in subs, _price_per_litre(x, quote, unit)))  # official substitutes first
            for pid in partners[:MAX_SPLIT_PARTNERS]:
                qty = rules.units_for(pid, rest, unit)
                if qty:
                    options.append({"kind": "split", "lines": [
                        _line(base, float(base_units), _role(base, usual), quote, unit),
                        _line(pid, qty, _role(pid, usual), quote, unit)]})
    return options, split_needed


def _rank_key(o, usual):
    approvals = sum(1 for r in o["results"] if r["result"] == "approve")
    usual_litres = sum(l["litres"] for l in o["lines"] if usual and l["product_id"] == usual["usual_product"])
    return (approvals, -usual_litres, o["total_with_tax"])


def _first_problem(o):
    bad = [r for r in o["results"] if r["result"] != "pass"]
    worst = max(bad, key=lambda r: rules.SEVERITY[r["result"]]) if bad else None
    return worst["detail"] if worst else "all rules passed"


# ---------------------------------------------------------------- main entry

def handle(text=None, parsed=None, scenario=None, today=None, record=None, origin="typed", trigger=None):
    """Run the whole flow for one request. Returns the saved record.
    origin: "typed" (a person wrote it) or "auto" (the stock monitor sent it; `trigger` says why)."""
    rec = record or {"id": store.new_id(), "received_at": _now(), "text": text, "scenario": scenario,
                     "origin": origin, "steps": [], "history": []}
    if trigger and not record:
        rec["trigger"] = trigger
        _step(rec, "trigger", trigger, "stock monitor", actor="system")
    req = parsed or ai.parse_request(text, today)
    rec["parsed"] = req
    rec["not_checked"] = list(rules.rules()["not_checked"])
    amt = rules.num(req["amount"]) if req.get("amount") is not None else "?"
    _step(rec, "request", f"Read request: {amt} {req.get('unit') or '?'} {req.get('product') or '?'}, "
          f"deadline {rules.nice_date(req['deadline']) if req.get('deadline') else '?'}.", req.get("parsed_by", "parser"), parsed=req)
    if scenario:
        _step(rec, "demo", f"Demo scenario '{scenario}': simulated stock numbers will be used.", "demo_stock_overrides.json")

    # The AI read an amount that was not written as a number: check with the buyer first.
    if req.get("unverified") and not req.get("buyer_confirmed_understanding"):
        rec["request_checks"] = []
        rec.update(status="ask_buyer", outcome="ask_buyer", options=[], chosen=None, ask_kind="understanding",
                   question=f"I understood: {rules.num(req['amount'])} {req.get('unit') or ''} of "
                            f"{req.get('product') or 'the product'}"
                            + (f" by {rules.nice_date(req['deadline'])}" if req.get("deadline") else "") + ". Is that right?")
        rec["not_checked"].insert(0, "Products, prices and stock: not looked up until the buyer answers")
        _step(rec, "ask", f"Asked the buyer: {rec['question']}", "agent")
        return _finish(rec)
    rec.pop("ask_kind", None)

    # "As much as possible": the amount = what Valio Aimo can deliver of the usual product by the deadline.
    if req.get("as_much_as_possible") and req.get("amount") is None and req.get("deadline") and req.get("item_id"):
        found = _as_much_as_possible(rec, req, scenario)
        if found is not None:
            return found

    # A: checks on the request itself
    req_results = rules.check_request(req)
    rec["request_checks"] = [r.to_dict() for r in req_results]
    for r in req_results:
        _step(rec, "rules", f"{r.rule} - {r.name}: {r.result}. {r.detail}", "rules.yaml")
    if rules.outcome(req_results) in ("ask_buyer", "blocked"):
        bad = [r for r in req_results if r.result != "pass"]
        missing = next((r.facts.get("missing") for r in bad if r.rule == "A2" and r.facts.get("missing")), None)
        if req.get("as_much_as_possible") and missing:
            missing = [f for f in missing if f != "amount"]  # 'as much as possible' is the amount
        rec.update(status="ask_buyer", outcome="ask_buyer", options=[], chosen=None,
                   ask_kind="missing" if missing else "size", ask_fields=missing or [],
                   question=" ".join(r.detail for r in bad))
        rec["not_checked"].insert(0, "Products, prices and stock: not looked up until the buyer answers")
        _step(rec, "ask", f"Asked the buyer: {rec['question']}", "agent")
        return _finish(rec)

    # Search the product file
    usual, cands, labels = _candidates(req)
    rec["required_labels"] = labels
    if not cands:
        rec.update(status="cannot_fulfil", outcome="cannot_fulfil", options=[], chosen=None,
                   reason=f"No products found for '{req['product']}' that can be measured in {req['unit']}.")
        _step(rec, "search", rec["reason"], "product file")
        return _finish(rec)
    _step(rec, "search", f"Found {len(cands)} matching products"
          + (f" with label {', '.join(labels)}" if labels else "") + ".", "product file",
          products=[p["id"] for p in cands])

    # Price & stock from the Aimo API (only for these few products)
    try:
        quote = aimo.quote([p["id"] for p in cands], req["deadline"])
    except aimo.AimoError as e:
        rec.update(status="cannot_fulfil", outcome="cannot_fulfil", options=[], chosen=None,
                   reason=f"Valio Aimo API error: {e}")
        _step(rec, "lookup", rec["reason"], "Aimo API")
        return _finish(rec)
    quote = demo.apply_stock_overrides(quote, scenario)
    rec["quote_checked_at"] = quote["checked_at"]
    qp = quote["products"]
    ok = sum(1 for p in qp.values() if p["deliverable"] and not p["cutoff_passed"] and not p["sales_block"])
    cut = sum(1 for p in qp.values() if p["cutoff_passed"])
    nodel = sum(1 for p in qp.values() if not p["deliverable"])
    _step(rec, "lookup", f"Asked price and stock for {len(qp)} products, delivery {rules.nice_date(req['deadline'])}: "
          f"{ok} can be delivered, {cut} missed the order cut-off, {nodel} cannot be delivered.", "Aimo API",
          checked_at=quote["checked_at"])
    overridden = [pid for pid, p in qp.items() if p.get("demo_stock_override")]
    if overridden:
        _step(rec, "demo", "Demo stock override applied to " + ", ".join(
            f"{pid} ({int(qp[pid]['stock_amount'])})" for pid in overridden) + ".", "demo_stock_overrides.json")

    # Build and check options
    options, split_needed = build_options(req, usual, [p["id"] for p in cands], quote)
    if split_needed:
        _step(rec, "split", "No single product can cover the full amount, so the agent built split options.", "agent")
    for i, o in enumerate(options, 1):
        o["option_id"] = f"O{i}"
        o.update(rules.check_option(o, quote, req, labels))
        o["unit"] = req["unit"]
        o["results"] = req_results_dicts = [r.to_dict() for r in req_results] + o["results"]
        o["outcome"] = rules.outcome(req_results_dicts)
    usable = sorted([o for o in options if o["outcome"] != "blocked"], key=lambda o: _rank_key(o, usual))
    blocked = [o for o in options if o["outcome"] == "blocked"]
    _step(rec, "options", f"Built {len(options)} options: {len(usable)} usable, {len(blocked)} blocked.", "rules.yaml")
    rec["options"] = usable[:5] + blocked[:3]  # keep the record readable

    if not usable:
        # Count, per rule, how many options it blocked: "8 options: B3 Arrives by the deadline".
        counts = {}
        for o in blocked:
            for r in {(r["rule"], r["name"]) for r in o["results"] if r["result"] == "blocked"}:
                counts[r] = counts.get(r, 0) + 1
        parts = [f"{n} option{'s' if n != 1 else ''} failed {rid} - {name}"
                 for (rid, name), n in sorted(counts.items(), key=lambda x: -x[1])]
        rec.update(status="cannot_fulfil", outcome="cannot_fulfil", chosen=None,
                   reason=f"None of the {len(blocked)} options can be used: " + "; ".join(parts) + ".",
                   blocked_by=[{"rule": rid, "name": name, "options": n} for (rid, name), n in counts.items()])
        _step(rec, "result", rec["reason"], "rules.yaml")
        return _finish(rec)

    best = usable[0]
    rec["chosen"] = best["option_id"]
    rec["why_chosen"] = _why(best, usable, usual)
    rec["alternatives"] = [f"{' + '.join(str(int(l['quantity'])) + ' × ' + l['product_id'] for l in o['lines'])}: "
                           f"€{o['total_with_tax']:,.2f}, {_first_problem(o)}" for o in (usable[1:4] + blocked[:2])]
    rec["alternatives_detail"] = [
        {"lines": [{"quantity": l["quantity"], "product_id": l["product_id"], "name": l["name"], "brand": l["brand"]}
                   for l in o["lines"]],
         "total_with_tax": o["total_with_tax"], "outcome": o["outcome"], "note": _first_problem(o)}
        for o in (usable[1:4] + blocked[:3])]
    summary = " + ".join(f"{int(l['quantity'])} × {l['product_id']} ({l['brand']})" for l in best["lines"])
    _step(rec, "choose", f"Chose {best['option_id']}: {summary}, €{best['total_with_tax']:,.2f} incl. tax. "
          f"{rec['why_chosen']}", "agent", option=best["option_id"])
    problems = [r for r in best["results"] if r["result"] != "pass" and not r["rule"].startswith("A")]
    for r in problems:
        _step(rec, "rules", f"{r['rule']} - {r['name']}: {r['result']}. {r['detail']}", "rules.yaml")
    for w in best.get("warnings", []):
        rec["not_checked"].insert(0, f"Stock: {w}")

    if best["outcome"] == "auto_order":
        _step(rec, "rules", f"All {len(best['results'])} rule checks passed.", "rules.yaml")
        _place(rec, best, req, placed_by="agent")
    else:
        rec.update(status="needs_approval", outcome="needs_approval")
        _step(rec, "approval", "Sent to the dashboard for a person to approve or reject.", "agent")
    return _finish(rec)


def _why(best, usable, usual):
    if len(usable) == 1:
        return "It is the only usable option."
    parts = []
    if not any(r["result"] == "approve" for r in best["results"]):
        parts.append("it needs no approval")
    if usual and any(l["product_id"] == usual["usual_product"] for l in best["lines"]):
        parts.append("it uses the usual product")
    cheaper = all(best["total_with_tax"] <= o["total_with_tax"] for o in usable[1:])
    if cheaper:
        parts.append("it is the cheapest usable option")
    return ("Picked because " + ", ".join(parts) + ".") if parts else "Picked by the rulebook order (section D)."


def _place(rec, option, req, placed_by):
    order = orders.place_order(option["lines"], req["deadline"], placed_by, rec["id"],
                               note=f"demo scenario: {rec['scenario']}" if rec.get("scenario") else None)
    rec.update(status="ordered", outcome="ordered", order_id=order["order_id"])
    if req.get("item_id"):
        from . import kitchen  # so the kitchen knows this is on its way and does not reorder
        kitchen.add_incoming(req["item_id"], sum(l["litres"] for l in option["lines"]), order["order_id"],
                             req["deadline"], rec["id"])
    _step(rec, "order", f"Placed mock order {order['order_id']}, €{order['total_with_tax']:,.2f} incl. tax, "
          f"delivery {rules.nice_date(req['deadline'])}. Placed by {placed_by}.", "mock order service", order_id=order["order_id"])


# ---------------------------------------------------------------- people's answers

def decide(rid, decision, by):
    """A person approves or rejects a request that needs approval."""
    rec = store.get(rid)
    if not rec or rec.get("status") != "needs_approval":
        raise ValueError(f"{rid} is not waiting for approval")
    option = next(o for o in rec["options"] if o["option_id"] == rec["chosen"])
    if decision == "reject":
        rec.update(status="rejected", outcome="rejected", decided_by=by)
        _step(rec, "decision", f"Rejected by {by}. Nothing was ordered.", "dashboard", actor=f"person:{by}")
        return _finish(rec)
    if decision != "approve":
        raise ValueError("decision must be 'approve' or 'reject'")
    # Before ordering, ask Valio Aimo again: is the price and stock still what the approver saw?
    req = rec["parsed"]
    checked_before = rec.get("quote_checked_at")
    try:
        fresh = aimo.quote([l["product_id"] for l in option["lines"]], req["deadline"])
    except aimo.AimoError as e:
        rec["notice"] = {"kind": "blocked", "text": f"Could not re-check price and stock with Valio Aimo ({e}). Nothing was ordered. Please try again."}
        _step(rec, "recheck", rec["notice"]["text"], "Aimo API")
        return _finish(rec)
    fresh = demo.apply_stock_overrides(fresh, rec.get("scenario"), phase="approval")
    changes = []
    for l in option["lines"]:
        q = fresh["products"].get(l["product_id"]) or {}
        old, new = l["price_per_unit_with_tax"], q.get("price_per_unit_with_tax")
        if new is not None and old is not None and abs(new - old) > 0.0001:
            changes.append(f"{l['product_id']} went from {rules.eur(old)} to {rules.eur(new)} per unit"
                           + (" (demo price change)" if q.get("demo_price_override") else ""))
    old_total = option["total_with_tax"]
    new_lines = [_line(l["product_id"], l["quantity"], l["role"], fresh, req["unit"]) for l in option["lines"]]
    check = rules.check_option({"lines": new_lines}, fresh, req, rec.get("required_labels", []))
    results = [r for r in option["results"] if r["rule"].startswith("A")] + check["results"]
    rec["quote_checked_at"] = fresh["checked_at"]
    when = rules.nice_time(checked_before) if checked_before else "the first check"

    if rules.outcome(results) == "blocked":
        why = " ".join(r["detail"] for r in check["results"] if r["result"] == "blocked")
        option.update(lines=new_lines, results=results, outcome="blocked", total_with_tax=check["total_with_tax"])
        rec.update(status="cannot_fulfil", outcome="cannot_fulfil",
                   reason=f"{by} pressed Approve, but the re-check with Valio Aimo shows this can no longer be ordered: {why} "
                          f"Nothing was ordered. Send the request again to get new options.")
        rec["notice"] = {"kind": "blocked", "text": rec["reason"]}
        _step(rec, "recheck", rec["reason"], "Aimo API")
        return _finish(rec)

    if changes:
        option.update(lines=new_lines, results=results, outcome=rules.outcome(results), total_with_tax=check["total_with_tax"])
        text = (f"The price changed since it was checked ({when}): " + "; ".join(changes) +
                f". The total is now {rules.eur(check['total_with_tax'])} (was {rules.eur(old_total)}). "
                f"Nothing was ordered. Approve again if the new price is OK.")
        rec["notice"] = {"kind": "changed", "text": text, "old_total": old_total, "new_total": check["total_with_tax"]}
        _step(rec, "recheck", f"{by} pressed Approve. " + text, "Aimo API", actor=f"person:{by}",
              old_total=old_total, new_total=check["total_with_tax"])
        return _finish(rec)

    valid = min((l["price_valid_to"] for l in new_lines if l.get("price_valid_to")), default=None)
    ok_text = (f"Re-checked with Valio Aimo at approval: price and stock are unchanged since {when}."
               + (f" Price valid until {rules.nice_date(valid)}." if valid and not valid.startswith("9999") else ""))
    option.update(lines=new_lines, results=results)
    rec["decided_by"] = by
    _step(rec, "decision", f"Approved by {by}.", "dashboard", actor=f"person:{by}")
    _step(rec, "recheck", ok_text, "Aimo API")
    _place(rec, option, req, placed_by=f"human:{by}")
    rec["notice"] = {"kind": "ok", "text": f"Approved and ordered as {rec['order_id']}. {ok_text}"}
    return _finish(rec)


def answer(rid, confirm=False, new_amount=None, new_deadline=None, new_unit=None):
    """The buyer answers a question: 'is 10,000 L right?', or fills in what was missing (amount, date)."""
    rec = store.get(rid)
    if not rec or rec.get("status") != "ask_buyer":
        raise ValueError(f"{rid} is not waiting for the buyer")
    req = dict(rec["parsed"])
    if new_deadline:
        try:
            if date.fromisoformat(new_deadline) < date.today():
                raise ValueError
        except ValueError:
            raise ValueError("The date must be today or later (YYYY-MM-DD).")
        req["deadline"] = new_deadline
        _step(rec, "answer", f"The buyer set the deadline to {rules.nice_date(new_deadline)}.", "buyer", actor="buyer")
        if new_amount is None and not confirm:
            confirm = None  # only the date was missing
    if new_unit and not req.get("unit"):
        req["unit"] = new_unit
    if new_amount is not None and float(new_amount) <= 0:
        raise ValueError("The amount must be more than 0.")
    if new_amount is not None:
        req["as_much_as_possible"] = False
        req["amount"] = float(new_amount)
        # The buyer typed this amount themselves: that IS their confirmation. Never ask twice.
        req["buyer_confirmed_understanding"] = True
        req["buyer_confirmed_amount"] = True
        rec["amount_changed_from"] = rec["parsed"].get("amount")
        _step(rec, "answer", f"The buyer changed the amount to {rules.num(float(new_amount))} {req['unit']}.", "buyer", actor="buyer")
    elif confirm and rec.get("ask_kind") == "understanding":
        req["buyer_confirmed_understanding"] = True
        _step(rec, "answer", "The buyer confirmed the agent understood the request correctly.", "buyer", actor="buyer")
    elif confirm:
        req["buyer_confirmed_amount"] = True
        _step(rec, "answer", f"The buyer confirmed {rules.num(req['amount'])} {req['unit']}.", "buyer", actor="buyer")
    elif confirm is None:
        pass
    else:
        raise ValueError("give confirm=True, new_amount or new_deadline")
    rec["history"].append({"status": rec["status"], "question": rec.get("question")})
    for k in ("question", "options", "chosen", "why_chosen", "alternatives", "alternatives_detail", "reason", "blocked_by",
              "ask_kind", "ask_fields", "current_request"):
        rec.pop(k, None)
    req["parsed_by"] = rec["parsed"].get("parsed_by", "parser") + " + buyer answer"
    # What the request is NOW, in plain words (the dashboard shows this, with the original text below it).
    if req.get("amount") is not None and req.get("unit"):
        rec["current_request"] = (f"{rules.num(req['amount'])} {req['unit']} of {(req.get('product') or 'product').lower()}"
                              + (f" by {rules.nice_date(req['deadline'])}" if req.get("deadline") else ""))
    return handle(parsed=req, scenario=rec.get("scenario"), record=rec)


# ---------------------------------------------------------------- follow-ups and cancelling

def usual_only(rid, by):
    """'Order only what is available of the usual product.' Starts a new request for everything
    Valio Aimo has of the usual product by the same deadline (it goes through all the rules again).
    If the original is still waiting for a decision, it is closed as replaced."""
    rec = store.get(rid)
    if not rec or not (rec.get("parsed") or {}).get("item_id"):
        raise ValueError(f"{rid} is not about one of the kitchen's usual items")
    if rec.get("status") not in ("needs_approval", "rejected", "cannot_fulfil"):
        raise ValueError(f"{rid} is {rec.get('status')}; nothing to replace")
    old = rec["parsed"]
    if rec["status"] == "needs_approval":
        rec.update(status="cancelled", outcome="cancelled", decided_by=by)
        _step(rec, "decision", f"Closed by {by}: replaced by an order for only the usual product.", "dashboard", actor=f"person:{by}")
        store.save(rec)
    else:
        _step(rec, "decision", f"{by} asked for only the usual product instead.", "dashboard", actor=f"person:{by}")
        store.save(rec)
    item = rules.find_usual_item(old["item_id"])
    new = handle(text=f"Only the usual product, as much as is available (instead of {rid})",
                 parsed={"product": old.get("product"), "item_id": old["item_id"], "amount": None, "unit": old.get("unit") or item["unit"],
                         "deadline": old["deadline"], "as_much_as_possible": True, "max_amount": old.get("amount"),
                         "parsed_by": f"follow-up of {rid} by {by}"},
                 scenario=rec.get("scenario"), origin=rec.get("origin", "typed"))
    new["follow_up_of"] = rid
    rec["replaced_by"] = new["id"]
    store.save(rec)
    return store.save(new)


def cancel(rid, by):
    """A person cancels a request that is waiting, or a mock order that has not arrived yet."""
    rec = store.get(rid)
    if not rec:
        raise ValueError(f"No request {rid}")
    if rec.get("status") in ("needs_approval", "ask_buyer"):
        rec.update(status="cancelled", outcome="cancelled", decided_by=by)
        _step(rec, "decision", f"Cancelled by {by}. Nothing was ordered.", "dashboard", actor=f"person:{by}")
        return _finish(rec)
    if rec.get("status") == "ordered" and rec.get("order_id"):
        from . import kitchen
        if not kitchen.cancel_incoming(rec["order_id"]) and kitchen.was_delivered(rec["order_id"]):
            raise ValueError(f"{rec['order_id']} has already been delivered, so it can't be cancelled.")
        orders.cancel_order(rec["order_id"], by)
        rec.update(status="cancelled", outcome="cancelled", decided_by=by)
        _step(rec, "decision", f"Mock order {rec['order_id']} cancelled by {by} before delivery.", "dashboard", actor=f"person:{by}")
        return _finish(rec)
    raise ValueError(f"{rid} is {rec.get('status')} and can't be cancelled")


def order_substitute(rid, product_id, by):
    """The chef picks a recipe-compatible substitute for a request that could not be (fully) ordered.
    Starts a new request for exactly that product, by the same deadline. All the rules still apply."""
    from . import substitutes
    rec = store.get(rid)
    old = (rec or {}).get("parsed") or {}
    if not rec or not old.get("item_id"):
        raise ValueError(f"{rid} is not about one of the kitchen's ingredients")
    if rec.get("status") not in ("needs_approval", "rejected", "cannot_fulfil"):
        raise ValueError(f"{rid} is {rec.get('status')}; nothing to replace")
    item = rules.find_usual_item(old["item_id"])
    p = catalog.get(product_id)
    if not p:
        raise ValueError(f"Unknown product {product_id}")
    swap = next((s for s in item.get("recipe_swaps", []) if s["product_id"] == product_id), None)
    if rec["status"] == "needs_approval":
        rec.update(status="cancelled", outcome="cancelled", decided_by=by)
    _step(rec, "decision", f"{by} chose a substitute instead: {p['name_en'] or p['name_fi']} ({product_id})"
          + (" from the chef's swap list" if swap else "") + ".", "dashboard", actor=f"person:{by}")
    store.save(rec)
    have = substitutes.usual_available(old["item_id"], old["deadline"], rec.get("scenario"))
    combine = bool(have and 0 < have < old["amount"])  # use all of the usual product, the substitute fills the gap
    products = [item["usual_product"], product_id] if combine else [product_id]
    what = (f"{rules.num(have)} {old['unit'].lower() if old['unit'] == 'KG' else old['unit']} of the usual product + "
            f"{p['name_en'] or p['name_fi']} for the rest") if combine else (p["name_en"] or p["name_fi"])
    new = handle(text=f"Substitute for {item['name'].lower()}: {what} (instead of {rid})",
                 parsed={"product": item["name"], "item_id": old["item_id"], "amount": old["amount"], "unit": old["unit"],
                         "deadline": old["deadline"], "only_products": products, "substitute_for": old["item_id"],
                         "person_approved_products": [product_id], "chosen_by": by,
                         "buyer_confirmed_amount": True, "parsed_by": f"substitute chosen by {by}"},
                 scenario=rec.get("scenario"), origin=rec.get("origin", "typed"))
    new["follow_up_of"] = rid
    new["substitute_note"] = swap["note"] if swap else None
    if rec.get("event_id"):
        new["event_id"] = rec["event_id"]
    rec["replaced_by"] = new["id"]
    store.save(rec)
    return store.save(new)
