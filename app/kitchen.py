"""The kitchen: menu, ingredients, stock, and automatic restocking.

MOCK DATA: the menu, the recipe amounts and the kitchen's own stock are made up (data/kitchen.default.json).
Product codes, prices and Aimo's stock are real answers from the Valio Aimo mock API.

How automatic restocking works:
  1. Each ingredient has a reorder level. Stock goes down as dishes are served ("simulate a day").
  2. When stock is at or below the reorder level, the stock monitor sends a purchase request for the
     ingredient's usual order amount. It goes through the SAME agent and the SAME rules as a typed request.
  3. Duplicate guard: no new request while one is waiting for a person, or while an order is on its way.

The kitchen file is the single list of "usual orders" (which product, how much): rule A1 reads it.
"""
import copy
import json
import threading
from datetime import date, datetime, timedelta

from . import catalog, config, log

DEFAULT_FILE = config.DATA / "kitchen.default.json"
STATE_FILE = config.STATE / "kitchen.json"
_lock = threading.RLock()


class KitchenError(Exception):
    pass


def _u(unit):
    return "kg" if unit == "KG" else unit


def load():
    with _lock:
        if not STATE_FILE.exists():
            doc = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
            doc.setdefault("incoming", [])
            doc["day"] = 0
            save(doc)
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save(doc):
    with _lock:
        config.STATE.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def reset():
    STATE_FILE.unlink(missing_ok=True)
    return load()


def ingredients():
    return load()["ingredients"]


def ingredient(item_id, doc=None):
    return next((i for i in (doc or load())["ingredients"] if i["item_id"] == item_id), None)


def daily_use(doc=None):
    """How much of each ingredient the current menu uses per day (portions per day x amount per portion)."""
    doc = doc or load()
    use = {}
    for d in doc["dishes"]:
        for ing in d["ingredients"]:
            use[ing["item_id"]] = use.get(ing["item_id"], 0) + d["portions_per_day"] * ing["amount"]
    return {k: round(v, 3) for k, v in use.items()}


def used_by(item_id, doc=None):
    doc = doc or load()
    return [{"dish_id": d["dish_id"], "name": d["name"], "amount": i["amount"], "portions_per_day": d["portions_per_day"],
             "per_day": round(d["portions_per_day"] * i["amount"], 3)}
            for d in doc["dishes"] for i in d["ingredients"] if i["item_id"] == item_id]


def _by_item(records=None):
    """All requests grouped by ingredient, read from disk once."""
    from . import store
    out = {}
    for r in records if records is not None else store.all_records():
        iid = (r.get("parsed") or {}).get("item_id")
        if iid:
            out.setdefault(iid, []).append(r)
    return out


def _open_request(item_id, grouped=None):
    """A request for this ingredient that is still waiting for a person (approval or an answer)."""
    for r in (grouped if grouped is not None else _by_item()).get(item_id, []):
        if r.get("status") in ("needs_approval", "ask_buyer"):
            return r
    return None


def _last_failed(item_id, grouped=None):
    """The newest request for this ingredient, if the rules said it can't be done (not an API hiccup)."""
    mine = (grouped if grouped is not None else _by_item()).get(item_id, [])
    last = mine[-1] if mine else None
    return last if last and last.get("status") == "cannot_fulfil" and last.get("blocked_by") else None


def overview():
    doc = load()
    use = daily_use(doc)
    grouped = _by_item()
    rows = []
    for i in doc["ingredients"]:
        incoming = [x for x in doc["incoming"] if x["item_id"] == i["item_id"]]
        waiting = _open_request(i["item_id"], grouped)
        low = i["stock"] <= i["reorder_level"]
        failed = _last_failed(i["item_id"], grouped) if low and not incoming and not waiting else None
        # A waiting request only matters while stock is low; otherwise it is shown as a note, not as the status.
        state = ("on_order" if incoming else "waiting_for_person" if waiting and low else "failed" if failed else "low" if low else "ok")
        p = catalog.get(i["usual_product"]) or {}
        d = use.get(i["item_id"], 0)
        rows.append({**i, "daily_use": d, "days_left": round(i["stock"] / d, 1) if d else None,
                     "incoming": incoming, "incoming_amount": round(sum(x["amount"] for x in incoming), 2),
                     "open_request": waiting["id"] if waiting else failed["id"] if failed else None, "state": state,
                     "open_request_note": (f"{waiting['id']} is still open" if waiting and not low else None),
                     "usual_product_name": p.get("name_en") or p.get("name_fi"), "usual_brand": p.get("brand"),
                     "used_by": used_by(i["item_id"], doc)})
    return {"kitchen": doc["kitchen"], "day": doc.get("day", 0), "ingredients": rows, "dishes": doc["dishes"],
            "note": doc.get("_note")}


