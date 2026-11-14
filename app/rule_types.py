"""All rule TYPES the agent understands. A rule in rules.yaml is one of these types plus settings.

Each type says:
  scope     request (checked once, before searching) | line (each product) | order (whole option)
  category  heading on the Rules screen
  locked    safety rule: settings can be tuned, the rule can't be removed
  results   which results the rule may give when it is not met
  settings  the knobs, with their kind and limits (used to validate edits)
  describe  one plain sentence for the rulebook and the dashboard
  check     plain code, no AI. Returns (result, detail, facts[, warning]) or None (= nothing to check)

To add a NEW type: add one entry here. Nothing else needs to change.
"""
from . import catalog

EUR = lambda x: f"€{x:,.2f}"


def NUM(x):
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def _nice_date(iso):
    from datetime import date
    d = date.fromisoformat(iso[:10])
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _nice_time(iso):
    return f"{_nice_date(iso)} {iso[11:16]}"


RESULT_WORDS = {"approve": "needs approval", "ask": "ask the buyer", "blocked": "can't be used"}


# ======================================================================= request scope

def _check_clear_request(rule, req, ctx):
    missing = [f for f in rule["settings"]["required_fields"] if req.get(f) in (None, "")]
    if missing:
        return rule["result"], f"Missing from the request: {', '.join(missing)}. The agent does not guess.", {"missing": missing}
    return "pass", "Product, amount, unit and deadline are all known.", {}


def _check_usual_size(rule, req, ctx):
    s = rule["settings"]
    item = ctx["usual_item"]
    if not item:
        return s["if_new_item"], f"'{req['product']}' is not in the kitchen's usual orders (new item).", {"product": req["product"]}
    if req["unit"] != item["unit"]:
        return "ask", f"Request is in {req['unit']} but the usual order is in {item['unit']}.", {}
    ratio = req["amount"] / item["usual_amount"]
    facts = {"requested": req["amount"], "usual": item["usual_amount"], "unit": item["unit"],
             "ratio": round(ratio, 2), "max_ratio": s["max_ratio"]}
    base = (f"{NUM(req['amount'])} {item['unit']} is {NUM(round(ratio, 1))}× the usual "
            f"{NUM(item['usual_amount'])} {item['unit']} per {item['per']} (limit {NUM(s['max_ratio'])}×)")
    if ratio > s["max_ratio"] and req.get("buyer_confirmed_amount"):
        return s["if_buyer_confirms"], base + ". The buyer confirmed it, but it is still unusual.", facts
    if ratio > s["max_ratio"]:
        return rule["result"], base + ". Ask the buyer to confirm.", facts
    return "pass", base + ".", facts


# ======================================================================= line scope (each product)

def _check_can_be_sold(rule, line, q, p, req, ctx):
    pid = line["product_id"]
    if not q or not q["deliverable"]:
        return rule["result"], f"{pid}: the API returned no delivery date.", {"product": pid}
    if q["sales_block"]:
        return rule["result"], f"{pid}: product has a sales block.", {"product": pid}
    if not q["priced"]:
        return rule["result"], f"{pid}: the API returned no price.", {"product": pid}
    return "pass", f"{pid}: no sales block, price and delivery date returned.", {"product": pid}


def _check_matches_request(rule, line, q, p, req, ctx):
    pid, need = line["product_id"], ctx["required_labels"]
    missing = [l for l in need if l not in p["labels"]]
    if missing:
        return rule["result"], f"{pid}: missing label(s) {', '.join(missing)}.", {"product": pid, "missing_labels": missing}
    return "pass", (f"{pid}: has {', '.join(need)}." if need else f"{pid}: no labels required."), {"product": pid}


def _check_deadline(rule, line, q, p, req, ctx):
    if not q or not q["deliverable"]:
        return None
    pid = line["product_id"]
    facts = {"product": pid, "available_date": q["available_date"], "deadline": req["deadline"], "closing_time": q["closing_time"]}
    if q["cutoff_passed"]:
        return rule["result"], f"{pid}: order cut-off {_nice_time(q['closing_time'])} has passed.", facts
    if q["available_date"] > req["deadline"]:
        return rule["result"], (f"{pid}: earliest delivery {_nice_date(q['available_date'])}, "
                                f"after the deadline {_nice_date(req['deadline'])}."), facts
    return "pass", f"{pid}: available {_nice_date(q['available_date'])}; order by {_nice_time(q['closing_time'])}.", facts


