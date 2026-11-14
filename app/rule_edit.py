"""Changing the rulebook safely.

A change is a small dict:
  {"action": "edit",   "rule_id": "C1", "settings": {"limit_eur": 2500}}          (only what changes)
  {"action": "add",    "type": "avoid_brands", "name": "No Arla", "settings": {"brands": ["Arla"]}, "result": "blocked"}
  {"action": "remove", "rule_id": "C3"}
or a whole new rules.yaml text (manual editing).

Every change goes: preview (validated, before/after in plain words) -> apply by a named person.
Apply saves the old version (for undo), writes rules.yaml, regenerates rulebook.md and logs the change.
The AI (build step 6) only ever produces a change dict for preview; it can't apply.
"""
import copy
import json
from datetime import datetime

import yaml

from . import config, log, rulebook, rules
from .rule_types import TYPES

CHANGES_FILE = config.STATE / "rule_changes.jsonl"
VERSIONS_DIR = config.STATE / "rule_versions"
HEADER = """# THE RULEBOOK (single source). rulebook.md is generated from this file.
# Edit via the dashboard (Rules screen) or by hand; every change is checked before it is saved.
# Rule types and their settings: app/rule_types.py. Results: approve | ask | blocked.
# Locked rules (safety) can be tuned but not removed.
"""


class RuleEditError(Exception):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(problems))


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _plain(rule):
    return f"{rule['id']} - {rule['name']}: {rules.describe(rule)}"


def apply_change_to(doc, change):
    """Returns (new_doc, problems). Does not save anything."""
    new = copy.deepcopy(doc)
    act = change.get("action")
    if act == "edit":
        r = next((x for x in new["rules"] if x["id"] == change.get("rule_id")), None)
        if not r:
            return new, [f"There is no rule {change.get('rule_id')}."]
        for k in ("name", "result"):
            if change.get(k) is not None:
                r[k] = change[k]
        r["settings"] = {**r["settings"], **(change.get("settings") or {})}
    elif act == "add":
        t = change.get("type")
        if t not in TYPES:
            return new, [f"Unknown rule type '{t}'. Known types: {', '.join(TYPES)}."]
        r = {"id": rules.next_id(new, t), "type": t, "name": change.get("name") or TYPES[t]["title"],
             "settings": change.get("settings") or {}, "result": change.get("result") or TYPES[t]["results"][0]}
        # put it after the last rule of the same scope, so the file stays grouped
        scope = TYPES[t]["scope"]
        idx = max([i for i, x in enumerate(new["rules"]) if TYPES[x["type"]]["scope"] == scope], default=len(new["rules"]) - 1)
        new["rules"].insert(idx + 1, r)
    elif act == "remove":
        r = next((x for x in new["rules"] if x["id"] == change.get("rule_id")), None)
        if not r:
            return new, [f"There is no rule {change.get('rule_id')}."]
        if TYPES[r["type"]]["locked"]:
            return new, [f"{r['id']} - {r['name']} is a locked safety rule. You can change its settings, not remove it."]
        new["rules"].remove(r)
    else:
        return new, ["action must be add, edit or remove."]
    return new, rules.validate(new)


def diff(old, new):
    """Plain before/after per rule that changed."""
    o = {r["id"]: r for r in old["rules"]}
    n = {r["id"]: r for r in new["rules"]}
    out = []
    for rid in list(o) + [x for x in n if x not in o]:
        a, b = o.get(rid), n.get(rid)
        if a == b:
            continue
        out.append({"rule_id": rid,
                    "change": "added" if not a else "removed" if not b else "changed",
                    "before": _plain(a) if a else None,
                    "after": _plain(b) if b else None})
    return out


def preview(change=None, text=None):
    """Validate a change (dict) or a whole rules.yaml text. Nothing is saved."""
    old = rules.rules()
    if text is not None:
        try:
            new = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            return {"ok": False, "problems": [f"The text can't be read{where}: {getattr(e, 'problem', 'format error')}."], "diff": []}
        if isinstance(new, dict):
            new = {**{k: v for k, v in old.items() if k != "rules"}, **new}
        problems = rules.validate(new)
    else:
        new, problems = apply_change_to(old, change or {})
    if problems:
        return {"ok": False, "problems": problems, "diff": []}
    d = diff(old, new)
    if not d:
        return {"ok": False, "problems": ["Nothing would change."], "diff": []}
    return {"ok": True, "problems": [], "diff": d, "new_rules": new}


