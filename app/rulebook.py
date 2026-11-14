"""Generates rulebook.md (the human-readable rulebook) from rules.yaml, so the two never disagree."""
from datetime import datetime

from . import config, rules
from .rule_types import TYPES

RULEBOOK_MD = config.RULEBOOK_MD


def render():
    doc = rules.rules()
    out = [f"# Rulebook: {doc['kitchen']} buying agent", "",
           "> **Generated from `rules.yaml`. Don't edit this file by hand.** Change the rules on the dashboard's",
           "> Rules screen (or edit `rules.yaml`); this file is rewritten automatically after every change.",
           f"> Last generated: {datetime.now().astimezone().strftime('%a %d %b %Y %H:%M')}", "",
           "The agent checks **every** option against these rules. The checks are plain code, no AI.",
           "When a rule is not met, it gives one of these results:", "",
           "| Result | What the agent does |", "|---|---|",
           "| ✅ **Pass** | Nothing. The rule is fine. |",
           "| 🟡 **Needs approval** | Doesn't order. A person approves or rejects on the dashboard. |",
           "| ❓ **Ask the buyer** | Doesn't order. Asks the buyer what they meant (e.g. a probable typo). |",
           "| ⛔ **Can't be used** | This option is dropped. If no option is left, the agent says why. |", "",
           "**The agent orders by itself only if every rule passes.** The worst result decides.", ""]
    for cat, title in [("Request", "A. The request (checked before searching)"),
                       ("Product", "B. Each product in an option"),
                       ("Order", "C. The whole order")]:
        rs = [r for r in doc["rules"] if TYPES[r["type"]]["category"] == cat]
        if not rs:
            continue
        out += [f"## {title}", ""]
        for r in rs:
            lock = " 🔒" if TYPES[r["type"]]["locked"] else ""
            out += [f"### {r['id']} - {r['name']}{lock}", f"- {rules.describe(r)}", ""]
    out += ["🔒 = safety rule: its settings can be changed, but it can't be removed.", "",
            "## D. Choosing between options",
            "If several options can be used, the agent picks the one that:",
            "1. needs the fewest approvals, then", "2. uses the kitchen's usual product the most, then",
            "3. is the cheapest (price incl. tax from the Valio Aimo API).", "",
            "Splits are only built when no single product can cover the amount by the deadline. The usual",
            "product is used as much as possible; the rest comes from official substitutes first.", "",
            "## E. Always true",
            "- Every order, approval, rejection, question and rule change is written to the audit log.",
            "- Summaries only state facts from the data and the rule results.",
            "- A person can always reject. A rejected option is never ordered.",
            "- The AI never changes a rule by itself: it proposes, a person applies. If unsure, it asks.", "",
            "## Not checked by the agent", ""] + [f"- {n}" for n in doc["not_checked"]] + [""]
    return "\n".join(out)


def write():
    RULEBOOK_MD.write_text(render(), encoding="utf-8")
