"""The four AI jobs. Gemini is used ONLY for language, never for deciding what is allowed.

  1. parse_request   request text        -> fields (product, amount, unit, deadline)
  2. summarize       rule-check results  -> short summary for the approver
  3. ask_log         question + log      -> answer that cites exact log lines
  4. propose_rule    plain-language wish -> a proposed rule change (a person must apply it)

Every job works the same way:
  try Gemini -> plain code CHECKS the answer -> if no key, an error, or the check fails:
  use the plain-code fallback. If the AI (or the fallback) is unsure, it ASKS instead of guessing.
Each result says who produced it ("Gemini" or "plain code").
"""
import difflib
import json
import re
from datetime import date, timedelta

from . import catalog, llm, log, parse, rule_edit, rules
from .rule_types import TYPES

import os

# "when_needed" (default): Gemini reads a request only if the simple reader could not read all of it.
# "always": Gemini reads every request. Costs one call per request.
PARSE_MODE = os.getenv("GEMINI_PARSE", "when_needed")
AI = "Gemini"
CODE = "plain code (no AI)"


def status():
    return {"gemini": llm.available(), "model": llm.MODEL if llm.available() else None, "usage": llm.usage(),
            "mode": "Gemini is connected" if llm.available() else "Simple mode: no Gemini key yet, plain-code fallbacks are used"}


def _numbers(text):
    """All numbers in a text, normalised ('1,000.50' -> '1000.5'), for checking the AI didn't invent any."""
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", str(text)):
        try:
            out.add(str(float(m.replace(",", ""))))
        except ValueError:
            pass
    return out


# =============================================================== 1. read the request

def parse_request(text, today=None):
    today = today or date.today()
    simple = parse.parse(text, today)
    if not llm.available():
        simple["parsed_by"] = "simple pattern reader (no Gemini key yet)"
        return simple
    complete = (all(simple.get(k) not in (None, "") for k in ("product", "unit", "deadline")) and simple.get("item_id")
                and (simple.get("amount") is not None or simple.get("as_much_as_possible")))
    if complete and PARSE_MODE != "always":
        simple["parsed_by"] = "simple pattern reader (clear request, no AI needed)"
        return simple
    items = [{"item_id": i["item_id"], "name": i["name"], "aliases": i["aliases"]} for i in rules.usual_orders()["items"]]
    system = ("You read purchase requests for a professional kitchen and return JSON with exactly these keys: "
              "product (string or null), item_id (one of the given item ids, or null), amount (number or null), "
              "unit ('L', 'KG' or null), deadline ('YYYY-MM-DD' or null), as_much_as_possible (true if the buyer asks for "
              "as much as possible / everything available instead of an amount, else false). "
              "Ignore repeated words and small typos (e.g. 'but Nov 16th' means 'by Nov 16th'). "
              "Use null for anything the text does not state. Never guess an amount or a date.")
    user = json.dumps({"today": today.isoformat(), "weekday": today.strftime("%A"),
                       "kitchen_usual_items": items, "request_text": text}, ensure_ascii=False)
    try:
        g = llm.generate_json(system, user)
    except llm.LLMError as e:
        simple["parsed_by"] = f"simple pattern reader (Gemini failed: {e})"
        return simple
    out = _check_parsed(g, text, today)
    if out is None:
        simple["parsed_by"] = "simple pattern reader (Gemini's answer failed the checks)"
        return simple
    return out


