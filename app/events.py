"""Last-minute events: "40 extra guests on Monday: salmon soup and meatballs".

1. Plain code works out what the recipes need for the extra portions, and what the kitchen can spare
   (stock + deliveries arriving in time - what normal service uses until then).
2. For each ingredient that is short, the agent sends a purchase request with the event date as deadline.
   Every request goes through the same rules (spend limit, brands, stock, cut-off...).
3. For anything that can't be sourced in time, it proposes recipe-aware fixes (substitutes.py):
   a substitute that works in that dish, or a different dish that can be made from kitchen stock.
   A person chooses; nothing is changed on the menu by the agent alone.

Events are saved as data/events/EV-0001.json. Every step is in the audit log.
"""
import json
import re
from datetime import date, datetime

from . import agent, catalog, config, kitchen, llm, log, parse, rules, store, substitutes

DIR = config.STATE / "events"


def _u(unit):
    return "kg" if unit == "KG" else unit


def _new_id():
    DIR.mkdir(parents=True, exist_ok=True)
    return f"EV-{len(list(DIR.glob('EV-*.json'))) + 1:04d}"


def save(ev):
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{ev['id']}.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
    return ev


def get(eid):
    f = DIR / f"{eid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def all_events():
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(DIR.glob("EV-*.json"))] if DIR.exists() else []


# ---------------------------------------------------------------- reading a description

def read_description(text, today=None):
    """'40 extra guests on Monday: salmon soup and meatballs' -> {date, guests, dishes}. Never guesses:
    anything it can't find is left empty for the person to fill in on the form."""
    today = today or date.today()
    doc = kitchen.load()
    t = (text or "").lower()
    m = re.search(r"(\d+)\s*(?:extra\s+|more\s+|additional\s+)?(?:guests|people|persons|covers|pax|portions|vierasta|henkeä)", t)
    guests = int(m.group(1)) if m else None
    when = parse._deadline(text or "", today)
    dishes = []
    for d in doc["dishes"]:
        # the dish's key words: the part of its name before "with" / "in" / "(", e.g. "creamy salmon soup", "meatballs"
        head = re.split(r"\s(?:with|in)\s|\s\(", d["name"].lower())[0]
        words = [w for w in re.findall(r"[a-zäö]+", head) if len(w) > 3 and w not in ("and", "fillet")]
        need = len(words) if len(words) <= 2 else len(words) - 1
        key = re.findall(r"\(([^)]+)\)", d["name"].lower())
        if any(k in t for k in key) or (words and sum(re.search(rf"\b{w}", t) is not None for w in words) >= need):
            dishes.append(d["dish_id"])
    out = {"guests": guests, "date": when, "dishes": dishes, "read_by": "plain code (no AI)"}
    if llm.available() and (not guests or not when or not dishes):
        try:
            g = llm.generate_json(
                "Read an event booking for a restaurant kitchen. Return JSON {\"guests\": int or null, \"date\": \"YYYY-MM-DD\" or null, "
                "\"dish_ids\": [ids from the menu only]}. Use null / [] for anything not stated. Never guess.",
                json.dumps({"today": today.isoformat(), "weekday": today.strftime("%A"), "text": text,
                            "menu": [{"dish_id": d["dish_id"], "name": d["name"]} for d in doc["dishes"]]}, ensure_ascii=False))
            ids = {d["dish_id"] for d in doc["dishes"]}
            gd = [x for x in (g.get("dish_ids") or []) if x in ids]
            gg = g.get("guests") if isinstance(g.get("guests"), int) and 0 < g.get("guests") <= 5000 else None
            try:
                gw = g.get("date") if g.get("date") and date.fromisoformat(g["date"]) >= today else None
            except ValueError:
                gw = None
            out = {"guests": guests or gg, "date": when or gw, "dishes": dishes or gd, "read_by": "Gemini (checked)"}
        except llm.LLMError:
            pass
    return out


# ---------------------------------------------------------------- planning

