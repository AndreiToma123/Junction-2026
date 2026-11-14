# Roadmap

Junction 2026 · Valio Aimo "Trusted Trade Agents" · final submission **Sunday 15 Nov, 10:00**

Last updated: Sat 14 Nov 2026, late afternoon.

---

## 1. Where we are

Everything in the original build plan is done and tested with live Valio Aimo data.

| # | Piece | Status | What exists |
|---|---|---|---|
| 0 | Read the data first | ✅ Done | `DATA_NOTES.md`, samples in `data/samples/` |
| 1 | API connection | ✅ Done | Product search (ours), live price / stock / delivery (Aimo), mock orders (ours) |
| 2 | Rulebook + rule checks | ✅ Done | `rules.yaml` (15 rule types, 10 rules in the default rulebook), plain-code checks, generated `rulebook.md` |
| 3 | Agent flow | ✅ Done | Request → checks → search → options incl. split → rules → order / approval / question / "can't be done" |
| 4 | Audit log | ✅ Done | `data/log.jsonl`, one line per step, tamper check, search |
| 5 | Dashboard | ✅ Done | 4 screens: New request, Requests, Audit log, Rules. Works on a phone |
| 6 | Gemini | ✅ Done, tested live | 4 jobs: read request, summary, ask the log, propose rule changes. Works without a key too |
| 7 | Demo scenarios + README | ✅ Done | `scripts/run_scenarios.py` (normal, split, spend, typo), `README.md`, `run.sh` |
| + | Rulebook editing | ✅ Done | Edit / add / remove / text edit / undo / restore defaults / ask the AI. Every change previewed and logged |

### Extension "B": the restaurant example (agreed 14 Nov)

| Step | What | Status |
|---|---|---|
| B1 | Re-check price and stock at approval (story 6) | ✅ Done |
| B2 | Menu (11 dishes), 18 ingredients, kitchen stock, automatic restock with duplicate guard, Kitchen screen, kg support | ✅ Done |
| B3 | Menu editing: edit / add / remove dishes, shared-ingredient view, rules for new ingredients | ⬜ Next |
| B4 | Wallets: recurring budgets per category, event wallets (charged by purpose) | ⬜ |
| B5 | "Order what is available now" (partial order) with "restock date unknown" | ⬜ First to cut |

Tests: 9 rule cases, rule-editing tests, 31 AI safety checks, 4 scenarios. All pass. Test scripts
run in a sandbox and can't touch real data.

### Current state to be aware of
- **The live rulebook is not the default.** It has: spend limit €2,500, a "Never buy Arla" rule, and
  Arla on the approved list. With these, the `spend` scenario orders by itself and the `split`
  scenario ends in "can't be done". Before the demo: Rules screen → "Restore the default rules",
  or run `.venv/bin/python -m scripts.reset_demo --rules`.
- Gemini is connected on the free tier and used sparingly (see decision 25).

---

## 2. Decisions so far (short version)

Full list with reasons: [DECISIONS.md](DECISIONS.md) (34 decisions).

**Scope and data**
- Ordering is our own mock order service; nothing is sent to Aimo (1).
- Order history is generated test data (2, 16). The split scenario uses simulated stock, labelled everywhere (4).
- We search the product file ourselves and ask the API only about the matches (5).
- A product whose order cut-off has passed is blocked, even if the API shows a delivery date (6).
- The agent orders whole packs only (9). Spend limit includes tax (3).

**Guardrails**
- The worst rule result decides: can't be used > ask the buyer > needs approval > order alone (10).
- A confirmed unusual amount still needs a person's approval (11). Typing a new amount counts as the buyer's confirmation, so the agent never asks twice (23).
- Six safety rules are locked: they can be tuned, not removed (14).
- Rules can be edited on the dashboard; `rules.yaml` is the single source (12).
- The AI never changes rules itself: it proposes, a person applies. If unsure, it asks (13, 21).

**Audit**
- One JSON Lines log with a hash chain; every step, decision and rule change (15).
- No login in the demo: a name is required for approvals and rule changes (18).

**AI**
- Gemini does four language jobs and never decides what is allowed; plain code checks every answer (19).
- Everything works without Gemini (20). Gemini is used sparingly on the free tier (25).