def _save_version(doc):
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    n = len(list(VERSIONS_DIR.glob("v*.yaml"))) + 1
    path = VERSIONS_DIR / f"v{n:04d}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path.name


def _write(doc):
    rules.RULES_FILE.write_text(HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120),
                                encoding="utf-8")
    rulebook.write()


def _changes():
    if not CHANGES_FILE.exists():
        return []
    return [json.loads(l) for l in CHANGES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


def list_changes():
    return _changes()


def apply(by, change=None, text=None, source="manual", request_text=None):
    """Apply a change for a named person. source: manual | text | ai (the AI proposed, `by` approved)."""
    if not by or not by.strip():
        raise RuleEditError(["A rule change needs the name of the person making it."])
    p = preview(change, text)
    if not p["ok"]:
        raise RuleEditError(p["problems"])
    old = rules.rules()
    version = _save_version(old)
    _write(p["new_rules"])
    entry = {"change_id": f"RC-{len(_changes()) + 1:04d}", "time": _now(), "by": by, "source": source,
             "request_text": request_text, "change": change, "diff": p["diff"], "previous_version": version}
    config.STATE.mkdir(parents=True, exist_ok=True)
    with open(CHANGES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    how = {"manual": "on the Rules screen", "text": "by editing the rules file", "ai": "as proposed by the AI"}.get(source, f"({source})")
    for d in p["diff"]:
        msg = f"Rule {d['rule_id']} {d['change']} by {by} {how}."
        if d["before"]:
            msg += f" Before: {d['before']}"
        if d["after"]:
            msg += f" After: {d['after']}"
        log.write("rule_change", msg, "rules.yaml", actor=f"person:{by}",
                  data={"change_id": entry["change_id"], "rule_id": d["rule_id"], "request_text": request_text})
    return entry


def undo(change_id, by):
    """Restore the rulebook as it was just before `change_id` (itself logged as a change)."""
    entry = next((c for c in _changes() if c["change_id"] == change_id), None)
    if not entry:
        raise RuleEditError([f"No rule change {change_id}."])
    # Reverse only the rules this change touched; later changes to other rules stay.
    before = yaml.safe_load((VERSIONS_DIR / entry["previous_version"]).read_text(encoding="utf-8"))
    old_rules = {r["id"]: r for r in before["rules"]}
    doc = copy.deepcopy(rules.rules())
    for d in entry["diff"]:
        rid = d["rule_id"]
        current = next((r for r in doc["rules"] if r["id"] == rid), None)
        if d["change"] == "added" and current:
            doc["rules"].remove(current)
        elif d["change"] == "removed" and not current:
            doc["rules"].append(old_rules[rid])
        elif d["change"] == "changed" and current:
            doc["rules"][doc["rules"].index(current)] = old_rules[rid]
    order = {"request": 0, "line": 1, "order": 2}
    doc["rules"].sort(key=lambda r: order[TYPES[r["type"]]["scope"]])  # keep the file grouped
    return apply(by, text=yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), source=f"undo of {change_id}")


def overview():
    """Everything the Rules screen needs: rules in plain words + the menu of rule types."""
    doc = rules.rules()
    return {
        "rules": [{**r, "description": rules.describe(r), "locked": TYPES[r["type"]]["locked"],
                   "category": TYPES[r["type"]]["category"], "type_title": TYPES[r["type"]]["title"],
                   "allowed_results": TYPES[r["type"]]["results"]} for r in doc["rules"]],
        "types": [{"type": k, "title": t["title"], "category": t["category"], "locked": t["locked"],
                   "results": t["results"], "settings": t["settings"]} for k, t in TYPES.items()],
        "yaml": rules.RULES_FILE.read_text(encoding="utf-8"),
        "default_yaml": config.RULES_DEFAULT_FILE.read_text(encoding="utf-8") if config.RULES_DEFAULT_FILE.exists() else None,
    }
