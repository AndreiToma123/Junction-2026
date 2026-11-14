# Decisions log

Short record of what we decided and why. Newest last.

| # | Date | Decision | Why |
|---|---|---|---|
| 1 | 14 Nov | **Ordering is a mock order service of our own** (`app/orders.py`, saves to `data/orders.jsonl`, numbers like MOCK-0001). Nothing is sent to Valio Aimo. | The Aimo mock API has no order endpoint. |
| 2 | 14 Nov | **Order history is generated test data** (`data/usual_orders.json` + a few weeks of past log lines). | No order history exists in the API or the files. |
| 3 | 14 Nov | **Spend limit counts tax** (`catalogPrice.valueWithTax`). Limit €2,000 per order. | Your choice. |
| 4 | 14 Nov | **Split scenario uses a simulated stock limit** (`data/demo_stock_overrides.json`): Valio 464306 = 600, other Valio lactose-free semi-skimmed = 0, Arla 469363 = 500. Prices, dates and cut-offs stay real. Labelled "demo stock override" everywhere. | No Valio lactose-free milk has limited stock in the live data. |
| 5 | 14 Nov | **Product search is ours**, over the product file. Then we ask the API only about the matches. | The API has no search, and its docs ask us not to query all products. |
| 6 | 14 Nov | **Products whose order cut-off has passed are blocked**, even if the API shows a delivery date. | Supplier ("Terminal") products show cut-off Thu 12 Nov but "available 16 Nov". We trust the cut-off. Ask a mentor. |
| 7 | 14 Nov | **Gemini gets a third job: "Ask the log".** The user asks a question in plain words (e.g. "why did the milk taste different the past 3 weeks?"). Gemini answers **only from log lines we give it**, and must cite the exact time of each line it uses. If it can't answer from the log, it says so. | Makes the audit log usable for people, not just stored. Rule checks stay plain code, no AI. |
| 8 | 14 Nov | **Dashboard has 3 screens**: New request (prompt + live steps), Approvals (summary boxes, order table, rule checks), Audit log (with "Ask the log"). Design: https://claude.ai/artifact/9B2DeWezCczBPpbqHigCTa | Your feedback on readability. |
| 9 | 14 Nov | **The agent orders whole packs only** (e.g. whole cases), rounding up. | The data allows steps of 0.01 sales units, but a kitchen can't receive 0.37 of a case. Rule B5 still flags rounding that adds more than 5%. |
| 10 | 14 Nov | **The worst rule result decides the outcome**: blocked > ask the buyer > needs approval > order by itself. | Simple and predictable; written in `rules.yaml` and `app/rules.py`. |
| 11 | 14 Nov | **Buyer confirms a big amount → still needs approval** (rule A1). | A confirmed 10× order is still unusual; a person should see it. |
| 12 | 14 Nov | **The rulebook can be edited from the dashboard**: (a) edit a rule's numbers/lists, (b) remove a rule, (c) add a rule from a list of rule types the code knows, (d) edit the rules file as text, (e) ask the AI in plain words. `rules.yaml` is the single source; `rulebook.md` is generated from it. | Your request. Rule checks stay plain code, so every rule is predictable and testable. |
| 13 | 14 Nov | **The AI never changes rules by itself.** It proposes a change with a before/after, and a person clicks Apply. If the request is unclear or doesn't fit a known rule type, the AI asks a question instead of guessing. | Your rule: "if unsure, check with the human". |
| 14 | 14 Nov | **Safety rules are locked** (can be tuned, not removed): request is clear, can be sold, matches the request, arrives by the deadline, enough stock, order steps. Every rule change is logged with who, when, before and after, and can be undone. | Without these the agent could order things that can't arrive or don't exist. |
| 15 | 14 Nov | **Audit log = one file, `data/log.jsonl` (JSON Lines, an open format)**, one plain-language line per step with time, actor (agent / person / buyer), request, source and the exact numbers used. Lines are only added, never changed. Each line holds a fingerprint (SHA-256 hash) of the line before, so any edit or deletion is detected (`/api/log/verify`). | Judges score auditability; the log can be opened in any text editor or loaded by any tool. |
| 16 | 14 Nov | **3 weeks of test history** (24 Oct, 31 Oct, 7 Nov) are written into the log by `scripts/reset_demo.py`, marked `"test_history": true` and "[test history]". | Gives "Ask the log" something real to find (the Arla split on 31 Oct, the UHT milk on 7 Nov). |

## How "Ask the log" stays trustworthy
1. Plain code first picks the candidate log lines (by product, brand, date range, request id).
2. Gemini gets only those lines, each with its id and time, and writes a short answer.
3. Code checks that every time/id Gemini cites really exists in those lines. If not, we show plain matching lines instead of the answer.
4. The answer always ends with what the log cannot tell (e.g. taste, quality).