def _check_stock(rule, line, q, p, req, ctx):
    if not q or not q["deliverable"]:
        return None
    pid, qty = line["product_id"], line["quantity"]
    override = " (demo stock override)" if q.get("demo_stock_override") else ""
    facts = {"product": pid, "ordered": qty, "stock_status": q["stock_status"], "stock": q["stock_amount"],
             "demo_stock_override": bool(q.get("demo_stock_override"))}
    if q["stock_status"] == "known":
        if qty > q["stock_amount"]:
            return rule["result"], f"{pid}: {NUM(qty)} wanted but only {NUM(q['stock_amount'])} in stock{override}.", facts
        return "pass", f"{pid}: {NUM(qty)} of {NUM(q['stock_amount'])} in stock{override}.", facts
    if q["stock_status"] == "no_limit":
        return "pass", f"{pid}: the API reports no stock limit.", facts
    unknown = rule["settings"]["if_stock_unknown"]
    if unknown == "allow_with_warning":
        return "pass", f"{pid}: supplier product, stock unknown.", facts, "stock not confirmed by supplier"
    return unknown, f"{pid}: supplier product, stock unknown.", facts


def _check_avoid_brands(rule, line, q, p, req, ctx):
    pid, brands = line["product_id"], rule["settings"]["brands"]
    if p["brand"] in brands:
        return rule["result"], f"{pid}: brand {p['brand']} is on the avoid list.", {"product": pid, "brand": p["brand"]}
    return "pass", f"{pid}: brand {p['brand'] or 'none'} is not on the avoid list.", {"product": pid}


def _check_avoid_products(rule, line, q, p, req, ctx):
    pid = line["product_id"]
    if pid in rule["settings"]["products"]:
        return rule["result"], f"{pid} is on the avoid list.", {"product": pid}
    return "pass", f"{pid} is not on the avoid list.", {"product": pid}


def _check_max_price_per_litre(rule, line, q, p, req, ctx):
    if not q or not q.get("priced") or not p["litres_per_sales_unit"]:
        return None
    pid, limit = line["product_id"], rule["settings"]["max_eur_per_litre"]
    per_l = q["price_per_unit_with_tax"] / p["litres_per_sales_unit"]
    facts = {"product": pid, "price_per_litre": round(per_l, 3), "max": limit}
    if per_l > limit + 1e-9:
        return rule["result"], f"{pid}: {EUR(per_l)}/L is over the {EUR(limit)}/L limit.", facts
    return "pass", f"{pid}: {EUR(per_l)}/L is within {EUR(limit)}/L.", facts


def _check_min_reliability(rule, line, q, p, req, ctx):
    pid, s = line["product_id"], rule["settings"]
    r = p.get("delivery_reliability")
    if r is None:
        if s["if_unknown"] == "pass":
            return "pass", f"{pid}: no delivery reliability score in Aimo's data.", {"product": pid}, "delivery reliability unknown"
        return s["if_unknown"], f"{pid}: no delivery reliability score in Aimo's data.", {"product": pid}
    facts = {"product": pid, "reliability": r, "min": s["min_score"]}
    if r < s["min_score"]:
        return rule["result"], f"{pid}: delivery reliability {r:.3f} is below {s['min_score']:.3f}.", facts
    return "pass", f"{pid}: delivery reliability {r:.3f} (min {s['min_score']:.3f}).", facts


def _check_required_labels(rule, line, q, p, req, ctx):
    pid, need = line["product_id"], rule["settings"]["labels"]
    missing = [l for l in need if l not in p["labels"]]
    if missing:
        return rule["result"], f"{pid}: missing required label(s) {', '.join(missing)}.", {"product": pid, "missing": missing}
    return "pass", f"{pid}: has {', '.join(need)}.", {"product": pid}


# ======================================================================= order scope (whole option)

def _check_order_steps(rule, option, quote, req, ctx):
    s = rule["settings"]
    litres, bad = 0.0, []
    for line in option["lines"]:
        p = catalog.get(line["product_id"])
        step = p["allowed_lot_size"] or 1
        if abs(round(line["quantity"] / step) * step - line["quantity"]) > 1e-9:
            bad.append(line["product_id"])
        litres += line["quantity"] * (p["litres_per_sales_unit"] or 0)
    over = (litres - req["amount"]) / req["amount"] * 100 if req["amount"] else 0
    facts = {"asked": req["amount"], "ordered": round(litres, 3), "overshoot_pct": round(over, 2), "max": s["max_overshoot_pct"]}
    ctx["litres"] = round(litres, 3)
    if bad:
        return "blocked", f"Quantity not in allowed order steps for {', '.join(bad)}.", facts
    if litres + 1e-9 < req["amount"]:
        return "blocked", f"Option covers only {NUM(round(litres, 2))} of {NUM(req['amount'])} {req['unit']}.", facts
    if over > s["max_overshoot_pct"]:
        return rule["result"], (f"Rounding up to whole units gives {NUM(round(litres, 2))} {req['unit']}, "
                                f"{over:.1f}% more than asked (limit {NUM(s['max_overshoot_pct'])}%)."), facts
    return "pass", (f"{NUM(round(litres, 2))} {req['unit']} ordered for {NUM(req['amount'])} asked "
                    f"(+{over:.1f}%, limit {NUM(s['max_overshoot_pct'])}%)."), facts


