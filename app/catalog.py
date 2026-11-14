"""Product search over Valio Aimo's static product file.

The Aimo API has no search, so we search the product file ourselves and only then ask
the API for price and stock of the few matches (as the API docs ask us to).

The 87 MB file is read once and reduced to a small index (data/catalog_index.json)
holding only the fields the agent uses.
"""
import csv
import json
import re
from functools import lru_cache

from . import config

# Brand spellings in the data are not clean ("Arla ", "Arla Pro", "Valio maidot").
# We map them to a main brand. Anything not listed keeps its own (trimmed) name.
BRAND_FAMILIES = {"arla": "Arla", "valio": "Valio"}


def main_brand(raw):
    if not raw or not raw.strip():
        return None
    first = raw.strip().split()[0].lower()
    return BRAND_FAMILIES.get(first, raw.strip())


def _name(p, lang):
    for n in p.get("synkkaData", {}).get("names", []):
        if n["language"] == lang:
            return n["value"]
    return None


def _labels(p):
    out = set()
    for c in p.get("synkkaData", {}).get("classifications", []):
        if c["name"] in ("nutritionalClaim", "packagingMarkedLabel"):
            out.update(v["id"] for v in c["values"])
    return sorted(out)


_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(l|dl|cl|ml)\b", re.I)
_TO_L = {"l": 1, "dl": 0.1, "cl": 0.01, "ml": 0.001}


def _litres_per_sales_unit(p):
    """How many litres one sales unit holds, and where that number came from.

    Order of trust: 1) volume in the product data, 2) size written in the product name.
    Returns (litres, source) or (None, reason).
    """
    sizes = {u["unitId"]: u["sizeInBaseUnits"] for u in p.get("units", [])}
    in_sales_unit = sizes.get(p["salesUnit"], 1.0)  # base units per sales unit
    for u in p.get("synkkaData", {}).get("unitConversions") or []:
        vol = u.get("volume")
        if vol and vol.get("unit") == "LTR":
            if u["synkkaPackagingType"] == "BASE_UNIT_OR_EACH":
                return round(vol["value"] * in_sales_unit, 4), "product data (volume per piece)"
            if u["synkkaPackagingType"] == "CASE" and p["salesUnit"] != p["baseUnit"]:
                return round(vol["value"], 4), "product data (volume per case)"
    name = _name(p, "en") or _name(p, "fi") or ""
    m = _SIZE.findall(name)
    if m:
        value, unit = m[-1]
        per_piece = float(value.replace(",", ".")) * _TO_L[unit.lower()]
        return round(per_piece * in_sales_unit, 4), "product name"
    return None, "no size found"


def _slim(p):
    s = p.get("synkkaData", {})
    litres, litres_source = _litres_per_sales_unit(p)
    return {
        "id": p["id"],
        "name_en": _name(p, "en"),
        "name_fi": _name(p, "fi"),
        "brand_raw": s.get("brand"),
        "brand": main_brand(s.get("brand")),
        "vendor": p.get("vendorName"),
        "category": p.get("category"),
        "sales_unit": p["salesUnit"],
        "base_unit": p["baseUnit"],
        "units": {u["unitId"]: u["sizeInBaseUnits"] for u in p.get("units", [])},
        "allowed_lot_size": p.get("allowedLotSize"),
        "litres_per_sales_unit": litres,
        "litres_source": litres_source,
        "labels": _labels(p),
        "substitutes": [x["substituteProduct"] for x in p.get("substitutions") or []],
        "deposits": p.get("deposits") or [],
    }


def build_index():
    products = json.load(open(config.PRODUCT_FILE, encoding="utf-8"))
    reliability = {}
    with open(config.RELIABILITY_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            reliability[row["productId"]] = float(row["deliveryReliability"])
    index = {}
    for p in products:
        item = _slim(p)
        item["delivery_reliability"] = reliability.get(p["id"])  # None = not in the file
        index[p["id"]] = item
    config.DATA.mkdir(exist_ok=True)
    json.dump(index, open(config.CATALOG_INDEX, "w", encoding="utf-8"), ensure_ascii=False)
    return index


@lru_cache(maxsize=1)
def load():
    if config.CATALOG_INDEX.exists() and (
        not config.PRODUCT_FILE.exists()
        or config.CATALOG_INDEX.stat().st_mtime >= config.PRODUCT_FILE.stat().st_mtime
    ):
        return json.load(open(config.CATALOG_INDEX, encoding="utf-8"))
    return build_index()


def get(product_id):
    return load().get(product_id)


def search(words="", labels=(), category=None, brand=None, limit=50):
    """Find products whose English or Finnish name contains ALL the given words,
    and which carry ALL the given labels (e.g. FREE_FROM_LACTOSE)."""
    words = [w.lower() for w in words.split() if w.strip()]
    hits = []
    for p in load().values():
        text = f"{p['name_en'] or ''} {p['name_fi'] or ''}".lower()
        if words and not all(w in text for w in words):
            continue
        if labels and not set(labels) <= set(p["labels"]):
            continue
        if category and p["category"] != category:
            continue
        if brand and p["brand"] != brand:
            continue
        hits.append(p)
        if len(hits) >= limit:
            break
    return hits
