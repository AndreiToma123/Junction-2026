"""Our backend (FastAPI). Run: .venv/bin/uvicorn app.main:app --reload

Interactive docs (open standard: OpenAPI) at http://127.0.0.1:8000/docs
"""
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import agent, ai, aimo, catalog, config, events, kitchen, log, orders, rule_edit, store

app = FastAPI(
    title="Trusted Trade Agent: buying agent backend",
    description="Product search (our own, over Aimo's product file), live price and stock "
    "(Aimo mock API), and mock orders (ours, demo only).",
    version="0.1.0",
)


@app.get("/api/health")
def health():
    try:
        return {"ok": True, "aimo": aimo.ping()}
    except aimo.AimoError as e:
        return {"ok": False, "aimo": str(e)}


@app.get("/api/products/search")
def search(
    q: str = Query("", description="Words that must all be in the product name (en or fi)"),
    label: List[str] = Query([], description="Labels the product must have, e.g. FREE_FROM_LACTOSE"),
    category: Optional[str] = None,
    brand: Optional[str] = None,
    limit: int = 50,
):
    hits = catalog.search(q, label, category, brand, limit)
    return {"count_returned": len(hits), "products": hits}


@app.get("/api/products/{product_id}")
def product(product_id: str):
    p = catalog.get(product_id)
    if not p:
        raise HTTPException(404, "Unknown product")
    return p


class QuoteRequest(BaseModel):
    products: List[str] = Field(..., min_length=1, max_length=1000)
    delivery_date: str = Field(..., examples=["2026-11-16"])


@app.post("/api/quote")
def quote(req: QuoteRequest):
    try:
        return aimo.quote(req.products, req.delivery_date)
    except aimo.AimoError as e:
        raise HTTPException(502, str(e))


class OrderLine(BaseModel):
    product_id: str
    quantity: float
    price_per_unit_with_tax: Optional[float] = None


class OrderRequest(BaseModel):
    lines: List[OrderLine]
    delivery_date: str
    placed_by: str = "agent"
    request_id: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/orders")
def place_order(req: OrderRequest):
    try:
        return orders.place_order(
            [l.model_dump() for l in req.lines], req.delivery_date, req.placed_by, req.request_id, req.note
        )
    except orders.OrderError as e:
        raise HTTPException(422, str(e))


@app.get("/api/orders")
def list_orders():
    return orders.list_orders()


# ---------------------------------------------------------------- the agent

class BuyRequest(BaseModel):
    text: str = Field(..., examples=["I need 1,000 litres of lactose-free milk by Monday."])
    scenario: Optional[str] = Field(None, description="Demo only: 'split' uses simulated stock")


@app.post("/api/requests")
def new_request(req: BuyRequest):
    return agent.handle_text(req.text, scenario=req.scenario)


@app.get("/api/requests")
def list_requests(full: bool = False):
    """All requests, newest last. Light versions unless ?full=true (open one request for everything)."""
    recs = store.all_records()
    return recs if full else [store.summary(r) for r in recs]


@app.get("/api/requests/{rid}")
def get_request(rid: str):
    from . import budgets
    rec = store.get(rid)
    if not rec:
        raise HTTPException(404, "Unknown request")
    if rec.get("kind") == "basket":
        return agent.basket_view(rec)
    chosen = next((o for o in rec.get("options") or [] if o["option_id"] == rec.get("chosen")), None)
    pending = rec.get("status") == "needs_approval"
    rec["budget"] = budgets.status(rec.get("wallet") or budgets.wallet_for(rec), chosen["total_with_tax"] if chosen and pending else 0)
    return rec


class Decision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    by: str = Field(..., min_length=1, examples=["kitchen manager"])


@app.post("/api/requests/{rid}/decision")
def decide(rid: str, d: Decision):
    try:
        return agent.decide(rid, d.decision, d.by)
    except ValueError as e:
        raise HTTPException(409, str(e))


class ByWhom(BaseModel):
    by: str = Field(..., min_length=1)


@app.post("/api/requests/{rid}/usual-only")
def usual_only(rid: str, body: ByWhom):
    """Order only what Valio Aimo has of the usual product (a new request, checked by all the rules)."""
    try:
        return agent.usual_only(rid, body.by)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/requests/{rid}/substitutes")
def request_substitutes(rid: str):
    """Recipe-compatible substitutes for a kitchen ingredient that could not be (fully) ordered."""
    from . import substitutes
    rec = store.get(rid)
    req = (rec or {}).get("parsed") or {}
    if not req.get("item_id") or not req.get("amount") or not req.get("deadline"):
        raise HTTPException(409, "Substitutes are only for the kitchen's own ingredients, with an amount and a date.")
    try:
        return substitutes.for_request(rec)
    except (ValueError, aimo.AimoError) as e:
        raise HTTPException(409, str(e))