def _option_total(option, quote, with_tax=True):
    total, unpriced = 0.0, []
    for line in option["lines"]:
        q = quote["products"].get(line["product_id"])
        if q and q["priced"]:
            total += line["quantity"] * (q["price_per_unit_with_tax"] if with_tax else q["price_per_unit_without_tax"])
        else:
            unpriced.append(line["product_id"])
    return round(total, 2), unpriced


def _check_spend_limit(rule, option, quote, req, ctx):
    s = rule["settings"]
    total, unpriced = _option_total(option, quote, s["includes_tax"])
    tax = "incl." if s["includes_tax"] else "excl."
    facts = {"total": total, "limit": s["limit_eur"], "includes_tax": s["includes_tax"]}
    if unpriced:
        return "blocked", f"No price for {', '.join(unpriced)}.", facts
    if total > s["limit_eur"]:
        return rule["result"], f"{EUR(total)} {tax} tax is {EUR(total - s['limit_eur'])} over the {EUR(s['limit_eur'])} limit.", facts
    return "pass", f"{EUR(total)} {tax} tax is within the {EUR(s['limit_eur'])} limit.", facts


def _brands(option):
    return {l["product_id"]: catalog.get(l["product_id"])["brand"] for l in option["lines"]}


def _check_approved_brands(rule, option, quote, req, ctx):
    ok = rule["settings"]["brands"]
    brands = _brands(option)
    others = sorted({b or "no brand" for b in brands.values() if b not in ok})
    facts = {"brands": brands, "approved": ok}
    if others:
        return rule["result"], (f"{', '.join(others)} {'is' if len(others) == 1 else 'are'} not an approved brand "
                                f"(approved: {', '.join(ok)})."), facts
    return "pass", f"{', '.join(sorted(set(brands.values())))}: approved.", facts


def _check_max_brands(rule, option, quote, req, ctx):
    n, mx = len(set(_brands(option).values())), rule["settings"]["max_brands"]
    res = rule["result"] if n > mx else "pass"
    return res, f"{n} brand{'s' if n != 1 else ''} (max {mx}).", {"brand_count": n, "max": mx}


# ======================================================================= the registry

