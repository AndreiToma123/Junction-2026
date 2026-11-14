# Technical notes: design choices, trust protocol, next steps

Companion to [README.md](README.md) (what it does, how to run it). This document explains **how** it is built,
**why** each choice was made, how it answers the four problems in the challenge, and **what comes next**
on the way to production. Every decision, with its reason, is listed in [DECISIONS.md](DECISIONS.md);
the data findings in [DATA_NOTES.md](DATA_NOTES.md).

---

## 1. Architecture

```
 outside agent / dashboard ──HTTP+JSON (OpenAPI 3.1)──► FastAPI backend (app/main.py)
                                                            │
     request reader (parse.py, Gemini via ai.py) ──► agent.py (the flow) ──► rules.py + rule_types.py (plain code)
                                                            │                    ▲ rules.yaml (versioned, editable)
          catalog.py (our search over Aimo's product file)  │
          aimo.py (Valio Aimo API: availability, price; read-only, retries, caching)
          kitchen.py / events.py / substitutes.py / budgets.py (kitchen context)
          orders.py (mock order service)          log.py (audit log, JSON Lines, SHA-256 chain)
```

- **One process, files on disk, no database.** Every state file is human-readable JSON / JSON Lines / YAML in `data/`.
- **The dashboard is a client like any other**: it only uses the public API, so an outside buying agent can do
  everything the dashboard does. The API description is generated (`/openapi.json`, `docs/agent_api_openapi.json`).
- **Tests run in a sandbox** (`scripts/_sandbox.py`): a throw-away copy of the state and the default rules.

## 2. Technical choices and why

| Choice | Why |
|---|---|
| **Decisions in plain code, AI only for language** | Guardrails must be predictable, testable and explainable. An LLM may *read* a request, *word* a summary, *propose* a rule change or *pick* substitutes from a pre-checked list, but never decides whether an order is allowed. |
| **Every AI answer is checked by code** | Amounts must be real numbers written in the text; summaries may only use numbers from the data; cited log lines must exist; rule proposals go through the same validation as manual edits; substitutes must be on the checked list. If a check fails, the plain-code fallback is used. |
| **Works without AI** | The whole flow runs in "simple mode" with no key: no single external service can stop ordering. |
| **Rulebook as YAML with typed rules** (`rules.yaml`, 15 rule types) | Readable by a manager, versioned, diffable, validated before saving; `rulebook.md` is generated so the two never disagree. New rule types are one entry in `rule_types.py`. |
| **Worst result decides** (blocked > ask > approve > pass) | Simple, predictable, easy to explain. |
| **Search over the product file, live API only for matches** | The API has no search and asks not to be queried for everything (rate limits, customer-specific prices). Typically 14–60 products per request. |
| **Live re-check at approval** | An approval given at 11:30 must not buy at a price checked at 09:00. |
| **Hash-chained JSON Lines log** | Append-only, readable in any editor, tamper-evident without extra infrastructure. |
| **Mock order service with the shape of a real one** | The mock API has no order endpoint; ours returns an order id, lines, totals, delivery date, budget and who placed it. |
| **Frugal Gemini use** (free tier) | Call only when needed, cache identical questions, per-minute budget, cool-down after "too many requests". |
| **No framework, no build step for the UI** | One HTML file: easy to read, runs anywhere, nothing to install. |

## 3. Guardrails model: who decides what

| The agent decides alone | A person signs off | The buyer is asked |
|---|---|---|
| Usual product, within the spend limit, arrives in time, enough stock, whole packs | Over the spend limit (€2,000 incl. tax per order) | Something missing: product, amount, unit, date (never guessed) |
| A split with an **approved brand** or a **swap the chef pre-approved** | A brand that isn't approved, or a swap that is on the chef's list but not pre-approved, or not on it at all (*substitutions outside stated preferences*) | An amount far above the usual (> 3×); "as much as possible" (shows what Aimo has, asks for a number) |
| Closing its own automatic restock requests once they are no longer needed | Rounding that adds > 5%; a confirmed unusual amount; any item never bought before | Amounts the AI read from words ("a thousand litres") |

- **Safety rules are locked** (request clear, can be sold, matches the request, arrives in time, stock, whole packs).
- **Autonomy is the kitchen's choice**, on screen: the spend limit, approved brands, and per ingredient which recipe
  swaps are pre-approved. Every change is logged with who made it, and can be undone.
- **Budgets** (weekly per category, per event) are shown to the approver before and after the order.

## 4. Response logic: partial availability and good-better-best

1. **Single-product options** for every matching product (only those whose pack size is known), in whole packs.
2. **Split only if no single product can cover the amount by the deadline**: the usual product first (all of its stock,
   in its own order step), then partners in order: Aimo's own substitute links and the chef's swap list first, then price.
3. **Every option is checked by every rule**, then **ranked**: fewest approvals → most of the usual product → lowest
   price; blocked options last with what blocked them. Each option keeps its rank and a one-line reason.
4. **When something is short**, the person gets, in this order: the situation and the exact reason (bold) → approve /
   reject → *order only the usual product* → *wait for the restock* (when a date is known) → **recipe-compatible
   substitutes**, ranked: pre-approved swaps → the chef's list in the chef's order → AI picks by fit → price.
   Only products deliverable in time, in stock, priced and in a sensible pack size are offered.