**Dashboard**
- One plain HTML page, four screens (8, 17). Rule ids shown as "A1 - Name" (24).

---

## 3. Parked (removed for now, easy to bring back)

Two boxes were removed from the bottom of the request page on 14 Nov. The data is still saved
with every request (`not_checked` and `alternatives_detail`) and returned by the API.

**"Not checked by the agent"** (was a dashed box). It listed:
- Allergens and food quality (out of scope)
- Customer-specific price: the mock API ignores the customer number
- Real order confirmation: this is a mock order, nothing is sent to Valio Aimo
- When relevant: "Stock: stock not confirmed by supplier", and
  "Products, prices and stock: not looked up until the buyer answers"

**"Other options considered"**. It listed up to 3 usable options that ranked lower and up to 3
blocked ones, each with its total and the reason, for example:
- "1,000 × Valio semi skimmed milk 1 l lactose free UHT (464201): €1,770.00. Usable, but ranked lower."
- "200 × Valio Eila® … 1 l lactose free (465039): €1,558.00. 465039: order cut-off Thu 12 Nov 15:00 has passed."

Note: the original brief asked for the summary to say what was *not* checked. It is still in the
README and in the data, but no longer visible on the page.

---

## 4. How the eight stories are covered

| Story | Covered? | What works / what is missing |
|---|---|---|
| 1 Normal Tuesday | 🟡 Partly | One product, auto-order with order number, delivery date and log: yes. Missing: several products in one request ("usual dairy order plus butter"), kg units, a delivery time of day (the API gives a date only), a message to the cook |
| 2 Not enough milk | 🟢 Mostly | Split with a second brand, approval for the new brand, summary, approve on a phone: yes (stock is simulated). Missing: "wait until Wednesday for the rest" (the API gives no restock date), push notification |
| 3 Hidden allergen | 🔴 No | Out of scope so far. The data has allergen fields and ingredient text, so it is possible |
| 4 Big event | 🟡 Partly | Spend-limit approval with "€X over your limit": yes. Missing: working out quantities for "breakfast for 300", a multi-item cart, per-item delivery times, editing the cart while approving, different wallets per agent |
| 5 Cheap swap (fresh → frozen) | 🔴 No | No quality rule. The data has a temperature class per product, so a "never fresh → frozen without a person" rule type is possible |
| 6 Price changed | 🔴 No | We save when the price was checked and the API gives price validity dates, but on approval we do **not** re-check price or stock |
| 7 Typo | 🟢 Yes | 10,000 L vs. usual 1,000 L: asks "Change to 1,000 L?" and orders nothing |
| 8 The log, three weeks later | 🟢 Yes | "Ask the log" finds the split, who approved it and when; a rule can be added right after. Limits: history is generated test data; "no Brand B *for coffee milk*" (a brand rule for one item only) is not possible yet, only "never buy Brand B" |

---

## 5. What could come next (not started; pick by value for the judges)

| Idea | Size | Why |
|---|---|---|
| **Re-check price and stock when a person approves** (story 6) | Small | Fits the "trust" theme: "approved within the time the price is valid" or "price changed, ask again" |
| **Fresh → frozen / temperature-class rule** (story 5) | Small–medium | One new rule type using existing data; shows quality guardrails |
| **Allergen rule: block "contains X", send "may contain" to a person** (story 3) | Medium | Strongest safety story; data exists. AI would only add caution, never remove it |
| **Several products in one request** (stories 1 and 4) | Large | Needs a cart, per-item rules and kg / piece units |
| **Brand rule per item** (story 8's "no Brand B for coffee milk") | Small | Small extension of the avoid-brands rule |
| Bring back "Not checked" / "Other options" in a tidier form | Small | See Parked |
| Notify the approver (email / chat) | Medium | Realism; not needed for the demo |
| Ask a mentor about the passed cut-off on supplier products | - | Confirms decision 6 |

If time is short, cut everything in this section: the four demo scenarios already run end to end.

## 6. Before submitting
- [ ] Restore the default rules and run `reset_demo`, then the 4 scenarios once.
- [ ] Commit the work (keys are not in git: checked).
- [ ] Check decisions 14, 18 and 22 in `DECISIONS.md` (made without asking you first).
- [ ] Record or rehearse the demo in this order: normal → split → spend → typo → Ask the log → change a rule.
