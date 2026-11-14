"""The buying agent: request -> checks -> search -> price & stock -> options -> rules -> act.

Acts in exactly one of four ways (worst rule result decides, see rules.py):
  auto_order      every rule passed -> places a mock order by itself
  needs_approval  something needs a person -> waits on the dashboard
  ask_buyer       request unclear or unusually big -> asks the buyer, orders nothing
  cannot_fulfil   no option can be used at all -> says why, orders nothing

Every step is recorded in record["steps"] as one plain-language line with its source.
Every sentence is built from data or rule results; nothing is invented.
"""
from datetime import datetime, timezone

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


def _finish(rec):
    """Last thing on every path: write the short summary (from rule results only), then save."""
    rec["summary"] = ai.summarize(rec)
    return store.save(rec)


# ---------------------------------------------------------------- finding products

def _candidates(req):
    """Products that could fill the request, from the product file (no API calls)."""
    item = rules.find_usual_item(req.get("item_id")) if req.get("item_id") else None
    if item:
        s = item["search"]
        hits = catalog.search(s.get("words", ""), s.get("labels", []), s.get("category"), limit=200)
        labels = s.get("labels", [])
    else:
        hits = catalog.search(req["product"] or "", [], None, limit=200)
        labels = []
    # Only products we can convert to the requested unit (litres for now).
    usable = [p for p in hits if req["unit"] != "L" or p["litres_per_sales_unit"]]
    return item, usable, labels


# ---------------------------------------------------------------- building options

def _line(pid, qty, role, quote):
    q = quote["products"].get(pid) or {}
    p = catalog.get(pid)
    price = q.get("price_per_unit_with_tax")
    return {"product_id": pid, "name": _name(pid), "brand": p["brand"], "quantity": qty,
            "sales_unit": p["sales_unit"], "litres": round(qty * (p["litres_per_sales_unit"] or 0), 3),
            "price_per_unit_with_tax": price,
            "line_total_with_tax": round(qty * price, 2) if price is not None else None,
            "role": role,
            # facts for the dashboard, straight from the API answer
            "litres_per_sales_unit": p["litres_per_sales_unit"],
            "stock_status": q.get("stock_status"), "stock_amount": q.get("stock_amount"),
            "demo_stock_override": bool(q.get("demo_stock_override")),
            "available_date": q.get("available_date"), "closing_time": q.get("closing_time"),
            "tax_rate": q.get("tax_rate")}


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
    if usual and pid in (catalog.get(usual["usual_product"]) or {}).get("substitutes", []):
        return "official substitute"
    return "alternative"


def _price_per_litre(pid, quote):
    q = quote["products"].get(pid) or {}
    p = catalog.get(pid)
    if not q.get("priced") or not p["litres_per_sales_unit"]:
        return float("inf")
    return q["price_per_unit_with_tax"] / p["litres_per_sales_unit"]


def build_options(req, usual, cand_ids, quote):
    amount = req["amount"]
    options = []
    # 1) One product covers everything.
    for pid in cand_ids:
        qty = rules.units_for(pid, amount)
        if qty:
            options.append({"kind": "single", "lines": [_line(pid, qty, _role(pid, usual), quote)]})

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
            base = min(with_stock, key=lambda x: _price_per_litre(x, quote)) if with_stock else None
        if base:
            base_units = _usable_stock_units(quote["products"][base])
            per = catalog.get(base)["litres_per_sales_unit"]
            base_units = min(base_units, rules.units_for(base, amount))
            rest = amount - base_units * per
            subs = (catalog.get(usual["usual_product"]) or {}).get("substitutes", []) if usual else []
            partners = [pid for pid in cand_ids if pid != base and _usable_stock_units(quote["products"].get(pid)) != 0]
            partners.sort(key=lambda x: (x not in subs, _price_per_litre(x, quote)))  # official substitutes first
            for pid in partners[:MAX_SPLIT_PARTNERS]:
                qty = rules.units_for(pid, rest)
                if qty:
                    options.append({"kind": "split", "lines": [
                        _line(base, float(base_units), _role(base, usual), quote),
                        _line(pid, qty, _role(pid, usual), quote)]})
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

def handle(text=None, parsed=None, scenario=None, today=None, record=None):
    """Run the whole flow for one request. Returns the saved record."""
    rec = record or {"id": store.new_id(), "received_at": _now(), "text": text, "scenario": scenario,
                     "steps": [], "history": []}
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

    # A: checks on the request itself
    req_results = rules.check_request(req)
    rec["request_checks"] = [r.to_dict() for r in req_results]
    for r in req_results:
        _step(rec, "rules", f"{r.rule} {r.name}: {r.result}. {r.detail}", "rules.yaml")
    if rules.outcome(req_results) in ("ask_buyer", "blocked"):
        bad = [r for r in req_results if r.result != "pass"]
        rec.update(status="ask_buyer", outcome="ask_buyer", options=[], chosen=None,
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
        parts = [f"{n} option{'s' if n != 1 else ''} failed {rid} {name}"
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
        _step(rec, "rules", f"{r['rule']} {r['name']}: {r['result']}. {r['detail']}", "rules.yaml")
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
    rec["decided_by"] = by
    _step(rec, "decision", f"Approved by {by}.", "dashboard", actor=f"person:{by}")
    _place(rec, option, rec["parsed"], placed_by=f"human:{by}")
    return _finish(rec)


def answer(rid, confirm=False, new_amount=None):
    """The buyer answers a question (e.g. 'is 10,000 L right?')."""
    rec = store.get(rid)
    if not rec or rec.get("status") != "ask_buyer":
        raise ValueError(f"{rid} is not waiting for the buyer")
    req = dict(rec["parsed"])
    if new_amount is not None:
        req["amount"] = float(new_amount)
        req["buyer_confirmed_understanding"] = True  # the buyer typed the amount themselves
        _step(rec, "answer", f"The buyer changed the amount to {rules.num(float(new_amount))} {req['unit']}.", "buyer", actor="buyer")
    elif confirm and rec.get("ask_kind") == "understanding":
        req["buyer_confirmed_understanding"] = True
        _step(rec, "answer", "The buyer confirmed the agent understood the request correctly.", "buyer", actor="buyer")
    elif confirm:
        req["buyer_confirmed_amount"] = True
        _step(rec, "answer", f"The buyer confirmed {rules.num(req['amount'])} {req['unit']}.", "buyer", actor="buyer")
    else:
        raise ValueError("give confirm=True or new_amount")
    rec["history"].append({"status": rec["status"], "question": rec.get("question")})
    for k in ("question", "options", "chosen", "why_chosen", "alternatives", "alternatives_detail", "reason", "blocked_by"):
        rec.pop(k, None)
    req["parsed_by"] = rec["parsed"].get("parsed_by", "parser") + " + buyer answer"
    return handle(parsed=req, scenario=rec.get("scenario"), record=rec)
