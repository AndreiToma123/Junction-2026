# Live test scenarios

Run these on the dashboard (http://localhost:8000) to check the behaviour end to end with live
Valio Aimo data. Each one says what to do and what you should see. Amounts and prices come from the
live mock API, so small differences are possible.

**Timing:** Aimo's order cut-off for Monday delivery is **Sun 15 Nov 17:00**. After that, "by Monday"
can't be met (the agent will say so, which is correct). Then use "by Tuesday" or a later date instead.

---

## 0. Set up (once, ~1 minute)
1. Type your name in **"You are"** (top right).
2. **Rules** → scroll to **Start over** → tick *Add the 3 weeks of example history* → **Reset everything** → type `RESET`.
3. Check: **Requests** is empty; **Audit log** shows "Log intact" and the [test history] lines.

## 1. Normal order: the agent orders alone
**New request:** `I need 1,000 litres of lactose-free milk by Monday.`
- ✅ Green **Ordered**: MOCK-0001, 1,000 L, **€1,150.00**.
- ✅ Message: *"Arrives Mon 16 Nov, 06:00–08:00 (demo delivery time)"*.
- ✅ Requests → Ordered → open it: **How the agent ranked the options** lists ~4 options; #1 "Needs no approval, uses the usual product, cheapest". Budget tile: *Dairy, this week*.

## 2. Several products in one request (story 1)
**New request:** `Usual dairy order, plus 20 kg extra butter for the weekend by Wednesday`
- ✅ "5 products · one request each": milk, butter, cooking cream, eggs, cheese, **all Ordered**.
- ✅ Butter is **60 kg** (usual 40 + 20 extra).
- ✅ Milk / butter / cheese show a demo delivery time; cream and eggs say *"Valio Aimo gives the day, not the time"*.

## 2b. Several products with different dates (and a typo)
**New request:** `Usual dairy order, plus 20 kg extra butter by Wednesday, 500 L milk, 60 kg salmon and some potatoes by Thrusday`
- ✅ 8 products. The dairy items (and the extra butter) are for **Wed 18 Nov**; the 500 L milk, salmon and potatoes for **Thu 19 Nov** ("Thrusday" is understood).
- ✅ The top line counts them, e.g. *"7 ordered · 1 needs your answer"*, and **Needs you** lists the potatoes first, with a *How much* box right there. Type `150` → **Continue** → ✅ *"8 ordered"*.

## 3. Mixed results in one request
**New request:** `500 L lactose-free milk, 60 kg salmon and some potatoes by Monday`
- ✅ Milk **Ordered**, salmon **Needs approval**, potatoes **Needs your answer** ("how much").
- Open the potatoes request → type `150` → **Continue** → ✅ Ordered.

## 4. Supplier stock-out: three choices (story 2)
Open the **salmon** request from scenario 3 (Requests → Needs approval).
- ✅ **What needs deciding** starts with *"Split order: Valio Aimo has only 43 kg of the usual salmon… 20 kg with rainbow trout"*, then the reason (trout isn't pre-approved).
- ✅ Order table: 40 kg salmon + 20 kg trout. Ranking table: the split is #1; "60 kg salmon" is #2 *Can't be used: only 43 in stock*.
- ✅ **What needs deciding**: the situation in plain text, **the reason in bold**.
- ✅ **Or instead** offers: *Order only the usual product (40 kg)*, *Wait for the usual product: order all 60 kg for Wed 18 Nov* (demo restock), *Show recipe-compatible substitutes*: **five** fish, ranked in the chef's order (fresh trout, frozen trout, redfish, ocean perch, pangasius), each with its trade-off and "Why #n".
- Click **Wait for the usual product** → ✅ new request **Ordered** for Wed 18 Nov, stock check says *"includes a demo restock"*.

## 5. Spend limit + price re-checked at approval (story 6)
**New request**, open **Demo options** → *the price goes up before approval* → `I need 2,000 litres of lactose-free milk by Monday.`
- ✅ **Needs approval**: €2,300.00 is €300.00 over the €2,000 limit. Budget tile shows dairy left before / after.
- Click **Approve** → ✅ *"Price changed · please look again"*: €1.15 → €1.25, total now €2,500.00, **nothing ordered**.
- Click **Approve** again → ✅ Ordered.

## 6. Typo guard (story 7)
**New request:** `I need 10,000 litres of lactose-free milk by Monday.`
- ✅ **Needs your answer**: 10× the usual 1,000 L. Nothing searched, nothing ordered.
- Click **Change to 1,000 L** → ✅ Ordered, without being asked again.

## 7. The agent doesn't understand: no dead end
**New request:** `we need some stuff by friday`
- ✅ It asks with a form: **Which product** (a list), **How much**. Pick *Butter*, `20` → **Continue** → ✅ Ordered.

**New request:** `I want as much salmon as possible by Monday`
- ✅ It never buys blindly: *"Valio Aimo can deliver up to 40 kg … in steps of 10 kg. How much do you need?"*

## 8. The chef decides what the agent may do alone (swap list)
1. **Kitchen** → *Salmon fillet* → **Edit** → **Recipe swaps** → tick **Pre-approved** on Rainbow trout → **Save swaps**.
2. **New request:** `I need 60 kg of salmon by Nov 16th`
- ✅ **Ordered by the agent alone** (40 kg salmon + 20 kg trout). Rule C2 says *"462016 is on the chef's pre-approved swap list"*.
3. Untick **Pre-approved** again and save (so other tests behave as described).
- ✅ Audit log: *"Recipe swaps for Salmon fillet changed by …"*.

## 9. Automatic restocking + duplicates
**Kitchen** → **Simulate a day of service** (wait ~20 s) → repeat once or twice.
- ✅ Banner: which ingredients reached their reorder level and what happened (ordered / needs approval).
- ✅ Ingredients with an order on the way show **On its way**; they are **not** ordered again.
- Click **Receive deliveries** → ✅ stock goes up. Automatic requests that are no longer needed become **Closed: not needed** (Requests → Rejected / cancelled). Typed requests are only marked *Possible duplicate*.

## 10. Last-minute event (challenge scenario)
**Events** → *Describe it:* `150 extra guests on Monday: salmon soup and meatballs` → **Fill in the form**.
- ✅ Date, 150 guests and both dishes are filled in. Add **Event budget** `3000`.
- **+ Add a dish** twice (e.g. *Salmon pasta*: salmon 100 g + penne 90 g; *Veggie risotto*: risotto rice 85 g). Go to **Kitchen** and back → ✅ the whole form, both extra dishes included, is still there.
- **Plan the event** → ✅ a table per ingredient (needs / kitchen can spare / ordered), the **event cost against the 3,000 budget**, orders charged to the event.
- Plan a big one: `600 guests on Monday: salmon soup` → ✅ Salmon **Waiting for approval** or **Can't be sourced in time** → if can't: **Find a fix** → ✅ *"you can make N of 600 portions"* + **Move … portions** to another main → the event re-plans. Leave and come back → ✅ the fix is still shown.

## 11. Menu editing
**Kitchen** → **+ Add an ingredient** → name `Shrimps`, kg, search `shrimp` → pick one → usual order 10, reorder at 3 → **Add ingredient**.
→ **Menu** → **+ Add a dish** → *Shrimp pasta*: Shrimps 80 g, Penne 90 g, 10 a day → **Add to the menu**.
- ✅ *Penne pasta* → **Edit** → one place to change the grams in every dish that uses it.
- ✅ Removing *Shrimps* is refused while *Shrimp pasta* uses it.
- ✅ Audit log shows each change with your name.

## 12. Ask the log (needs the example history from step 0)
**Audit log** → `Why did the milk taste different the past 3 weeks?`
- ✅ Two deliveries were different: 31 Oct (part Arla) and 7 Nov (Valio UHT), each linked to its log line. ✅ It says the log can't tell taste.

## 13. Rules in plain words
**Rules** → *Tell the agent what to change:* `raise the spend limit to 2,500` → ✅ before/after preview → **Apply** → ✅ in the change history → **Undo this change**.
`stop buying Valio` → ✅ it asks which you mean instead of guessing.

## 14. Requests screen
- ✅ Opens on **Needs approval**. Cancel a request there → ✅ you stay in Needs approval.
- ✅ **All** folds finished requests away. **Clear requests** (type `CLEAR`) empties the lists but keeps orders, stock, rules and the log.
- ✅ The **Dark** button (moon) switches to dark mode; it then shows **Light** (sun).