# ---------------------------------------------------------------- stock movements

def simulate_day(today=None):
    """Serve one day of the menu: stock goes down by the day's use. Then check what needs restocking."""
    with _lock:
        doc = load()
        use = daily_use(doc)
        short = []
        for i in doc["ingredients"]:
            need = use.get(i["item_id"], 0)
            if need > i["stock"] + 1e-9:
                short.append(f"{i['name']} (needed {need:g} {_u(i['unit'])}, had {i['stock']:g})")
            i["stock"] = round(max(i["stock"] - need, 0), 2)
        doc["day"] = doc.get("day", 0) + 1
        save(doc)
    portions = sum(d["portions_per_day"] for d in doc["dishes"])
    log.write("service", f"Simulated day {doc['day']} of service: {portions} portions of {len(doc['dishes'])} dishes. "
              f"Kitchen stock reduced by the recipes." + (f" RAN OUT of: {'; '.join(short)}." if short else ""),
              "kitchen stock (mock)", actor="system", data={"day": doc["day"], "ran_out": short})
    return {"day": doc["day"], "ran_out": short, "restock": check_restock(today)}


def receive_deliveries():
    """Mock: everything on its way arrives now and is added to the kitchen stock."""
    with _lock:
        doc = load()
        arrived = doc["incoming"]
        for x in arrived:
            i = ingredient(x["item_id"], doc)
            if i:
                i["stock"] = round(i["stock"] + x["amount"], 2)
        doc["incoming"] = []
        save(doc)
    from . import agent
    for item_id in {x["item_id"] for x in arrived}:
        agent.tidy_duplicates(item_id, "a delivery arrived")
    for x in arrived:
        log.write("delivery", f"Delivery received (mock): {x['amount']:g} {_u(x['unit'])} of {x['name']} from order {x['order_id']}. "
                  f"Added to kitchen stock.", "kitchen stock (mock)", actor="system", request_id=x.get("request_id"),
                  data=x)
    return {"arrived": arrived}


def add_incoming(item_id, amount, order_id, delivery_date, request_id=None, for_event=None):
    """Called when an order is placed, so the kitchen knows it is on its way (and does not reorder)."""
    with _lock:
        doc = load()
        i = ingredient(item_id, doc)
        if not i:
            return
        doc["incoming"].append({"item_id": item_id, "name": i["name"], "unit": i["unit"], "amount": round(amount, 2),
                                "order_id": order_id, "delivery_date": delivery_date, "request_id": request_id,
                                "for_event": for_event or None})
        save(doc)


def cancel_incoming(order_id):
    with _lock:
        doc = load()
        before = len(doc["incoming"])
        doc["incoming"] = [x for x in doc["incoming"] if x["order_id"] != order_id]
        save(doc)
        return len(doc["incoming"]) < before


def was_delivered(order_id):
    return any(e["step"] == "delivery" and (e.get("data") or {}).get("order_id") == order_id for e in log.read())


def check_restock(today=None):
    """The stock monitor. For every ingredient at or below its reorder level: send a request, unless
    one is already waiting for a person or an order is already on its way."""
    from . import agent
    today = today or date.today()
    doc = load()
    sent, skipped = [], []
    for i in doc["ingredients"]:
        if i["stock"] > i["reorder_level"]:
            continue
        if any(x["item_id"] == i["item_id"] for x in doc["incoming"]):
            skipped.append({"item_id": i["item_id"], "name": i["name"], "why": "an order is already on its way"})
            continue
        waiting = _open_request(i["item_id"])
        if waiting:
            skipped.append({"item_id": i["item_id"], "name": i["name"], "why": f"{waiting['id']} is already waiting for a person"})
            continue
        failed = _last_failed(i["item_id"])
        if failed:
            skipped.append({"item_id": i["item_id"], "name": i["name"],
                            "why": f"{failed['id']} could not be ordered under the rules; a person needs to look at it"})
            continue
        deadline = (today + timedelta(days=i.get("lead_days", 2))).isoformat()
        why = (f"{i['name']} is down to {i['stock']:g} {_u(i['unit'])} (reorder level {i['reorder_level']:g} {_u(i['unit'])}). "
               f"The stock monitor asked for the usual order of {i['usual_amount']:g} {_u(i['unit'])}.")
        rec = agent.handle(
            text=f"Automatic restock: {i['usual_amount']:g} {_u(i['unit'])} of {i['name'].lower()}",
            parsed={"product": i["name"], "item_id": i["item_id"], "amount": float(i["usual_amount"]), "unit": i["unit"],
                    "deadline": deadline, "parsed_by": "stock monitor (no typing involved)"},
            origin="auto", trigger=why)
        sent.append({"item_id": i["item_id"], "name": i["name"], "request_id": rec["id"], "status": rec["status"],
                     "order_id": rec.get("order_id")})
    for s in skipped:
        log.write("trigger", f"{s['name']} is low, but no new request was sent: {s['why']}.", "stock monitor",
                  actor="system", data=s)
    return {"sent": sent, "skipped": skipped}


