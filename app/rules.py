"""Rule engine. Plain code, no AI. Runs every rule in rules.yaml using its type in rule_types.py.

Each check gives a RuleResult:
  rule    e.g. "C1"            name  e.g. "Spend limit"
  result  pass | approve | ask | blocked
  detail  one plain sentence built only from data and rule settings
  facts   the exact numbers used (so the log can prove the sentence)
  warning optional note, e.g. "stock not confirmed by supplier"
The worst result decides the outcome: blocked > ask > approve > pass.
"""
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date

import yaml

from . import catalog, config
from .rule_types import ID_PREFIX, NUM, TYPES

RULES_FILE = config.ROOT / "rules.yaml"
USUAL_FILE = config.DATA / "usual_orders.json"
SEVERITY = {"pass": 0, "approve": 1, "ask": 2, "blocked": 3}
VALID_RESULTS = {"approve", "ask", "blocked", "pass"}


@dataclass
class RuleResult:
    rule: str
    name: str
    result: str
    detail: str
    facts: dict = field(default_factory=dict)
    warning: str = None

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


_cache = {}


def rules():
    """The rulebook, re-read automatically when rules.yaml changes on disk."""
    mtime = RULES_FILE.stat().st_mtime
    if _cache.get("mtime") != mtime:
        _cache.update(mtime=mtime, doc=yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")))
    return _cache["doc"]


def active(scope):
    return [r for r in rules()["rules"] if TYPES[r["type"]]["scope"] == scope]


def usual_orders():
    return json.load(open(USUAL_FILE, encoding="utf-8"))


def find_usual_item(item_id):
    return next((i for i in usual_orders()["items"] if i["item_id"] == item_id), None)


def eur(x):
    return f"€{x:,.2f}"


num = NUM


def nice_date(iso):
    d = date.fromisoformat(iso[:10])
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def nice_time(iso):
    return f"{nice_date(iso)} {iso[11:16]}"


def describe(rule):
    return TYPES[rule["type"]]["describe"](rule["settings"], rule["result"])


def _result(rule, out):
    if out is None:
        return None
    res, detail, facts = out[0], out[1], out[2]
    return RuleResult(rule["id"], rule["name"], res, detail, facts, out[3] if len(out) > 3 else None)


# ---------------------------------------------------------------- running the checks

def check_request(req):
    """Rules about the request itself (scope 'request'), before any search."""
    ctx = {"usual_item": find_usual_item(req.get("item_id")) if req.get("item_id") else None}
    out = []
    for rule in active("request"):
        r = _result(rule, TYPES[rule["type"]]["check"](rule, req, ctx))
        if r:
            out.append(r)
            if r.rule == "A2" and r.result != "pass":
                break  # can't check the size of an unclear request
    return out


def check_option(option, quote, req, required_labels):
    """Product rules for every line, then whole-order rules.
    Returns {"results", "outcome", "total_with_tax", "litres", "warnings"}."""
    ctx = {"required_labels": required_labels}
    results = []
    for line in option["lines"]:
        q = quote["products"].get(line["product_id"])
        p = catalog.get(line["product_id"])
        for rule in active("line"):
            r = _result(rule, TYPES[rule["type"]]["check"](rule, line, q, p, req, ctx))
            if r:
                results.append(r)
    for rule in active("order"):
        r = _result(rule, TYPES[rule["type"]]["check"](rule, option, quote, req, ctx))
        if r:
            results.append(r)
    total = 0.0
    for line in option["lines"]:
        q = quote["products"].get(line["product_id"])
        if q and q["priced"]:
            total += line["quantity"] * q["price_per_unit_with_tax"]
    litres = sum(l["quantity"] * (catalog.get(l["product_id"])["litres_per_sales_unit"] or 0) for l in option["lines"])
    return {"results": [r.to_dict() for r in results], "outcome": outcome(results),
            "total_with_tax": round(total, 2), "litres": round(litres, 3),
            "warnings": sorted({r.warning for r in results if r.warning})}


def outcome(results):
    worst = max((SEVERITY[r.result if isinstance(r, RuleResult) else r["result"]] for r in results), default=0)
    return {0: "auto_order", 1: "needs_approval", 2: "ask_buyer", 3: "blocked"}[worst]


def units_for(product_id, litres):
    """Smallest whole number of sales units covering `litres` (rounded up; whole packs only)."""
    p = catalog.get(product_id)
    per, step = p["litres_per_sales_unit"], p["allowed_lot_size"] or 1
    if not per:
        return None
    return float(max(math.ceil(litres / per - 1e-9), step))


# ---------------------------------------------------------------- validating a rulebook

def validate(doc):
    """Check a whole rulebook before it is saved. Returns a list of plain-language problems (empty = OK)."""
    problems = []
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return ["The file must have a 'rules:' list."]
    seen = set()
    for i, r in enumerate(doc["rules"], 1):
        where = f"Rule {r.get('id', '#' + str(i))}"
        if not isinstance(r, dict):
            problems.append(f"Rule #{i} is not a rule.")
            continue
        t = TYPES.get(r.get("type"))
        if not t:
            problems.append(f"{where}: unknown type '{r.get('type')}'. Known types: {', '.join(TYPES)}.")
            continue
        if not r.get("id") or not re.fullmatch(r"[A-Z]\d{1,3}", str(r["id"])):
            problems.append(f"{where}: id must look like A1, B6, C4.")
        elif r["id"] in seen:
            problems.append(f"{where}: id used twice.")
        seen.add(r.get("id"))
        if not r.get("name"):
            problems.append(f"{where}: needs a name.")
        if r.get("result") not in t["results"]:
            problems.append(f"{where}: result must be one of {', '.join(t['results'])}.")
        s = r.get("settings") or {}
        r["settings"] = s
        for key in s:
            if key not in t["settings"]:
                problems.append(f"{where}: unknown setting '{key}'.")
        for key, spec in t["settings"].items():
            if key not in s:
                problems.append(f"{where}: missing setting '{key}'.")
                continue
            problems += [f"{where}, {key}: {m}" for m in _check_value(s[key], spec)]
    for tname, t in TYPES.items():
        if t["locked"] and not any(r.get("type") == tname for r in doc["rules"] if isinstance(r, dict)):
            problems.append(f"Safety rule '{t['title']}' is locked and can't be removed.")
    return problems


def _check_value(v, spec):
    k = spec["kind"]
    if k == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return ["must be a number."]
        if spec.get("integer") and not float(v).is_integer():
            return ["must be a whole number."]
        if v < spec["min"] or v > spec["max"]:
            return [f"must be between {NUM(spec['min'])} and {NUM(spec['max'])}."]
    elif k == "bool":
        if not isinstance(v, bool):
            return ["must be true or false."]
    elif k == "choice":
        if v not in spec["options"]:
            return [f"must be one of {', '.join(spec['options'])}."]
    elif k == "list":
        if not isinstance(v, list) or len(v) < spec.get("min_items", 0):
            return [f"must be a list with at least {spec.get('min_items', 0)} item(s)."]
        if spec.get("options") and any(x not in spec["options"] for x in v):
            return [f"items must be from {', '.join(spec['options'])}."]
        if spec.get("product_codes"):
            bad = [x for x in v if not catalog.get(str(x))]
            if bad:
                return [f"unknown product code(s): {', '.join(map(str, bad))}."]
        if spec.get("labels"):
            known = {l for p in catalog.load().values() for l in p["labels"]}
            bad = [x for x in v if x not in known]
            if bad:
                return [f"unknown label(s): {', '.join(bad)}."]
    return []


def next_id(doc, type_name):
    prefix = ID_PREFIX[TYPES[type_name]["scope"]]
    used = [int(r["id"][1:]) for r in doc["rules"] if str(r.get("id", "")).startswith(prefix)]
    return f"{prefix}{max(used, default=0) + 1}"
