"""Test the AI jobs WITHOUT a Gemini key.

Part 1: simple mode (no key): the plain-code fallbacks.
Part 2: a stand-in pretends to be Gemini and returns good, wrong and broken answers,
        to prove the safety checks accept the good ones and catch the bad ones.
The real Gemini is never called here.

Runs in a sandbox with a copy of the DEFAULT rulebook: your real rules and data are not touched.
Run: .venv/bin/python -m scripts.try_ai
"""
from datetime import date

from scripts import _sandbox  # noqa: F401  (must be first: tests run in a sandbox)
from app import agent, ai, llm

TODAY = date(2026, 11, 14)
OK, BAD = "  ok  ", " FAIL "
results = []


def check(name, cond, shown=""):
    results.append(cond)
    print(f"{OK if cond else BAD} {name}" + (f"\n         -> {shown}" if shown else ""))


def fake(answer):
    """Make llm behave as if Gemini answered `answer` (a dict), or failed (an Exception)."""
    llm.available = lambda: True

    def gen(system, user, timeout=25):
        if isinstance(answer, Exception):
            raise answer
        return answer
    llm.generate_json = gen


def no_key():
    llm.available = lambda: False


def reset():
    import runpy
    runpy.run_module("scripts.reset_demo", run_name="__main__")