def _check_parsed(g, text, today):
    """Plain-code checks on Gemini's reading. Returns cleaned fields, or None to reject it."""
    amount, unit, deadline, item_id = g.get("amount"), g.get("unit"), g.get("deadline"), g.get("item_id")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0):
        return None
    if unit not in (None, "L", "KG"):
        return None
    if deadline is not None:
        try:
            d = date.fromisoformat(str(deadline))
        except ValueError:
            return None
        if d < today or d > today + timedelta(days=90):
            return None
    if item_id is not None and not rules.find_usual_item(item_id):
        item_id = None
    product = g.get("product") if isinstance(g.get("product"), str) and g.get("product").strip() else None
    if item_id:
        product = rules.find_usual_item(item_id)["name"]
    as_much = g.get("as_much_as_possible") is True and amount is None
    if as_much and item_id and not unit:
        unit = rules.find_usual_item(item_id)["unit"]
    out = {"product": product, "item_id": item_id, "amount": float(amount) if amount is not None else None,
           "unit": unit, "deadline": deadline, "as_much_as_possible": as_much, "parsed_by": AI}
    # The amount must be written in the text as digits. If Gemini worked it out from words
    # ("a thousand litres"), we keep it but the agent asks the buyer to confirm first.
    if amount is not None:
        digits = re.sub(r"[^\d]", "", re.sub(r"(\d)[ ,.](\d{3})\b", r"\1\2", text))
        if str(int(amount)) not in digits:
            out["unverified"] = True
            out["parsed_by"] = AI + " (amount not written as a number, needs confirmation)"
    return out


# =============================================================== 2. summary for the approver

def summarize(rec):
    """One or two sentences saying what is decided and why. Built only from rule results."""
    facts = _summary_facts(rec)
    fallback = {"text": _template_summary(rec, facts), "written_by": CODE}
    if not llm.available() or rec.get("status") not in ("needs_approval", "cannot_fulfil"):
        return fallback  # simple outcomes don't need AI wording
    system = ("Write a summary of at most 2 short sentences for a kitchen manager who must approve or reject a purchase. "
              "Sentence 1: what the order is, from 'situation' (e.g. that it is a split and why). Sentence 2: the exact reason "
              "a person must decide, from 'rules_not_passed'. Use ONLY the facts in the JSON. "
              "Do not list products or repeat the order lines. Do not add numbers that are not in the JSON. "
              "'delivery_date' is the day the goods will be delivered (not a deadline). "
              "Plain words, no jargon. Return JSON: {\"summary\": \"...\"}")
    try:
        g = llm.generate_json(system, json.dumps(facts, ensure_ascii=False))
    except llm.LLMError:
        return fallback
    text = g.get("summary")
    if not isinstance(text, str) or not (10 <= len(text) <= 420):
        return fallback
    if not _numbers(text) <= _numbers(json.dumps(facts, ensure_ascii=False)):
        return fallback  # it used a number that is not in the facts
    return {"text": text.strip(), "written_by": AI}


def _summary_facts(rec):
    o = next((x for x in rec.get("options") or [] if x["option_id"] == rec.get("chosen")), None)
    results = o["results"] if o else rec.get("request_checks", [])
    return {
        "status": rec.get("status"), "request": rec.get("text"), "situation": rec.get("situation"),
        "total_with_tax_eur": o["total_with_tax"] if o else None,
        "kind": o["kind"] if o else None,
        "delivery_date": o["lines"][0].get("available_date") if o else None,
        "rules_not_passed": [{"rule": r["rule"], "name": r["name"], "result": r["result"], "detail": r["detail"]}
                             for r in results if r["result"] != "pass"],
        "rules_passed_count": sum(1 for r in results if r["result"] == "pass"),
        "why_chosen": rec.get("why_chosen"), "reason": rec.get("reason"), "question": rec.get("question"),
        "order_id": rec.get("order_id"), "decided_by": rec.get("decided_by"),
        "warnings": o.get("warnings") if o else None,
    }


def _template_summary(rec, f):
    st, n = f["status"], f["rules_passed_count"]
    bad = " ".join(r["detail"] for r in f["rules_not_passed"])
    if st == "needs_approval":
        return (f"{f['situation']} " if f.get("situation") else "") + f"A person must decide because: {bad}"
    if st == "ask_buyer":
        return f"Nothing was ordered. The agent asked the buyer: {f['question']}"
    if st == "ordered" and f["decided_by"]:
        return f"Ordered as {f['order_id']} after {f['decided_by']} approved it. Approved exception: {bad}"
    if st == "ordered":
        return f"Ordered automatically as {f['order_id']}: all {n} checks passed."
    if st == "rejected":
        return f"Rejected by {f['decided_by']}. Nothing was ordered."
    return f"Nothing was ordered. {f['reason'] or ''}".strip()


