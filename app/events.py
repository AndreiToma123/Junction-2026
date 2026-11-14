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
# An event's lines keep ALL request ids ever made for an ingredient ("request_ids"), so the history stays visible.


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

def _menu(ev=None):
    """Kitchen menu plus the event's own custom dishes."""
    m = {d["dish_id"]: d for d in kitchen.load()["dishes"]}
    for d in (ev or {}).get("custom_dishes", []):
        m[d["dish_id"]] = d
    return m


def _needs(ev):
    menu = _menu(ev)
    need = {}
    for d in ev["dishes"]:
        for g in menu[d["dish_id"]]["ingredients"]:
            n = need.setdefault(g["item_id"], {"amount": 0.0, "dishes": []})
            n["amount"] += g["amount"] * d["portions"]
            if d["dish_id"] not in n["dishes"]:
                n["dishes"].append(d["dish_id"])
    return need


def _spare(item_id, event_date, today, doc):
    """What the kitchen can spare by the event: stock + routine deliveries arriving in time - normal service until then."""
    i = kitchen.ingredient(item_id, doc)
    days = (date.fromisoformat(event_date) - today).days
    arriving = sum(x["amount"] for x in doc["incoming"] if x["item_id"] == item_id and x["delivery_date"] <= event_date
                   and not x.get("for_event"))
    normal = kitchen.daily_use(doc).get(item_id, 0) * days
    return i, round(arriving, 3), round(normal, 3), round(max(0.0, i["stock"] + arriving - normal), 3)


def _requests(line):
    """All requests for this line, including follow-ups (a substitute or 'only the usual product' replaces a request)."""
    out, seen = [], set()
    ids = line.get("request_ids") or [x for x in (line.get("request_id"), line.get("extra_request_id")) if x]  # older events
    for rid in ids:
        r = store.get(rid)
        while r and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
            r = store.get(r["replaced_by"]) if r.get("replaced_by") else None
    return out


def _replan(ev, by, today=None):
    """Bring every ingredient line in line with the event's current dishes:
    order what is still missing, cancel open requests that are no longer right, keep what is already ordered."""
    today = today or date.today()
    doc = kitchen.load()
    need = _needs(ev)
    lines = {l["item_id"]: l for l in ev["lines"]}
    for item_id, n in need.items():
        i, arriving, normal, spare = _spare(item_id, ev["date"], today, doc)
        line = lines.get(item_id) or {"item_id": item_id, "request_ids": [], "spare": spare, "stock": i["stock"],
                                        "arriving": arriving, "normal_use": normal}
        line.update(name=i["name"], unit=i["unit"], needed=round(n["amount"], 3), dishes=n["dishes"])
        lines[item_id] = line
    for item_id in list(lines):
        if item_id not in need:  # no dish uses it any more
            lines[item_id]["needed"] = 0.0
            lines[item_id]["dishes"] = []

    for line in lines.values():
        reqs = _requests(line)
        ordered = sum(r["parsed"]["amount"] for r in reqs if r["status"] == "ordered")
        missing = round(max(0.0, line["needed"] - line["spare"] - ordered), 3)
        open_ = [r for r in reqs if r["status"] in ("needs_approval", "ask_buyer")]
        for r in open_:  # an open request for the wrong amount (or no longer needed) is replaced
            if missing <= 0 or abs(r["parsed"]["amount"] - _round_up(line, missing)) > 1e-6:
                agent.cancel(r["id"], by)
                r["status"] = "cancelled"
        open_ = [r for r in open_ if r["status"] != "cancelled"]
        last = reqs[-1] if reqs else None
        stuck_same = (last and last["status"] in ("cannot_fulfil", "rejected")
                      and abs(last["parsed"]["amount"] - _round_up(line, missing)) < 1e-6)
        if missing > 0 and not open_ and not stuck_same:
            amount = _round_up(line, missing)
            rec = agent.handle(
                text=f"Event {ev['id']}: {rules.num(amount)} {_u(line['unit'])} of {line['name'].lower()} by {rules.nice_date(ev['date'])}",
                parsed={"product": line["name"], "item_id": line["item_id"], "amount": amount, "unit": line["unit"],
                        "deadline": ev["date"], "buyer_confirmed_amount": True, "parsed_by": f"event planner ({ev['id']})"},
                scenario=ev.get("scenario"), origin="event", event_id=ev["id"],
                trigger=(f"Event {ev['id']} needs {rules.num(line['needed'])} {_u(line['unit'])} of {line['name'].lower()}; the kitchen can spare "
                         f"{rules.num(line['spare'])} after normal service until {rules.nice_date(ev['date'])}"
                         + (f" and {rules.num(ordered)} is already ordered" if ordered else "")
                         + f", so {rules.num(missing)} is missing"
                         + (f"; ordering {rules.num(amount)} (whole packs, the rest goes into stock)." if amount > missing + 1e-9 else ".")))
            line["request_ids"].append(rec["id"])
            line.pop("fix", None)  # an old fix no longer applies
        line["short"] = missing
        line["to_order"] = _round_up(line, missing) if missing > 0 else 0.0
    ev["lines"] = sorted([l for l in lines.values() if l["needed"] > 0 or l.get("request_ids")],
                         key=lambda l: (_status(l) == "covered", _status(l) == "ordered", l["name"]))
    return _refresh(ev)


