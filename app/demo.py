"""Demo overrides: simulated stock (split scenario) and a simulated price change at approval.

Replaces stock numbers in a quote from the Aimo API. Prices, dates and cut-offs stay real.
Every changed product is marked so the log and summary can say "demo stock override".
"""
import copy
import json

from . import config

OVERRIDES_FILE = config.DATA / "demo_stock_overrides.json"


def _table():
    try:
        return json.load(open(OVERRIDES_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def delivery_window(product_id):
    """DEMO delivery time for a few products (the real API only gives a day)."""
    v = (_table().get("delivery_windows") or {}).get(product_id)
    return v if isinstance(v, str) and product_id != "_what" else None


def restock_for(product_id):
    """DEMO restock date for a few products (the real API gives none)."""
    r = (_table().get("restock") or {}).get(product_id)
    return r if isinstance(r, dict) else None


def apply_restock(quote):
    """For deliveries on or after a DEMO restock date, add the restocked amount to the stock."""
    q = quote
    for pid, r in (_table().get("restock") or {}).items():
        if pid == "_what":
            continue
        p = q["products"].get(pid)
        if p and p.get("deliverable") and p["stock_status"] == "known" and quote["requested_date"] >= r["date"]:
            if q is quote:
                q = copy.deepcopy(quote)
                p = q["products"][pid]
            p["stock_amount"] = float(p["stock_amount"]) + float(r["amount"])
            p["demo_restock"] = True
    return q


def apply_stock_overrides(quote, scenario, phase="quote"):
    """phase: "quote" (first look-up) or "approval" (the re-check when a person approves).
    Demo restocks (a few products, dated) are applied first, in every scenario."""
    quote = apply_restock(quote)
    if not scenario:
        return quote
    table = json.load(open(OVERRIDES_FILE, encoding="utf-8"))["scenarios"].get(scenario)
    if table is None:
        raise ValueError(f"Unknown demo scenario '{scenario}'")
    q = copy.deepcopy(quote)
    for pid, amount in (table.get("stock") or {}).items():
        p = q["products"].get(pid)
        if not p or not p["deliverable"]:
            continue
        p["real_stock_status"], p["real_stock_amount"] = p["stock_status"], p["stock_amount"]
        p["stock_status"], p["stock_amount"] = "known", float(amount)
        p["demo_stock_override"] = True
    if phase == "approval":
        for pid, price in (table.get("price_at_approval") or {}).items():
            p = q["products"].get(pid)
            if not p or not p["priced"]:
                continue
            ratio = price / p["price_per_unit_with_tax"]
            p["real_price_per_unit_with_tax"] = p["price_per_unit_with_tax"]
            p["price_per_unit_with_tax"] = float(price)
            p["price_per_unit_without_tax"] = round(p["price_per_unit_without_tax"] * ratio, 2)
            p["demo_price_override"] = True
    q["demo_scenario"] = scenario
    return q
