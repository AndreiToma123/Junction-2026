"""Demo stock overrides (simulated stock for demo scenario 2 only, see DECISIONS.md #4).

Replaces stock numbers in a quote from the Aimo API. Prices, dates and cut-offs stay real.
Every changed product is marked so the log and summary can say "demo stock override".
"""
import copy
import json

from . import config

OVERRIDES_FILE = config.DATA / "demo_stock_overrides.json"


def apply_stock_overrides(quote, scenario):
    if not scenario:
        return quote
    table = json.load(open(OVERRIDES_FILE, encoding="utf-8"))["scenarios"].get(scenario)
    if table is None:
        raise ValueError(f"Unknown demo scenario '{scenario}'")
    q = copy.deepcopy(quote)
    for pid, amount in table.items():
        p = q["products"].get(pid)
        if not p or not p["deliverable"]:
            continue
        p["real_stock_status"], p["real_stock_amount"] = p["stock_status"], p["stock_amount"]
        p["stock_status"], p["stock_amount"] = "known", float(amount)
        p["demo_stock_override"] = True
    q["demo_scenario"] = scenario
    return q
