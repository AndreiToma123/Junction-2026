# Trusted Trade Agent: a buying agent for a professional kitchen

Junction 2026 · Valio Aimo challenge "Trusted Trade Agents"

A cook types **"I need 1,000 litres of lactose-free milk by Monday"**. The agent finds the products
in Valio Aimo's catalogue, asks Aimo's (mock) systems for live price, stock and delivery, builds
options (including a **split** across two products when one runs short), and checks every option
against a **rulebook written in plain code**. Then it does exactly one of four things:

| The agent… | When |
|---|---|
| ✅ **orders by itself** | every rule passes |
| 🟡 **asks a person to approve** | something is outside its authority (over the spend limit, a new brand…) |
| ❓ **asks the buyer** | the request is unclear or looks like a typo (10× the usual amount) |
| ⛔ **says it can't be done, and why** | no option can arrive in time, in stock, within the hard rules |

Every step is written to a **tamper-evident audit log** in plain language, and people can change
the rules from the dashboard without touching code.

---

## Run it

You need Python 3.10+ and the Valio Aimo API key.

1. Copy `.env.example` to `.env` and put the key after `AIMO_API_KEY=`.
   (Optional: add a Google Gemini key after `GEMINI_API_KEY=`. Without it the app runs in "simple mode".)
2. Put the product file from the Aimo portal at `docs/aimo_api/valio_aimo_junction26_product_data.json`
   (87 MB, not stored in git).
3. Start it:

```bash
./run.sh
```

4. Open **http://localhost:8000**. Type your name in the top bar (it goes into the audit log).

### The four demo scenarios

```bash
.venv/bin/python -m scripts.reset_demo --rules   # clean start, default rules, 3 weeks of test history
.venv/bin/python -m scripts.run_scenarios     # all four; or add: normal | split | spend | typo
```

| Scenario | What the buyer types | What happens |
|---|---|---|
| `normal` | 1,000 L lactose-free milk by Monday | All rules pass → **orders by itself** (€1,150.00) |
| `split` | same, with only 600 L of the usual Valio in stock *(simulated)* | Splits 600 L Valio + 400 L Arla → **needs approval**: new brand |
| `spend` | 2,000 L … | €2,300.00 is over the €2,000 limit → **needs approval** |
| `typo` | 10,000 L … | 10× the usual → **asks the buyer**, orders nothing |

Each request then appears on the dashboard, where you approve, reject or answer it. You can also
type the same sentences on the dashboard yourself (for `split`, tick "Demo options").

Other checks you can run (they use a sandbox copy, so your data and rules are never touched):

```bash
.venv/bin/python -m scripts.try_rules         # 9 rule cases with live data
.venv/bin/python -m scripts.try_rule_edits    # editing the rulebook: preview, apply, refuse, undo
.venv/bin/python -m scripts.try_ai            # 31 checks on the AI safety net (no Gemini key needed)
```

---

## The dashboard (4 screens)

1. **New request**: type what you need, or tap "Your usual". Watch the agent's steps, then a clear result.
2. **Requests**: what needs action, with summary boxes (decision, reason, total, delivery), the order,
   each rule's result, what was *not* checked, other options considered, and Approve / Reject.
3. **Audit log**: search by words, request, person or date; ask in your own words
   ("why did the milk taste different the past 3 weeks?") and get an answer that links to the exact log lines.
4. **Rules**: every rule in plain words. Edit, add, remove, undo; or tell the agent what to change
   ("never buy Arla"). The agent only *proposes*: you see a before/after and press Apply.

---

## How it works

```
 request text ──► read request ──► check request ──► search catalogue ──► live price, stock, delivery
                  (Gemini or       (too big? unclear?)  (our own search)    (Valio Aimo mock API)
                   pattern reader)        │
                                          ▼
       ask the buyer ◄── unusual/unclear            build options (single product, or a split)
                                                              │
                                                              ▼
                                      check EVERY option against the rulebook (plain code)
                                                              │
                         ┌──────────────────────┬─────────────┴───────────┬──────────────────────┐
                         ▼                      ▼                         ▼                      ▼
                   all rules pass        needs a person             nothing usable
                   place mock order      wait on the dashboard      explain which rules blocked
                         │                      │ approve / reject
                         └──────────► audit log (one line per step) ◄──────┘
```

### Guardrails: what the agent may decide alone

The rulebook is `rules.yaml` (the readable version, `rulebook.md`, is generated from it).
The worst result decides: can't be used > ask the buyer > needs approval > pass.

| Rule | If not met |
|---|---|
| A2 - Request is clear 🔒 | ask the buyer (never guesses an amount or date) |
| A1 - Much bigger than usual (over 3×) | ask the buyer; if confirmed, still needs approval |
| B1 - Can be sold 🔒 · B2 - Matches the request 🔒 · B3 - Arrives by the deadline 🔒 · B4 - Enough stock 🔒 | option can't be used |
| C4 - Whole packs, rounding adds at most 5% 🔒 | needs approval |
| C1 - Spend limit €2,000 incl. tax | needs approval |
| C2 - Approved brands (Valio) | needs approval |
| C3 - At most 2 brands in one order | option can't be used |

🔒 = safety rule: can be tuned, not removed. More rule types can be added from the dashboard:
avoid brands, avoid products, max price per litre, minimum delivery reliability, required labels.

