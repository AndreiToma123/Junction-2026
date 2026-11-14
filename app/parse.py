"""Turn request text into fields: product, item_id, amount, unit, deadline.

This is the SIMPLE version (plain pattern matching, English and a little Finnish).
Gemini replaces it in build step 6; this one stays as the fallback if Gemini is down.
It never guesses: anything it can't read is left empty, and rule A2 then asks the buyer.
"""
import re
from datetime import date, timedelta

from . import rules

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "maanantai": 0, "tiistai": 1, "keskiviikko": 2, "torstai": 3, "perjantai": 4, "lauantai": 5, "sunnuntai": 6}
UNITS = {"l": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L", "litraa": "L", "kg": "KG"}

_AMOUNT = re.compile(r"(\d[\d\s,.]*)\s*(litres|liters|litre|liter|litraa|l|kg)\b", re.I)


def _number(s):
    s = s.strip().replace(" ", "")
    # "1,000" and "1.000" are thousands; "1,5" is a decimal
    if re.fullmatch(r"\d{1,3}([,.]\d{3})+", s):
        return float(re.sub(r"[,.]", "", s))
    return float(s.replace(",", "."))


MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
          "tammi": 1, "helmi": 2, "maalis": 3, "huhti": 4, "touko": 5, "kesä": 6, "heinä": 7, "elo": 8, "syys": 9, "loka": 10, "marras": 11, "joulu": 12}
_MON = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")[a-zä]*\.?"  # longest first: "marras" before "mar"


def _future(today, month, day):
    """The next date with this month and day (this year, or next year if it has passed)."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d.isoformat()
    return None


def _deadline(text, today):
    t = text.lower()
    # "Nov 16th", "November 16", "16 Nov", "16th of November", "16. marraskuuta"
    m = re.search(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t) or None
    if m:
        return _future(today, MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+(?:of\s+)?{_MON}", t)
    if m:
        return _future(today, MONTHS[m.group(2)], int(m.group(1)))
    # "16.11." or "16/11" (day first, as in Finland)
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})\.?(?!\d)", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return _future(today, int(m.group(2)), int(m.group(1)))
    if "tomorrow" in t or "huomenna" in t:
        return (today + timedelta(days=1)).isoformat()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}", t):
            days = (wd - today.weekday()) % 7 or 7  # next such day, never today
            return (today + timedelta(days=days)).isoformat()
    import difflib  # small typos: "Thrusday", "wensday", "firday"
    for w in re.findall(r"[a-zäö]{5,}", t):
        close = difflib.get_close_matches(w, list(WEEKDAYS), n=1, cutoff=0.75)
        if close:
            days = (WEEKDAYS[close[0]] - today.weekday()) % 7 or 7
            return (today + timedelta(days=days)).isoformat()
    return None


def _item(text):
    t = text.lower()
    for item in rules.usual_orders()["items"]:
        if any(a in t for a in item["aliases"] + [item["name"].lower()]):
            return item
    return None


AS_MUCH = re.compile(r"\b(as much|as many)\b.*\b(as possible|as (?:there is|is|you have|they have|can be)|available|you can|we can get)\b|\b(all|everything)\b.*\b(available|in stock)\b|\bmax(?:imum)?\b.*\b(available|possible)\b", re.I)


def parse(text, today=None):
    today = today or date.today()
    m = _AMOUNT.search(text)
    item = _item(text)
    product = item["name"] if item else None
    if not product:
        # take the words after the amount as the product, e.g. "50 kg oat flakes by Friday"
        after = text[m.end():] if m else ""
        words = re.split(r"\b(by|until|before|for|mennessä)\b", after, flags=re.I)[0]
        product = re.sub(r"^\s*(of)\s+", "", words.strip(" .,"), flags=re.I) or None
    as_much = bool(AS_MUCH.search(text)) and not m
    return {
        "product": product,
        "item_id": item["item_id"] if item else None,
        "amount": _number(m.group(1)) if m else None,
        "unit": UNITS[m.group(2).lower()] if m else (item["unit"] if as_much and item else None),
        "as_much_as_possible": as_much,
        "deadline": _deadline(text, today),
        "parsed_by": "simple pattern reader",
    }


# ---------------------------------------------------------------- several products in one request

_SPLIT = re.compile(r"\s*(?:,|;|\n|\+|&|\bplus\b|\band\b|\bja\b)\s*", re.I)
_CATEGORY_WORDS = {"dairy": "dairy", "milk products": "dairy", "maito": "dairy", "dry goods": "dry goods", "dry": "dry goods",
                   "vegetable": "fresh produce", "veg": "fresh produce", "produce": "fresh produce", "fish": "fish & meat",
                   "meat": "fish & meat"}


def split_items(text, today=None):
    """'Usual dairy order, plus 20 kg extra butter by Wednesday, 500 L milk, 60 kg salmon and some potatoes
    by Thursday' -> one entry per product, each with ITS date: a date applies to the products named before
    it (since the previous date). Products after the last date get that last date.
    Returns [] if the text is about one product only.
    'usual <category> order' expands to every usual item of that category with its usual amount.
    The same product named twice FOR THE SAME DATE is added up (usual butter + 20 kg extra butter)."""
    today = today or date.today()
    found, pending, last_date = [], [], None

    for seg in [x for x in _SPLIT.split(text or "") if x and x.strip()]:
        low = seg.lower()
        seg_date = _deadline(seg, today)
        new = []
        cat = next((c for w, c in _CATEGORY_WORDS.items() if w in low), None)
        if "usual" in low and cat:
            new = [{"item_id": i["item_id"], "product": i["name"], "amount": float(i["usual_amount"]), "unit": i["unit"],
                    "source": f"usual {cat} order"} for i in rules.usual_orders()["items"] if i.get("category") == cat]
        else:
            m = _AMOUNT.search(seg)
            item = _item(seg)
            if item:
                new = [{"item_id": item["item_id"], "product": item["name"], "amount": _number(m.group(1)) if m else None,
                        "unit": UNITS[m.group(2).lower()] if m else item["unit"], "source": seg.strip()}]
            elif m:
                after = seg[m.end():]
                words = re.split(r"\b(by|until|before|for|mennessä)\b", after, flags=re.I)[0]
                name = re.sub(r"^\s*(of|extra)\s+", "", words.strip(" .,"), flags=re.I) or None
                new = [{"item_id": None, "product": name, "amount": _number(m.group(1)), "unit": UNITS[m.group(2).lower()],
                        "source": seg.strip()}]
        pending += new
        if seg_date:
            for x in pending:
                x["deadline"] = seg_date
            found += pending
            pending, last_date = [], seg_date
    for x in pending:
        x["deadline"] = last_date
    found += pending

    items, order = {}, []
    for x in found:
        key = (x["item_id"] or (x["product"] or "").lower(), x["deadline"])
        if key in items and items[key]["amount"] is not None and x["amount"] is not None:
            items[key]["amount"] += x["amount"]
            items[key]["source"] += " + " + x["source"]
            continue
        items[key] = {**x, "as_much_as_possible": False, "parsed_by": "simple pattern reader (several products)"}
        order.append(key)
    out = [items[k] for k in order]
    return out if len(out) > 1 else []
