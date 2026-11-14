"""Budgets ("wallets"): where an order's money comes from, charged by PURPOSE, not by product.

  weekly:<category>  routine buying (typed requests and automatic restock), per category and ISO week
                     (dairy, dry goods, fresh produce, fish & meat; limits in the kitchen file)
  event:<EV-id>      everything ordered because of that event, even if it is the same milk as usual

A budget is information for the person approving, not a blocking rule: the agent shows what is left
before and after the order. (Making it a rule is one rule type away.)
Cancelled orders don't count.
"""
from datetime import date, datetime

from . import kitchen, orders


def wallet_for(rec):
    if rec.get("event_id"):
        return f"event:{rec['event_id']}"
    item = kitchen.ingredient((rec.get("parsed") or {}).get("item_id") or "")
    return f"weekly:{item['category']}" if item else "weekly:other"


def _week(iso):
    d = datetime.fromisoformat(iso).date() if "T" in iso else date.fromisoformat(iso)
    return d.isocalendar()[:2]


def limits():
    return kitchen.load().get("budgets", {}).get("weekly", {})


def status(wallet, extra=0.0, today=None):
    """{"wallet", "label", "limit", "spent", "left", "after"} for this week (or the whole event)."""
    today = today or date.today()
    kind, key = wallet.split(":", 1)
    if kind == "event":
        from . import events
        ev = events.get(key) or {}
        limit, label = ev.get("budget"), f"Event {key}" + (f" ({ev.get('name')})" if ev.get("name") else "")
        mine = [o for o in orders.list_orders() if o.get("wallet") == wallet]
    else:
        limit, label = limits().get(key), f"{key.capitalize()}, this week"
        wk = today.isocalendar()[:2]
        mine = [o for o in orders.list_orders() if o.get("wallet") == wallet and _week(o["created_at"]) == wk]
    spent = round(sum(o["total_with_tax"] for o in mine if not str(o.get("status", "")).startswith("cancelled")), 2)
    out = {"wallet": wallet, "label": label, "limit": limit, "spent": spent, "this_order": round(extra, 2)}
    if limit is not None:
        out["left"] = round(limit - spent, 2)
        out["after"] = round(limit - spent - extra, 2)
    return out


def overview(today=None):
    return [status(f"weekly:{c}", today=today) for c in limits()]
