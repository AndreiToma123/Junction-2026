"""Our backend (FastAPI). Run: .venv/bin/uvicorn app.main:app --reload

Interactive docs (open standard: OpenAPI) at http://127.0.0.1:8000/docs
"""
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import agent, ai, aimo, catalog, config, kitchen, log, orders, rule_edit, store

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
    return agent.handle(req.text, scenario=req.scenario)


@app.get("/api/requests")
def list_requests():
    return store.all_records()


@app.get("/api/requests/{rid}")
def get_request(rid: str):
    rec = store.get(rid)
    if not rec:
        raise HTTPException(404, "Unknown request")
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


class BuyerAnswer(BaseModel):
    confirm: bool = False
    new_amount: Optional[float] = None


@app.post("/api/requests/{rid}/answer")
def answer(rid: str, a: BuyerAnswer):
    try:
        return agent.answer(rid, confirm=a.confirm, new_amount=a.new_amount)
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
    return ai.status()


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
