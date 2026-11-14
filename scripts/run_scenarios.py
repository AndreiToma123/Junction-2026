"""Run the 4 demo scenarios end to end with LIVE Aimo data (read-only) and MOCK orders.

Run: .venv/bin/python -m scripts.run_scenarios          (all four)
     .venv/bin/python -m scripts.run_scenarios split    (just one: normal | split | spend | typo)
"""
import sys

from app import agent

SCENARIOS = {
    "normal": ("I need 1,000 litres of lactose-free milk by Monday.", None),
    "split": ("I need 1,000 litres of lactose-free milk by Monday.", "split"),
    "spend": ("I need 2,000 litres of lactose-free milk by Monday.", None),
    "typo": ("I need 10,000 litres of lactose-free milk by Monday.", None),
}


def show(rec):
    print(f"\n=== {rec['id']}: \"{rec['text']}\"" + (f"  [demo: {rec['scenario']}]" if rec.get("scenario") else ""))
    for s in rec["steps"]:
        print(f"  {s['step']:9} {s['message']}")
    print(f"  --> STATUS: {rec['status'].upper()}" + (f"  order {rec['order_id']}" if rec.get("order_id") else ""))


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for n in names:
        text, scenario = SCENARIOS[n]
        show(agent.handle(text, scenario=scenario))