# =============================================================== 3. ask the log

IMPORTANT_STEPS = {"choose", "order", "decision", "split", "rule_change", "ask", "answer", "approval", "result"}
STOP = set("a an and are as at be been but by did do does for from has have how i in is it its me my of on or our "
           "past last that the their there these this to was we were what when which who why will with you your "
           "any some all not than then so if about week weeks day days month months".split())


def _window(question, today):
    q = question.lower()
    m = re.search(r"(?:past|last|previous)\s+(\d+)?\s*(day|week|month)s?", q)
    if m:
        n = int(m.group(1) or 1)
        days = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return (today - timedelta(days=days)).isoformat(), f"the last {n} {m.group(2)}{'s' if n != 1 else ''}"
    if "today" in q:
        return today.isoformat(), "today"
    if "yesterday" in q:
        return (today - timedelta(days=1)).isoformat(), "since yesterday"
    if "this week" in q:
        return (today - timedelta(days=today.weekday())).isoformat(), "this week"
    return None, "the whole log"


def _item_in(question):
    q = question.lower()
    for item in rules.usual_orders()["items"]:
        words = item["aliases"] + [item["name"].lower()] + [w for w in item["name"].lower().split() if len(w) > 3 and w not in ("free",)]
        if any(w in q for w in words):
            return item
    return None


def _order_facts(lines, item):
    """Plain code: for each request, which products were chosen, and do they differ from the usual one?"""
    usual, facts, by_req = item["usual_product"], [], {}
    for e in lines:
        if e["step"] == "choose":
            by_req[e["request_id"]] = e
    ordered = {e["request_id"] for e in lines if e["step"] == "order"}
    for rid, e in by_req.items():
        if rid not in ordered:
            continue  # only things that were really ordered
        prods = re.findall(r"(\d+) × (\d{6}) \(([^)]*)\)", e["message"])
        if not prods:
            continue
        other = [(q, pid, brand) for q, pid, brand in prods if pid != usual]
        parts = []
        for q, pid, brand in other:
            p = catalog.get(pid) or {}
            usual_brand = (catalog.get(usual) or {}).get("brand")
            kind = "a different brand" if brand != usual_brand else "same brand, different product"
            parts.append(f"{int(q):,} of it was {p.get('name_en') or pid} ({pid}), {kind}")
        facts.append({"request_id": rid, "line_id": e["id"], "time": e["time"], "different": bool(other),
                      "text": "; ".join(parts) if other else "the usual product"})
    return facts