def plan(name, event_date, dishes, by, today=None, scenario=None):
    """dishes: [{"dish_id", "portions"}]. Creates the event, sends the purchase requests, returns the event."""
    today = today or date.today()
    if not dishes:
        raise ValueError("Choose at least one dish.")
    try:
        when = date.fromisoformat(event_date)
    except (TypeError, ValueError):
        raise ValueError("The event date must be YYYY-MM-DD.")
    if when <= today:
        raise ValueError("The event must be after today: Valio Aimo can't deliver today.")
    doc = kitchen.load()
    menu = {d["dish_id"]: d for d in doc["dishes"]}
    for d in dishes:
        if d["dish_id"] not in menu or not (0 < d["portions"] <= 5000):
            raise ValueError(f"Unknown dish or bad portion count: {d}")
    days = (when - today).days
    use = kitchen.daily_use(doc)

    need = {}
    for d in dishes:
        for g in menu[d["dish_id"]]["ingredients"]:
            n = need.setdefault(g["item_id"], {"amount": 0.0, "dishes": []})
            n["amount"] += g["amount"] * d["portions"]
            n["dishes"].append(d["dish_id"])
    ev = {"id": _new_id(), "name": name or "Event", "date": event_date, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
          "created_by": by, "dishes": [{**d, "name": menu[d["dish_id"]]["name"]} for d in dishes], "lines": [], "changes": [],
          "scenario": scenario}
    portions = sum(d["portions"] for d in dishes)
    log.write("event", f"Event {ev['id']} '{ev['name']}' on {rules.nice_date(event_date)}: {portions} extra portions of "
              + ", ".join(f"{d['portions']} × {menu[d['dish_id']]['name']}" for d in dishes) + ".",
              "event planner", actor=f"person:{by}", data={"event_id": ev["id"]})

    for item_id, n in need.items():
        i = kitchen.ingredient(item_id, doc)
        arriving = sum(x["amount"] for x in doc["incoming"] if x["item_id"] == item_id and x["delivery_date"] <= event_date)
        normal = use.get(item_id, 0) * days  # normal service until (and including) the event day
        spare = max(0.0, i["stock"] + arriving - normal)
        short = round(max(0.0, n["amount"] - spare), 3)
        to_order = short
        if short > 0:  # round up to the usual product's pack / order step; the rest goes into kitchen stock
            per = catalog.content_per_sales_unit(catalog.get(i["usual_product"]), i["unit"])
            units = rules.units_for(i["usual_product"], short, i["unit"])
            if per and units:
                to_order = round(units * per, 3)
        line = {"item_id": item_id, "name": i["name"], "unit": i["unit"], "needed": round(n["amount"], 3),
                "stock": i["stock"], "arriving": round(arriving, 3), "normal_use": round(normal, 3), "spare": round(spare, 3),
                "short": short, "to_order": to_order, "dishes": n["dishes"], "request_id": None, "status": "covered"}
        if short > 0:
            rec = agent.handle(
                text=f"Event {ev['id']}: {rules.num(to_order)} {_u(i['unit'])} of {i['name'].lower()} by {rules.nice_date(event_date)}",
                parsed={"product": i["name"], "item_id": item_id, "amount": to_order, "unit": i["unit"], "deadline": event_date,
                        "buyer_confirmed_amount": True, "parsed_by": f"event planner ({ev['id']})"},
                scenario=scenario, origin="event",
                trigger=(f"Event {ev['id']} needs {rules.num(n['amount'])} {_u(i['unit'])} of {i['name'].lower()}; the kitchen can spare "
                         f"{rules.num(spare)} after normal service until {rules.nice_date(event_date)}, so {rules.num(short)} is missing"
                         + (f"; ordering {rules.num(to_order)} (whole packs, the rest goes into stock)." if to_order > short + 1e-9 else ".")))
            rec["event_id"] = ev["id"]
            store.save(rec)
            line.update(request_id=rec["id"], status=rec["status"])
        ev["lines"].append(line)
    ev["lines"].sort(key=lambda l: (l["status"] == "covered", l["status"] == "ordered", l["name"]))
    _refresh(ev)
    return save(ev)


def _refresh(ev):
    """Update each line's status from its request, and attach recipe-aware fixes where something is stuck."""
    for line in ev["lines"]:
        rid = line.get("request_id")
        rec = store.get(rid) if rid else None
        while rec and rec.get("replaced_by"):  # follow substitutes / follow-ups
            rec = store.get(rec["replaced_by"])
        if rec:
            line["request_id"], line["status"] = rec["id"], rec["status"]
            line["order_id"] = rec.get("order_id")
            line["problem"] = rec.get("reason") or (" ".join(r["detail"] for o in rec.get("options", []) if o["option_id"] == rec.get("chosen")
                                                              for r in o["results"] if r["result"] != "pass") or None)
    ev["summary"] = {
        "covered_from_stock": sum(1 for l in ev["lines"] if l["status"] == "covered"),
        "ordered": sum(1 for l in ev["lines"] if l["status"] == "ordered"),
        "waiting_for_person": sum(1 for l in ev["lines"] if l["status"] in ("needs_approval", "ask_buyer")),
        "cannot_source": sum(1 for l in ev["lines"] if l["status"] == "cannot_fulfil"),
    }
    return ev


