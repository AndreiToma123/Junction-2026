"""Recipe-aware substitutes: when an ingredient can't be sourced (stock-out, late delivery), what can
the kitchen use instead, in THIS recipe, and what is the trade-off?

Three kinds of answer, all checked against live Valio Aimo data before they are shown:
  1. The chef's own list of known swaps (data/kitchen.default.json, "recipe_swaps"). Some are
     pre-approved: the agent may order those by itself (rule C2).
  2. Products the AI picks from Aimo's catalogue as recipe-compatible. The AI only chooses from a
     list that plain code has already checked (deliverable in time, in stock, priced). AI picks always
     need the chef's approval.
  3. Changing a dish: another dish on the menu that does not need the missing ingredient and can be
     made from what is already in the kitchen.

Nothing here orders anything. A person picks a substitute; the order then goes through all the rules.
"""
import json

from . import aimo, catalog, kitchen, llm, rules

MAX_CATALOG_CANDIDATES = 25


def _u(unit):
    return "kg" if unit == "KG" else unit


def _check(pid, amount, unit, deadline, q):
    """Plain-code facts about ordering `amount` of product `pid` by `deadline`. Returns dict or None."""
    p = catalog.get(pid)
    if not p or not q:
        return None
    per = catalog.content_per_sales_unit(p, unit)
    if not per:
        return None
    units = rules.units_for(pid, amount, unit)
    stock_ok = q["stock_status"] in ("no_limit", "unknown") or (q["stock_status"] == "known" and q["stock_amount"] >= units)
    in_time = bool(q["deliverable"]) and not q["cutoff_passed"] and bool(q["available_date"]) and q["available_date"] <= deadline
    ok = in_time and stock_ok and bool(q["priced"]) and not q["sales_block"]
    why = None
    if not q["deliverable"]:
        why = "Valio Aimo gave no delivery date"
    elif q["cutoff_passed"]:
        why = f"order cut-off {rules.nice_time(q['closing_time'])} has passed"
    elif q["available_date"] > deadline:
        why = f"earliest delivery {rules.nice_date(q['available_date'])}, after {rules.nice_date(deadline)}"
    elif not stock_ok:
        why = f"only {rules.num(q['stock_amount'] * per)} {_u(unit)} in stock"
    elif not q["priced"]:
        why = "no price"
    return {"product_id": pid, "name": p["name_en"] or p["name_fi"], "brand": p["brand"], "unit": unit,
            "units": units, "amount": round(units * per, 3), "sales_unit": p["sales_unit"],
            "price_per_unit_with_tax": q.get("price_per_unit_with_tax"),
            "total_with_tax": round(units * q["price_per_unit_with_tax"], 2) if q.get("priced") else None,
            "price_per": round(q["price_per_unit_with_tax"] / per, 2) if q.get("priced") else None,
            "available_date": q.get("available_date"), "stock_status": q["stock_status"], "stock_amount": q.get("stock_amount"),
            "temperature": p.get("temperature"), "category": p.get("category"), "ok": ok, "why_not": why}


def _catalog_candidates(item, exclude):
    """Same kind of product as the usual one: same category, with a pack size in the right unit."""
    usual = catalog.get(item["usual_product"]) or {}
    cats = {usual.get("category")} | {(catalog.get(s["product_id"]) or {}).get("category") for s in item.get("recipe_swaps", [])}
    out = [p for p in catalog.load().values()
           if p["category"] in cats and p["id"] not in exclude and catalog.content_per_sales_unit(p, item["unit"])]
    return out[:200]


def _dish_swaps(item_id, dishes, portions, deadline):
    """Other menu dishes that don't need the missing ingredient and can be made from kitchen stock alone."""
    from datetime import date
    doc = kitchen.load()
    days = max((date.fromisoformat(deadline) - date.today()).days, 0)
    use = kitchen.daily_use(doc)
    # what the kitchen can spare by then: stock + deliveries arriving in time - normal service until then
    stock = {i["item_id"]: max(0.0, i["stock"] + sum(x["amount"] for x in doc["incoming"] if x["item_id"] == i["item_id"]
                                                     and x["delivery_date"] <= deadline) - use.get(i["item_id"], 0) * days)
             for i in doc["ingredients"]}
    names = {i["item_id"]: i["name"] for i in doc["ingredients"]}
    out = []
    courses = {d.get("course") for d in doc["dishes"] if d["dish_id"] in dishes}
    for d in doc["dishes"]:
        if d["dish_id"] in dishes or any(g["item_id"] == item_id for g in d["ingredients"]):
            continue
        if courses and d.get("course") not in courses:
            continue  # only swap a main for a main, a breakfast for a breakfast
        short = [names.get(g["item_id"], g["item_id"]) for g in d["ingredients"] if g["amount"] * portions > stock.get(g["item_id"], 0) + 1e-9]
        out.append({"dish_id": d["dish_id"], "name": d["name"], "portions": portions, "from_stock_only": not short,
                    "short": short})
    out.sort(key=lambda x: (not x["from_stock_only"], len(x["short"])))
    return out[:3]


def usual_available(item_id, deadline, scenario=None):
    """How much of the usual product Valio Aimo can deliver by the deadline (None = no limit)."""
    from . import demo
    item = kitchen.ingredient(item_id)
    pid = item["usual_product"]
    q = demo.apply_stock_overrides(aimo.quote([pid], deadline), scenario)["products"].get(pid)
    if not q or not q["deliverable"] or q["cutoff_passed"] or not q["priced"] or (q["available_date"] or "9") > deadline:
        return 0.0
    if q["stock_status"] != "known":
        return None
    p = catalog.get(pid)
    return round(rules.whole_steps_within(p, q["stock_amount"]) * catalog.content_per_sales_unit(p, item["unit"]), 3)