def _round_up(line, amount):
    """Round up to whole packs of the usual product (the extra goes into stock)."""
    if amount <= 0:
        return 0.0
    i = kitchen.ingredient(line["item_id"])
    per = catalog.content_per_sales_unit(catalog.get(i["usual_product"]), i["unit"])
    units = rules.units_for(i["usual_product"], amount, i["unit"])
    return round(units * per, 3) if per and units else round(amount, 3)


def _status(line):
    reqs = _requests(line)
    if line.get("short", 0) <= 0:
        return "ordered" if any(r["status"] == "ordered" for r in reqs) else "covered"
    if not reqs:
        return "missing"  # short, and no request was sent (should not happen; shown honestly if it does)
    for r in reversed(reqs):
        if r["status"] != "cancelled":
            return r["status"] if r["status"] != "superseded" else "cancelled"
    return "cancelled" if reqs else "covered"


def plan(name, event_date, dishes, by, today=None, scenario=None, budget=None, custom_dishes=None, add_to_menu=False):
    """dishes: [{"dish_id", "portions"}]; custom_dishes: [{"name", "course", "ingredients": [{"item_id", "amount"}]}]
    (a dish just for this event; its portions are in `dishes` under the id it gets here)."""
    today = today or date.today()
    try:
        when = date.fromisoformat(event_date)
    except (TypeError, ValueError):
        raise ValueError("The event date must be YYYY-MM-DD.")
    if when <= today:
        raise ValueError("The event must be after today: Valio Aimo can't deliver today.")
    if budget is not None and (budget < 0 or budget > 1_000_000):
        raise ValueError("The event budget must be between €0 and €1,000,000.")
    ev = {"id": _new_id(), "name": name or "Event", "date": event_date, "budget": budget,
          "created_at": datetime.now().astimezone().isoformat(timespec="seconds"), "created_by": by,
          "dishes": [], "custom_dishes": [], "lines": [], "changes": [], "scenario": scenario}
    known = {i["item_id"] for i in kitchen.ingredients()}
    for n, c in enumerate(custom_dishes or [], 1):
        ings = [g for g in c.get("ingredients", []) if g.get("item_id") in known and 0 < float(g.get("amount", 0)) <= 5]
        if not c.get("name") or not ings:
            raise ValueError("A custom dish needs a name and at least one ingredient from the kitchen list.")
        dish = {"dish_id": c.get("dish_id") or f"{ev['id'].lower()}-custom-{n}", "name": c["name"].strip()[:80],
                "course": c.get("course") or "main", "custom": True, "portions_per_day": 0,
                "ingredients": [{"item_id": g["item_id"], "amount": float(g["amount"])} for g in ings]}
        ev["custom_dishes"].append(dish)
        if add_to_menu or c.get("add_to_menu"):
            kitchen.save_dish(by, {"name": dish["name"], "course": dish["course"], "portions_per_day": 0, "ingredients": dish["ingredients"]})
    menu = _menu(ev)
    if not dishes:
        raise ValueError("Choose at least one dish.")
    for d in dishes:
        if d["dish_id"] not in menu or not (0 < d["portions"] <= 5000):
            raise ValueError(f"Unknown dish or bad portion count: {d}")
        ev["dishes"].append({"dish_id": d["dish_id"], "portions": int(d["portions"]), "name": menu[d["dish_id"]]["name"]})
    save(ev)
    portions = sum(d["portions"] for d in ev["dishes"])
    log.write("event", f"Event {ev['id']} '{ev['name']}' on {rules.nice_date(event_date)}: {portions} extra portions of "
              + ", ".join(f"{d['portions']} × {d['name']}" for d in ev["dishes"])
              + (f". Event budget €{budget:,.0f}." if budget is not None else ". No event budget set.")
              + (" Custom dish(es): " + ", ".join(c["name"] for c in ev["custom_dishes"]) + "." if ev["custom_dishes"] else ""),
              "event planner", actor=f"person:{by}", data={"event_id": ev["id"]})
    return save(_replan(ev, by, today))


