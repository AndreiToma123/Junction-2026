# Data notes

Status: **up to date (Sat 14 Nov 2026, evening).** Sections 1–5 come from the files in `docs/aimo_api/`
and live read-only calls (ping, availability, price). Sections 6–9 describe the data we added for the
demo kitchen, and everything that is simulated, so nobody mistakes it for Aimo data.

---

## 1. What we were given

| File (in `docs/aimo_api/`) | What it is |
|---|---|
| `openapi.json` | The API description (standard OpenAPI 3.1 format). |
| `Valio Aimo ... reference.html` | The same API description as a web page. Nothing extra in it. |
| `valio_aimo_junction26_product_data.json` | All products: 17,546 of them, 87 MB. Doesn't change during the event. |
| `valio_aimo_junction26_product_data_schema.json` | Explains every product field (standard JSON Schema format). |
| `valio_aimo_junction26_delivery_reliability.csv` | 4,425 products with a score from 0 to 1 for how reliably they arrive. |
| `valio_aimo_junction26_carbon_footprint.csv` | 11,033 products with kg CO2 per kg. Out of scope for us. |

The "try it out" page isn't needed. It shows the same API as `openapi.json`.

---

## 2. The API: only 3 live endpoints

Address: `https://junction26.valioaimo.fi`. The key goes in a header called `X-API-Key`.