**Where AI is used, and where it is not.** Rule checks, option building and the decision are plain
code. Gemini does four language jobs only: reading the request, wording the summary, answering
questions about the log, and proposing rule changes. Plain code checks every AI answer before use
(amounts must be written in the text, summaries may only use numbers from the rule results, log
answers must cite lines that exist, rule proposals go through the same validation as manual edits).
If the AI is unsure, it asks. Without a Gemini key everything still works through plain-code fallbacks.

### The audit log

`data/log.jsonl`, one line per step: time, who (agent / named person / buyer), request, what happened
in plain words, where the fact came from, and the exact numbers used. Lines are only added. Each line
carries a SHA-256 fingerprint of the line before it, so any later edit or deletion is detected
(`GET /api/log/verify`, shown as "Log intact" on the dashboard).

---

## Open standards and formats

| What | Standard / format |
|---|---|
| Our agent API (how an outside agent would talk to this) | **OpenAPI 3.1** over HTTP + JSON. Live at `/docs` and `/openapi.json`, saved in `docs/agent_api_openapi.json` |
| Valio Aimo mock API | OpenAPI 3.1 (`docs/aimo_api/openapi.json`) |
| Product data | JSON with a **JSON Schema** (2020-12); products identified by **GS1 GTIN**, data from GS1 Synkka |
| Rulebook | **YAML** (`rules.yaml`), human-readable, versioned, with generated Markdown |
| Audit log | **JSON Lines**, with a **SHA-256** hash chain |
| Dates, money, countries | ISO 8601, ISO 4217 (EUR), ISO 3166 |
| Dashboard | plain HTML, CSS and JavaScript, no build step |

No proprietary formats: every file opens in a text editor.

---

## What is real, what is mock

| Part | Status |
|---|---|
| Product catalogue, prices, stock, delivery dates, cut-off times, substitutes | **Real answers from the Valio Aimo mock API / data files** (itself synthetic data) |
| Placing orders | **Mock, ours.** The Aimo API has no order endpoint. Orders are saved to `data/orders.jsonl` as MOCK-0001… Nothing is sent to Valio Aimo |
| Order history ("usual orders") and the 3 weeks of past log lines | **Generated test data**, labelled as such (`data/usual_orders.json`, "[test history]") |
| Low stock in the `split` scenario | **Simulated** (`data/demo_stock_overrides.json`), labelled "demo stock" in the log, the summary and the dashboard. Prices and dates stay real |
| Login | None. People type their name; approvals and rule changes are refused without one |

## Assumptions

All 34 decisions, with reasons, are in **[DECISIONS.md](DECISIONS.md)**. Progress and what could come next: **[roadmap.md](roadmap.md)**. The ones that matter most:

- **Stock numbers.** `999999` on a supplier ("Terminal") product means *unknown* (documented by Aimo).
  `9999999999` and `666666666` on stocked products mean *no limit* (our reading, not documented).
- **Cut-off times are trusted.** Supplier products show an order cut-off that has already passed
  (Thu 12 Nov) yet still report "available Mon 16 Nov". We block them.
- **Whole packs only**, rounded up. The data allows steps of 0.01 of a pack.
- **Litres per pack** come from the product data when present, otherwise from the product name. We
  checked them against Aimo's own price-per-litre figures for all lactose-free milks.
- **Search is ours.** The API has no search, and its docs ask us not to query all products, so we search
  the product file and ask the API only about the matches (14 products for the milk request).
- **Spend limit includes tax.**

What the agent does **not** check (saved with every request): allergens and food quality,
customer-specific prices (the mock API ignores the customer number), real order confirmation.

More detail on the data: [DATA_NOTES.md](DATA_NOTES.md).

---

## Project layout

```
app/
  main.py        the backend (FastAPI): all endpoints
  agent.py       the flow: request → options → rules → order / approval / question
  rules.py       the rule engine           rule_types.py   every rule type (check + plain description)
  rule_edit.py   safe rulebook editing     rulebook.py     writes rulebook.md from rules.yaml
  catalog.py     product search            aimo.py         Valio Aimo API client (read-only)
  orders.py      mock order service        log.py          audit log with hash chain
  ai.py          the 4 AI jobs + checks    llm.py          Gemini connection
  parse.py       simple request reader     demo.py         simulated stock for the split scenario
  static/index.html   the dashboard
rules.yaml       THE rulebook              rulebook.md     readable version (generated)
scripts/         demo scenarios and test scripts
data/            usual orders, demo stock, and (not in git) log, requests, orders
docs/            Aimo's API docs and datasets, our API description
```

## Next steps toward production

1. **A real order endpoint** on Aimo's side; our mock order service already has the shape (lines, delivery date, who placed it).
2. **An MCP-style interface** on top of the same functions (search, quote, order, rules), so any agent framework can plug in.
3. **Trust layer:** signed price and stock answers from Aimo (e.g. verifiable credentials), so an agent can rely on them without a manual check; anchor the log's hash chain externally.
4. **Real identity and roles**: company login, who may approve what, approval limits per person.
5. **More units and categories** (kg, pieces), allergen and nutrition rules, per-customer prices.
6. **Learned "usual orders"** from real order history, with drift alerts.
7. **Notifications** to the approver (email / chat) instead of watching the dashboard.
