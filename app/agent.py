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

from . import ai, aimo, catalog, demo, log, orders, parse, rules, store

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
    """'As much as possible' is never bought blindly: the agent looks up what Valio Aimo can deliver
    and asks the buyer for a number, showing that amount. Always returns a finished record (asking)."""
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
        most = round(units * per, 3)
        u = unit.lower() if unit == "KG" else unit
        step = rules.order_step(p) * per
        question = (f"Valio Aimo can deliver up to {rules.num(most)} {u} of {name} ({pid}) by {rules.nice_date(req['deadline'])}"
                    + (f", in steps of {rules.num(step)} {u}" if step > 1 else "")
                    + (" (demo stock)" if q.get("demo_stock_override") else "")
                    + f". How much do you need? (Usual order: {rules.num(item['usual_amount'])} {u}.)")
        rec["available_hint"] = {"amount": most, "unit": unit, "product_id": pid}
    rec["request_checks"] = []
    rec.update(status="ask_buyer", outcome="ask_buyer", options=[], chosen=None, ask_kind="missing", ask_fields=["amount"],
               question=question)
    _step(rec, "ask", f"Asked the buyer: {question}", "agent")
    return _finish(rec)


def _finish(rec):
    """Last thing on every path: write the short summary (from rule results only), then save."""
    if rec.get("status") in ("needs_approval", "cannot_fulfil"):
        rec["restock"] = restock_info(rec)
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
            "delivery_window": demo.delivery_window(pid), "demo_restock": bool(q.get("demo_restock")),
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

def handle(text=None, parsed=None, scenario=None, today=None, record=None, origin="typed", trigger=None, event_id=None):
    """Run the whole flow for one request. Returns the saved record.
    origin: "typed" (a person wrote it) or "auto" (the stock monitor sent it; `trigger` says why)."""
    rec = record or {"id": store.new_id(), "received_at": _now(), "text": text, "scenario": scenario,
                     "origin": origin, "steps": [], "history": []}
    if event_id and not record:
        rec["event_id"] = event_id
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
    blocked.sort(key=lambda o: o["total_with_tax"])
    _rank(usable, blocked, usual, req)
    rec["options"] = usable[:5] + blocked[:3]  # keep the record readable
    rec["options_compared"] = {"usable": len(usable), "blocked": len(blocked)}

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
    rec["situation"] = _situation(best, usual, req, quote)
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


def _rank(usable, blocked, usual, req):
    """Give every option a place and one plain reason for it (rulebook section D: fewest approvals,
    most of the usual product, lowest price). Blocked options come last, with what blocked them."""
    unit = "kg" if req["unit"] == "KG" else req["unit"]
    best = usable[0] if usable else None
    for n, o in enumerate(usable, 1):
        o["rank"] = n
        o["price_per"] = round(o["total_with_tax"] / o["litres"], 2) if o.get("litres") else None
        appr = [r["name"] for r in o["results"] if r["result"] == "approve"]
        if n == 1:
            o["rank_reason"] = ("Needs no approval" if not appr else "Fewest approvals (" + ", ".join(appr) + ")") + (
                ", uses the usual product" if usual and any(l["product_id"] == usual["usual_product"] for l in o["lines"]) else "") + ", cheapest of those."
            continue
        a, b = _rank_key(o, usual), _rank_key(best, usual)
        if a[0] > b[0]:
            why = "needs approval: " + ", ".join(appr)
        elif a[1] > b[1]:
            why = "uses less of the usual product" if a[1] < 0 else "not the usual product"
        else:
            why = f"€{o['total_with_tax'] - best['total_with_tax']:,.2f} more expensive"
        o["rank_reason"] = why[0].upper() + why[1:] + f" (€{o['price_per']:,.2f}/{unit})." if o["price_per"] else why
    for n, o in enumerate(blocked, len(usable) + 1):
        o["rank"] = n
        o["price_per"] = round(o["total_with_tax"] / o["litres"], 2) if o.get("litres") else None
        o["rank_reason"] = "Can't be used: " + _first_problem(o)


