# Roadmap

Junction 2026 · Valio Aimo "Trusted Trade Agents" · submitted Sunday 15 Nov 2026

Live app: https://andreitoma123--valio-buying-agent-web.us-east.modal.run ·
Full description: [README.md](README.md) · How it is built: [TECHNICAL.md](TECHNICAL.md) ·
Every decision with its reason: [DECISIONS.md](DECISIONS.md) · Live test walk-through: [TEST_SCENARIOS.md](TEST_SCENARIOS.md)

---

## 1. The final product in short

A **buying agent for a professional kitchen** that works directly with Valio Aimo's (mock) systems.

- A cook writes what they need in plain words, several products and dates at once, or the kitchen's
  stock monitor or an event plan creates the request.
- The agent searches Valio Aimo's 17,546 products, asks the live API for price, stock and delivery,
  builds and ranks the options (including a split when the usual product runs short), and checks every
  option against a **rulebook written in plain code**.
- Per product it then does exactly one thing: **orders by itself** (every rule passes), **asks a person
  to approve** (spend limit, a new brand, a swap the chef hasn't pre-approved), **asks the buyer**
  (unclear or unusual), or **says it can't be done and why**.
- When something can't be sourced it offers **recipe-aware fixes**: ranked substitutes from Aimo's own
  assortment, waiting for a restock, or moving event portions to another dish.
- Price and stock are **re-checked at approval**; every step goes into a **tamper-evident,
  plain-language audit log** that can be questioned in words.
- The kitchen controls the agent's freedom on screen: rules (also in plain words, with undo), menu and
  recipes, the chef's swap list, and budgets.
- Gemini is used only for language; plain code checks every answer and everything works without it.

---

## 2. What was delivered

| Area | Status | What exists |
|---|---|---|
| Valio Aimo connection | ✅ | Our search over the product file, live price / stock / delivery from the API, mock order service |
| Rulebook | ✅ | `rules.yaml` (15 rule types, 10 default rules, 6 locked safety rules), generated `rulebook.md`, editing on screen and in plain words, undo |
| Agent flow | ✅ | Multi-product requests, options incl. split, ranking with reasons, order / approval / question / can't be done |
| Response logic | ✅ | Recipe-compatible substitutes (pre-approved → chef's list → AI fit → price), "order only the usual product", "wait for the restock" |
| Event planning | ✅ | Guests × recipes − what the kitchen can spare, event budget, recipe fix that moves portions to another dish |
| Kitchen | ✅ | Menu (11 dishes), 18 ingredients, stock, automatic restock with duplicate guard, budgets per category and per event |
| Audit trail | ✅ | JSON Lines with a SHA-256 chain, search, "Ask the log", download |
| Trust | ✅ | Re-check at approval, unknowns shown as unknown, signed-quote proposal for Aimo (TECHNICAL.md §6) |
| Dashboard | ✅ | One HTML page: New request, Requests, Events, Kitchen, Audit log, Rules; dark mode; works on a phone |
| Deployment | ✅ | Modal, persistent volume, reset and clear protected by a demo password |
| Tests | ✅ | 44 flow checks, 31 AI-safety checks, 9 rule cases, 4 scenarios; all run in a sandbox |

### The eight stories

| Story | Covered? | Notes |
|---|---|---|
| 1 Normal Tuesday | 🟢 Yes | Several products in one request, kg units, delivery message (some delivery times are demo data) |
| 2 Not enough milk / salmon | 🟢 Yes | Explained split, ranked substitutes, wait for the restock (restock date is demo data) |
| 3 Hidden allergen | 🔴 No | Not built; the product data has allergen fields, so it is possible |
| 4 Big event | 🟢 Mostly | Quantities from recipes, event budget, recipe fix. Missing: editing the cart while approving |
| 5 Cheap swap (fresh → frozen) | 🔴 No | No quality rule yet; the data has a temperature class per product |
| 6 Price changed | 🟢 Yes | Re-check at approval; a changed price stops the order |
| 7 Typo | 🟢 Yes | 10× the usual amount: asks first, orders nothing |
| 8 The log, three weeks later | 🟢 Yes | "Ask the log" answers with the exact lines. Missing: a brand rule for one item only |

---

## 3. What comes next

**Pilot-ready (weeks)**
1. Real order endpoint at Valio Aimo (our mock service already has the shape).
2. Signed quotes with a valid-until time; order by quote id.
3. Company login and roles, approval limits per person, budgets that can block.
4. Notifications to approvers and cooks instead of watching the dashboard.

**Trust and breadth (months)**
5. MCP-style interface over the same functions, so any agent framework can plug in.
6. Allergen-safe substitution (story 3): hard blocks from structured allergen data.
7. Quality rules (fresh → frozen, organic, origin) and brand rules per item.
8. Usual orders learned from real history, with drift alerts.

**Longer term**
9. Several suppliers behind the same protocol; Aimo's own selling agent proposing products within the buyer's rules.
10. External anchoring of the audit chain for dispute-proof records.

Details for each step: [TECHNICAL.md](TECHNICAL.md) §9.

---

## 4. Known limits

- Orders, the demo kitchen, some delivery times and one restock date are mock or demo data, labelled "demo" in the app.
- No real login: a typed name goes into the log. On the live app anyone with the link can approve or change rules; only reset and clear need the password.
- The reading of the supplier cut-off (cut-off passed but "available Monday") is our interpretation, to confirm with Valio Aimo.