5. **Events** work at recipe level: guests × recipes − what the kitchen can spare by then = what to order. If an
   ingredient can't be sourced, the fix says how many portions can still be made and offers to move the rest to
   another dish of the same course; the event is then re-planned automatically.

## 5. Audit trail

- **One line per step**: time, actor (agent / stock monitor / named person / buyer), request id, step, plain-language
  message, source of the fact (product file, Aimo API, rulebook, dashboard, demo data), and the exact numbers used.
- **Covers everything**: requests, lookups, options, rule results, choices, approvals and rejections, orders, re-checks,
  deliveries, rule changes (before/after), menu, budget and swap-list changes, resets.
- **Tamper-evident**: each line holds the SHA-256 of the previous one; `/api/log/verify` (the "Log intact" badge)
  detects any edited or deleted line.
- **Usable for both sides**: the buyer searches or asks in plain words ("why did the milk taste different?") and gets
  an answer citing the exact lines; Valio Aimo can read the same file (JSON Lines) to see why its products were or
  weren't chosen.

## 6. Trust protocol

### What the agent already does
- **Never trusts a cached fact for a commitment**: price and stock are fetched live for the decision and **re-fetched
  at approval**; a changed price stops the order until a person approves again.
- **Unknown is shown as unknown**: supplier stock (`999999`), missing delivery dates, passed cut-offs, missing restock
  dates and delivery times are flagged, never guessed. "Not checked by the agent" is stored with every request.
- **Every fact carries its source** in the log (Aimo API with timestamp, product file, demo data).

### Proposed minimum standard for Valio Aimo (so an agent needs no manual check)
A **signed quote** per product and delivery date, returned by the availability/price API:

| Field | Purpose |
|---|---|
| `quote_id`, `issued_at`, **`valid_until`** | the price and stock are *held* until this time (story 6) |
| `gtin` (GS1) and Aimo product code | unambiguous product identity |
| `price_ex_tax`, `tax_rate`, `currency`, `unit`, `content_per_unit` | the exact money and quantity being promised |
| `available_amount` + `stock_basis` (`counted` / `supplier_estimate` / `unknown`) | replaces the magic numbers (999999, 9999999999) |
| `delivery_date`, `delivery_window`, `order_cutoff` | what the agent can promise the kitchen |
| `restock_date` (optional) | lets the agent offer "wait for the restock" honestly |
| `origin` (country, GS1 Synkka reference), `certificates` | origin and label claims, verifiable against GS1 data |
| `signature` | Aimo's signature over all fields: JWS (RFC 7515), or a W3C Verifiable Credential |

The agent then **verifies the signature and `valid_until`**, orders by referencing the `quote_id` (no re-pricing
surprises), and stores the quote hash in its audit log, so both parties can later prove what was promised.

## 7. The interface for outside agents (selection)

| Purpose | Endpoint |
|---|---|
| Search / product facts | `GET /api/products/search`, `GET /api/products/{id}` |
| Live quote (price, stock, delivery) | `POST /api/quote` |
| Ask the agent to buy (one or several products) | `POST /api/requests` → status: ordered / needs_approval / ask_buyer / cannot_fulfil |
| Answer, approve, reject, cancel, alternatives | `POST /api/requests/{id}/answer · /decision · /cancel · /usual-only · /substitute · /wait-for-restock` |
| Rules (read, preview, apply, undo, propose in words) | `GET /api/rules`, `POST /api/rules/preview · /apply · /propose` |
| Audit log (search, verify, ask, download) | `GET /api/log`, `GET /api/log/verify`, `POST /api/log/ask`, `GET /api/log/download` |

Full description: `docs/agent_api_openapi.json` (46 endpoints), live at `/docs`.

## 8. Known limits (honest list)

- Orders, kitchen stock, menu, usual orders, delivery times and restock dates are **mock or demo data** (labelled).
- No real login or roles: a typed name is required for approvals and changes.
- Allergens, quality rules (fresh → frozen), per-item brand rules ("no Brand B for coffee milk") are not built.
- Budgets inform but don't block; one global spend limit.
- The supplier cut-off reading (cut-off passed but "available Monday") is our interpretation, to confirm with Aimo.

## 9. Next development steps

**Next weeks: pilot-ready**
1. Real order endpoint at Valio Aimo; our mock order service already has the shape (lines, totals, delivery, who placed it).
2. Signed quotes with `valid_until` (section 6); order by `quote_id`.
3. Company login and roles: who may approve what, approval limits per person and per budget; budgets that can block.
4. Notifications to approvers and cooks (email / Teams / push) instead of watching the dashboard.

**Next months: trust and breadth**
5. **MCP-style interface** over the same functions (search, quote, request, approve, log), so any agent framework can plug in.
6. Allergen-safe substitution (school / hospital kitchens): hard blocks from structured allergen data, AI only adds caution.
7. Quality rules (fresh → frozen, organic, origin), per-item brand rules, sustainability and delivery-reliability ranking.
8. Learned usual orders from real history with drift alerts (portions creeping up, cost drifting) before auto-renewal.

**Longer term: marketplace**
9. Several suppliers behind the same protocol; Valio Aimo's own selling agent proposing seasonal products to the
   buyer's agent, within the buyer's rules.
10. External anchoring of the audit chain (e.g. a periodic hash published to both parties) for dispute-proof records.