def ask_log(question, today=None):
    today = today or date.today()
    question = (question or "").strip()
    if not question:
        return {"answered_by": CODE, "answer": "Type a question first.", "points": [], "lines": [], "scope": "", "cannot_tell": None}
    since, scope = _window(question, today)
    item = _item_in(question)
    everything = [e for e in log.read() if not since or e["time"][:10] >= since]
    cands = [e for e in everything if e["step"] in IMPORTANT_STEPS or (e["step"] == "rules" and ": pass." not in e["message"])]
    cands = cands[-60:]  # keeps the question small for the free tier
    by_id = {e["id"]: e for e in cands}
    facts = _order_facts(everything, item) if item else []
    scope_text = f"Looked at {scope}: {len(cands)} important log lines" + (f", item: {item['name']}" if item else "") + "."

    def point(line_id, text):
        e = by_id[line_id]
        return {"line_id": line_id, "time": e["time"], "request_id": e["request_id"], "text": text}

    def lines_for(points):
        ids = [p["line_id"] for p in points]
        reqs = {p["request_id"] for p in points}
        return [e for e in cands if e["id"] in ids or (e["request_id"] in reqs and e["step"] in ("choose", "order", "decision", "split"))]

    # ---- Gemini, with every cited line checked
    if llm.available() and cands:
        system = ("You answer questions about a kitchen's purchase audit log. Use ONLY the log lines and the "
                  "'order_facts' given. Every point you make must cite the id of one log line that proves it. "
                  "If the log cannot answer the question, say so. The log shows what was ordered and why; it can "
                  "never show taste, quality or how food was stored or cooked: say that in 'cannot_tell' when relevant. "
                  "Return JSON: {\"answer\": one or two sentences, \"points\": [{\"line_id\": \"L-000001\", \"text\": \"...\"}], "
                  "\"cannot_tell\": string or null}")
        user = json.dumps({"question": question, "today": today.isoformat(),
                           "usual_item": {"name": item["name"], "usual_product": item["usual_product"]} if item else None,
                           "order_facts": facts,
                           "log_lines": [{"id": e["id"], "time": e["time"], "request": e["request_id"], "who": e["actor"],
                                          "step": e["step"], "message": e["message"]} for e in cands]}, ensure_ascii=False)
        try:
            g = llm.generate_json(system, user)
            pts = [point(p["line_id"], str(p.get("text", ""))[:300]) for p in (g.get("points") or [])
                   if isinstance(p, dict) and p.get("line_id") in by_id]
            invented = len(g.get("points") or []) - len(pts)
            if isinstance(g.get("answer"), str) and g["answer"].strip() and not invented:
                return {"answered_by": AI, "answer": g["answer"].strip()[:600], "points": pts, "lines": lines_for(pts),
                        "scope": scope_text, "cannot_tell": g.get("cannot_tell") if isinstance(g.get("cannot_tell"), str) else None}
        except (llm.LLMError, KeyError, TypeError):
            pass  # fall through to plain code

    # ---- plain code: order differences for a known item
    cannot = "The log only shows what was ordered and why. It cannot tell taste, quality or storage."
    if item and facts:
        diff = [f for f in facts if f["different"] and f["line_id"] in by_id]
        usual_name = (catalog.get(item["usual_product"]) or {}).get("name_en", item["usual_product"])
        if diff:
            pts = [point(f["line_id"], f["text"][0].upper() + f["text"][1:] + ".") for f in diff]
            ans = (f"Your usual {item['name'].lower()} is {usual_name} ({item['usual_product']}). "
                   f"In {scope}, {len(diff)} of {len(facts)} orders were different:")
        else:
            pts = []
            ans = (f"Your usual {item['name'].lower()} is {usual_name} ({item['usual_product']}). "
                   f"In {scope}, all {len(facts)} orders used the usual product.")
        return {"answered_by": CODE, "answer": ans, "points": pts, "lines": lines_for(pts) if pts else [],
                "scope": scope_text, "cannot_tell": cannot}

    # ---- plain code: word search
    words = [w for w in re.findall(r"[\w€-]+", question.lower()) if w not in STOP and len(w) > 2]
    hits = [e for e in everything if any(w in json.dumps(e, ensure_ascii=False).lower() for w in words)] if words else []
    hits = [e for e in hits if e["step"] in IMPORTANT_STEPS or e["step"] == "rules"][-60:]
    return {"answered_by": CODE, "points": [], "lines": hits, "scope": scope_text, "cannot_tell": None,
            "answer": (f"I can't answer that in a sentence without the AI, but {len(hits)} log lines mention "
                       f"{', '.join(words[:5])}." if hits else "No log lines match that question. Try other words, a product, a brand or a request number.")}


# =============================================================== 4. propose a rule change