def _situation(best, usual, req, quote):
    """One plain sentence saying WHAT the order is, before any rule talk. Built from data only."""
    unit = "kg" if req["unit"] == "KG" else req["unit"]
    lines = best["lines"]
    if usual and len(lines) > 1:
        u = next((l for l in lines if l["product_id"] == usual["usual_product"]), None)
        others = [l for l in lines if l is not u]
        if u:
            q = quote["products"].get(u["product_id"]) or {}
            have = f"{rules.num(q['stock_amount'] * (u.get('litres_per_sales_unit') or 1))} {unit}" if q.get("stock_status") == "known" else "not enough"
            return (f"Split order: Valio Aimo has only {have} of the usual {u['name']} ({u['product_id']}), so the agent takes "
                    f"{rules.num(u['litres'])} {unit} of it and fills the remaining "
                    + " + ".join(f"{rules.num(l['litres'])} {unit} with {l['name']} ({l['product_id']}, {l['role']})" for l in others) + ".")
    if usual and lines[0]["product_id"] != usual["usual_product"]:
        return (f"The usual product ({usual['usual_product']}) can't be used for this order, so the agent chose "
                f"{rules.num(lines[0]['litres'])} {unit} of {lines[0]['name']} ({lines[0]['product_id']}, {lines[0]['role']}).")
    return f"{rules.num(best['litres'])} {unit} of {lines[0]['name']} ({lines[0]['product_id']})" + (", the usual product." if usual else ".")


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
    from . import budgets
    wallet = budgets.wallet_for(rec)
    order = orders.place_order(option["lines"], req["deadline"], placed_by, rec["id"],
                               note=f"demo scenario: {rec['scenario']}" if rec.get("scenario") else None, wallet=wallet)
    rec.update(status="ordered", outcome="ordered", order_id=order["order_id"], wallet=wallet)
    if req.get("item_id"):
        from . import kitchen  # so the kitchen knows this is on its way and does not reorder
        kitchen.add_incoming(req["item_id"], sum(l["litres"] for l in option["lines"]), order["order_id"],
                             req["deadline"], rec["id"], for_event=rec.get("event_id") or (rec.get("origin") == "event"))
        if rec.get("origin") != "event":
            tidy_duplicates(req["item_id"], f"order {order['order_id']} ({rec['id']})")
    _step(rec, "order", f"Placed mock order {order['order_id']}, €{order['total_with_tax']:,.2f} incl. tax, "
          f"delivery {rules.nice_date(req['deadline'])}. Placed by {placed_by}.", "mock order service", order_id=order["order_id"])
    windows = sorted({l.get("delivery_window") for l in option["lines"] if l.get("delivery_window")})
    rec["delivery_message"] = (f"Ordered ({order['order_id']}). Arrives {rules.nice_date(req['deadline'])}"
                               + (f", {windows[0]} (demo delivery time)." if windows else " (Valio Aimo gives the day, not the time)."))
    _step(rec, "message", f"Message to the buyer: {rec['delivery_message']}", "agent")


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


def answer(rid, confirm=False, new_amount=None, new_deadline=None, new_unit=None, new_item_id=None):
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
            confirm = None  # only the date was missing (or several fields were filled in at once)
    if new_item_id:
        item = rules.find_usual_item(new_item_id)
        if not item:
            raise ValueError("Unknown product.")
        req.update(item_id=new_item_id, product=item["name"], unit=item["unit"])
        _step(rec, "answer", f"The buyer chose the product: {item['name']}.", "buyer", actor="buyer")
        if new_amount is None and not confirm and not new_deadline:
            confirm = None
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
        rec["current_request"] = (f"{rules.num(req['amount'])} {'kg' if req['unit'] == 'KG' else req['unit']} of {(req.get('product') or 'product').lower()}"
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
    from . import substitutes
    item = rules.find_usual_item(old["item_id"])
    have = substitutes.usual_available(old["item_id"], old["deadline"], rec.get("scenario"))
    amount = old["amount"] if have is None else min(have, old["amount"])
    if not amount:
        raise ValueError(f"Valio Aimo can't deliver any of the usual product by {rules.nice_date(old['deadline'])}.")
    u = old["unit"].lower() if old["unit"] == "KG" else old["unit"]
    new = handle(text=f"Only the usual product: {rules.num(amount)} {u} of {item['name'].lower()} (instead of {rid})",
                 parsed={"product": old.get("product"), "item_id": old["item_id"], "amount": float(amount), "unit": old["unit"],
                         "deadline": old["deadline"], "buyer_confirmed_amount": True, "only_products": [item["usual_product"]],
                         "parsed_by": f"follow-up of {rid} by {by} (amount shown on the button)"},
                 scenario=rec.get("scenario"), origin=rec.get("origin", "typed"), event_id=rec.get("event_id"))
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
                 scenario=rec.get("scenario"), origin=rec.get("origin", "typed"), event_id=rec.get("event_id"))
    new["follow_up_of"] = rid
    new["substitute_note"] = swap["note"] if swap else None
    if rec.get("event_id"):
        new["event_id"] = rec["event_id"]
    rec["replaced_by"] = new["id"]
    store.save(rec)
    return store.save(new)