TYPES = {
    # request
    "clear_request": dict(scope="request", category="Request", locked=True, results=["ask"],
        title="Request must be clear", check=_check_clear_request,
        settings={"required_fields": dict(kind="list", options=["product", "amount", "unit", "deadline"], min_items=1)},
        describe=lambda s, r: f"The request must say {', '.join(s['required_fields'])}. If anything is missing: {RESULT_WORDS[r]}."),
    "usual_size": dict(scope="request", category="Request", locked=False, results=["ask", "approve"],
        title="Much bigger than usual", check=_check_usual_size,
        settings={"max_ratio": dict(kind="number", min=1, max=100),
                  "if_buyer_confirms": dict(kind="choice", options=["approve", "pass"]),
                  "if_new_item": dict(kind="choice", options=["approve", "ask", "pass"])},
        describe=lambda s, r: (f"More than {NUM(s['max_ratio'])}× the kitchen's usual amount: {RESULT_WORDS[r]}. "
                               f"If the buyer confirms: {RESULT_WORDS.get(s['if_buyer_confirms'], 'allowed')}. "
                               f"Item never ordered before: {RESULT_WORDS.get(s['if_new_item'], 'allowed')}.")),
    # line
    "can_be_sold": dict(scope="line", category="Product", locked=True, results=["blocked"], settings={},
        title="Can be sold", check=_check_can_be_sold,
        describe=lambda s, r: f"The product must have a price, a delivery date and no sales block. Otherwise it {RESULT_WORDS[r]}."),
    "matches_request": dict(scope="line", category="Product", locked=True, results=["blocked"], settings={},
        title="Matches the request", check=_check_matches_request,
        describe=lambda s, r: f"The product must have every label the request needs (e.g. lactose-free). Otherwise it {RESULT_WORDS[r]}."),
    "arrives_by_deadline": dict(scope="line", category="Product", locked=True, results=["blocked"], settings={},
        title="Arrives by the deadline", check=_check_deadline,
        describe=lambda s, r: f"Delivery must be on or before the deadline, and the order cut-off must not have passed. Otherwise it {RESULT_WORDS[r]}."),
    "enough_stock": dict(scope="line", category="Product", locked=True, results=["blocked"], check=_check_stock,
        title="Enough stock",
        settings={"if_stock_unknown": dict(kind="choice", options=["allow_with_warning", "approve", "blocked"])},
        describe=lambda s, r: (f"The amount can't be more than the stock. Otherwise it {RESULT_WORDS[r]}. "
                               f"Supplier products with unknown stock: "
                               f"{'allowed, with a warning' if s['if_stock_unknown'] == 'allow_with_warning' else RESULT_WORDS[s['if_stock_unknown']]}.")),
    "avoid_brands": dict(scope="line", category="Product", locked=False, results=["blocked", "approve"],
        title="Avoid brands", check=_check_avoid_brands,
        settings={"brands": dict(kind="list", min_items=1)},
        describe=lambda s, r: f"Never buy these brands: {', '.join(s['brands'])}. If one is in an option: {RESULT_WORDS[r]}."),
    "avoid_products": dict(scope="line", category="Product", locked=False, results=["blocked", "approve"],
        title="Avoid products", check=_check_avoid_products,
        settings={"products": dict(kind="list", min_items=1, product_codes=True)},
        describe=lambda s, r: f"Never buy these product codes: {', '.join(s['products'])}. If one is in an option: {RESULT_WORDS[r]}."),
    "max_price_per_litre": dict(scope="line", category="Product", locked=False, results=["approve", "blocked"],
        title="Maximum price per litre", check=_check_max_price_per_litre,
        settings={"max_eur_per_litre": dict(kind="number", min=0.01, max=1000)},
        describe=lambda s, r: f"Price per litre (incl. tax) must be at most {EUR(s['max_eur_per_litre'])}. Otherwise: {RESULT_WORDS[r]}."),
    "min_delivery_reliability": dict(scope="line", category="Product", locked=False, results=["approve", "blocked"],
        title="Minimum delivery reliability", check=_check_min_reliability,
        settings={"min_score": dict(kind="number", min=0, max=1),
                  "if_unknown": dict(kind="choice", options=["pass", "approve", "blocked"])},
        describe=lambda s, r: (f"Aimo's delivery reliability score must be at least {s['min_score']:.3f}. Otherwise: {RESULT_WORDS[r]}. "
                               f"Products without a score: {'allowed, with a warning' if s['if_unknown'] == 'pass' else RESULT_WORDS[s['if_unknown']]}.")),
    "required_labels": dict(scope="line", category="Product", locked=False, results=["blocked", "approve"],
        title="Always require labels", check=_check_required_labels,
        settings={"labels": dict(kind="list", min_items=1, labels=True)},
        describe=lambda s, r: f"Every product must have: {', '.join(s['labels'])}. Otherwise: {RESULT_WORDS[r]}."),
    # order
    "order_steps": dict(scope="order", category="Order", locked=True, results=["approve", "blocked"],
        title="Allowed order steps", check=_check_order_steps,
        settings={"max_overshoot_pct": dict(kind="number", min=0, max=100)},
        describe=lambda s, r: (f"Orders are in whole packs, rounded up. If that adds more than {NUM(s['max_overshoot_pct'])}% "
                               f"to the amount asked: {RESULT_WORDS[r]}.")),
    "spend_limit": dict(scope="order", category="Order", locked=False, results=["approve", "blocked"],
        title="Spend limit", check=_check_spend_limit,
        settings={"limit_eur": dict(kind="number", min=0, max=1_000_000), "includes_tax": dict(kind="bool")},
        describe=lambda s, r: (f"One order may cost at most {EUR(s['limit_eur'])} "
                               f"({'including' if s['includes_tax'] else 'excluding'} tax). Over the limit: {RESULT_WORDS[r]}.")),
    "approved_brands": dict(scope="order", category="Order", locked=False, results=["approve", "blocked"],
        title="Approved brands", check=_check_approved_brands,
        settings={"brands": dict(kind="list", min_items=1)},
        describe=lambda s, r: f"Only these brands without asking: {', '.join(s['brands'])}. Any other brand: {RESULT_WORDS[r]}."),
    "max_brands": dict(scope="order", category="Order", locked=False, results=["blocked", "approve"],
        title="Brands per split", check=_check_max_brands,
        settings={"max_brands": dict(kind="number", min=1, max=10, integer=True)},
        describe=lambda s, r: f"One order may mix at most {NUM(s['max_brands'])} brands. More: {RESULT_WORDS[r]}."),
}

ID_PREFIX = {"request": "A", "line": "B", "order": "C"}
