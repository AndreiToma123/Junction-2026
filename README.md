# Trusted Trade Agent: a buying agent for a professional kitchen

Junction 2026 · Valio Aimo challenge "Trusted Trade Agents"

A restaurant's buying agent that works **directly with Valio Aimo's (mock) systems**. A cook types
*"500 L lactose-free milk, 60 kg salmon and some potatoes by Monday"*, or the kitchen's own stock
monitor notices the salmon is running low, or the chef books *"150 extra guests on Monday: salmon soup
and meatballs"*. The agent:

1. reads the request (one or several products),
2. searches Valio Aimo's catalogue and asks the live API for **price, stock and delivery** for the matches,
3. builds and **ranks options**: one product, or a **split** when the usual product runs short,
4. checks every option against a **rulebook written in plain code**,
5. and then does exactly one of four things per product:

| The agent… | When |
|---|---|
| ✅ **orders by itself** | every rule passes |
| 🟡 **asks a person to approve** | something is outside its authority (spend limit, a new brand, a recipe swap the chef hasn't pre-approved…) |
| ❓ **asks the buyer** | something is unclear or unusual (an amount 10× the usual, no date, which product?) |
| ⛔ **says it can't be done, and why** | nothing can arrive in time, in stock, within the hard rules |

When something can't be sourced it offers **recipe-aware fixes**: a substitute that works in that dish
(from the chef's own list, or picked by the AI from products plain code has already checked), waiting for
a restock, or moving portions of an event to another dish. Every step goes into a **tamper-evident audit
log** in plain language, and people can change the rules, the menu and the chef's swap list from the
dashboard without touching code.

---

## Run it

You need Python 3.10+ and the Valio Aimo API key.

1. Copy `.env.example` to `.env` and put the key after `AIMO_API_KEY=`.
   Optional: a Google Gemini key after `GEMINI_API_KEY=`. Without it the app runs in "simple mode"
   (plain-code fallbacks for the AI parts). The free tier is enough: calls are kept to a minimum.
2. Put the product file from the Aimo portal at `docs/aimo_api/valio_aimo_junction26_product_data.json`
   (87 MB, not stored in git).
3. Start it, then open **http://localhost:8000** and type your name in the top bar (it goes into the audit log):

```bash
./run.sh
```

For a clean demo: **Rules → Start over → Reset everything** (tick "Add the 3 weeks of example history"
if you want to try "Ask the log"), or:

```bash
.venv/bin/python -m scripts.reset_demo --rules
```

**Live test walk-through:** [TEST_SCENARIOS.md](TEST_SCENARIOS.md), step by step, with what you should see.
**How it is built, the trust protocol and next steps:** [TECHNICAL.md](TECHNICAL.md). **Pitch script:** [PITCH_SCRIPT.md](PITCH_SCRIPT.md).

---

## The challenge scenarios we cover

| Scenario (from the challenge) | Where | What happens |
|---|---|---|
| **Last-minute event** | Events tab | "150 extra guests on Monday: salmon soup and meatballs" → recipes × guests − what the kitchen can spare → orders the rest (charged to the event's budget) → flags what can't arrive in time → "you can make 361 of 600 portions; move the rest to meatballs". Dishes not on the menu can be added right in the form. |
| **Supplier stock-out** | Requests, Kitchen | Aimo has 43 kg of the usual salmon, the kitchen needs 60 → split with rainbow trout, explained first ("Split order: …"), then: approve, order only the usual product, **wait for the restock** (demo date), or pick a ranked recipe-compatible substitute. |
| **Standing orders** (simple form of drift detection) | Kitchen | Each ingredient has a reorder level; "Simulate a day of service" uses up stock by the recipes and the agent sends the usual order by itself. A 10× amount is never ordered without asking. |

And the core of the brief: request → lookup → order / approval, the guardrails, the audit log, partial
stock, and a re-check of price and stock at the moment someone approves.

## The dashboard

Screens: **New request · Requests · Events · Kitchen · Audit log · Rules** (the **Dark** button switches to dark mode).

1. **New request**: type what you need (several products, each with its own date, are fine), or tap one of
   "Your usual". The agent's steps appear one by one, then the result per product, with **what needs you first**.
2. **Requests**: grouped by what needs doing (*Needs approval* first). Each request reads top to bottom:
   **what needs deciding** (the situation, then **the exact reason in bold**) → total, budget, delivery → the order →
   **Approve / Reject** → *or instead* (only the usual product, wait for the restock, substitutes) →
   **how the agent ranked the options** → rule checks → the agent's steps and the audit log.
3. **Events**: plan an extra booking, see its cost against its budget, fix what can't be sourced.
4. **Kitchen**: stock and reorder levels, the weekly budgets, the **menu** (add / edit / remove dishes),
   **ingredients** (edit, change the amount in every dish at once, add a new one by searching Aimo), and
   the **chef's swap list** per ingredient, with a "pre-approved" switch that lets the agent use a swap alone.
5. **Audit log**: search, filter, download; or **ask it in your own words** ("why did the milk taste
   different the past 3 weeks?") and get an answer that cites the exact log lines.
6. **Rules**: every rule in plain words; edit, add, remove, undo; or tell the agent what to change
   ("never buy Arla"). The agent only *proposes*; a person presses Apply. Also: restore the default rules,
   reset everything.

---

## How it works

```
 typed request ─┐                                   kitchen stock below reorder level ─┐   event planner ─┐
                ▼                                                                       ▼                  ▼
   read it (Gemini, checked by code, or a pattern reader) ──► one request per product ◄────────────────────┘
                │ unclear / unusual? ──► ask the buyer (a form: product, amount, date)
                ▼
   search Aimo's catalogue (ours) ──► live price, stock, delivery, cut-off (Valio Aimo API)
                ▼
   build options (single product, or split: usual product first, chef's swaps / substitutes next)
                ▼
   check EVERY option against the rulebook (plain code) ──► rank them (fewest approvals → usual product → price)
                ▼
     all rules pass ─► mock order + message ("Arrives Mon 16 Nov, 06:00–08:00")
     needs a person ─► dashboard: approve (price & stock re-checked first) / reject / alternatives
     nothing usable ─► says which rules blocked it, offers recipe-aware fixes
                ▼
   audit log: one line per step, who did it, where the fact came from, hash-chained
```

### Guardrails: what the agent may decide alone

The rulebook is `rules.yaml` (readable version `rulebook.md`, generated). The worst result decides:
can't be used > ask the buyer > needs approval > pass.

| Rule | If not met |
|---|---|
| A2 - Request is clear 🔒 | ask the buyer (never guesses a product, amount or date) |
| A1 - Much bigger than usual (over 3×) | ask the buyer; if confirmed, still needs approval |
| B1 - Can be sold 🔒 · B2 - Matches the request 🔒 · B3 - Arrives by the deadline 🔒 · B4 - Enough stock 🔒 | option can't be used |
| C4 - Whole packs, rounding adds at most 5% 🔒 | needs approval |
| C1 - Spend limit €2,000 incl. tax | needs approval |
| C2 - Approved brands (Valio), the kitchen's usual product, or a swap the chef pre-approved | needs approval |
| C3 - At most 2 brands in one order | option can't be used |

🔒 = safety rule: can be tuned, not removed. More rule types can be added from the dashboard: avoid
brands, avoid products, max price per litre, minimum delivery reliability, required labels.

**Also always true:** price and stock are re-checked with Aimo when someone presses Approve (a changed
price means nothing is ordered until they approve again); "as much as possible" is never bought blindly
(the agent shows what Aimo has and asks for a number); automatic restock requests that are no longer
needed are closed, but a request a person typed is only flagged as a possible duplicate.

**Where AI is used, and where not.** Rule checks, option building, ranking and the decision are plain code.
Gemini does language jobs only: reading requests and event descriptions, wording the summary, answering
questions about the log, proposing rule changes, and picking recipe-compatible substitutes from a list
plain code has already checked. Every AI answer is checked by code before use (numbers must come from the
data, cited log lines must exist, rule proposals pass the same validation as manual edits, substitutes must
be on the checked list). If the AI is unsure, it asks. Without a key, everything still works.

### Budgets

Routine orders are charged to a **weekly budget per category** (dairy, dry goods, fresh produce, fish &
meat); everything ordered for an event goes to **that event's budget**, even the same milk as usual.
The approver sees what is left before and after the order; the event page shows the event's total cost
against its budget. Budgets inform; the spend-limit rule is what requires approval.

### The audit log

`data/log.jsonl`, one line per step: time, who (agent / stock monitor / named person / buyer), request,
what happened in plain words, where the fact came from, and the exact numbers used. Lines are only added.
Each line carries a SHA-256 fingerprint of the line before it, so any later edit or deletion is detected
(shown as "Log intact" on the dashboard). Menu, budget, swap-list and rule changes are logged too.

---

## Open standards and formats

| What | Standard / format |
|---|---|
| Our agent API (how an outside agent would talk to this) | **OpenAPI 3.1** over HTTP + JSON. Live at `/docs` and `/openapi.json`, saved in `docs/agent_api_openapi.json` |
| Valio Aimo mock API | OpenAPI 3.1 (`docs/aimo_api/openapi.json`) |
| Product data | JSON with a **JSON Schema** (2020-12); products identified by **GS1 GTIN**, data from GS1 Synkka |
| Rulebook | **YAML** (`rules.yaml`), versioned, with generated Markdown |
| Menu, stock, swaps, budgets | JSON (`data/kitchen.default.json`) |
| Audit log | **JSON Lines**, with a **SHA-256** hash chain |
| Dates, money, countries | ISO 8601, ISO 4217 (EUR), ISO 3166 |
| Dashboard | plain HTML, CSS and JavaScript, no build step |

No proprietary formats: every file opens in a text editor.

---

## What is real, what is mock

| Part | Status |
|---|---|
| Product catalogue, prices, stock, delivery dates, cut-off times, Aimo's substitute links | **Real answers from the Valio Aimo mock API and data files** (itself synthetic data) |
| Placing orders | **Mock, ours.** The Aimo API has no order endpoint. Orders are saved to `data/orders.jsonl` as MOCK-0001… Nothing is sent to Valio Aimo |
| Menu (11 dishes), recipes, kitchen stock, usual orders, budgets, the chef's swap list | **Made up** for the demo (`data/kitchen.default.json`); every ingredient is linked to a real Aimo product |
| 3 weeks of past log lines | **Generated test history**, labelled "[test history]" (optional on reset) |
| Delivery **times** (e.g. 06:00–08:00) for milk, butter, salmon, cheese | **Demo data**, labelled "(demo delivery time)". Everything else shows the day only, as the API gives |
| Restock date for salmon (Wed 18 Nov, +200 kg) | **Demo data**, labelled. Other products: "Valio Aimo doesn't say when" |
| Low stock (split demo) and a price rise before approval | **Simulated**, only when chosen under "Demo options", labelled "demo" everywhere |
| Login | None. People type their name; approvals and changes are refused without one |

## Assumptions

All decisions, with reasons, are in **[DECISIONS.md](DECISIONS.md)**; progress in **[roadmap.md](roadmap.md)**;
the data in **[DATA_NOTES.md](DATA_NOTES.md)**. The ones that matter most:

- **Stock numbers.** `999999` on a supplier ("Terminal") product means *unknown* (documented by Aimo).
  `9999999999` and `666666666` on stocked products mean *no limit* (our reading).
- **Cut-off times are trusted.** Supplier products show an order cut-off that has already passed
  (Thu 12 Nov) yet report "available Mon 16 Nov". We block them.
- **Whole packs only**, in the product's own order step (e.g. salmon in 10 kg steps), rounded up.
- **Pack sizes** (litres / kg) come from the product data, otherwise from the product name.
- **Search is ours.** The API has no search and asks not to be queried for everything, so we search the
  product file and ask the API only about the matches.
- **Spend limit includes tax.**

What the agent does **not** check (saved with every request): allergens and food quality,
customer-specific prices (the mock API ignores the customer number), real order confirmation, and the
stock of supplier products.

---

## Tests (all run in a sandbox: your data and rules are never touched)

```bash
.venv/bin/python -m scripts.run_scenarios     # the 4 original scenarios: normal | split | spend | typo
.venv/bin/python -m scripts.try_events        # 44 checks: several products, dates, unclear requests, stock-out,
                                              #   restock, duplicates, events, budgets, ranking, menu, swap list
.venv/bin/python -m scripts.try_rules         # 9 rule cases with live data
.venv/bin/python -m scripts.try_rule_edits    # editing the rulebook: preview, apply, refuse, undo
.venv/bin/python -m scripts.try_ai            # 31 checks on the AI safety net (no Gemini key needed)
```

## Project layout

```
app/
  main.py        the backend (FastAPI): all endpoints
  agent.py       the flow: request → options → ranking → rules → order / approval / question
  rules.py       the rule engine            rule_types.py  every rule type (check + plain description)
  rule_edit.py   safe rulebook editing      rulebook.py    writes rulebook.md from rules.yaml
  catalog.py     product search             aimo.py        Valio Aimo API client (read-only)
  orders.py      mock order service         log.py         audit log with hash chain
  kitchen.py     menu, stock, restock, menu editing, swap list
  events.py      event planner              substitutes.py recipe-aware substitutes
  budgets.py     weekly and event budgets   admin.py       clear requests / reset everything
  ai.py          the AI jobs + their checks llm.py         Gemini connection (frugal, free tier)
  parse.py       pattern reader (one or several products)   demo.py  demo stock, prices, times, restocks
  static/index.html   the dashboard
rules.yaml / rules.default.yaml   the rulebook / its defaults      rulebook.md  readable version (generated)
data/            demo kitchen, demo overrides; (not in git) log, requests, orders, events
scripts/         demo scenarios, tests, reset
docs/            Aimo's API docs and datasets, our API description
```

## Next steps toward production

1. **A real order endpoint** on Aimo's side; our mock order service already has the shape.
2. **An MCP-style interface** on top of the same functions (search, quote, order, rules), so any agent framework can plug in.
3. **Trust layer:** signed price and stock answers from Aimo with a "held until" time (story 6), and the log's hash chain anchored externally.
4. **Real identity and roles**: company login, who may approve what, limits per person and per budget.
5. **Allergens and quality rules** (school kitchens; fresh → frozen), and brand rules per item ("no Brand B for coffee milk").
6. **Learned usual orders** from real history, with drift alerts; restock dates and delivery times from Aimo.
7. **Notifications** to the approver and the cook (email / chat).