def tidy_duplicates(item_id, because):
    """After an order is placed or a delivery arrives for an ingredient, look at OTHER requests for the
    same ingredient that are still waiting for a person:
      - automatic restock requests (sent by the stock monitor) are closed IF the kitchen is now above its
        reorder level counting what is on its way: their reason no longer exists. Logged, nothing deleted.
      - requests a person typed, and event requests, are never closed automatically: they are only marked
        "possible duplicate" so a person can close them with one click.
    """
    from . import kitchen
    i = kitchen.ingredient(item_id)
    if not i:
        return
    doc = kitchen.load()
    on_way = sum(x["amount"] for x in doc["incoming"] if x["item_id"] == item_id and not x.get("for_event"))
    healthy = i["stock"] + on_way > i["reorder_level"]
    for r in store.all_records():
        if (r.get("parsed") or {}).get("item_id") != item_id or r.get("status") not in ("needs_approval", "ask_buyer"):
            continue
        if r.get("origin") == "auto" and healthy:
            r.update(status="superseded", outcome="superseded", decided_by="the agent")
            _step(r, "decision", f"Closed automatically: {i['name']} is back above its reorder level "
                  f"({rules.num(i['stock'])} in stock + {rules.num(on_way)} on its way > {rules.num(i['reorder_level'])}) "
                  f"after {because}. Nothing was ordered for this request.", "stock monitor", actor="system")
            _finish(r)
        elif r.get("origin") != "auto" and not r.get("possible_duplicate"):
            r["possible_duplicate"] = {"because": because, "stock": i["stock"], "on_way": on_way,
                                       "reorder_level": i["reorder_level"]}
            _step(r, "note", f"Possible duplicate: {i['name']} was ordered since this request ({because}). "
                  f"Not closed automatically because a person asked for it.", "agent")
            store.save(r)


# ---------------------------------------------------------------- several products in one request

def handle_text(text, scenario=None, today=None):
    """Entry point for typed requests. One product -> a normal request. Several products ->
    a parent request (kind "basket") with one child request per product; each child goes
    through all the rules on its own (some are ordered by the agent, some wait for a person,
    some ask for missing information)."""
    items = parse.split_items(text, today)
    if not items:
        return handle(text, scenario=scenario, today=today)
    parent = {"id": store.new_id(), "kind": "basket", "received_at": _now(), "text": text, "scenario": scenario,
              "origin": "typed", "status": "basket", "children": [], "steps": [], "history": []}
    _step(parent, "request", f"Read a request with {len(items)} products: " + "; ".join(
        f"{rules.num(i['amount']) + ' ' + ('kg' if i['unit'] == 'KG' else i['unit'] or '') if i['amount'] is not None else '? (amount missing)'} "
        f"{i['product'] or '?'} by {rules.nice_date(i['deadline']) if i['deadline'] else '? (no date)'}" for i in items)
          + ". Each product becomes its own request.", "simple pattern reader")
    store.save(parent)
    for i in items:
        what = (f"{rules.num(i['amount'])} {'kg' if i['unit'] == 'KG' else i['unit']} of {(i['product'] or '?').lower()}"
                if i["amount"] is not None else f"{i['product'] or '?'} (amount missing)")
        child = handle(text=f"{what} (part of {parent['id']}: {i['source']})", parsed={k: v for k, v in i.items() if k != "source"},
                       scenario=scenario, today=today)
        child["basket_id"] = parent["id"]
        store.save(child)
        parent["children"].append(child["id"])
    parent = basket_view(parent)
    _step(parent, "basket", "Split into " + ", ".join(f"{c['id']} ({c['status'].replace('_', ' ')})" for c in parent["child_summaries"]) + ".", "agent")
    return store.save(parent)


