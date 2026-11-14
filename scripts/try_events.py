"""Test the event planner, recipe-aware substitutes and the request fixes, with LIVE Aimo data.
Runs in a sandbox with fresh demo kitchen data: your real data is not touched. Uses no Gemini calls.

Run: .venv/bin/python -m scripts.try_events
"""
from scripts import _sandbox  # noqa: F401  (must be first)
from datetime import date

from app import agent, events, llm, parse, substitutes

llm.available = lambda: False  # plain-code mode: saves free-tier Gemini calls
T = date(2026, 11, 14)
results = []


def check(name, cond, shown=""):
    results.append(bool(cond))
    print(f"{'  ok  ' if cond else ' FAIL '} {name}" + (f"\n         -> {shown}" if shown else ""))


print("=== reading requests")
for text, want in [("I need at least at least 40 kg of salmon by Nov 16th", (40.0, "2026-11-16")),
                   ("I need at least 70 kg of salmon but Nov 16th", (70.0, "2026-11-16")),
                   ("70 kg salmon 16.11.", (70.0, "2026-11-16")),
                   ("10 kg salmon by 16 marraskuuta", (10.0, "2026-11-16"))]:
    p = parse.parse(text, T)
    check(f"'{text}'", (p["amount"], p["deadline"]) == want, f"{p['amount']} {p['unit']} by {p['deadline']}")

print("\n=== 'as much as possible' never buys by itself")
r = agent.handle("I want as much salmon as possible", today=T)
check("asks only for the date", r["status"] == "ask_buyer" and r.get("ask_fields") == ["deadline"], r["question"])
r = agent.answer(r["id"], new_deadline="2026-11-16")
check("then shows what Aimo has and asks for a number", r["status"] == "ask_buyer" and "up to 40 kg" in r["question"], r["question"])
r = agent.answer(r["id"], new_amount=40)
check("the buyer types 40: ordered", r["status"] == "ordered", r.get("current_request"))
r = agent.handle("we need some stuff by friday", today=T)
check("unclear product: asks with a list, not a dead end", "product" in (r.get("ask_fields") or []), r["question"])
r = agent.answer(r["id"], new_item_id="butter", new_amount=20)
check("picked butter + 20 kg from the form: ordered", r["status"] == "ordered", r.get("current_request"))

print("\n=== stock-out: salmon short, recipe-aware fixes")
r = agent.handle(text="restock salmon", parsed={"product": "Salmon fillet", "item_id": "salmon", "amount": 60.0, "unit": "KG",
                                                 "deadline": "2026-11-16", "parsed_by": "test"})
check("60 kg salmon -> split with trout needs the chef", r["status"] == "needs_approval")
s = substitutes.for_request(r)
check("substitutes cover only the missing part", s["combine_with_usual"] and s["needed"] == 20.0,
      f"usual available {s['usual_available']} kg, missing {s['needed']} kg: " + ", ".join(o["name"] for o in s["options"]))
rej = agent.decide(r["id"], "reject", "chef")
n = agent.usual_only(rej["id"], "chef")
check("after rejecting: 'only the usual product' orders what is available", n["status"] == "ordered", n.get("current_request"))
r2 = agent.handle(text="restock salmon", parsed={"product": "Salmon fillet", "item_id": "salmon", "amount": 60.0, "unit": "KG",
                                                  "deadline": "2026-11-16", "parsed_by": "test"})
n2 = agent.order_substitute(r2["id"], "462016", "chef")
check("chef picks trout: ordered without asking again", n2["status"] == "ordered",
      next(x["detail"] for o in n2["options"] if o["option_id"] == n2["chosen"] for x in o["results"] if x["rule"] == "C2"))
c = agent.cancel(n2["id"], "chef")
check("an order on its way can be cancelled", c["status"] == "cancelled", c["steps"][-1]["message"])

print("\n=== hanging duplicates")
auto = agent.handle(text="restock", parsed={"product": "Salmon fillet", "item_id": "salmon", "amount": 60.0, "unit": "KG",
                                             "deadline": "2026-11-16", "parsed_by": "test"}, origin="auto")
typed_old = agent.handle("I need 60 kg of salmon by Nov 16th", today=T)
agent.handle("I need 40 kg of salmon by Nov 16th", today=T)
from app import store
check("automatic restock request closed once salmon is on its way", store.get(auto["id"])["status"] == "superseded",
      store.get(auto["id"])["steps"][-1]["message"][:140])
check("typed request only flagged, not closed", store.get(typed_old["id"])["status"] == "needs_approval"
      and store.get(typed_old["id"]).get("possible_duplicate"))

