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


def _open_request(item_id):
    """A request for this ingredient that is still waiting for a person (approval or an answer)."""
    from . import store
    for r in store.all_records():
        if (r.get("parsed") or {}).get("item_id") == item_id and r.get("status") in ("needs_approval", "ask_buyer"):
            return r
    return None


def _last_failed(item_id):
    """The newest request for this ingredient, if the rules said it can't be done (not an API hiccup)."""
    from . import store
    mine = [r for r in store.all_records() if (r.get("parsed") or {}).get("item_id") == item_id]
    last = mine[-1] if mine else None
    return last if last and last.get("status") == "cannot_fulfil" and last.get("blocked_by") else None


def overview():
    doc = load()
    use = daily_use(doc)
    rows = []
    for i in doc["ingredients"]:
        incoming = [x for x in doc["incoming"] if x["item_id"] == i["item_id"]]
        waiting = _open_request(i["item_id"])
        low = i["stock"] <= i["reorder_level"]
        failed = _last_failed(i["item_id"]) if low and not incoming and not waiting else None
        state = ("on_order" if incoming else "waiting_for_person" if waiting else "failed" if failed else "low" if low else "ok")
        p = catalog.get(i["usual_product"]) or {}
        d = use.get(i["item_id"], 0)
        rows.append({**i, "daily_use": d, "days_left": round(i["stock"] / d, 1) if d else None,
                     "incoming": incoming, "incoming_amount": round(sum(x["amount"] for x in incoming), 2),
                     "open_request": waiting["id"] if waiting else failed["id"] if failed else None, "state": state,
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
    for x in arrived:
        log.write("delivery", f"Delivery received (mock): {x['amount']:g} {_u(x['unit'])} of {x['name']} from order {x['order_id']}. "
                  f"Added to kitchen stock.", "kitchen stock (mock)", actor="system", request_id=x.get("request_id"),
                  data=x)
    return {"arrived": arrived}


def add_incoming(item_id, amount, order_id, delivery_date, request_id=None):
    """Called when an order is placed, so the kitchen knows it is on its way (and does not reorder)."""
    with _lock:
        doc = load()
        i = ingredient(item_id, doc)
        if not i:
            return
        doc["incoming"].append({"item_id": item_id, "name": i["name"], "unit": i["unit"], "amount": round(amount, 2),
                                "order_id": order_id, "delivery_date": delivery_date, "request_id": request_id})
        save(doc)


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
