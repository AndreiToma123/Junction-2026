# Rulebook: Demo Kitchen Oy buying agent

> **Generated from `rules.yaml`. Don't edit this file by hand.** Change the rules on the dashboard's
> Rules screen (or edit `rules.yaml`); this file is rewritten automatically after every change.
> Last generated: Sat 14 Nov 2026 18:00

The agent checks **every** option against these rules. The checks are plain code, no AI.
When a rule is not met, it gives one of these results:

| Result | What the agent does |
|---|---|
| ✅ **Pass** | Nothing. The rule is fine. |
| 🟡 **Needs approval** | Doesn't order. A person approves or rejects on the dashboard. |
| ❓ **Ask the buyer** | Doesn't order. Asks the buyer what they meant (e.g. a probable typo). |
| ⛔ **Can't be used** | This option is dropped. If no option is left, the agent says why. |

**The agent orders by itself only if every rule passes.** The worst result decides.

## A. The request (checked before searching)

### A2 - Request is clear 🔒
- The request must say product, amount, unit, deadline. If anything is missing: ask the buyer.

### A1 - Much bigger than usual
- More than 3× the kitchen's usual amount: ask the buyer. If the buyer confirms: needs approval. Item never ordered before: needs approval.

## B. Each product in an option

### B1 - Can be sold 🔒
- The product must have a price, a delivery date and no sales block. Otherwise it can't be used.

### B2 - Matches the request 🔒
- The product must have every label the request needs (e.g. lactose-free). Otherwise it can't be used.

### B3 - Arrives by the deadline 🔒
- Delivery must be on or before the deadline, and the order cut-off must not have passed. Otherwise it can't be used.

### B4 - Enough stock 🔒
- The amount can't be more than the stock. Otherwise it can't be used. Supplier products with unknown stock: allowed, with a warning.

## C. The whole order

### C4 - Allowed order steps 🔒
- Orders are in whole packs, rounded up. If that adds more than 5% to the amount asked: needs approval.

### C1 - Spend limit
- One order may cost at most €2,000.00 (including tax). Over the limit: needs approval.

### C2 - Approved brands
- Only these brands without asking: Valio, plus the kitchen's usual product for each item. Anything else: needs approval.

### C3 - Brands per split
- One order may mix at most 2 brands. More: can't be used.

🔒 = safety rule: its settings can be changed, but it can't be removed.

## D. Choosing between options
If several options can be used, the agent picks the one that:
1. needs the fewest approvals, then
2. uses the kitchen's usual product the most, then
3. is the cheapest (price incl. tax from the Valio Aimo API).

Splits are only built when no single product can cover the amount by the deadline. The usual
product is used as much as possible; the rest comes from official substitutes first.

## E. Always true
- Every order, approval, rejection, question and rule change is written to the audit log.
- Summaries only state facts from the data and the rule results.
- A person can always reject. A rejected option is never ordered.
- The AI never changes a rule by itself: it proposes, a person applies. If unsure, it asks.

## Not checked by the agent

- Allergens and food quality (out of scope)
- Customer-specific price: the mock API ignores the customer number
- Real order confirmation: this is a mock order, nothing is sent to Valio Aimo
