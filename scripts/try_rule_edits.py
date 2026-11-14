"""Try rule editing: preview, apply, refuse bad changes, manual text edit, undo.
Uses live Aimo data to show how a rule change changes the agent's decision.
At the end the rulebook is restored exactly as it was, and the test changes are removed.

Run: .venv/bin/python -m scripts.try_rule_edits
"""
import shutil

from app import agent, config, rule_edit, rules, store

PERSON = "test person"


def say(title):
    print(f"\n=== {title}")


def show_preview(change=None, text=None):
    p = rule_edit.preview(change, text)
    if p["ok"]:
        for d in p["diff"]:
            print(f"  {d['change'].upper()} {d['rule_id']}")
            if d["before"]:
                print(f"    before: {d['before']}")
            if d["after"]:
                print(f"    after:  {d['after']}")
    else:
        print("  REFUSED: " + " | ".join(p["problems"]))
    return p


def run(text, scenario=None):
    r = agent.handle(text, scenario=scenario)
    print(f"  agent -> {r['status'].upper()}" + (f" ({r.get('reason', '')})" if r["status"] == "cannot_fulfil" else ""))
    return r


if __name__ == "__main__":
    original_rules = rules.RULES_FILE.read_text(encoding="utf-8")
    original_book = (config.ROOT / "rulebook.md").read_text(encoding="utf-8")
    try:
        say("1 Edit: raise spend limit to €2,500, then 2,000 L is ordered without approval")
        show_preview({"action": "edit", "rule_id": "C1", "settings": {"limit_eur": 2500}})
        c1 = rule_edit.apply(PERSON, {"action": "edit", "rule_id": "C1", "settings": {"limit_eur": 2500}})
        run("I need 2,000 litres of lactose-free milk by Monday.")

        say("2 Add: never buy Arla, then the split can't be done")
        show_preview({"action": "add", "type": "avoid_brands", "name": "Never Arla",
                      "settings": {"brands": ["Arla"]}, "result": "blocked"})
        rule_edit.apply(PERSON, {"action": "add", "type": "avoid_brands", "name": "Never Arla",
                                 "settings": {"brands": ["Arla"]}, "result": "blocked"})
        run("I need 1,000 litres of lactose-free milk by Monday.", scenario="split")

        say("3 Add: minimum delivery reliability 0.99 (Aimo's score)")
        show_preview({"action": "add", "type": "min_delivery_reliability", "name": "Reliable delivery",
                      "settings": {"min_score": 0.99, "if_unknown": "pass"}, "result": "approve"})

        say("4 Refused: remove a locked safety rule")
        show_preview({"action": "remove", "rule_id": "B3"})
        say("5 Refused: spend limit as text")
        show_preview({"action": "edit", "rule_id": "C1", "settings": {"limit_eur": "lots"}})
        say("6 Refused: unknown label")
        show_preview({"action": "add", "type": "required_labels", "settings": {"labels": ["MADE_ON_MARS"]}, "result": "blocked"})
        say("7 Refused: broken text edit")
        show_preview(text="rules: [this is not: a rule")

        say("8 Manual text edit: change max brands to 3")
        text = rules.RULES_FILE.read_text(encoding="utf-8").replace("max_brands: 2", "max_brands: 3")
        show_preview(text=text)
        rule_edit.apply(PERSON, text=text, source="text")

        say("9 Undo the spend-limit change (back to €2,000)")
        rule_edit.undo(c1["change_id"], PERSON)
        print("  C1 now:", rules.describe(next(r for r in rules.rules()["rules"] if r["id"] == "C1")))
        print("\n  change log:")
        for c in rule_edit.list_changes():
            print(f"   {c['change_id']} {c['time'][11:19]} by {c['by']} ({c['source']}): "
                  + "; ".join(f"{d['change']} {d['rule_id']}" for d in c["diff"]))
    finally:
        rules.RULES_FILE.write_text(original_rules, encoding="utf-8")
        (config.ROOT / "rulebook.md").write_text(original_book, encoding="utf-8")
        rule_edit.CHANGES_FILE.unlink(missing_ok=True)
        shutil.rmtree(rule_edit.VERSIONS_DIR, ignore_errors=True)
        shutil.rmtree(store.DIR, ignore_errors=True)
        config.ORDERS_FILE.unlink(missing_ok=True)
        print("\n(rulebook restored, test requests/orders/changes removed)")
