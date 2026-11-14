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

print("\n=== 'as much as possible'")
r = agent.handle("I want as much salmon as possible", today=T)
check("asks only for the date", r["status"] == "ask_buyer" and r.get("ask_fields") == ["deadline"], r["question"])
r = agent.answer(r["id"], new_deadline="2026-11-16")
check("after the date: orders everything Aimo has (in 10 kg steps)", r["status"] == "ordered", r.get("current_request"))

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

print("\n=== last-minute events")
d = events.read_description("150 extra guests on Monday: salmon soup and meatballs", T)
check("reads guests, date and dishes", d["guests"] == 150 and d["date"] == "2026-11-16" and set(d["dishes"]) == {"salmon-soup", "meatballs"}, str(d))
ev = events.plan("Company dinner", "2026-11-16", [{"dish_id": x, "portions": 150} for x in d["dishes"]], "chef", today=T)
check("Monday event: orders what is missing, rest from stock", ev["summary"]["ordered"] >= 1, str(ev["summary"]))
ev2 = events.plan("Tomorrow", "2026-11-15", [{"dish_id": "salmon-soup", "portions": 150}], "chef", today=T)
stuck = [l for l in ev2["lines"] if l["status"] == "cannot_fulfil"]
check("tomorrow event: salmon can't be sourced in time", any(l["item_id"] == "salmon" for l in stuck), str(ev2["summary"]))
f = events.fixes(ev2["id"], "salmon")
check("...and the agent offers dishes that can be made from stock", any(x["from_stock_only"] for x in f["dish_swaps"]),
      ", ".join(x["name"] for x in f["dish_swaps"]))
ev3 = events.swap_dish(ev2["id"], "salmon-soup", f["dish_swaps"][0]["dish_id"], "chef")
check("swapping the dish re-plans the event", ev3["summary"]["cannot_source"] == 0, "; ".join(c["change"] for c in ev3["changes"]))

print(f"\n{sum(results)} of {len(results)} checks passed.")