# ---------------------------------------------------------------- editing the menu and ingredients
# Every change is checked, saved, and written to the audit log with the person's name.

COURSES = ("main", "breakfast", "dessert")
CATEGORIES = ("dairy", "dry goods", "fresh produce", "fish & meat")


def _slug(name, taken):
    import re
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "item"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def _check_recipe(ingredients, doc):
    known = {i["item_id"] for i in doc["ingredients"]}
    clean = []
    for g in ingredients or []:
        if g.get("item_id") not in known:
            raise KitchenError(f"Unknown ingredient {g.get('item_id')}")
        a = float(g.get("amount") or 0)
        if not 0 < a <= 5:
            raise KitchenError("Each ingredient needs an amount per portion between 1 g and 5 kg (or 5 L).")
        clean.append({"item_id": g["item_id"], "amount": round(a, 4)})
    if not clean:
        raise KitchenError("A dish needs at least one ingredient.")
    if len({g["item_id"] for g in clean}) != len(clean):
        raise KitchenError("The same ingredient is listed twice.")
    return clean


def save_dish(by, dish):
    """Add a dish (no dish_id) or change one (with dish_id): name, course, portions per day, recipe."""
    with _lock:
        doc = load()
        name = (dish.get("name") or "").strip()[:80]
        if not name:
            raise KitchenError("A dish needs a name.")
        course = dish.get("course") or "main"
        if course not in COURSES:
            raise KitchenError(f"Course must be one of {', '.join(COURSES)}.")
        ppd = int(dish.get("portions_per_day") or 0)
        if not 0 <= ppd <= 5000:
            raise KitchenError("Portions per day must be between 0 and 5,000.")
        recipe = _check_recipe(dish.get("ingredients"), doc)
        old = next((d for d in doc["dishes"] if d["dish_id"] == dish.get("dish_id")), None) if dish.get("dish_id") else None
        new = {"dish_id": old["dish_id"] if old else _slug(name, {d["dish_id"] for d in doc["dishes"]}), "name": name,
               "course": course, "portions_per_day": ppd, "ingredients": recipe}
        if old:
            doc["dishes"][doc["dishes"].index(old)] = new
        else:
            doc["dishes"].append(new)
        save(doc)
    names = {i["item_id"]: i["name"] for i in doc["ingredients"]}
    recipe_txt = ", ".join(f"{names[g['item_id']]} {g['amount'] * 1000:g} {'ml' if ingredient(g['item_id'], doc)['unit'] == 'L' else 'g'}" for g in recipe)
    log.write("menu", (f"Dish changed by {by}: {name}" if old else f"Dish added by {by}: {name}")
              + f" ({course}, {ppd} portions a day): {recipe_txt} per portion.", "kitchen menu", actor=f"person:{by}",
              data={"dish_id": new["dish_id"], "before": old})
    return new


def remove_dish(by, dish_id):
    with _lock:
        doc = load()
        d = next((x for x in doc["dishes"] if x["dish_id"] == dish_id), None)
        if not d:
            raise KitchenError("Unknown dish")
        doc["dishes"].remove(d)
        save(doc)
    log.write("menu", f"Dish removed by {by}: {d['name']}.", "kitchen menu", actor=f"person:{by}", data={"removed": d})
    return {"removed": dish_id}


def set_ingredient_amounts(by, item_id, amounts):
    """Change how much of ONE ingredient each dish uses, in one go. amounts: {dish_id: amount per portion}."""
    with _lock:
        doc = load()
        i = ingredient(item_id, doc)
        if not i:
            raise KitchenError("Unknown ingredient")
        changed = []
        for d in doc["dishes"]:
            if d["dish_id"] not in amounts:
                continue
            g = next((x for x in d["ingredients"] if x["item_id"] == item_id), None)
            a = float(amounts[d["dish_id"]])
            if not g or not 0 < a <= 5:
                raise KitchenError(f"Bad amount for {d['name']}")
            if abs(g["amount"] - a) > 1e-9:
                changed.append(f"{d['name']} {g['amount'] * 1000:g} → {a * 1000:g}")
                g["amount"] = round(a, 4)
        save(doc)
    if changed:
        u = "ml" if i["unit"] == "L" else "g"
        log.write("menu", f"{i['name']} per portion changed by {by} ({u}): " + "; ".join(changed) + ".", "kitchen menu",
                  actor=f"person:{by}", data={"item_id": item_id})
    return ingredient(item_id)