def _refresh(ev):
    """Update each line from its requests, and the event's cost against its budget."""
    from . import budgets
    pending_cost = 0.0
    if any(not l.get("request_ids") for l in ev["lines"]):
        # Older events: rebuild the links from the requests themselves (each request knows its event and ingredient).
        mine = [r for r in store.all_records() if r.get("event_id") == ev["id"]]
        for line in ev["lines"]:
            if not line.get("request_ids"):
                line["request_ids"] = [r["id"] for r in mine if (r.get("parsed") or {}).get("item_id") == line["item_id"]
                                       and not r.get("follow_up_of")]
    for line in ev["lines"]:
        reqs = _requests(line)
        line["status"] = _status(line)
        current = next((r for r in reversed(reqs) if r["status"] not in ("cancelled", "superseded")), reqs[-1] if reqs else None)
        line["request_id"] = current["id"] if current else None
        line["order_ids"] = [r["order_id"] for r in reqs if r["status"] == "ordered"]
        line["problem"] = None
        if current and current["status"] in ("cannot_fulfil", "rejected", "needs_approval", "ask_buyer"):
            o = next((o for o in current.get("options", []) if o["option_id"] == current.get("chosen")), None)
            line["problem"] = current.get("reason") or current.get("question") or (
                " ".join(r["detail"] for r in o["results"] if r["result"] != "pass") if o else None)
            if current["status"] == "needs_approval" and o:
                pending_cost += o["total_with_tax"]
    ev["summary"] = {
        "covered_from_stock": sum(1 for l in ev["lines"] if l["status"] == "covered"),
        "ordered": sum(1 for l in ev["lines"] if l["status"] == "ordered"),
        "waiting_for_person": sum(1 for l in ev["lines"] if l["status"] in ("needs_approval", "ask_buyer")),
        "cannot_source": sum(1 for l in ev["lines"] if l["status"] in ("cannot_fulfil", "rejected", "cancelled", "missing")),
    }
    b = budgets.status(f"event:{ev['id']}")
    ev["cost"] = {"ordered": b["spent"], "waiting_for_approval": round(pending_cost, 2),
                  "total": round(b["spent"] + pending_cost, 2), "budget": ev.get("budget"),
                  "left": round(ev["budget"] - b["spent"] - pending_cost, 2) if ev.get("budget") is not None else None}
    return ev


def view(eid):
    ev = get(eid)
    if not ev:
        return None
    return save(_refresh(ev))


def set_budget(eid, budget, by):
    ev = get(eid)
    if not ev:
        raise ValueError("Unknown event")
    if budget is not None and (budget < 0 or budget > 1_000_000):
        raise ValueError("The event budget must be between €0 and €1,000,000.")
    old = ev.get("budget")
    ev["budget"] = budget
    log.write("budget", f"Event {eid} budget changed by {by}: "
              + (f"€{old:,.0f}" if old is not None else "none") + " → " + (f"€{budget:,.0f}" if budget is not None else "none") + ".",
              "event planner", actor=f"person:{by}", data={"event_id": eid})
    return save(_refresh(ev))


# ---------------------------------------------------------------- fixing what can't be sourced

