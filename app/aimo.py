"""Client for the Valio Aimo mock API (read-only calls only: ping, availability, price).

It also turns the raw answers into plain facts, e.g. the special stock numbers:
  999999        on a Terminal product  -> stock unknown (documented)
  >= 666666666  on a Stock product     -> no stock limit (not documented, our reading)
"""
from datetime import datetime

import httpx

from . import config

TERMINAL_UNKNOWN = 999999
NO_LIMIT_FROM = 666666666
MAX_PER_CALL = 1000  # API limit per request


class AimoError(Exception):
    pass


def _client():
    if not config.AIMO_API_KEY:
        raise AimoError("AIMO_API_KEY is missing from .env")
    return httpx.Client(
        base_url=config.AIMO_BASE_URL,
        headers={"X-API-Key": config.AIMO_API_KEY, "User-Agent": config.USER_AGENT},
        timeout=20,
    )


def ping():
    with _client() as c:
        r = c.get("/api/ping")
        r.raise_for_status()
        return r.json()


def _post(path, product_ids, date):
    ids = list(dict.fromkeys(product_ids))  # unique, keep order
    if not ids:
        return []
    if len(ids) > MAX_PER_CALL:
        raise AimoError(f"Too many products in one call ({len(ids)} > {MAX_PER_CALL})")
    body = {"customer": config.CUSTOMER_ID, "requestedDate": date, "products": ids}
    with _client() as c:
        r = c.post(path, json=body)
        if r.status_code != 200:
            raise AimoError(f"{path} returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


def availability_raw(product_ids, date):
    return _post("/api/availability", product_ids, date)


def price_raw(product_ids, date):
    return _post("/api/price", product_ids, date)


def _stock_fact(model, amount):
    if amount is None:
        return "none", None
    if model == "Terminal" and amount == TERMINAL_UNKNOWN:
        return "unknown", None
    if amount >= NO_LIMIT_FROM:
        return "no_limit", None
    return "known", amount


def quote(product_ids, date):
    """Price + availability for a few products on one delivery date, as plain facts.

    Returns {"checked_at", "requested_date", "products": {id: {...}}}.
    Every value comes straight from the API; nothing is guessed.
    """
    av = availability_raw(product_ids, date)
    pr = price_raw(product_ids, date)
    now = datetime.fromisoformat(av["timeNow"])
    prices = {p["productId"]: p for p in pr.get("products", [])}
    out = {}
    for a in av.get("products", []):
        pid = a["productId"]
        line = a["scheduleLines"][0] if a.get("scheduleLines") else None
        stock_status, stock_amount = _stock_fact(
            a.get("logisticModel"), line["availableAmount"] if line else None
        )
        closing = a.get("closingTime")
        p = prices.get(pid) or {}
        priced = bool(p) and not p.get("error")
        out[pid] = {
            "product_id": pid,
            "deliverable": line is not None,
            "sales_block": bool(a.get("salesBlock") or (line and line.get("salesBlock"))),
            "logistic_model": a.get("logisticModel"),
            "available_date": line["availableDate"] if line else None,
            "sales_unit": line["salesUnit"] if line else None,
            "stock_status": stock_status,   # known | no_limit | unknown | none
            "stock_amount": stock_amount,   # only when stock_status == known
            "raw_available_amount": line["availableAmount"] if line else None,
            "closing_time": closing,
            "cutoff_passed": bool(closing) and datetime.fromisoformat(closing) < now,
            "priced": priced,
            "price_per_unit_with_tax": p["catalogPrice"]["valueWithTax"] if priced else None,
            "price_per_unit_without_tax": p["catalogPrice"]["value"] if priced else None,
            "tax_rate": p["tax"]["taxRate"] if priced else None,
            "comparison_price_with_tax": p["comparisonPrice"]["valueWithTax"] if priced and p.get("comparisonPrice") else None,
            "comparison_unit": p.get("comparisonUnit") if priced else None,
            "list_price_with_tax": p["listPrice"]["valueWithTax"] if priced and p.get("listPrice") else None,
            "currency": pr.get("currency"),
            "price_valid_to": p["catalogPrice"]["validTo"] if priced else None,
        }
    return {"checked_at": av["timeNow"], "requested_date": date, "products": out}