def basket_view(parent):
    """Fresh status of every product in a several-product request."""
    kids = [store.get(c) for c in parent.get("children", [])]
    out = []
    for k in kids:
        if not k:
            continue
        while k.get("replaced_by") and store.get(k["replaced_by"]):
            k = store.get(k["replaced_by"])
        o = next((x for x in k.get("options") or [] if x["option_id"] == k.get("chosen")), None)
        out.append({"id": k["id"], "status": k["status"], "text": k.get("current_request") or k.get("text"),
                    "product": (k.get("parsed") or {}).get("product"), "total": o["total_with_tax"] if o else None,
                    "order_id": k.get("order_id"), "delivery_message": k.get("delivery_message"),
                    "question": k.get("question"), "reason": k.get("reason"), "ask_kind": k.get("ask_kind"),
                    "ask_fields": k.get("ask_fields"), "deadline": (k.get("parsed") or {}).get("deadline"),
                    "parsed": {f: (k.get("parsed") or {}).get(f) for f in ("unit", "deadline", "amount", "item_id")},
                    "request_checks": k.get("request_checks"), "text_raw": k.get("text")})
    order = {"needs_approval": 0, "ask_buyer": 0, "cannot_fulfil": 1}
    out.sort(key=lambda c: order.get(c["status"], 2))  # what needs a person first
    parent["child_summaries"] = out
    parent["counts"] = {s: sum(1 for c in out if c["status"] == s) for s in {c["status"] for c in out}}
    return parent


# ---------------------------------------------------------------- waiting for a restock (story 2)

def restock_info(rec):
    """When the usual product is short: when does Valio Aimo get more? The real API does not say;
    a few products have a DEMO restock date (data/demo_stock_overrides.json, "restock")."""
    req = rec.get("parsed") or {}
    item = rules.find_usual_item(req.get("item_id")) if req.get("item_id") else None
    if not item:
        return None
    o = next((x for x in rec.get("options") or [] if x["option_id"] == rec.get("chosen")), None)
    short = (o and len(o["lines"]) > 1) or (rec.get("status") == "cannot_fulfil" and any(b["rule"] == "B4" for b in rec.get("blocked_by") or []))
    if not short:
        return None
    r = demo.restock_for(item["usual_product"])
    if r:
        return {"product_id": item["usual_product"], "date": r["date"], "amount": r["amount"], "demo": True,
                "after_deadline": r["date"] > req["deadline"]}
    return {"product_id": item["usual_product"], "unknown": True}


def wait_for_restock(rid, by):
    """Order the full amount of the usual product for the day it is restocked (demo restock dates only)."""
    rec = store.get(rid)
    info = restock_info(rec) if rec else None
    if not info or info.get("unknown"):
        raise ValueError("Valio Aimo gives no restock date for this product, so the agent can't order for a later day.")
    old = rec["parsed"]
    if rec["status"] == "needs_approval":
        rec.update(status="cancelled", outcome="cancelled", decided_by=by)
    _step(rec, "decision", f"{by} chose to wait for the usual product (restock {rules.nice_date(info['date'])}, demo data).",
          "dashboard", actor=f"person:{by}")
    store.save(rec)
    item = rules.find_usual_item(old["item_id"])
    new = handle(text=f"Wait for the usual product: {rules.num(old['amount'])} {'kg' if old['unit'] == 'KG' else old['unit']} of "
                      f"{item['name'].lower()} on {rules.nice_date(info['date'])} (instead of {rid})",
                 parsed={**{k: old[k] for k in ("product", "item_id", "amount", "unit")}, "deadline": info["date"],
                         "only_products": [item["usual_product"]], "buyer_confirmed_amount": True,
                         "parsed_by": f"follow-up of {rid} by {by}: wait for restock"},
                 scenario=rec.get("scenario"), origin=rec.get("origin", "typed"), event_id=rec.get("event_id"))
    new["follow_up_of"] = rid
    rec["replaced_by"] = new["id"]
    store.save(rec)
    return store.save(new)