class SubstitutePick(BaseModel):
    product_id: str
    by: str = Field(..., min_length=1)


@app.post("/api/requests/{rid}/substitute")
def order_substitute(rid: str, body: SubstitutePick):
    try:
        return agent.order_substitute(rid, body.product_id, body.by)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/requests/{rid}/wait-for-restock")
def wait_for_restock(rid: str, body: ByWhom):
    try:
        return agent.wait_for_restock(rid, body.by)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/requests/{rid}/cancel")
def cancel_request(rid: str, body: ByWhom):
    """Cancel a request that is waiting, or a mock order that has not been delivered yet."""
    try:
        return agent.cancel(rid, body.by)
    except (ValueError, orders.OrderError) as e:
        raise HTTPException(409, str(e))


class BuyerAnswer(BaseModel):
    confirm: bool = False
    new_amount: Optional[float] = None
    new_deadline: Optional[str] = Field(None, description="YYYY-MM-DD")
    new_unit: Optional[str] = Field(None, pattern="^(L|KG)$")
    new_item_id: Optional[str] = None


@app.post("/api/requests/{rid}/answer")
def answer(rid: str, a: BuyerAnswer):
    try:
        return agent.answer(rid, confirm=a.confirm, new_amount=a.new_amount, new_deadline=a.new_deadline, new_unit=a.new_unit,
                            new_item_id=a.new_item_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- the rulebook
# Every change: preview first (nothing saved), then apply with the name of the person.

class RuleChange(BaseModel):
    action: str = Field(..., pattern="^(add|edit|remove)$")
    rule_id: Optional[str] = None
    type: Optional[str] = None
    name: Optional[str] = None
    settings: Optional[dict] = None
    result: Optional[str] = None


class ApplyChange(BaseModel):
    by: str = Field(..., min_length=1)
    change: Optional[RuleChange] = None
    text: Optional[str] = Field(None, description="A whole new rules.yaml, for manual editing")
    source: str = "manual"
    request_text: Optional[str] = None


@app.get("/api/rules")
def get_rules():
    return rule_edit.overview()


class PreviewChange(BaseModel):
    change: Optional[RuleChange] = None
    text: Optional[str] = None


@app.post("/api/rules/preview")
def preview_rules(body: PreviewChange):
    p = rule_edit.preview(body.change.model_dump(exclude_none=True) if body.change else None, body.text)
    p.pop("new_rules", None)
    return p


@app.post("/api/rules/apply")
def apply_rules(body: ApplyChange):
    try:
        return rule_edit.apply(body.by, body.change.model_dump(exclude_none=True) if body.change else None,
                               body.text, body.source, body.request_text)
    except rule_edit.RuleEditError as e:
        raise HTTPException(422, {"problems": e.problems})


@app.get("/api/rules/changes")
def rule_changes():
    return rule_edit.list_changes()


class Undo(BaseModel):
    by: str = Field(..., min_length=1)


@app.post("/api/rules/changes/{change_id}/undo")
def undo_rule_change(change_id: str, body: Undo):
    try:
        return rule_edit.undo(change_id, body.by)
    except rule_edit.RuleEditError as e:
        raise HTTPException(422, {"problems": e.problems})


# ---------------------------------------------------------------- the audit log

@app.get("/api/log")
def get_log(
    q: Optional[str] = Query(None, description="Words that must all appear in the line"),
    request_id: Optional[str] = None,
    step: Optional[str] = None,
    actor: Optional[str] = Query(None, description="agent, person, buyer"),
    since: Optional[str] = Query(None, description="YYYY-MM-DD"),
    until: Optional[str] = Query(None, description="YYYY-MM-DD"),
    product: Optional[str] = Query(None, description="Product code or name"),
    limit: int = 500,
):
    lines = log.search(q, request_id, step, actor, since, until, product, limit)
    return {"count": len(lines), "lines": lines}


@app.get("/api/log/verify")
def verify_log():
    return log.verify()


@app.get("/api/log/download")
def download_log():
    from fastapi.responses import FileResponse
    if not log.LOG_FILE.exists():
        raise HTTPException(404, "The log is empty")
    return FileResponse(log.LOG_FILE, media_type="application/jsonl", filename="audit_log.jsonl")


# ---------------------------------------------------------------- AI helpers (Gemini, with plain-code fallbacks)

@app.get("/api/ai/status")
def ai_status():
    import os
    return {**ai.status(), "demo_password_required": bool(os.getenv("DEMO_PASSWORD"))}


class Question(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)


@app.post("/api/log/ask")
def ask_log(q: Question):
    """Answer a plain-language question from the audit log, citing the exact lines used."""
    return ai.ask_log(q.question)


class Wish(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@app.post("/api/rules/propose")
def propose_rule(w: Wish):
    """Turn a plain-language wish into a proposed rule change. Nothing is saved: a person must apply it."""
    return ai.propose_rule(w.text)


# ---------------------------------------------------------------- the kitchen: menu, stock, automatic restock

@app.get("/api/kitchen")
def get_kitchen():
    return kitchen.overview()


@app.post("/api/kitchen/simulate-day")
def kitchen_simulate_day():
    """Mock: serve one day of the menu, then let the stock monitor send requests for what ran low."""
    return kitchen.simulate_day()


@app.post("/api/kitchen/check")
def kitchen_check():
    return kitchen.check_restock()


@app.post("/api/kitchen/receive")
def kitchen_receive():
    """Mock: all orders on their way arrive now."""
    return kitchen.receive_deliveries()


# ---------------------------------------------------------------- clearing and resetting

def _check_password(given):
    """On a public demo, DEMO_PASSWORD (set on the server) protects the actions that affect every tester."""
    import hmac
    import os
    import time
    want = os.getenv("DEMO_PASSWORD")
    if want and not hmac.compare_digest(str(given or "").encode(), want.encode()):
        time.sleep(1)  # slows down anyone trying many passwords
        raise HTTPException(403, "Wrong or missing demo password. Nothing was changed.")


class ClearBody(BaseModel):
    by: str = Field(..., min_length=1)
    confirm: str = Field(..., description='Must be "CLEAR"')
    password: Optional[str] = None


@app.post("/api/admin/clear-requests")
def clear_requests(body: ClearBody):
    from . import admin
    if body.confirm != "CLEAR":
        raise HTTPException(422, 'Type CLEAR to confirm.')
    _check_password(body.password)
    return admin.clear_requests(body.by)


class ResetBody(BaseModel):
    by: str = Field(..., min_length=1)
    confirm: str = Field(..., description='Must be "RESET"')
    with_history: bool = False
    password: Optional[str] = None


@app.post("/api/admin/reset")
def reset_everything(body: ResetBody):
    from . import admin
    if body.confirm != "RESET":
        raise HTTPException(422, 'Type RESET to confirm.')
    _check_password(body.password)
    return admin.reset_all(body.by, body.with_history)


# ---------------------------------------------------------------- budgets

@app.get("/api/budgets")
def get_budgets():
    from . import budgets
    return budgets.overview()


class WeeklyBudgets(BaseModel):
    weekly: dict
    by: str = Field(..., min_length=1)


@app.put("/api/budgets")
def set_budgets(body: WeeklyBudgets):
    from . import budgets
    clean = {}
    for k, v in body.weekly.items():
        if k not in budgets.limits() or not isinstance(v, (int, float)) or v < 0 or v > 1_000_000:
            raise HTTPException(422, f"Bad budget for {k}")
        clean[k] = float(v)
    doc = kitchen.load()
    old = doc["budgets"]["weekly"]
    doc["budgets"]["weekly"] = {**old, **clean}
    kitchen.save(doc)
    changed = {k: (old.get(k), v) for k, v in clean.items() if old.get(k) != v}
    if changed:
        log.write("budget", f"Weekly budgets changed by {body.by}: " + "; ".join(f"{k} €{a:,.0f} → €{b:,.0f}" for k, (a, b) in changed.items()) + ".",
                  "dashboard", actor=f"person:{body.by}", data={"changed": changed})
    return budgets.overview()


# ---------------------------------------------------------------- editing the menu and ingredients

class DishIngredient(BaseModel):
    item_id: str
    amount: float = Field(..., gt=0, le=5)


class DishBody(BaseModel):
    by: str = Field(..., min_length=1)
    dish_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=80)
    course: str = "main"
    portions_per_day: int = Field(0, ge=0, le=5000)
    ingredients: List[DishIngredient]


def _kitchen_call(fn, *a):
    try:
        return fn(*a)
    except kitchen.KitchenError as e:
        raise HTTPException(422, str(e))


@app.post("/api/kitchen/dishes")
def save_dish(body: DishBody):
    d = body.model_dump()
    return _kitchen_call(kitchen.save_dish, d.pop("by"), d)


@app.post("/api/kitchen/dishes/{dish_id}/remove")
def remove_dish(dish_id: str, body: ByWhom):
    return _kitchen_call(kitchen.remove_dish, body.by, dish_id)


class IngredientAmounts(BaseModel):
    by: str = Field(..., min_length=1)
    amounts: dict


@app.post("/api/kitchen/ingredients/{item_id}/amounts")
def set_amounts(item_id: str, body: IngredientAmounts):
    return _kitchen_call(kitchen.set_ingredient_amounts, body.by, item_id, body.amounts)


class IngredientBody(BaseModel):
    by: str = Field(..., min_length=1)
    item_id: Optional[str] = None
    name: Optional[str] = None
    unit: Optional[str] = Field(None, pattern="^(L|KG)$")
    usual_product: Optional[str] = None
    usual_amount: Optional[float] = None
    reorder_level: Optional[float] = None
    stock: Optional[float] = None
    category: Optional[str] = None


@app.post("/api/kitchen/ingredients")
def save_ingredient(body: IngredientBody):
    d = body.model_dump()
    return _kitchen_call(kitchen.save_ingredient, d.pop("by"), d)


@app.post("/api/kitchen/ingredients/{item_id}/remove")
def remove_ingredient(item_id: str, body: ByWhom):
    return _kitchen_call(kitchen.remove_ingredient, body.by, item_id)


class SwapItem(BaseModel):
    product_id: str
    name: Optional[str] = None
    note: str = ""
    dishes: List[str] = []
    pre_approved: bool = False


class SwapsBody(BaseModel):
    by: str = Field(..., min_length=1)
    swaps: List[SwapItem]


@app.post("/api/kitchen/ingredients/{item_id}/swaps")
def save_swaps(item_id: str, body: SwapsBody):
    """The chef's recipe swaps for one ingredient. Pre-approved swaps may be ordered by the agent alone."""
    return _kitchen_call(kitchen.save_swaps, body.by, item_id, [x.model_dump() for x in body.swaps])


@app.get("/api/kitchen/find-products")
def find_products(q: str = Query(..., min_length=2), unit: str = Query("KG", pattern="^(L|KG)$")):
    """Valio Aimo products to link a new ingredient to (live price and stock for the matches only)."""
    return kitchen.find_products(q, unit)


# ---------------------------------------------------------------- last-minute events

class EventText(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@app.post("/api/events/read")
def read_event(body: EventText):
    """Read '40 extra guests on Monday: salmon soup and meatballs' into date, guests and dishes (to check on a form)."""
    return events.read_description(body.text)


class EventDish(BaseModel):
    dish_id: str
    portions: int = Field(..., gt=0, le=5000)


class CustomIngredient(BaseModel):
    item_id: str
    amount: float = Field(..., gt=0, le=5, description="per portion, in kg or litres")


class CustomDish(BaseModel):
    dish_id: str
    name: str = Field(..., min_length=1, max_length=80)
    course: str = "main"
    ingredients: List[CustomIngredient]
    add_to_menu: bool = False


class NewEvent(BaseModel):
    name: str = "Event"
    date: str = Field(..., examples=["2026-11-16"])
    dishes: List[EventDish]
    by: str = Field(..., min_length=1)
    scenario: Optional[str] = None
    budget: Optional[float] = Field(None, ge=0)
    custom_dishes: List[CustomDish] = []
    add_to_menu: bool = False


@app.post("/api/events")
def new_event(body: NewEvent):
    try:
        return events.plan(body.name, body.date, [d.model_dump() for d in body.dishes], body.by, scenario=body.scenario,
                           budget=body.budget, custom_dishes=[c.model_dump() for c in body.custom_dishes], add_to_menu=body.add_to_menu)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/events")
def list_events():
    return [events.view(e["id"]) for e in events.all_events()][::-1]


@app.get("/api/events/{eid}")
def get_event(eid: str):
    ev = events.view(eid)
    if not ev:
        raise HTTPException(404, "Unknown event")
    return ev


@app.get("/api/events/{eid}/fixes/{item_id}")
def event_fixes(eid: str, item_id: str):
    try:
        return events.fixes(eid, item_id)
    except (ValueError, aimo.AimoError) as e:
        raise HTTPException(409, str(e))


class DishSwap(BaseModel):
    from_dish: str
    to_dish: str
    by: str = Field(..., min_length=1)
    move: Optional[int] = Field(None, gt=0, description="portions to move; default all")


class EventBudget(BaseModel):
    budget: Optional[float] = Field(None, ge=0)
    by: str = Field(..., min_length=1)


@app.put("/api/events/{eid}/budget")
def event_budget(eid: str, body: EventBudget):
    try:
        return events.set_budget(eid, body.budget, body.by)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/events/{eid}/swap-dish")
def event_swap_dish(eid: str, body: DishSwap):
    try:
        return events.swap_dish(eid, body.from_dish, body.to_dish, body.by, move=body.move)
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- for the dashboard

@app.get("/api/usual")
def usual():
    """The kitchen's usual orders (generated test data), for one-tap ordering."""
    from . import rules
    return rules.usual_orders()


@app.get("/", include_in_schema=False)
def dashboard():
    from fastapi.responses import FileResponse
    return FileResponse(config.ROOT / "app" / "static" / "index.html", headers={"Cache-Control": "no-store"})