def fixes(eid, item_id, today=None):
    """Recipe-aware fixes for one stuck ingredient, SAVED on the event so they are still there later."""
    today = today or date.today()
    ev = get(eid)
    line = next((l for l in (ev or {}).get("lines", []) if l["item_id"] == item_id), None)
    if not line:
        raise ValueError("Unknown event or ingredient")
    menu = _menu(ev)
    rec = store.get(line["request_id"]) if line.get("request_id") else None
    missing = line.get("short") or line["needed"]
    if rec and rec.get("status") in ("needs_approval", "rejected", "cannot_fulfil"):
        out = substitutes.for_request(rec)
    else:
        out = substitutes.propose(item_id, missing, ev["date"], dishes=line["dishes"])
        out["usual_available"] = substitutes.usual_available(item_id, ev["date"], ev.get("scenario"))
    have = out.get("usual_available")
    ordered = sum(r["parsed"]["amount"] for r in _requests(line) if r["status"] == "ordered")
    can_get = line["spare"] + ordered + (have if have else 0)

    # How many portions of each affected dish can still be made, and which dish could take the rest.
    doc = kitchen.load()
    leftover = {}
    for i in doc["ingredients"]:
        _, _, _, sp = _spare(i["item_id"], ev["date"], today, doc)
        used = next((l["needed"] for l in ev["lines"] if l["item_id"] == i["item_id"]), 0.0)
        leftover[i["item_id"]] = max(0.0, sp - used)
    partial = []
    for d in ev["dishes"]:
        if d["dish_id"] not in line["dishes"]:
            continue
        per = next(g["amount"] for g in menu[d["dish_id"]]["ingredients"] if g["item_id"] == item_id)
        others = line["needed"] - per * d["portions"]
        keep = int(max(0, min(d["portions"], (can_get - others) // per))) if per else d["portions"]
        move = d["portions"] - keep
        if move <= 0:
            continue
        course = menu[d["dish_id"]].get("course")
        cands = []
        for alt in list(menu.values()):
            if alt["dish_id"] == d["dish_id"] or alt.get("course", "main") != (course or "main"):
                continue
            if any(g["item_id"] == item_id for g in alt["ingredients"]):
                continue
            short = [kitchen.ingredient(g["item_id"], doc)["name"] for g in alt["ingredients"]
                     if g["amount"] * move > leftover.get(g["item_id"], 0) + 1e-9]
            cands.append({"dish_id": alt["dish_id"], "name": alt["name"], "already_on_event": any(x["dish_id"] == alt["dish_id"] for x in ev["dishes"]),
                          "from_stock_only": not short, "short": short})
        cands.sort(key=lambda c: (not c["from_stock_only"], not c["already_on_event"], len(c["short"])))
        partial.append({"dish_id": d["dish_id"], "name": d["name"], "portions": d["portions"], "can_make": keep, "move": move,
                        "per_portion": per, "alternatives": cands[:4]})
    out["portion_plan"] = partial
    out["can_get"] = round(can_get, 3)
    out["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    line["fix"] = out
    save(ev)
    return out


def swap_dish(eid, from_dish, to_dish, by, move=None, today=None):
    """Move `move` portions (default: all) of one dish to another (on the menu, or already on the event).
    Then re-plan: open requests that no longer fit are cancelled, missing amounts are ordered."""
    ev = get(eid)
    if not ev:
        raise ValueError("Unknown event")
    menu = _menu(ev)
    old = next((d for d in ev["dishes"] if d["dish_id"] == from_dish), None)
    if not old or to_dish not in menu or to_dish == from_dish:
        raise ValueError("Unknown dish")
    move = old["portions"] if move is None else int(move)
    if not (0 < move <= old["portions"]):
        raise ValueError("Bad number of portions to move")
    old["portions"] -= move
    ev["dishes"] = [d for d in ev["dishes"] if d["portions"] > 0]
    tgt = next((d for d in ev["dishes"] if d["dish_id"] == to_dish), None)
    if tgt:
        tgt["portions"] += move
    else:
        ev["dishes"].append({"dish_id": to_dish, "portions": move, "name": menu[to_dish]["name"]})
    text = (f"{move} portions of {menu[from_dish]['name']} moved to {menu[to_dish]['name']}"
            + (f" ({old['portions']} of {menu[from_dish]['name']} kept)" if old["portions"] else ""))
    ev["changes"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"), "by": by, "change": text})
    log.write("event", f"Event {eid}: {by} changed the menu: {text}.", "event planner", actor=f"person:{by}", data={"event_id": eid})
    for l in ev["lines"]:
        l.pop("fix", None)
    return save(_replan(ev, by, today))