def for_request(rec):
    """Substitutes for a request: only for the part the usual product can't cover."""
    req = rec["parsed"]
    have = usual_available(req["item_id"], req["deadline"], rec.get("scenario"))
    missing = req["amount"] if have is None or have <= 0 else max(req["amount"] - have, 0)
    if have is None:
        missing = req["amount"]  # usual product has no limit: the problem is something else (date, price, rules)
    out = propose(req["item_id"], missing or req["amount"], req["deadline"])
    out["usual_available"] = have
    out["combine_with_usual"] = bool(have and 0 < have < req["amount"])
    return out


def propose(item_id, amount, deadline, dishes=None, portions=None, use_ai=True):
    """Substitutes for `amount` of ingredient `item_id` needed by `deadline`, for `dishes` (optional)."""
    item = kitchen.ingredient(item_id)
    if not item:
        raise ValueError(f"{item_id} is not one of the kitchen's ingredients")
    unit = item["unit"]
    dishes = dishes or [u["dish_id"] for u in kitchen.used_by(item_id)]
    swaps = [s for s in item.get("recipe_swaps", []) if not s.get("dishes") or set(s["dishes"]) & set(dishes)]
    chef_ids = [s["product_id"] for s in swaps]
    cands = _catalog_candidates(item, set(chef_ids) | {item["usual_product"]})
    ids = list(dict.fromkeys(chef_ids + [p["id"] for p in cands]))[:999]
    try:
        quote = aimo.quote(ids, deadline)["products"] if ids else {}
    except aimo.AimoError as e:
        return {"item_id": item_id, "name": item["name"], "error": f"Could not ask Valio Aimo: {e}", "options": [], "dish_swaps": []}

    options = []
    for s in swaps:
        f = _check(s["product_id"], amount, unit, deadline, quote.get(s["product_id"]))
        if f:
            options.append({**f, "source": "chef's swap list", "pre_approved": bool(s.get("pre_approved")),
                            "note": s["note"], "needs_chef": not s.get("pre_approved")})
    checked = [f for f in (_check(p["id"], amount, unit, deadline, quote.get(p["id"])) for p in cands) if f and f["ok"]]
    checked.sort(key=lambda f: f["price_per"] or 1e9)
    checked = checked[:MAX_CATALOG_CANDIDATES]

    picked_by = "plain code (no AI)"
    if use_ai and llm.available() and checked:
        picks = _ai_pick(item, dishes, amount, checked)
        if picks is not None:
            picked_by = "Gemini"
            for pid, note, fit in picks:
                f = next(c for c in checked if c["product_id"] == pid)
                options.append({**f, "source": "suggested by AI", "pre_approved": False, "note": note, "fit": fit, "needs_chef": True})
    if picked_by != "Gemini":
        # Without AI: offer the cheapest same-category products, clearly marked "not checked against the recipe".
        for f in checked[:2]:
            options.append({**f, "source": "same product group (not checked against the recipe)", "pre_approved": False,
                            "note": "Same product group as the usual product. Nobody has checked it suits the recipe: the chef decides.",
                            "needs_chef": True})
    usable = [o for o in options if o["ok"]]
    usable.sort(key=lambda o: (not o["pre_approved"], o["source"] != "chef's swap list", o.get("fit") == "poor"))
    not_usable = [o for o in options if not o["ok"]]
    return {"item_id": item_id, "name": item["name"], "unit": unit, "needed": amount, "deadline": deadline,
            "dishes": [d for d in kitchen.load()["dishes"] if d["dish_id"] in dishes],
            "options": usable, "not_usable": not_usable, "picked_by": picked_by,
            "dish_swaps": _dish_swaps(item_id, dishes, portions, deadline) if portions else []}


def _ai_pick(item, dishes, amount, checked):
    """Ask Gemini which checked products suit the recipe. Returns [(product_id, note, fit)] or None."""
    doc = kitchen.load()
    recipes = [{"dish": d["name"], "ingredients": [{"item": kitchen.ingredient(g["item_id"], doc)["name"], "per_portion": g["amount"]}
                                                   for g in d["ingredients"]]}
               for d in doc["dishes"] if d["dish_id"] in dishes]
    system = ("You are a chef's assistant. An ingredient is missing for the dishes below. From the CANDIDATES list only, "
              "pick at most 3 products that work as a substitute in these recipes, best first. Judge by cooking logic "
              "(texture, flavour, how it cooks, fresh vs frozen vs preserved), not price alone. Reject products that would "
              "change the dish too much. For each pick give one short sentence on the trade-off for the chef, and 'fit': "
              "'good' or 'ok'. If none fit, return an empty list. Return JSON: "
              "{\"picks\": [{\"product_id\": \"...\", \"fit\": \"good\", \"note\": \"...\"}]}")
    user = json.dumps({"missing": item["name"], "usual_product": (catalog.get(item["usual_product"]) or {}).get("name_en"),
                       "amount_needed": f"{rules.num(amount)} {_u(item['unit'])}", "recipes": recipes,
                       "candidates": [{"product_id": c["product_id"], "name": c["name"], "price_per": c["price_per"],
                                       "frozen": c["temperature"] == 7} for c in checked]}, ensure_ascii=False)
    try:
        g = llm.generate_json(system, user)
    except llm.LLMError:
        return None
    ids = {c["product_id"] for c in checked}
    picks = []
    for p in g.get("picks") or []:
        if not isinstance(p, dict) or p.get("product_id") not in ids:
            continue  # only products plain code has already checked
        note = str(p.get("note") or "")[:240]
        if note:
            picks.append((p["product_id"], note, p.get("fit") if p.get("fit") in ("good", "ok") else "ok"))
    return picks[:3]
