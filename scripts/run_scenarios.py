"""The 4 demo scenarios, end to end: LIVE Valio Aimo data (read-only) + MOCK orders.

Run all four:   .venv/bin/python -m scripts.run_scenarios
Run just one:   .venv/bin/python -m scripts.run_scenarios normal     (orders by itself)
                .venv/bin/python -m scripts.run_scenarios split      (needs approval: new brand)
                .venv/bin/python -m scripts.run_scenarios spend      (needs approval: spend limit)
                .venv/bin/python -m scripts.run_scenarios typo       (agent asks instead of ordering)

Each one prints what the agent did, step by step, and checks the result is the expected one.
The requests also show up on the dashboard (http://localhost:8000), where you can approve,
reject or answer them. For a clean start first run:  .venv/bin/python -m scripts.reset_demo
"""
import sys

from app import agent

SCENARIOS = {
    # name: (what the buyer types, demo stock scenario, expected status, what it shows)
    "normal": ("I need 1,000 litres of lactose-free milk by Monday.", None, "ordered",
               "Every rule passes, so the agent orders by itself."),
    "split": ("I need 1,000 litres of lactose-free milk by Monday.", "split", "needs_approval",
              "Only 600 L of the usual Valio in stock (simulated). The agent splits with Arla; a new brand needs approval."),
    "spend": ("I need 2,000 litres of lactose-free milk by Monday.", None, "needs_approval",
              "2,000 L costs more than the €2,000 limit, so a person must approve."),
    "typo": ("I need 10,000 litres of lactose-free milk by Monday.", None, "ask_buyer",
             "10x the usual amount: probably a typo. The agent asks the buyer and orders nothing."),
}
NEXT = {
    "ordered": "Nothing to do. See it under Requests > All.",
    "needs_approval": "Open the dashboard > Requests and press Approve or Reject.",
    "ask_buyer": "Open the dashboard > Requests and answer the agent's question.",
}


def run(name):
    text, scenario, expected, what = SCENARIOS[name]
    print(f"\n{'=' * 78}\nSCENARIO: {name}\n{what}\nThe buyer types: \"{text}\"\n{'-' * 78}")
    rec = agent.handle(text, scenario=scenario)
    for s in rec["steps"]:
        print(f"  {s['step']:9} {s['message']}")
    ok = rec["status"] == expected
    print(f"{'-' * 78}\n  RESULT:   {rec['status'].upper()}" + (f"  (mock order {rec['order_id']})" if rec.get("order_id") else "")
          + f"   [{'as expected' if ok else 'NOT EXPECTED, wanted ' + expected}]")
    print(f"  IN SHORT: {rec['summary']['text']}")
    print(f"  NEXT:     {NEXT.get(rec['status'], '')}  ({rec['id']})")
    return ok


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"Unknown scenario: {', '.join(unknown)}. Choose from: {', '.join(SCENARIOS)}")
    results = [run(n) for n in names]
    print(f"\n{sum(results)} of {len(results)} scenarios gave the expected result.")
    sys.exit(0 if all(results) else 1)