def propose_rule(text):
    """Turn a plain-language wish into a rule change for PREVIEW. Never applies anything.
    Returns {"status": "proposal", change, preview, explanation} or {"status": "question", question}."""
    text = (text or "").strip()
    if not text:
        return {"status": "question", "proposed_by": CODE, "question": "What would you like to change?"}
    doc = rules.rules()
    if llm.available():
        system = ("You turn a kitchen manager's wish into ONE change to a purchasing rulebook. "
                  "Return JSON, either {\"status\": \"proposal\", \"change\": {...}, \"explanation\": \"one sentence\"} "
                  "or {\"status\": \"question\", \"question\": \"...\"}. "
                  "A change is {\"action\": \"edit\", \"rule_id\", \"settings\": {only changed keys}, optional \"result\"} or "
                  "{\"action\": \"add\", \"type\", \"name\", \"settings\": {all keys}, \"result\"} or {\"action\": \"remove\", \"rule_id\"}. "
                  "Use only the rule types and settings given. If the wish is unclear, could mean two things, needs a value "
                  "that was not given, or does not fit any rule type, DO NOT GUESS: return a question. "
                  "Locked rules cannot be removed.")
        user = json.dumps({"wish": text,
                           "current_rules": [{k: r[k] for k in ("id", "type", "name", "settings", "result")} for r in doc["rules"]],
                           "rule_types": {k: {"title": t["title"], "locked": t["locked"], "results": t["results"],
                                              "settings": t["settings"]} for k, t in TYPES.items()},
                           "brands_that_look_like_words_in_the_wish": _brands_like(text)}, ensure_ascii=False)
        try:
            g = llm.generate_json(system, user)
            if g.get("status") == "question" and isinstance(g.get("question"), str):
                return {"status": "question", "proposed_by": AI, "question": g["question"].strip()[:400]}
            if g.get("status") == "proposal" and isinstance(g.get("change"), dict):
                out = _as_proposal(g["change"], AI, str(g.get("explanation") or ""))
                if out:
                    return out  # passed the same validation as a manual edit
        except llm.LLMError:
            pass
    return _propose_by_patterns(text, doc)


def _as_proposal(change, by, explanation):
    change = {k: v for k, v in change.items() if k in ("action", "rule_id", "type", "name", "settings", "result") and v is not None}
    p = rule_edit.preview(change)
    if not p["ok"]:
        return None
    return {"status": "proposal", "proposed_by": by, "change": change, "diff": p["diff"], "explanation": explanation[:300]}


_brand_cache = {}


def _brands():
    if "b" not in _brand_cache:
        _brand_cache["b"] = {p["brand"] for p in catalog.load().values() if p["brand"]}
    return _brand_cache["b"]


def _brands_like(text):
    by_lower = {b.lower(): b for b in _brands()}
    out = []
    for w in re.findall(r"[\w'&-]{3,}", text.lower()):
        out += [by_lower[m] for m in difflib.get_close_matches(w, by_lower, n=3, cutoff=0.75)]
    return sorted(set(out))[:20]


def _find_brand(words):
    """Match a brand name written by the user to a brand in the product data. Returns (brand, suggestion)."""
    by_lower = {b.lower(): b for b in _brands()}
    w = words.strip(" .,'\"").lower()
    if w in by_lower:
        return by_lower[w], None
    close = difflib.get_close_matches(w, by_lower, n=1, cutoff=0.8)
    return None, by_lower[close[0]] if close else None


def _rule_of(doc, type_name):
    return next((r for r in doc["rules"] if r["type"] == type_name), None)