print("\n=== last-minute events")
from app import kitchen
kitchen.reset()  # fresh kitchen stock: the orders above would otherwise cover the event
d = events.read_description("150 extra guests on Monday: salmon soup and meatballs", T)
check("reads guests, date and dishes", d["guests"] == 150 and d["date"] == "2026-11-16" and set(d["dishes"]) == {"salmon-soup", "meatballs"}, str(d))
ev = events.plan("Company dinner", "2026-11-16", [{"dish_id": x, "portions": 150} for x in d["dishes"]], "chef", today=T, budget=2500)
check("Monday event: orders what is missing, rest from stock", ev["summary"]["ordered"] >= 1, str(ev["summary"]))
check("event cost is shown against its budget", ev["cost"]["budget"] == 2500 and ev["cost"]["total"] > 0, str(ev["cost"]))
big = events.plan("Wedding", "2026-11-16", [{"dish_id": "salmon-soup", "portions": 1200}, {"dish_id": "meatballs", "portions": 300}], "chef", today=T)
sal = next(l for l in big["lines"] if l["item_id"] == "salmon")
check("1,200 portions of salmon soup: salmon is stuck", sal["status"] in ("needs_approval", "cannot_fulfil"), f"{sal['status']}: need {sal['needed']}, spare {sal['spare']}, " + (sal.get("problem") or "")[:120])
f = events.fixes(big["id"], "salmon", today=T)
plan = f["portion_plan"][0]
check("fix: how many portions can be made, and where to move the rest", plan["can_make"] < plan["portions"] and plan["alternatives"],
      f"make {plan['can_make']} of {plan['portions']}, move {plan['move']} to e.g. {plan['alternatives'][0]['name']}")
check("the fix is saved with the event", "fix" in next(l for l in events.get(big["id"])["lines"] if l["item_id"] == "salmon"))
alt = next(a for a in plan["alternatives"] if a["already_on_event"])
after = events.swap_dish(big["id"], plan["dish_id"], alt["dish_id"], "chef", move=plan["move"], today=T)
check("moving the portions re-plans: nothing stuck any more", after["summary"]["cannot_source"] == 0 and after["summary"]["waiting_for_person"] == 0,
      "; ".join(c["change"] for c in after["changes"]))
cd = events.plan("Custom", "2026-11-16", [{"dish_id": "x1", "portions": 40}], "chef", today=T,
                 custom_dishes=[{"dish_id": "x1", "name": "Salmon pasta", "course": "main",
                                 "ingredients": [{"item_id": "salmon", "amount": 0.1}, {"item_id": "penne", "amount": 0.1}]}])
check("a dish not on the menu can be added right in the event", [d["name"] for d in cd["dishes"]] == ["Salmon pasta"], str(cd["summary"]))

print("\n=== ranking and menu editing")
r = agent.handle("I need 1,000 litres of lactose-free milk by Monday.", today=T)
ranks = [o["rank"] for o in r["options"]]
check("every option has a place and a reason", ranks == sorted(ranks) and all(o.get("rank_reason") for o in r["options"]),
      "; ".join(f"#{o['rank']} {o['rank_reason'][:50]}" for o in r["options"][:3]))
s2 = agent.handle(text="restock salmon", parsed={"product": "Salmon fillet", "item_id": "salmon", "amount": 60.0, "unit": "KG",
                                                  "deadline": "2026-11-16", "parsed_by": "test"})
check("a split says it is a split first", (s2.get("situation") or "").startswith("Split order"), s2.get("situation"))
subs = substitutes.for_request(s2)
check("substitutes are ranked with a reason", all(o.get("rank") and o.get("rank_reason") for o in subs["options"]),
      "; ".join(f"#{o['rank']} {o['name']}: {o['rank_reason']}" for o in subs["options"]))
found = kitchen.find_products("shrimp", "KG")
ing = kitchen.save_ingredient("chef", {"name": "Shrimps", "unit": "KG", "usual_product": found[0]["product_id"], "usual_amount": 10,
                                       "reorder_level": 3, "stock": 5, "category": "fish & meat"})
dish = kitchen.save_dish("chef", {"name": "Shrimp pasta", "course": "main", "portions_per_day": 10,
                                   "ingredients": [{"item_id": ing["item_id"], "amount": 0.08}, {"item_id": "penne", "amount": 0.1}]})
check("a new ingredient (linked to a real Aimo product) and a new dish can be added", dish["dish_id"] in {d["dish_id"] for d in kitchen.load()["dishes"]},
      f"{ing['name']} → {found[0]['name']}")
kitchen.set_ingredient_amounts("chef", "penne", {"shrimp-pasta": 0.09})
check("a shared ingredient's amount can be changed per dish in one place",
      next(g for g in next(d for d in kitchen.load()["dishes"] if d["dish_id"] == "shrimp-pasta")["ingredients"] if g["item_id"] == "penne")["amount"] == 0.09)
try:
    kitchen.remove_ingredient("chef", ing["item_id"]); refused = False
except kitchen.KitchenError:
    refused = True
check("an ingredient still used in a dish can't be removed", refused)

print(f"\n{sum(results)} of {len(results)} checks passed.")
