"""Mock order service, for the demo only.

The Valio Aimo API has no ordering. Orders here are saved to data/orders.jsonl
(one JSON object per line) and get an order number like MOCK-0001.
Nothing is ever sent to Valio Aimo.

Before saving, the order is checked against the product data (product exists,
amount is a whole multiple of the allowed order step). It does NOT check rules,
prices or stock: that is the agent's job before it calls this.
"""
import json
import threading
from datetime import datetime, timezone

from . import catalog, config

_lock = threading.Lock()


class OrderError(Exception):
    pass


def _all():
    if not config.ORDERS_FILE.exists():
        return []
    with open(config.ORDERS_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def list_orders():
    return _all()


def get_order(order_id):
    return next((o for o in _all() if o["order_id"] == order_id), None)


def place_order(lines, delivery_date, placed_by, request_id=None, note=None):
    """lines: [{"product_id", "quantity" (in sales units), "price_per_unit_with_tax"}]
    placed_by: "agent" (inside the rules) or "human:<name>" (after approval)."""
    if not lines:
        raise OrderError("An order needs at least one line")
    checked = []
    for ln in lines:
        p = catalog.get(ln["product_id"])
        if not p:
            raise OrderError(f"Unknown product {ln['product_id']}")
        qty = float(ln["quantity"])
        step = p["allowed_lot_size"] or 1
        if qty <= 0 or abs(round(qty / step) * step - qty) > 1e-9:
            raise OrderError(f"Quantity {qty} for {p['id']} is not a multiple of {step}")
        price = ln.get("price_per_unit_with_tax")
        checked.append({
            "product_id": p["id"],
            "name": p["name_en"] or p["name_fi"],
            "brand": p["brand"],
            "quantity": qty,
            "sales_unit": p["sales_unit"],
            "price_per_unit_with_tax": price,
            "line_total_with_tax": round(qty * price, 2) if price is not None else None,
        })
    with _lock:
        n = len(_all()) + 1
        order = {
            "order_id": f"MOCK-{n:04d}",
            "status": "confirmed (mock)",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "delivery_date": delivery_date,
            "placed_by": placed_by,
            "request_id": request_id,
            "lines": checked,
            "total_with_tax": round(sum(l["line_total_with_tax"] or 0 for l in checked), 2),
            "currency": "EUR",
            "note": note,
            "mock": True,
        }
        config.STATE.mkdir(parents=True, exist_ok=True)
        with open(config.ORDERS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(order, ensure_ascii=False) + "\n")
    return order