def save_ingredient(by, data):
    """Add a new ingredient (no item_id) or change one: linked Aimo product, usual order, reorder level, budget category."""
    with _lock:
        doc = load()
        old = ingredient(data.get("item_id"), doc) if data.get("item_id") else None
        name = (data.get("name") or (old or {}).get("name") or "").strip()[:60]
        unit = data.get("unit") or (old or {}).get("unit")
        pid = data.get("usual_product") or (old or {}).get("usual_product")
        if not name or unit not in ("L", "KG"):
            raise KitchenError("An ingredient needs a name and a unit (kg or litres).")
        p = catalog.get(pid or "")
        if not p:
            raise KitchenError("Choose the Valio Aimo product to order for it.")
        if not catalog.content_per_sales_unit(p, unit):
            raise KitchenError(f"The size of {p['name_en'] or pid} in {'kg' if unit == 'KG' else 'litres'} is not known; choose another product.")
        usual = float(data.get("usual_amount") if data.get("usual_amount") is not None else (old or {}).get("usual_amount") or 0)
        level = float(data.get("reorder_level") if data.get("reorder_level") is not None else (old or {}).get("reorder_level") or 0)
        stock = float(data.get("stock") if data.get("stock") is not None else (old or {}).get("stock") or 0)
        cat = data.get("category") or (old or {}).get("category") or "dry goods"
        if usual <= 0 or level < 0 or stock < 0 or cat not in CATEGORIES:
            raise KitchenError("Usual order must be more than 0; reorder level and stock 0 or more; category from the list.")
        if old:
            before = {k: old.get(k) for k in ("name", "usual_product", "usual_amount", "reorder_level", "category", "stock")}
            old.update(name=name, usual_product=pid, usual_amount=usual, reorder_level=level, category=cat, stock=stock)
            if before["usual_product"] != pid:
                old["search"] = {"words": "", "category": p["category"]}
            new = old
        else:
            new = {"item_id": _slug(name, {i["item_id"] for i in doc["ingredients"]}), "name": name, "unit": unit,
                   "aliases": [name.lower()], "usual_product": pid, "search": {"words": "", "category": p["category"]},
                   "usual_amount": usual, "per": "order", "reorder_level": level, "stock": stock, "lead_days": 2,
                   "category": cat, "recipe_swaps": []}
            doc["ingredients"].append(new)
            before = None
        save(doc)
    u = "kg" if unit == "KG" else unit
    log.write("menu", (f"Ingredient changed by {by}: " if before else f"Ingredient added by {by}: ")
              + f"{name}, ordered as {p['name_en'] or p['name_fi']} ({pid}); usual order {usual:g} {u}, reorder at {level:g} {u}, "
              f"budget: {cat}.", "kitchen menu", actor=f"person:{by}", data={"item_id": new["item_id"], "before": before})
    return new


def remove_ingredient(by, item_id):
    with _lock:
        doc = load()
        i = ingredient(item_id, doc)
        if not i:
            raise KitchenError("Unknown ingredient")
        users = [d["name"] for d in doc["dishes"] if any(g["item_id"] == item_id for g in d["ingredients"])]
        if users:
            raise KitchenError(f"{i['name']} is still used in: {', '.join(users)}. Change those recipes first.")
        doc["ingredients"].remove(i)
        save(doc)
    log.write("menu", f"Ingredient removed by {by}: {i['name']}.", "kitchen menu", actor=f"person:{by}", data={"removed": i})
    return {"removed": item_id}


def find_products(words, unit, limit=12):
    """Products to link a new ingredient to: matching words, a known size in the unit, with live price and stock."""
    from . import aimo
    from datetime import date as _d, timedelta as _td
    hits = [p for p in catalog.search(words, limit=400) if catalog.content_per_sales_unit(p, unit)][:60]
    if not hits:
        return []
    try:
        q = aimo.quote([p["id"] for p in hits], (_d.today() + _td(days=2)).isoformat())["products"]
    except aimo.AimoError:
        q = {}
    out = []
    for p in hits:
        x = q.get(p["id"]) or {}
        ok = x.get("deliverable") and not x.get("cutoff_passed") and x.get("priced")
        per = catalog.content_per_sales_unit(p, unit)
        out.append({"product_id": p["id"], "name": p["name_en"] or p["name_fi"], "brand": p["brand"], "per_pack": per,
                    "price_per": round(x["price_per_unit_with_tax"] / per, 2) if ok else None, "orderable": bool(ok),
                    "stock": "no limit" if x.get("stock_status") == "no_limit" else x.get("stock_amount")})
    out.sort(key=lambda o: (not o["orderable"], o["price_per"] or 1e9))
    return out[:limit]