def view(eid):
    ev = get(eid)
    if not ev:
        return None
    return save(_refresh(ev))


def fixes(eid, item_id):
    """Recipe-aware fixes for one stuck ingredient of an event."""
    ev = get(eid)
    line = next((l for l in (ev or {}).get("lines", []) if l["item_id"] == item_id), None)
    if not line:
        raise ValueError("Unknown event or ingredient")
    portions = max(d["portions"] for d in ev["dishes"] if d["dish_id"] in line["dishes"])
    rec = store.get(line["request_id"]) if line.get("request_id") else None
    if rec and rec.get("status") in ("needs_approval", "rejected", "cannot_fulfil"):
        out = substitutes.for_request(rec)
    else:
        out = substitutes.propose(item_id, line["short"] or line["needed"], ev["date"], dishes=line["dishes"])
    out["dish_swaps"] = substitutes._dish_swaps(item_id, [d["dish_id"] for d in ev["dishes"]], portions, ev["date"])
    out["affected_dishes"] = [d for d in ev["dishes"] if d["dish_id"] in line["dishes"]]
    return out


def swap_dish(eid, from_dish, to_dish, by):
    """A person replaces a dish on the event menu. Open requests for ingredients no longer needed are cancelled;
    the event is re-planned for the new dish (its ingredients go through the same steps)."""
    ev = get(eid)
    if not ev:
        raise ValueError("Unknown event")
    old = next((d for d in ev["dishes"] if d["dish_id"] == from_dish), None)
    menu = {d["dish_id"]: d for d in kitchen.load()["dishes"]}
    if not old or to_dish not in menu:
        raise ValueError("Unknown dish")
    portions = old["portions"]
    still = {g["item_id"] for d in ev["dishes"] if d["dish_id"] != from_dish for g in menu[d["dish_id"]]["ingredients"]}
    for line in ev["lines"]:
        if from_dish in line["dishes"] and line["item_id"] not in still and line["status"] in ("needs_approval", "ask_buyer"):
            agent.cancel(line["request_id"], by)
    ev["dishes"] = [d for d in ev["dishes"] if d["dish_id"] != from_dish]
    ev["changes"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"), "by": by,
                          "change": f"{menu[from_dish]['name']} replaced by {menu[to_dish]['name']} ({portions} portions)"})
    log.write("event", f"Event {eid}: {by} replaced {menu[from_dish]['name']} with {menu[to_dish]['name']} for {portions} portions.",
              "event planner", actor=f"person:{by}", data={"event_id": eid})
    keep = [l for l in ev["lines"] if l["item_id"] in still]
    extra = plan(f"{ev['name']} (dish change)", ev["date"], [{"dish_id": to_dish, "portions": portions}], by, scenario=ev.get("scenario"))
    # merge the new dish's lines into this event, then drop the helper event file
    merged = {l["item_id"]: l for l in keep}
    for l in extra["lines"]:
        if l["item_id"] in merged:
            merged[l["item_id"]]["needed"] += l["needed"]
            merged[l["item_id"]]["dishes"] += l["dishes"]
            if l.get("request_id"):
                merged[l["item_id"]]["extra_request_id"] = l["request_id"]
        else:
            merged[l["item_id"]] = l
    ev["dishes"].append({"dish_id": to_dish, "portions": portions, "name": menu[to_dish]["name"]})
    ev["lines"] = sorted(merged.values(), key=lambda l: (l["status"] == "covered", l["status"] == "ordered", l["name"]))
    (DIR / f"{extra['id']}.json").unlink(missing_ok=True)
    for rid in [l.get("request_id") for l in extra["lines"] if l.get("request_id")]:
        r = store.get(rid)
        if r:
            r["event_id"] = eid
            store.save(r)
    return save(_refresh(ev))
