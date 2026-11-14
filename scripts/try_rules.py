"""Try the rule checks on hand-built options with LIVE Aimo data (read-only calls).

Run: .venv/bin/python -m scripts.try_rules
The agent will build these options by itself in the next step; here we only test the rules.
"""
from app import aimo, demo, rules

DEADLINE = "2026-11-16"
LABELS = ["FREE_FROM_LACTOSE"]
ICON = {"pass": "PASS    ", "approve": "APPROVAL", "ask": "ASK     ", "blocked": "BLOCKED "}


def show(title, req, lines=None, scenario=None):
    print(f"\n=== {title}")
    req = {"product": "lactose-free milk", "item_id": "lf-semi-skimmed-milk", "unit": "L", "deadline": DEADLINE, **req}
    req_results = rules.check_request(req)
    for r in req_results:
        print(f"  {ICON[r.result]} {r.rule} {r.detail}")
    if rules.outcome(req_results) != "auto_order" or not lines:
        print(f"  -> OUTCOME: {rules.outcome(req_results)} (no products looked up)")
        return
    quote = aimo.quote([l["product_id"] for l in lines], DEADLINE)
    quote = demo.apply_stock_overrides(quote, scenario)
    res = rules.check_option({"lines": lines}, quote, req, LABELS)
    for r in res["results"]:
        print(f"  {ICON[r['result']]} {r['rule']} {r['detail']}" + (f"  [warning: {r['warning']}]" if r.get("warning") else ""))
    print(f"  -> OUTCOME: {res['outcome']}   total €{res['total_with_tax']:,.2f}   {res['litres']} L")


if __name__ == "__main__":
    show("1 Normal order: 1,000 L usual Valio", {"amount": 1000}, [{"product_id": "464306", "quantity": 1000}])
    show("2 Split: 600 Valio + 400 Arla (demo stock)", {"amount": 1000},
         [{"product_id": "464306", "quantity": 600}, {"product_id": "469363", "quantity": 400}], scenario="split")
    show("3 Over spend limit: 2,000 L Valio", {"amount": 2000}, [{"product_id": "464306", "quantity": 2000}])
    show("4 Typo: 10,000 L", {"amount": 10000})
    print("\n--- extra problem cases")
    show("5 Supplier product, cut-off passed (465039, 200 cases of 5 L)", {"amount": 1000}, [{"product_id": "465039", "quantity": 200}])
    show("6 Real stock too low: 1,000 L Arla 469363 (384 in stock)", {"amount": 1000}, [{"product_id": "469363", "quantity": 1000}])
    show("7 Rounding: 2.5 dl x 15 cases (3.75 L) -> 267 cases", {"amount": 1000},
         [{"product_id": "463779", "quantity": rules.units_for("463779", 1000)}])
    show("8 Missing deadline", {"amount": 1000, "deadline": None})
    show("9 Not a usual item", {"amount": 50, "item_id": None, "product": "oat drink"})