def _propose_by_patterns(text, doc):
    """Plain-code fallback: understands a few common wishes. Anything else -> asks."""
    t = text.lower()
    ask = lambda q: {"status": "question", "proposed_by": CODE, "question": q}
    ok = lambda change, why: _as_proposal(change, CODE, why) or ask("That change is not valid for this rule. Could you say it another way?")
    money = re.search(r"(?:€\s*)?(\d[\d ,.]*\d|\d)\s*(?:€|eur|euros?)?", t)

    def number(m):
        s = m.group(1).replace(" ", "")
        s = re.sub(r"[,.](?=\d{3}\b)", "", s).replace(",", ".")
        return float(s)

    # remove a rule by id: "remove rule C3"
    m = re.search(r"\b(?:remove|delete|drop)\b.*?\b([abc]\d{1,3})\b", t)
    if m:
        rid = m.group(1).upper()
        r = next((x for x in doc["rules"] if x["id"] == rid), None)
        if not r:
            return ask(f"There is no rule {rid}. Which rule do you want to remove?")
        if TYPES[r["type"]]["locked"]:
            return ask(f"{rid} - {r['name']} is a safety rule and can't be removed. Do you want to change its settings instead?")
        return ok({"action": "remove", "rule_id": rid}, f"Remove rule {rid} - {r['name']}.")

    # spend limit: "raise the spend limit to 2,500"
    if re.search(r"\b(spend|spending|budget|order)\b.*\blimit\b|\blimit\b.*\b(spend|order|€|eur)", t):
        r = _rule_of(doc, "spend_limit")
        if not r:
            return ask("There is no spend limit rule right now. Do you want to add one? If so, what should the limit be?")
        if not money:
            return ask(f"What should the new spend limit be? It is {rules.eur(r['settings']['limit_eur'])} now.")
        return ok({"action": "edit", "rule_id": r["id"], "settings": {"limit_eur": number(money)}},
                  f"Set the spend limit to {rules.eur(number(money))}.")

    # "more than 5 times the usual"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:x|×|times)", t)
    if m and ("usual" in t or "normal" in t or "typo" in t):
        r = _rule_of(doc, "usual_size")
        if r:
            return ok({"action": "edit", "rule_id": r["id"], "settings": {"max_ratio": float(m.group(1).replace(",", "."))}},
                      "Change how many times the usual amount is allowed before the agent asks.")

    # "max 3 brands"
    m = re.search(r"(\d+)\s*brands?\b", t)
    if m and re.search(r"\b(max|most|up to|at most|mix|split)\b", t):
        r = _rule_of(doc, "max_brands")
        if r:
            return ok({"action": "edit", "rule_id": r["id"], "settings": {"max_brands": int(m.group(1))}},
                      "Change how many brands one order may mix.")

    # brands: "stop buying Arla" / "never buy Arla" / "allow Arla" / "approve Arla"
    m_avoid = re.search(r"\b(?:stop buying|never buy|don'?t buy|do not buy|no more|avoid|ban|block)\s+(?:from\s+|brand\s+)?(.+?)(?:\s+(?:products?|anymore|any more|brand))?[.!]?$", t)
    m_allow = re.search(r"\b(?:allow|approve|add|accept|trust)\s+(?:the\s+)?(?:brand\s+)?(.+?)(?:\s+(?:as an? approved brand|brand|products?|too|as well))?[.!]?$", t)
    for m, kind in ((m_avoid, "avoid"), (m_allow, "allow")):
        if not m:
            continue
        brand, suggestion = _find_brand(m.group(1))
        if not brand:
            return ask(f"I don't know a brand called '{m.group(1).strip()}'." +
                       (f" Did you mean {suggestion}?" if suggestion else " Which brand do you mean?"))
        approved = _rule_of(doc, "approved_brands")
        if kind == "allow":
            if not approved:
                return ask("There is no approved-brands rule right now, so every brand is already allowed.")
            if brand in approved["settings"]["brands"]:
                return ask(f"{brand} is already an approved brand. Did you mean something else?")
            return ok({"action": "edit", "rule_id": approved["id"], "settings": {"brands": approved["settings"]["brands"] + [brand]}},
                      f"Add {brand} to the approved brands.")
        if approved and brand in approved["settings"]["brands"]:
            return ask(f"{brand} is on your approved brands list. Do you want to (a) remove it from the approved list, so it needs "
                       f"approval each time, or (b) never buy it at all? Please say which.")
        avoid = _rule_of(doc, "avoid_brands")
        if avoid:
            if brand in avoid["settings"]["brands"]:
                return ask(f"{brand} is already on the avoid list.")
            return ok({"action": "edit", "rule_id": avoid["id"], "settings": {"brands": avoid["settings"]["brands"] + [brand]}},
                      f"Add {brand} to the brands the agent never buys.")
        return ok({"action": "add", "type": "avoid_brands", "name": f"Never buy {brand}",
                   "settings": {"brands": [brand]}, "result": "blocked"}, f"Add a rule: never buy {brand}.")

    names = ", ".join(r["name"].lower() for r in doc["rules"] if not TYPES[r["type"]]["locked"])
    return ask("I'm not sure which rule you mean, so I haven't changed anything. I can change: " + names +
               ". For example: 'set the spend limit to €2,500', 'never buy Arla', 'allow Arla', 'max 3 brands', 'remove rule C3'."
               + ("" if llm.available() else " (Simple mode: with a Gemini key I understand free wording.)"))