if __name__ == "__main__":
    from app import rules
    rules_before = rules.RULES_FILE.read_text(encoding="utf-8")
    reset()
    print("\n===== PART 1: simple mode (no Gemini key) =====")
    no_key()
    print(ai.status()["mode"])

    r = ai.parse_request("I need 1,000 litres of lactose-free milk by Monday.", TODAY)
    check("reads a normal request", r["amount"] == 1000 and r["deadline"] == "2026-11-16", r["parsed_by"])

    a = ai.ask_log("Why did the milk taste different the past 3 weeks?", TODAY)
    check("ask the log: finds the 2 different milk orders", len(a["points"]) == 2, a["answer"])
    for p in a["points"]:
        print(f"         - {p['time'][:16]} {p['request_id']} ({p['line_id']}): {p['text']}")
    print(f"         ! {a['cannot_tell']}")
    a = ai.ask_log("what happened with Arla?", TODAY)
    check("ask the log: other questions fall back to a word search", a["points"] == [] and len(a["lines"]) > 0, a["answer"])

    for wish, want in [("raise the spend limit to 2,500", "proposal"), ("never buy Arla", "proposal"),
                       ("allow Arla", "proposal"), ("max 3 brands", "proposal"), ("remove rule C3", "proposal"),
                       ("stop buying Valio", "question"), ("never buy Arlaa", "question"), ("remove rule B3", "question"),
                       ("change the spend limit", "question"), ("make the milk cheaper somehow", "question")]:
        p = ai.propose_rule(wish)
        shown = (p["diff"][0]["after"] or p["diff"][0]["before"]) if p["status"] == "proposal" else p["question"]
        check(f"rule wish '{wish}' -> {want}", p["status"] == want, shown)

    print("\n===== PART 2: stand-in Gemini (checks on the AI's answers) =====")
    ai.PARSE_MODE = "always"  # test the reading checks even for clear requests
    good = {"product": "lactose-free milk", "item_id": "lf-semi-skimmed-milk", "amount": 1000, "unit": "L", "deadline": "2026-11-16"}
    fake(good)
    r = ai.parse_request("We need 1 000 l of lactose free milk for Monday", TODAY)
    check("accepts a correct reading", r["parsed_by"] == "Gemini" and r["amount"] == 1000, r["parsed_by"])

    r = ai.parse_request("We need a thousand litres of lactose free milk for Monday", TODAY)
    check("amount written in words -> marked for confirmation", r.get("unverified") is True, r["parsed_by"])
    rec = agent.handle("We need a thousand litres of lactose free milk for Monday", today=TODAY)
    check("...and the agent asks the buyer instead of ordering", rec["status"] == "ask_buyer", rec["question"])
    no_key()  # (the buyer's answer needs no AI)
    rec = agent.answer(rec["id"], confirm=True)
    check("...buyer says yes -> order goes through", rec["status"] == "ordered", rec["summary"]["text"])

    for label, bad in [("negative amount", {**good, "amount": -5}), ("made-up unit", {**good, "unit": "barrels"}),
                       ("deadline in the past", {**good, "deadline": "2020-01-01"}), ("amount as text", {**good, "amount": "lots"})]:
        fake(bad)
        r = ai.parse_request("I need 1,000 litres of lactose-free milk by Monday.", TODAY)
        check(f"rejects a wrong reading ({label}) and uses the simple reader", "failed the checks" in r["parsed_by"] and r["amount"] == 1000)
    fake(llm.LLMError("timeout"))
    r = ai.parse_request("I need 1,000 litres of lactose-free milk by Monday.", TODAY)
    check("Gemini is down -> simple reader still works", r["amount"] == 1000, r["parsed_by"])

    no_key()
    rec = agent.handle("I need 2,000 litres of lactose-free milk by Monday.", today=TODAY)
    fake({"summary": "This order costs €2,300.00, which is €300.00 over the €2,000.00 limit, so it needs your approval."})
    s = ai.summarize(rec)
    check("summary: accepts wording that only uses real numbers", s["written_by"] == "Gemini", s["text"])
    fake({"summary": "This order costs €2,300.00 and saves you €450 compared with last month."})
    s = ai.summarize(rec)
    check("summary: rejects an invented number (€450) and uses the plain summary", s["written_by"] != "Gemini", s["text"])

    line = next(e for e in __import__("app.log", fromlist=["x"]).read() if e["request_id"] == "REQ-H02" and e["step"] == "choose")
    fake({"answer": "One order on 31 Oct used Arla for part of the milk.", "points": [{"line_id": line["id"], "text": "400 L was Arla."}], "cannot_tell": "Taste is not in the log."})
    a = ai.ask_log("Why did the milk taste different the past 3 weeks?", TODAY)
    check("ask the log: accepts an answer that cites a real line", a["answered_by"] == "Gemini" and len(a["points"]) == 1, a["answer"])
    fake({"answer": "The supplier changed the recipe on 2 Nov.", "points": [{"line_id": "L-999999", "text": "Recipe changed."}]})
    a = ai.ask_log("Why did the milk taste different the past 3 weeks?", TODAY)
    check("ask the log: rejects an answer citing a line that doesn't exist", a["answered_by"] != "Gemini", a["answer"])

    fake({"status": "proposal", "change": {"action": "edit", "rule_id": "C1", "settings": {"limit_eur": 3000}}, "explanation": "Raise the limit."})
    p = ai.propose_rule("we can spend a bit more, say three thousand")
    check("rule wish: accepts a valid proposal (still needs a person to apply)", p["status"] == "proposal" and p["proposed_by"] == "Gemini", p["diff"][0]["after"])
    fake({"status": "question", "question": "Do you want to block Arla completely, or require approval each time?"})
    p = ai.propose_rule("I don't really like Arla")
    check("rule wish: the AI asks when unsure", p["status"] == "question", p["question"])
    fake({"status": "proposal", "change": {"action": "remove", "rule_id": "B3"}, "explanation": "Remove the deadline rule."})
    p = ai.propose_rule("stop checking deadlines")
    check("rule wish: an invalid proposal (remove a safety rule) is not shown", p["status"] == "question", p["question"][:120])
    fake({"status": "proposal", "change": {"action": "edit", "rule_id": "C1", "settings": {"limit_eur": "unlimited"}}})
    p = ai.propose_rule("no limit on spending please")
    check("rule wish: an invalid value is not shown", p["status"] == "question")

    check("rulebook file is byte-for-byte unchanged after all of this", rules.RULES_FILE.read_text(encoding="utf-8") == rules_before)
    no_key()
    reset()
    print(f"\n{sum(results)} of {len(results)} checks passed.")