| Endpoint | What it does | Changes data? |
|---|---|---|
| `GET /api/ping` | Checks that the key works. | No |
| `POST /api/availability` | For a list of products and a delivery date, returns: whether it can be delivered, **how much is available**, the **earliest date** it's available, the **order cut-off time**, and whether the product is blocked from sale. | No (it's POST but only reads) |
| `POST /api/price` | For a list of products and a date, returns the price per sales unit (with and without tax), the **price per litre or kg**, the tax rate, and the list price. | No (it's POST but only reads) |
| 3 × `GET .../download` | Download the 3 data files above. We already have them. | No |

Both live calls take: `customer` (any text; it doesn't change the answer), `requestedDate`
(YYYY-MM-DD), and `products` (1 to 1,000 product codes).

Rules from the docs:
- **Don't** ask for prices or stock of all products. First narrow the list down using the
  product file, then ask about only those few products. There are hidden rate limits.
- "Terminal" products come from the supplier's warehouse. For these, the amount always
  shows **999999**, which means "unknown", not "lots". We must treat it as unknown.
- `availableDate` can be **later** than the date we asked for. That's how we catch a missed deadline.
- `scheduleLines` is empty = the product can't be delivered on any date.

Saved live samples (in `data/samples/`): `ping.json`, `availability_milk_2026-11-16.json`,
`price_milk_2026-11-16.json`, and the same for all 35 lactose-free milks
(`availability_lf_milk_*.json`, `price_lf_milk_2026-11-16.json`).

### What the live data actually shows (important for the agent)
- **Stock numbers have 3 "special" values, not just one:**
  `999999` = Terminal product, stock unknown (as documented).
  `9999999999` and `666666666` = Stock products with **no real limit** (not documented, our reading).
  Only a few products show a real number. E.g. Arla 1 L UHT (469363) = **384**, Arla Café milk (463380) = **698**.
- **Stock and dates don't change with the date we ask for.** We asked for 16, 17 and 20 Nov
  and got the same answers. `availableDate` is the earliest delivery date (16 Nov for all milks).
- **Order cut-off:** Stock products: Sun 15 Nov 17:00 → Monday delivery is still possible.
  Terminal products: Thu 12 Nov 15:00, which **has already passed**, yet they still say "available 16 Nov".
  Our choice: trust the cut-off. A product whose cut-off has passed can't make the deadline (rule B3).
  Worth asking a mentor.
- One product (464440) comes back with no delivery lines at all, so it can't be ordered.
- **Prices** (with tax, Monday delivery). Cheapest lactose-free semi-skimmed is our usual
  Valio 464306 at **€1.15/L** (list price €1.63, so the kitchen gets a discount).
  Arla 469363 costs **€2.81/L**. Tax on milk is 13.5%.
  → 1,000 L of the usual = **€1,150**. 2,000 L = **€2,300** (over the €2,000 limit ✓).
- The API refuses Python's default client (HTTP 403). Normal clients like curl work. Our code sends its own clear client name.
- **Some answers are incomplete**: a product can come back without a delivery date, and the server sometimes
  drops a connection. The client retries calmly, reuses an answer for 20 seconds, and treats an incomplete
  answer as "can't be delivered".
- **Order steps**: most products have `allowedLotSize` 0.01 (we order whole packs), but some are real steps:
  salmon fillet in **10 kg**, a pack of chicken breast **4.4 kg**. The agent rounds up to the product's own step.
- **Weights**: `unitConversions[].netWeight` gives kg per pack for most food (13,711 products); otherwise
  we read it from the name ("2,5 kg", "500 g"). Litres the same way.
- **No restock dates and no delivery times** in the API: only the earliest delivery *day*.

### ⚠️ Big finding: there is NO search and NO ordering in the API

1. **Search**: the API can't search. We search the **product file** ourselves (by name,
   brand, labels), then ask the API for price and stock of the matches.
2. **Ordering**: the API **can't place orders**. There's no order endpoint at all.
   Ordering is **a mock order for the demo only**: our backend saves the order to a file
   and gives back an order number. Nothing is sent to Aimo. The README says so clearly.

---

## 3. Product fields we will use

| What we need | Where it is | Notes |
|---|---|---|
| Product code | `id` | Used in all API calls. |
| Name | `synkkaData.names[]` (en / fi / sv) | Example: "Valio Eila® semi skimmed milk drink 1 l lactose free". |
| Brand | `synkkaData.brand` | 82% of products have one. Spelling isn't clean: `"Arla "` (with a space), `"Arla"`, `"Arla Luomu"`, `"Arla Pro"`, `"Valio maidot"`. We clean these up into a main brand. |
| Supplier | `vendorName` | e.g. "VALIO OY TUORE", "ARLA OY". |
| Sales unit | `salesUnit` | The unit you order in. Uses codes like `ST` (single piece), `KI` (case), `PAK`, `KG`. The codes aren't explained in the data. Our reading: ST = one item, KI = a case of several. |
| Pack contents | `units[]` | How many single pieces are in each unit. Example: Valio Eila 1 L, `KI` = 5 pieces, so 1 KI = 5 L. |
| Litres per piece | `synkkaData.unitConversions[].volume` | Only about half of products have it. If it's missing, we read it from the name ("1 l", "10 l"). We record in the log which one we used. |
| Smallest order step | `allowedLotSize` | The order amount must be a multiple of this. |
| Labels / claims | `synkkaData.classifications` → `nutritionalClaim` | Example: `FREE_FROM_LACTOSE` (4,178 products). Better than looking for "lactose free" in the name. |
| Substitutes | `substitutions[]` | 250 official "use this instead" rules. Example: **Valio Eila 1 L (464306) → Arla 1 L (469363)** and 3 Valio products. Great for the split. |
| Category | `category` | A code only, no names. 10400 seems to be milk (91 products, mostly Valio and Arla). We use it to find same-kind products (substitute candidates, a new ingredient's search). |
| Temperature class | `temperatureCondition` | A code only. 7 looks like frozen. Shown to the AI when it picks substitutes (fresh vs frozen); no rule uses it yet. |
| Net weight | `synkkaData.unitConversions[].netWeight` | kg per pack, for ingredients bought by weight. |
| Delivery reliability | reliability CSV | Only 25% of products are in it. Missing = "unknown", not "bad". |

Price, stock, delivery date and cut-off time are **not** in the file. They only come from the live API.

Saved sample: `data/samples/products_lactose_free_milk.json` (7 lactose-free milks: Valio and Arla, 1 L and 10 L).

### Lactose-free milk we can use for the demo
| Code | Product | Brand | Sold as |
|---|---|---|---|
| 464306 | Valio Hyvä suomalainen Arki® Eila® semi skimmed 1 l | Valio | ST (1 L) |
| 465039 | Valio Eila® semi skimmed 1 l | Valio | KI = 5 × 1 L |
| 464398 | Valio Eila® semi skimmed 1 l UHT | Valio | ST |
| 463038 | Valio Eila® semi skimmed 10 l novobox | Valio | KI = 2 × 10 L |
| 469363 | Arla semi skimmed 1 L UHT lactose free | Arla | ST |
| 464179 | Arla 10 L Novobox semi skimmed lactose free | Arla | ST (10 L) |

---

## 4. Order history

**There isn't any.** Nothing in the API or the files shows past orders. The kitchen's "usual orders"
(product, usual amount) now live in the demo kitchen file (section 6), which replaced the first
`usual_orders.json`. Three weeks of past orders exist only as generated log lines marked "[test history]".

---

## 5. Rulebook vs. data

The rulebook is [rules.yaml](rules.yaml) (readable: [rulebook.md](rulebook.md)). How each rule matches the data:

| Rule | Data that checks it | Can we check it? |
|---|---|---|
| Spend limit | `/api/price` → `catalogPrice.valueWithTax` × amount | ✅ Yes (including tax, as you decided) |
| Approved brands | `synkkaData.brand` (after cleanup) | ✅ Yes. 18% of products have no brand (most fresh produce): the kitchen's usual product and the chef's pre-approved swaps are therefore always allowed |
| Max brands per split | count of the cleaned-up brands in the option | ✅ Yes |
| Deadline | `/api/availability` → `availableDate` + `closingTime` | ✅ Yes (Terminal products' cut-off has already passed for Monday) |
| "Much bigger than usual" | usual amount per ingredient in the demo kitchen file | ✅ Yes, using made-up usual orders |
| Enough stock | `availableAmount` | ⚠️ Real numbers only for some Stock products. Terminal = unknown → "not checked". 9999999999 / 666666666 = no limit |
| Order in allowed steps | `allowedLotSize`, `units` | ✅ Yes |
| Deposits (returnable boxes) | `deposits[]` | ⚠️ Shown, but no price for them |

Things the data **cannot** support (we must say "not checked" for these): allergens / quality
(out of scope anyway), real customer-specific prices (the customer number is ignored),
true stock for Terminal products, and confirming an order with Aimo (no order API).

---

## Decided: the split demo uses a simulated stock limit (option B)

The live data has no Valio lactose-free milk with limited stock, so for **scenario 2 only**
we use a clearly labelled file, `data/demo_stock_overrides.json`. It replaces a few stock
numbers before the rules run. Prices, dates and cut-offs stay real.

| Product | Real stock | Demo stock | Why |
|---|---|---|---|
| Valio 464306 (usual) | no limit | **600** | "only 600 L of the usual brand" |
| Valio 464398, 464201, 463779 (other Valio lactose-free semi-skimmed) | no limit | **0** | otherwise the agent would quietly top up with Valio and no approval would be needed |
| Arla 469363 | 384 | **500** | real stock (384) is just short of the 400 L needed |

Result: 600 × €1.15 + 400 × €2.81 = **€1,814.00**. That's under the limit, so the only reason
for approval is the new brand (rule C2). Every log line and summary for this scenario says
"demo stock override".

Not used: option A (making Arla the usual milk, so everything stays real data). Only applied when
"Demo options → low stock" is chosen.

---

## 6. The demo kitchen (made up, linked to real products)

`data/kitchen.default.json` (the live copy is `data/kitchen.json`, editable on the Kitchen screen):

- **11 dishes** (salmon soup, meatballs, chicken penne, mushroom risotto, beef pasta bake, oven-baked salmon,
  chicken stew, oat porridge, oven pancake, carrot soup, breakfast buffet), each with a course, portions a day
  and grams / ml per portion. Made up.
- **18 ingredients**, each linked to a real Aimo product that was deliverable from stock when checked
  (e.g. salmon fillet 462150, penne 463190), with a usual order amount, a reorder level (about two days of use),
  a starting stock and a budget category. Amounts made up; product codes, prices and Aimo stock real.
- **The chef's swap list** per ingredient: real Aimo products that can replace it in named dishes, a note on the
  trade-off, and whether it is **pre-approved** (the agent may order it alone). The list order is the chef's
  preference and is how substitutes are ranked. E.g. salmon → fresh rainbow trout 462016, frozen trout 469560,
  redfish 475684, ocean perch 475485, pangasius 475530 (all "chef decides", all deliverable by Monday when checked);
  penne → penne rigate (471450, pre-approved).
- **Weekly budgets**: dairy €4,000, dry goods €1,500, fresh produce €1,500, fish & meat €4,500.

## 7. Demo values (simulated, always labelled)

`data/demo_stock_overrides.json`. Everything here is shown with the word "demo" wherever it appears.

| What | Applies | Why |
|---|---|---|
| Low stock of the usual milk | only when "Demo options → low stock" is chosen | the split scenario (section 5) |
| A price rise at approval (464306 → €1.25) | only when "Demo options → price goes up" is chosen | shows the re-check at approval |
| Delivery times: milk 464306 and butter 464411 06:00–08:00, salmon 462150 and cheese 465256 07:00–09:00 | always | the API gives only a day; all other products say "Valio Aimo gives the day, not the time" |
| Restock: salmon 462150, +200 kg from Wed 18 Nov | always | the API gives no restock date; all other products say the date is unknown |

## 8. What the data still can't tell us

Allergens and quality are in the product data but not used (out of scope). Real customer prices
(the customer number is ignored), true stock of supplier products, restock dates, delivery times,
order confirmation: none of these exist in the mock API; the dashboard says so where it matters.

## 9. Questions for a mentor

- Supplier ("Terminal") products: is a passed order cut-off really final if `availableDate` still shows Monday?
- Do `9999999999` and `666666666` really mean "no stock limit"?
