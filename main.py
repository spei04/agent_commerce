import json
import secrets
import uuid
import hmac
import hashlib
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from fastapi.openapi.utils import get_openapi

from database import engine, get_db, Base, SessionLocal
from models import (
    Wallet,
    Product,
    Transaction,
    IntentLog,
    ApprovalRequest,
    SkuInsight,
    CatalogSync,
    BalanceHold,
    PaymentIntent,
    WebhookEvent,
)
from auth import require_admin, require_wallet_key, is_admin
import policy as policy_engine
import resolver as intent_resolver

Base.metadata.create_all(bind=engine)


def _ensure_sqlite_schema() -> None:
    """
    MVP schema migration for SQLite.
    `create_all()` doesn't ALTER existing tables, so we add new columns/indexes in-place.
    """
    if not engine.url.drivername.startswith("sqlite"):
        return

    def has_column(conn, table: str, column: str) -> bool:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)  # type: ignore[index]

    with engine.begin() as conn:
        if not has_column(conn, "wallets", "api_key"):
            conn.execute(text("ALTER TABLE wallets ADD COLUMN api_key TEXT"))
        if not has_column(conn, "wallets", "reserved_balance"):
            conn.execute(text("ALTER TABLE wallets ADD COLUMN reserved_balance FLOAT DEFAULT 0.0"))
        if not has_column(conn, "transactions", "idempotency_key"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN idempotency_key TEXT"))
        if not has_column(conn, "transactions", "payment_status"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN payment_status TEXT DEFAULT 'not_started'"))
        if not has_column(conn, "transactions", "payment_intent_id"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN payment_intent_id TEXT"))
        if not has_column(conn, "transactions", "settled_at"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN settled_at DATETIME"))
        if not has_column(conn, "approval_requests", "reason"):
            conn.execute(text("ALTER TABLE approval_requests ADD COLUMN reason TEXT"))
        if not has_column(conn, "approval_requests", "policy_trace"):
            conn.execute(text("ALTER TABLE approval_requests ADD COLUMN policy_trace TEXT"))
        if not has_column(conn, "approval_requests", "idempotency_key"):
            conn.execute(text("ALTER TABLE approval_requests ADD COLUMN idempotency_key TEXT"))

        # Prevent double-charge on retry for a given wallet.
        # Partial index is supported on modern SQLite; ignore if unavailable.
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_wallet_idempotency "
                "ON transactions(wallet_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            ))
        except Exception:
            pass
        try:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_wallet_idempotency "
                "ON approval_requests(wallet_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            ))
        except Exception:
            pass


_ensure_sqlite_schema()

app = FastAPI(title="Agent Commerce")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_worker_started = False

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version="0.1.0",
        description=(
            "Agent Commerce API.\n\n"
            "Auth (MVP):\n"
            "- Admin endpoints require `X-Admin-Key` (default `demo-admin`, configurable via `AGENT_COMMERCE_ADMIN_KEY`).\n"
            "- Agent endpoints require `X-Wallet-Key` (returned once on wallet creation).\n\n"
            "Payments (simulated): purchases settle asynchronously; watch `payment_status` and `settled_at`.\n"
            "For agent-friendly metadata, also fetch `/.well-known/agent-commerce.json`."
        ),
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["AdminKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Admin-Key",
        "description": "Admin key for dashboard and admin endpoints.",
    }
    schema["components"]["securitySchemes"]["WalletKeyAuth"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Wallet-Key",
        "description": "Wallet key for agent purchase/resolve endpoints.",
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ─── Schemas ──────────────────────────────────────────────────────────────────

class WalletCreate(BaseModel):
    agent_id: str
    name: str
    balance: float = 100.0
    auto_approve_limit: float = 20.0   # below → auto-approve
    spend_limit_max: float = 100.0     # above → hard block; middle → requires approval
    allowed_vendors: List[str] = []
    daily_limit: float = 0.0
    weekly_limit: float = 0.0
    velocity_max_txn: int = 0
    webhook_url: Optional[str] = None

class WalletTopup(BaseModel):
    amount: float

class ProductCreate(BaseModel):
    vendor_name: str
    name: str
    description: Optional[str] = None
    price: float
    min_order: int = 1
    lead_time_days: int = 1
    tags: List[str] = []

class IntentRequest(BaseModel):
    wallet_id: str
    intent: str
    budget: Optional[float] = None
    constraints: dict = Field(default_factory=dict)
    auto_buy: bool = False
    quantity: int = 1

class PurchaseRequest(BaseModel):
    wallet_id: str
    product_id: str
    quantity: int = 1
    intent: Optional[str] = None
    idempotency_key: Optional[str] = None
    simulate_outcome: Optional[str] = None  # succeed | fail | requires_action

class ApprovalAction(BaseModel):
    note: Optional[str] = None


# ─── Wallets ──────────────────────────────────────────────────────────────────

@app.post("/wallets")
def create_wallet(data: WalletCreate, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    if db.query(Wallet).filter(Wallet.agent_id == data.agent_id).first():
        raise HTTPException(400, "agent_id already has a wallet")
    api_key = secrets.token_urlsafe(24)
    wallet = Wallet(
        id=str(uuid.uuid4()),
        agent_id=data.agent_id,
        name=data.name,
        balance=data.balance,
        reserved_balance=0.0,
        api_key=api_key,
        auto_approve_limit=data.auto_approve_limit,
        spend_limit_max=data.spend_limit_max,
        allowed_vendors=json.dumps(data.allowed_vendors),
        daily_limit=data.daily_limit,
        weekly_limit=data.weekly_limit,
        velocity_max_txn=data.velocity_max_txn,
        webhook_url=data.webhook_url,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    # Return the wallet key once. Subsequent reads do not expose it.
    return {**_wallet_dict(wallet), "wallet_key": api_key}


@app.get("/wallets")
def list_wallets(db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    return [_wallet_dict(w) for w in db.query(Wallet).all()]


@app.get("/wallets/{wallet_id}")
def get_wallet(
    wallet_id: str,
    db: Session = Depends(get_db),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
):
    if is_admin(x_admin_key):
        wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
        if not wallet:
            raise HTTPException(404, "wallet not found")
        return _wallet_dict(wallet)
    wallet = require_wallet_key(wallet_id=wallet_id, wallet_key=x_wallet_key, db=db)
    return _wallet_dict(wallet)


@app.patch("/wallets/{wallet_id}")
def update_wallet(wallet_id: str, data: dict, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(404, "wallet not found")
    for field in ("auto_approve_limit", "spend_limit_max", "name", "daily_limit", "weekly_limit", "velocity_max_txn", "webhook_url", "status"):
        if field in data:
            setattr(wallet, field, data[field])
    if "allowed_vendors" in data:
        wallet.allowed_vendors = json.dumps(data["allowed_vendors"])
    db.commit()
    return _wallet_dict(wallet)


@app.post("/wallets/{wallet_id}/topup")
def topup(wallet_id: str, data: WalletTopup, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(404, "wallet not found")
    if data.amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    wallet.balance += data.amount
    db.commit()
    return {"balance": wallet.balance}

@app.post("/wallets/{wallet_id}/rotate_key")
def rotate_wallet_key(wallet_id: str, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(404, "wallet not found")
    wallet.api_key = secrets.token_urlsafe(24)
    db.commit()
    return {"wallet_id": wallet.id, "wallet_key": wallet.api_key}


# ─── Products (Merchant Catalog) ──────────────────────────────────────────────

@app.post("/products")
def register_product(data: ProductCreate, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    if data.price < 0:
        raise HTTPException(400, "price must be >= 0")
    if data.min_order < 1:
        raise HTTPException(400, "min_order must be >= 1")
    product = Product(
        id=str(uuid.uuid4()),
        vendor_name=data.vendor_name,
        name=data.name,
        description=data.description,
        price=data.price,
        min_order=data.min_order,
        lead_time_days=data.lead_time_days,
        tags=json.dumps(data.tags),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return _product_dict(product)


@app.get("/products")
def list_products(db: Session = Depends(get_db)):
    return [_product_dict(p) for p in db.query(Product).all()]


# ─── Intent Resolution ────────────────────────────────────────────────────────

@app.post("/resolve")
def resolve(
    data: IntentRequest,
    db: Session = Depends(get_db),
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
):
    wallet = require_wallet_key(wallet_id=data.wallet_id, wallet_key=x_wallet_key, db=db)

    matches = intent_resolver.resolve_intent(db, data.intent, data.budget, data.constraints)

    log = IntentLog(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        agent_id=wallet.agent_id,
        raw_intent=data.intent,
        constraints=json.dumps(data.constraints),
        matched_count=len(matches),
    )
    db.add(log)
    db.commit()

    result = {"matches": matches, "intent_log_id": log.id}

    if data.auto_buy and matches:
        req = PurchaseRequest(
            wallet_id=data.wallet_id,
            product_id=matches[0]["product_id"],
            quantity=data.quantity,
            intent=data.intent,
        )
        result["transaction"] = _execute_purchase(req, wallet, db)

    return result


# ─── Purchase ─────────────────────────────────────────────────────────────────

@app.post("/purchase")
def purchase(
    data: PurchaseRequest,
    db: Session = Depends(get_db),
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
):
    wallet = require_wallet_key(wallet_id=data.wallet_id, wallet_key=x_wallet_key, db=db)
    return _execute_purchase(data, wallet, db)


def _execute_purchase(data: PurchaseRequest, wallet: Wallet, db: Session) -> dict:
    if data.quantity < 1:
        raise HTTPException(400, "quantity must be >= 1")

    if data.idempotency_key:
        existing = db.query(Transaction).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.idempotency_key == data.idempotency_key,
        ).first()
        if existing:
            return _txn_dict(existing)

    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(404, "product not found")

    total = product.price * data.quantity
    result = policy_engine.evaluate_full(total, product.vendor_name, wallet, db)

    if result.decision == policy_engine.Decision.REQUIRES_APPROVAL:
        if data.idempotency_key:
            existing_req = db.query(ApprovalRequest).filter(
                ApprovalRequest.wallet_id == wallet.id,
                ApprovalRequest.idempotency_key == data.idempotency_key,
                ApprovalRequest.status == "pending",
            ).first()
            if existing_req:
                return {
                    "status": "pending_approval",
                    "approval_request_id": existing_req.id,
                    "reason": existing_req.reason or result.reason,
                    "amount": existing_req.amount,
                    "vendor_name": existing_req.vendor_name,
                    "product_name": existing_req.product_name,
                    "policy_trace": json.loads(existing_req.policy_trace) if existing_req.policy_trace else result.trace,
                }

        # Reserve funds so approval guarantees can succeed later.
        reserved_ok = db.execute(text(
            "UPDATE wallets SET reserved_balance = COALESCE(reserved_balance, 0.0) + :amount "
            "WHERE id = :wallet_id AND (balance - COALESCE(reserved_balance, 0.0)) >= :amount"
        ), {"amount": total, "wallet_id": wallet.id}).rowcount
        if not reserved_ok:
            txn = Transaction(
                id=str(uuid.uuid4()),
                wallet_id=wallet.id,
                agent_id=wallet.agent_id,
                product_id=product.id,
                vendor_name=product.vendor_name,
                product_name=product.name,
                amount=total,
                idempotency_key=data.idempotency_key,
                quantity=data.quantity,
                intent=data.intent,
                status="blocked",
                payment_status="not_started",
                reason="insufficient balance (concurrent spend)",
                policy_trace=json.dumps(result.trace + [{"check": "reserve", "passed": False, "detail": "available balance changed concurrently"}]),
            )
            db.add(txn)
            db.commit()
            return _txn_dict(txn)

        # Create an approval request — no transaction yet, no balance change
        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            wallet_id=wallet.id,
            agent_id=wallet.agent_id,
            product_id=product.id,
            vendor_name=product.vendor_name,
            product_name=product.name,
            amount=total,
            idempotency_key=data.idempotency_key,
            quantity=data.quantity,
            intent=data.intent,
            reason=result.reason,
            policy_trace=json.dumps(result.trace),
        )
        db.add(req)
        db.add(BalanceHold(
            id=str(uuid.uuid4()),
            wallet_id=wallet.id,
            amount=total,
            kind="approval",
            status="active",
            approval_request_id=req.id,
        ))
        _enqueue_webhook(
            db=db,
            wallet=wallet,
            event_type="approval_required",
            payload={
                "approval_request_id": req.id,
                "agent_id": wallet.agent_id,
                "amount": total,
                "vendor_name": product.vendor_name,
                "product_name": product.name,
            },
        )
        db.commit()

        return {
            "status": "pending_approval",
            "approval_request_id": req.id,
            "reason": result.reason,
            "amount": total,
            "vendor_name": product.vendor_name,
            "product_name": product.name,
            "policy_trace": result.trace,
        }

    # Auto-approve or blocked — create transaction immediately
    txn = Transaction(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        agent_id=wallet.agent_id,
        product_id=product.id,
        vendor_name=product.vendor_name,
        product_name=product.name,
        amount=total,
        idempotency_key=data.idempotency_key,
        quantity=data.quantity,
        intent=data.intent,
        status="approved" if result.approved else "blocked",
        reason=result.reason,
        policy_trace=json.dumps(result.trace),
        payment_status="not_started",
    )
    db.add(txn)

    if result.approved:
        # Reserve (authorization hold). Settlement happens async and will capture/release.
        reserved_ok = db.execute(text(
            "UPDATE wallets SET reserved_balance = COALESCE(reserved_balance, 0.0) + :amount "
            "WHERE id = :wallet_id AND (balance - COALESCE(reserved_balance, 0.0)) >= :amount"
        ), {"amount": total, "wallet_id": wallet.id}).rowcount
        if not reserved_ok:
            txn.status = "blocked"
            txn.reason = "insufficient balance (concurrent spend)"
            txn.payment_status = "not_started"
            trace = result.trace + [{"check": "reserve", "passed": False, "detail": "available balance changed concurrently"}]
            txn.policy_trace = json.dumps(trace)
        else:
            txn.payment_status = "processing"
            hold = BalanceHold(
                id=str(uuid.uuid4()),
                wallet_id=wallet.id,
                amount=total,
                kind="auth",
                status="active",
                transaction_id=txn.id,
            )
            db.add(hold)

            intent = _create_payment_intent(
                db=db,
                wallet=wallet,
                txn=txn,
                amount=total,
                idempotency_key=data.idempotency_key,
                simulate_outcome=data.simulate_outcome,
            )
            txn.payment_intent_id = intent.id

            insight = db.query(SkuInsight).filter(SkuInsight.product_id == product.id).first()
            if insight:
                insight.selections += 1

    db.commit()
    return _txn_dict(txn)


# ─── Approvals ────────────────────────────────────────────────────────────────

@app.get("/approvals")
def list_approvals(status: Optional[str] = "pending", db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    q = db.query(ApprovalRequest)
    if status:
        q = q.filter(ApprovalRequest.status == status)
    return [_approval_dict(r) for r in q.order_by(ApprovalRequest.created_at.desc()).all()]


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, data: ApprovalAction, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    req = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(404, "approval request not found")
    if req.status != "pending":
        raise HTTPException(400, f"request is already {req.status}")

    wallet = db.query(Wallet).filter(Wallet.id == req.wallet_id).first()
    product = db.query(Product).filter(Product.id == req.product_id).first()
    if not wallet or not product:
        raise HTTPException(404, "wallet or product not found")

    # Human approval overrides wallet suspension. Funds were reserved at request-time.
    hold = db.query(BalanceHold).filter(
        BalanceHold.approval_request_id == req.id,
        BalanceHold.status == "active",
    ).first()
    if not hold:
        # Backward-compat: older approval requests may exist without holds.
        reserved_ok = db.execute(text(
            "UPDATE wallets SET reserved_balance = COALESCE(reserved_balance, 0.0) + :amount "
            "WHERE id = :wallet_id AND (balance - COALESCE(reserved_balance, 0.0)) >= :amount"
        ), {"amount": req.amount, "wallet_id": wallet.id}).rowcount
        if not reserved_ok:
            raise HTTPException(400, "insufficient balance")
        hold = BalanceHold(
            id=str(uuid.uuid4()),
            wallet_id=wallet.id,
            amount=req.amount,
            kind="approval",
            status="active",
            approval_request_id=req.id,
        )
        db.add(hold)
        db.flush()

    # Execute the transaction now (snapshot approval semantics; settlement async).
    txn = Transaction(
        id=str(uuid.uuid4()),
        wallet_id=req.wallet_id,
        agent_id=req.agent_id,
        product_id=req.product_id,
        vendor_name=req.vendor_name,
        product_name=req.product_name,
        amount=req.amount,
        idempotency_key=req.idempotency_key,
        quantity=req.quantity,
        intent=req.intent,
        status="approved",
        reason="approved by reviewer",
        policy_trace=req.policy_trace,
        payment_status="processing",
    )
    db.add(txn)
    hold.transaction_id = txn.id

    intent = _create_payment_intent(
        db=db,
        wallet=wallet,
        txn=txn,
        amount=req.amount,
        idempotency_key=req.idempotency_key,
        simulate_outcome=None,
    )
    txn.payment_intent_id = intent.id

    insight = db.query(SkuInsight).filter(SkuInsight.product_id == req.product_id).first()
    if insight:
        insight.selections += 1

    req.status = "approved"
    req.reviewer_note = data.note
    req.reviewed_at = datetime.utcnow()
    db.commit()

    return {"approved": True, "transaction": _txn_dict(txn)}


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, data: ApprovalAction, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    req = db.query(ApprovalRequest).filter(ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(404, "approval request not found")
    if req.status != "pending":
        raise HTTPException(400, f"request is already {req.status}")

    hold = db.query(BalanceHold).filter(
        BalanceHold.approval_request_id == req.id,
        BalanceHold.status == "active",
    ).first()
    if hold:
        db.execute(text(
            "UPDATE wallets SET reserved_balance = COALESCE(reserved_balance, 0.0) - :amount "
            "WHERE id = :wallet_id AND COALESCE(reserved_balance, 0.0) >= :amount"
        ), {"amount": hold.amount, "wallet_id": req.wallet_id})
        hold.status = "released"
        hold.released_at = datetime.utcnow()

    # Log a blocked transaction for audit trail
    txn = Transaction(
        id=str(uuid.uuid4()),
        wallet_id=req.wallet_id,
        agent_id=req.agent_id,
        product_id=req.product_id,
        vendor_name=req.vendor_name,
        product_name=req.product_name,
        amount=req.amount,
        idempotency_key=req.idempotency_key,
        quantity=req.quantity,
        intent=req.intent,
        status="blocked",
        reason=f"rejected by reviewer: {data.note or 'no reason given'}",
        policy_trace=req.policy_trace,
        payment_status="not_started",
    )
    db.add(txn)

    req.status = "rejected"
    req.reviewer_note = data.note
    req.reviewed_at = datetime.utcnow()
    db.commit()

    return {"rejected": True, "transaction": _txn_dict(txn)}


# ─── Transactions ─────────────────────────────────────────────────────────────

@app.get("/transactions")
def list_transactions(
    wallet_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
):
    if not wallet_id:
        if not is_admin(x_admin_key):
            raise HTTPException(401, "missing/invalid admin key")
    else:
        if not is_admin(x_admin_key):
            require_wallet_key(wallet_id=wallet_id, wallet_key=x_wallet_key, db=db)

    q = db.query(Transaction)
    if wallet_id:
        q = q.filter(Transaction.wallet_id == wallet_id)
    if status:
        q = q.filter(Transaction.status == status)
    return [_txn_dict(t) for t in q.order_by(Transaction.created_at.desc()).limit(limit)]


# ─── Intent Log ───────────────────────────────────────────────────────────────

@app.get("/intents")
def list_intents(limit: int = 20, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    logs = db.query(IntentLog).order_by(IntentLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id": l.id,
            "agent_id": l.agent_id,
            "raw_intent": l.raw_intent,
            "constraints": json.loads(l.constraints),
            "matched_count": l.matched_count,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


# ─── Dashboard Summary ────────────────────────────────────────────────────────

@app.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    all_txns = db.query(Transaction).all()
    approved = [t for t in all_txns if t.status == "approved"]
    blocked  = [t for t in all_txns if t.status == "blocked"]
    pending  = db.query(ApprovalRequest).filter(ApprovalRequest.status == "pending").count()

    total_volume = sum(t.amount for t in approved)

    agents: dict = {}
    for t in all_txns:
        if t.agent_id not in agents:
            agents[t.agent_id] = {"approved": 0, "blocked": 0, "volume": 0.0}
        if t.status == "approved":
            agents[t.agent_id]["approved"] += 1
            agents[t.agent_id]["volume"] += t.amount
        else:
            agents[t.agent_id]["blocked"] += 1

    vendor_vol: dict = {}
    for t in approved:
        vendor_vol[t.vendor_name] = vendor_vol.get(t.vendor_name, 0) + t.amount
    top_vendors = sorted(vendor_vol.items(), key=lambda x: x[1], reverse=True)[:6]

    return {
        "total_volume": round(total_volume, 4),
        "total_transactions": len(all_txns),
        "approved_count": len(approved),
        "blocked_count": len(blocked),
        "pending_approvals": pending,
        "success_rate": round(len(approved) / len(all_txns) * 100, 1) if all_txns else 0,
        "active_agents": len(agents),
        "agents": agents,
        "top_vendors": [{"vendor": v, "volume": round(vol, 4)} for v, vol in top_vendors],
    }


# ─── Merchant: Catalog Sync ───────────────────────────────────────────────────

class CatalogSyncItem(BaseModel):
    vendor_name: str
    name: str
    description: Optional[str] = None
    price: float
    min_order: int = 1
    lead_time_days: int = 1
    tags: List[str] = []

class CatalogSyncRequest(BaseModel):
    source: str                        # e.g. "shopify", "sap", "csv"
    products: List[CatalogSyncItem]


@app.post("/catalog/sync")
def catalog_sync(data: CatalogSyncRequest, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """
    Bulk upsert products from an external PIM/ERP.
    Matches on (vendor_name, name) — updates existing, creates new.
    """
    added = updated = 0
    for item in data.products:
        existing = db.query(Product).filter(
            Product.vendor_name == item.vendor_name,
            Product.name == item.name,
        ).first()
        if existing:
            existing.price = item.price
            existing.description = item.description
            existing.min_order = item.min_order
            existing.lead_time_days = item.lead_time_days
            existing.tags = json.dumps(item.tags)
            updated += 1
        else:
            db.add(Product(
                id=str(uuid.uuid4()),
                vendor_name=item.vendor_name,
                name=item.name,
                description=item.description,
                price=item.price,
                min_order=item.min_order,
                lead_time_days=item.lead_time_days,
                tags=json.dumps(item.tags),
            ))
            added += 1

    sync_log = CatalogSync(
        id=str(uuid.uuid4()),
        source=data.source,
        products_added=added,
        products_updated=updated,
    )
    db.add(sync_log)
    db.commit()
    return {"source": data.source, "added": added, "updated": updated, "sync_id": sync_log.id}


@app.get("/catalog/syncs")
def list_catalog_syncs(limit: int = 20, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    syncs = db.query(CatalogSync).order_by(CatalogSync.created_at.desc()).limit(limit).all()
    return [
        {
            "id": s.id,
            "source": s.source,
            "status": s.status,
            "products_added": s.products_added,
            "products_updated": s.products_updated,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in syncs
    ]


# ─── Merchant: SKU Intelligence ───────────────────────────────────────────────

@app.get("/analytics/sku")
def sku_analytics(vendor_name: Optional[str] = None, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """
    Per-product AI visibility scores: impressions, selections, avg rank,
    selection rate, and a composite visibility_score (0–100).
    """
    q = db.query(SkuInsight, Product).join(Product, SkuInsight.product_id == Product.id)
    if vendor_name:
        q = q.filter(Product.vendor_name == vendor_name)

    rows = q.order_by(SkuInsight.impressions.desc()).all()

    results = []
    for insight, product in rows:
        avg_rank = round(insight.rank_sum / insight.impressions, 2) if insight.impressions else None
        sel_rate = round(insight.selections / insight.impressions * 100, 1) if insight.impressions else 0
        # Visibility score: higher impressions + lower avg rank + higher sel_rate = better
        rank_factor = max(0, 10 - (avg_rank or 10)) / 10 if avg_rank else 0
        visibility_score = round(
            min(100, (insight.impressions * 5) * (0.4 + 0.4 * rank_factor + 0.2 * sel_rate / 100))
        )
        results.append({
            "product_id": product.id,
            "vendor_name": product.vendor_name,
            "name": product.name,
            "price": product.price,
            "tags": json.loads(product.tags) if isinstance(product.tags, str) else product.tags,
            "impressions": insight.impressions,
            "selections": insight.selections,
            "avg_rank": avg_rank,
            "selection_rate": sel_rate,
            "visibility_score": visibility_score,
            "last_seen": insight.last_seen.isoformat() if insight.last_seen else None,
        })

    return results


# ─── Merchant: Prompt Analytics ───────────────────────────────────────────────

@app.get("/analytics/intents")
def intent_analytics(limit: int = 200, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """
    Aggregate raw agent intents into keyword frequencies and volume trends.
    Returns trending terms and daily intent volume for the last 30 days.
    """
    import re
    from collections import Counter

    logs = db.query(IntentLog).order_by(IntentLog.created_at.desc()).limit(limit).all()

    STOPWORDS = {"a", "an", "the", "for", "and", "or", "to", "of", "in", "with",
                 "buy", "get", "find", "need", "want", "some", "best", "cheap",
                 "fast", "good", "that", "this", "have", "can", "bulk", "order"}

    keyword_counts: Counter = Counter()
    daily_volume: dict = {}

    for log in logs:
        words = re.findall(r"[a-zA-Z]{3,}", (log.raw_intent or "").lower())
        keyword_counts.update(w for w in words if w not in STOPWORDS)

        day = log.created_at.strftime("%Y-%m-%d") if log.created_at else "unknown"
        daily_volume[day] = daily_volume.get(day, 0) + 1

    top_keywords = [{"keyword": k, "count": c} for k, c in keyword_counts.most_common(30)]
    trend = sorted(daily_volume.items())

    # Zero-match intents — potential demand gaps
    unmatched = [
        {"intent": l.raw_intent, "agent_id": l.agent_id, "created_at": l.created_at.isoformat() if l.created_at else None}
        for l in logs if l.matched_count == 0
    ]

    return {
        "total_intents": len(logs),
        "top_keywords": top_keywords,
        "daily_volume": [{"date": d, "count": c} for d, c in trend],
        "unmatched_intents": unmatched[:20],
    }


# ─── Merchant: Content Optimization ──────────────────────────────────────────

@app.get("/analytics/optimize/{product_id}")
def optimize_product(product_id: str, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """
    Analyse a product's discoverability gap.
    Compares recent agent intents against the product's tags and description,
    surfacing keywords that agents are searching for but the product doesn't match.
    """
    import re
    from collections import Counter

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "product not found")

    tags = json.loads(product.tags) if isinstance(product.tags, str) else product.tags
    tags_lower = {t.lower() for t in tags}
    description_lower = (product.description or "").lower()
    name_lower = product.name.lower()

    logs = db.query(IntentLog).order_by(IntentLog.created_at.desc()).limit(500).all()

    STOPWORDS = {"a", "an", "the", "for", "and", "or", "to", "of", "in", "with",
                 "buy", "get", "find", "need", "want", "some", "best", "cheap",
                 "fast", "good", "that", "this", "have", "can", "bulk", "order"}

    all_keywords: Counter = Counter()
    for log in logs:
        words = re.findall(r"[a-zA-Z]{3,}", (log.raw_intent or "").lower())
        all_keywords.update(w for w in words if w not in STOPWORDS)

    # Keywords agents search for that this product doesn't currently match
    gaps = []
    for kw, count in all_keywords.most_common(50):
        if kw not in tags_lower and kw not in name_lower and kw not in description_lower:
            gaps.append({"keyword": kw, "search_volume": count})

    insight = db.query(SkuInsight).filter(SkuInsight.product_id == product_id).first()
    current_impressions = insight.impressions if insight else 0
    current_selections = insight.selections if insight else 0

    # Suggested tags to add
    suggested_tags = [g["keyword"] for g in gaps[:10]]

    return {
        "product_id": product_id,
        "name": product.name,
        "vendor_name": product.vendor_name,
        "current_tags": tags,
        "current_impressions": current_impressions,
        "current_selections": current_selections,
        "keyword_gaps": gaps[:15],
        "suggested_tags": suggested_tags,
        "optimization_potential": "high" if len(gaps) > 8 else "medium" if len(gaps) > 3 else "low",
    }


@app.post("/analytics/optimize/{product_id}/apply")
def apply_optimization(product_id: str, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """
    Auto-apply the top suggested tags from the gap analysis to the product.
    """
    import re
    from collections import Counter

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "product not found")

    tags = json.loads(product.tags) if isinstance(product.tags, str) else product.tags
    tags_lower = {t.lower() for t in tags}
    description_lower = (product.description or "").lower()
    name_lower = product.name.lower()

    logs = db.query(IntentLog).order_by(IntentLog.created_at.desc()).limit(500).all()
    STOPWORDS = {"a", "an", "the", "for", "and", "or", "to", "of", "in", "with",
                 "buy", "get", "find", "need", "want", "some", "best", "cheap",
                 "fast", "good", "that", "this", "have", "can", "bulk", "order"}

    all_keywords: Counter = Counter()
    for log in logs:
        words = re.findall(r"[a-zA-Z]{3,}", (log.raw_intent or "").lower())
        all_keywords.update(w for w in words if w not in STOPWORDS)

    new_tags = list(tags)
    added = []
    for kw, _ in all_keywords.most_common(50):
        if kw not in tags_lower and kw not in name_lower and kw not in description_lower:
            new_tags.append(kw)
            added.append(kw)
            if len(added) >= 5:
                break

    product.tags = json.dumps(new_tags)
    db.commit()

    return {"product_id": product_id, "tags_added": added, "tags_now": new_tags}


@app.post("/wallets/{wallet_id}/unsuspend")
def unsuspend_wallet(wallet_id: str, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(404, "wallet not found")
    wallet.status = "active"
    db.commit()
    return _wallet_dict(wallet)


# ─── Demo Infrastructure ──────────────────────────────────────────────────────

DEMO_WALLETS = {
    "procurement": {
        "agent_id": "demo-procurement-v1",
        "name": "Demo: Procurement Agent",
        "balance": 500.0,
        "auto_approve_limit": 50.0,
        "spend_limit_max": 300.0,
        "daily_limit": 70.0,
        "weekly_limit": 0.0,
        "velocity_max_txn": 3,
        "allowed_vendors": [],
    },
    "research": {
        "agent_id": "demo-research-v1",
        "name": "Demo: Research Agent",
        "balance": 200.0,
        "auto_approve_limit": 20.0,
        "spend_limit_max": 100.0,
        "daily_limit": 0.0,
        "weekly_limit": 80.0,
        "velocity_max_txn": 0,
        "allowed_vendors": ["arxiv-reports", "data-warehouse.io"],
    },
}

DEMO_PRODUCTS = {
    "small":      {"vendor_name": "packright.com",      "name": "Demo: Corrugated Mailer Box 12×9",   "price": 8.0,   "tags": ["packaging", "boxes", "corrugated"]},
    "medium":     {"vendor_name": "arxiv-reports",      "name": "Demo: Market Research Report Q1",    "price": 45.0,  "tags": ["research", "reports", "market"]},
    "large":      {"vendor_name": "cloudsoft.io",       "name": "Demo: Enterprise Cloud License",     "price": 320.0, "tags": ["software", "cloud", "enterprise"]},
    "restricted": {"vendor_name": "exfil-data.io",      "name": "Demo: Proprietary Dataset Bundle",   "price": 15.0,  "tags": ["data", "dataset", "proprietary"]},
    "bulk":       {"vendor_name": "shipthat.com",       "name": "Demo: Express Shipping Bundle",      "price": 30.0,  "tags": ["shipping", "express", "bundle"]},
    "fast":       {"vendor_name": "cloud-provider.com", "name": "Demo: API Credits 1000 units",       "price": 12.0,  "tags": ["api", "credits", "cloud"]},
}


@app.post("/demo/reset")
def demo_reset(db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """Create or reset demo wallets and products to initial state."""
    wallet_ids = {}
    product_ids = {}
    wallet_keys = {}

    for key, cfg in DEMO_WALLETS.items():
        wallet = db.query(Wallet).filter(Wallet.agent_id == cfg["agent_id"]).first()
        if wallet:
            # Reset to initial values
            wallet.balance = cfg["balance"]
            wallet.reserved_balance = 0.0
            wallet.auto_approve_limit = cfg["auto_approve_limit"]
            wallet.spend_limit_max = cfg["spend_limit_max"]
            wallet.daily_limit = cfg["daily_limit"]
            wallet.weekly_limit = cfg["weekly_limit"]
            wallet.velocity_max_txn = cfg["velocity_max_txn"]
            wallet.allowed_vendors = json.dumps(cfg["allowed_vendors"])
            wallet.status = "active"
            if not wallet.api_key:
                wallet.api_key = secrets.token_urlsafe(24)
        else:
            wallet = Wallet(
                id=str(uuid.uuid4()),
                agent_id=cfg["agent_id"],
                name=cfg["name"],
                balance=cfg["balance"],
                api_key=secrets.token_urlsafe(24),
                auto_approve_limit=cfg["auto_approve_limit"],
                spend_limit_max=cfg["spend_limit_max"],
                daily_limit=cfg["daily_limit"],
                weekly_limit=cfg["weekly_limit"],
                velocity_max_txn=cfg["velocity_max_txn"],
                allowed_vendors=json.dumps(cfg["allowed_vendors"]),
                status="active",
            )
            db.add(wallet)
        db.flush()
        wallet_ids[key] = wallet.id
        wallet_keys[key] = wallet.api_key

        # Clear demo transactions, approvals, holds, and payment artifacts
        db.query(Transaction).filter(Transaction.wallet_id == wallet.id).delete()
        db.query(ApprovalRequest).filter(ApprovalRequest.wallet_id == wallet.id).delete()
        db.query(BalanceHold).filter(BalanceHold.wallet_id == wallet.id).delete()
        db.query(PaymentIntent).filter(PaymentIntent.wallet_id == wallet.id).delete()
        db.query(WebhookEvent).filter(WebhookEvent.wallet_id == wallet.id).delete()

    for key, cfg in DEMO_PRODUCTS.items():
        product = db.query(Product).filter(
            Product.vendor_name == cfg["vendor_name"],
            Product.name == cfg["name"],
        ).first()
        if not product:
            product = Product(
                id=str(uuid.uuid4()),
                vendor_name=cfg["vendor_name"],
                name=cfg["name"],
                price=cfg["price"],
                tags=json.dumps(cfg["tags"]),
            )
            db.add(product)
            db.flush()
        product_ids[key] = product.id

    # Pre-seed two pending approval requests so the queue is populated on load
    DEMO_PENDING = [
        {
            "wallet_key": "procurement",
            "product_key": "large",
            "amount": 149.00,
            "quantity": 1,
            "intent": "provision software licenses for three new engineering hires starting Monday",
        },
        {
            "wallet_key": "research",
            "product_key": "medium",
            "amount": 65.00,
            "quantity": 1,
            "intent": "purchase competitor analysis report bundle ahead of Q2 planning session",
        },
    ]
    approval_ids = []
    for item in DEMO_PENDING:
        wid = wallet_ids[item["wallet_key"]]
        pid = product_ids[item["product_key"]]
        p = db.query(Product).filter(Product.id == pid).first()
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        result = policy_engine.evaluate_full(item["amount"], p.vendor_name, w, db)
        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            wallet_id=wid,
            agent_id=w.agent_id,
            product_id=pid,
            vendor_name=p.vendor_name,
            product_name=p.name,
            amount=item["amount"],
            idempotency_key=f"demo-approval-{item['wallet_key']}-{item['product_key']}-{item['amount']}",
            quantity=item["quantity"],
            intent=item["intent"],
            status="pending",
            reason=result.reason,
            policy_trace=json.dumps(result.trace),
        )
        db.add(req)
        # Reserve funds + create a matching hold so dashboard approvals always work.
        w.reserved_balance = float(w.reserved_balance or 0.0) + float(item["amount"])
        db.add(BalanceHold(
            id=str(uuid.uuid4()),
            wallet_id=wid,
            amount=item["amount"],
            kind="approval",
            status="active",
            approval_request_id=req.id,
        ))
        db.flush()
        approval_ids.append(req.id)

    db.commit()
    return {
        "wallets": wallet_ids,
        "wallet_keys": wallet_keys,
        "products": product_ids,
        "configs": {k: {**v, "id": wallet_ids[k]} for k, v in DEMO_WALLETS.items()},
        "pending_approvals": approval_ids,
    }

@app.get("/.well-known/agent-commerce.json")
def agent_manifest():
    """
    Lightweight agent-discovery manifest for tool-using agents.
    Does not contain secrets; intended to be safe to publish.
    """
    return {
        "name": "Agent Commerce",
        "purpose": "Spending governance + simulated payments for AI agents",
        "auth": {
            "admin": {
                "header": "X-Admin-Key",
                "env": "AGENT_COMMERCE_ADMIN_KEY",
                "default_demo_value": "demo-admin",
            },
            "wallet": {
                "header": "X-Wallet-Key",
                "how_to_get": "Returned once as wallet_key from POST /wallets (admin). Can be rotated via POST /wallets/{id}/rotate_key (admin).",
            },
        },
        "openapi": {
            "path": "/openapi.json",
            "notes": "Includes security scheme names AdminKeyAuth and WalletKeyAuth for tooling; endpoints still enforce auth via headers.",
        },
        "core_flows": [
            {
                "name": "Resolve + Purchase",
                "steps": [
                    {"method": "POST", "path": "/resolve", "auth": "wallet"},
                    {"method": "POST", "path": "/purchase", "auth": "wallet"},
                    {"method": "GET", "path": "/transactions?wallet_id=...", "auth": "wallet"},
                ],
            },
            {
                "name": "Approval Queue",
                "steps": [
                    {"method": "POST", "path": "/purchase", "auth": "wallet", "notes": "May return status=pending_approval"},
                    {"method": "GET", "path": "/approvals?status=pending", "auth": "admin"},
                    {"method": "POST", "path": "/approvals/{id}/approve", "auth": "admin"},
                ],
            },
        ],
        "payments": {
            "mode": "simulated_async",
            "transaction_fields": ["payment_status", "payment_intent_id", "settled_at"],
            "statuses": ["not_started", "processing", "succeeded", "failed"],
            "tick_endpoint": {"method": "POST", "path": "/simulate/tick", "auth": "admin"},
        },
        "webhooks": {
            "delivery": "at_least_once",
            "signature_header": "X-Agent-Commerce-Signature",
            "event_header": "X-Agent-Commerce-Event",
            "event_types": ["approval_required", "payment_succeeded", "payment_failed"],
            "signing": {"algo": "HMAC-SHA256", "format": "v1=<hex>", "secret": "wallet_key"},
        },
        "idempotency": {
            "field": "idempotency_key",
            "scope": "per wallet",
            "recommended": "UUID per attempted purchase; reuse on retry",
        },
    }


@app.get("/docs.txt", response_class=PlainTextResponse)
def docs_txt():
    """
    Plain-text agent-facing docs: cheap to fetch, easy to embed, no HTML parsing required.
    """
    return """AGENT COMMERCE — PLAIN TEXT DOCS

Quick Links:
- OpenAPI: /openapi.json
- Agent manifest: /.well-known/agent-commerce.json
- Human docs: /docs-ref

Auth:
- Admin endpoints: header X-Admin-Key (default demo-admin; configurable via env AGENT_COMMERCE_ADMIN_KEY)
- Agent endpoints: header X-Wallet-Key (wallet_key returned ONCE by POST /wallets; rotate via POST /wallets/{id}/rotate_key)

Core Flows:
1) Resolve (optional):
   POST /resolve  (X-Wallet-Key)
2) Purchase:
   POST /purchase (X-Wallet-Key) with idempotency_key
   - status=approved: returns transaction with payment_status=processing
   - status=blocked: returns transaction with reason + policy_trace
   - status=pending_approval: returns approval_request_id (funds are reserved)
3) Human Approval (snapshot semantics):
   GET /approvals?status=pending (X-Admin-Key)
   POST /approvals/{id}/approve  (X-Admin-Key)
   POST /approvals/{id}/reject   (X-Admin-Key)

Idempotency:
- Always include idempotency_key on POST /purchase.
- If a request times out or you get a network error, retry with the SAME idempotency_key.
- Idempotency is scoped per wallet.

Simulated Payments (async settlement):
- Transactions include: payment_status, payment_intent_id, settled_at
- payment_status transitions: not_started | processing | succeeded | failed
- Holds reserve funds: reserved_balance reduces available_balance on wallet
- Demo tick endpoint (admin): POST /simulate/tick

Webhooks:
- Delivery is at-least-once (duplicates can happen).
- Headers:
  - X-Agent-Commerce-Event: approval_required | payment_succeeded | payment_failed
  - X-Agent-Commerce-Signature: v1=<hex(hmac_sha256(wallet_key, request_body))>
- Always verify signature and dedupe by event id in the JSON payload.

Recipes:
Purchase With Retry:
- Generate UUID once; reuse on retries until you get a definitive response.

Handle Pending Approval:
- If status=pending_approval, notify a human and wait for settlement (webhook or poll /transactions).

Wait For Settlement:
- Poll /transactions?wallet_id=... until payment_status != processing (or rely on webhooks).
"""


@app.get("/demo")
def serve_demo():
    return FileResponse("demo.html")


# ─── Dashboard & Docs ─────────────────────────────────────────────────────────

@app.get("/")
def serve_dashboard():
    return FileResponse("dashboard.html")

@app.get("/docs-ref")
def serve_docs():
    return FileResponse("docs.html")


# ─── Serializers ──────────────────────────────────────────────────────────────

def _wallet_dict(w: Wallet) -> dict:
    reserved = float(w.reserved_balance or 0.0)
    available = float(w.balance or 0.0) - reserved
    return {
        "id": w.id,
        "agent_id": w.agent_id,
        "name": w.name,
        "balance": w.balance,
        "reserved_balance": reserved,
        "available_balance": available,
        "auto_approve_limit": w.auto_approve_limit,
        "spend_limit_max": w.spend_limit_max,
        "allowed_vendors": json.loads(w.allowed_vendors) if isinstance(w.allowed_vendors, str) else w.allowed_vendors,
        "status": w.status or "active",
        "daily_limit": w.daily_limit or 0.0,
        "weekly_limit": w.weekly_limit or 0.0,
        "velocity_max_txn": w.velocity_max_txn or 0,
        "webhook_url": w.webhook_url,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }

def _product_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "vendor_name": p.vendor_name,
        "name": p.name,
        "description": p.description,
        "price": p.price,
        "min_order": p.min_order,
        "lead_time_days": p.lead_time_days,
        "tags": json.loads(p.tags) if isinstance(p.tags, str) else p.tags,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }

def _txn_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
        "wallet_id": t.wallet_id,
        "agent_id": t.agent_id,
        "product_id": t.product_id,
        "vendor_name": t.vendor_name,
        "product_name": t.product_name,
        "amount": t.amount,
        "quantity": t.quantity,
        "intent": t.intent,
        "status": t.status,
        "payment_status": t.payment_status,
        "payment_intent_id": t.payment_intent_id,
        "reason": t.reason,
        "policy_trace": json.loads(t.policy_trace) if t.policy_trace else [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "settled_at": t.settled_at.isoformat() if t.settled_at else None,
    }

def _approval_dict(r: ApprovalRequest) -> dict:
    return {
        "id": r.id,
        "wallet_id": r.wallet_id,
        "agent_id": r.agent_id,
        "product_id": r.product_id,
        "vendor_name": r.vendor_name,
        "product_name": r.product_name,
        "amount": r.amount,
        "quantity": r.quantity,
        "intent": r.intent,
        "status": r.status,
        "reason": r.reason,
        "policy_trace": json.loads(r.policy_trace) if r.policy_trace else [],
        "reviewer_note": r.reviewer_note,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _enqueue_webhook(db: Session, wallet: Wallet, event_type: str, payload: dict) -> None:
    if not wallet.webhook_url:
        return
    evt = WebhookEvent(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        destination_url=wallet.webhook_url,
        event_type=event_type,
        payload=json.dumps({
            "id": str(uuid.uuid4()),
            "type": event_type,
            "created": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        }),
        status="pending",
        attempts=0,
        next_attempt_at=datetime.utcnow(),
    )
    db.add(evt)


def _simulate_outcome(wallet: Wallet, amount: float, vendor_name: str, override: Optional[str]) -> str:
    if override in ("succeed", "fail", "requires_action"):
        return override
    # Deterministic defaults for demos.
    if "exfil" in (vendor_name or "").lower():
        return "fail"
    if amount >= 250:
        return "requires_action"
    return "succeed"


def _create_payment_intent(
    db: Session,
    wallet: Wallet,
    txn: Transaction,
    amount: float,
    idempotency_key: Optional[str],
    simulate_outcome: Optional[str],
) -> PaymentIntent:
    # De-dupe payment intent creation on retries.
    if idempotency_key:
        existing = db.query(PaymentIntent).filter(
            PaymentIntent.wallet_id == wallet.id,
            PaymentIntent.idempotency_key == idempotency_key,
        ).first()
        if existing:
            return existing

    intent = PaymentIntent(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        transaction_id=txn.id,
        amount=amount,
        currency="usd",
        status="processing",
        idempotency_key=idempotency_key,
        settle_after=datetime.utcnow() + timedelta(seconds=1.0),
    )
    db.add(intent)
    db.flush()

    outcome = _simulate_outcome(wallet=wallet, amount=amount, vendor_name=txn.vendor_name or "", override=simulate_outcome)
    if outcome == "requires_action":
        # Simulate 3DS: longer processing, then succeed.
        intent.settle_after = datetime.utcnow() + timedelta(seconds=3.0)
    if outcome == "fail":
        intent.failure_code = "simulated_failure"

    db.commit()
    return intent


def _sign_webhook(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _process_webhook_outbox(db: Session, limit: int = 50) -> int:
    import requests as _req

    now = datetime.utcnow()
    q = db.query(WebhookEvent).filter(
        WebhookEvent.status.in_(["pending", "retry"]),
        WebhookEvent.next_attempt_at <= now,
    ).order_by(WebhookEvent.created_at.asc()).limit(limit)

    sent = 0
    for evt in q.all():
        wallet = db.query(Wallet).filter(Wallet.id == evt.wallet_id).first()
        secret = wallet.api_key if wallet and wallet.api_key else "demo"
        payload = evt.payload
        try:
            r = _req.post(
                evt.destination_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Commerce-Signature": _sign_webhook(payload, secret),
                    "X-Agent-Commerce-Event": evt.event_type,
                },
                timeout=3,
            )
            if 200 <= r.status_code < 300:
                evt.status = "sent"
                evt.sent_at = datetime.utcnow()
                evt.last_error = None
                sent += 1
            else:
                raise Exception(f"http {r.status_code}")
        except Exception as e:
            evt.attempts = (evt.attempts or 0) + 1
            evt.status = "retry" if evt.attempts < 8 else "failed"
            backoff = min(60, 2 ** min(evt.attempts, 6))
            evt.next_attempt_at = datetime.utcnow() + timedelta(seconds=backoff)
            evt.last_error = str(e)

    db.commit()
    return sent


def _settle_payments(db: Session, limit: int = 50) -> int:
    now = datetime.utcnow()
    intents = db.query(PaymentIntent).filter(
        PaymentIntent.status == "processing",
        PaymentIntent.settle_after <= now,
    ).order_by(PaymentIntent.created_at.asc()).limit(limit).all()

    settled = 0
    for intent in intents:
        txn = db.query(Transaction).filter(Transaction.id == intent.transaction_id).first()
        wallet = db.query(Wallet).filter(Wallet.id == intent.wallet_id).first()
        if not txn or not wallet:
            intent.status = "failed"
            intent.failure_code = "missing_wallet_or_txn"
            continue

        # Determine final outcome.
        fail = bool(intent.failure_code)
        if fail:
            # Release reserved funds.
            db.execute(text(
                "UPDATE wallets SET reserved_balance = COALESCE(reserved_balance, 0.0) - :amount "
                "WHERE id = :wallet_id AND COALESCE(reserved_balance, 0.0) >= :amount"
            ), {"amount": intent.amount, "wallet_id": wallet.id})
            intent.status = "failed"
            txn.payment_status = "failed"
            hold = db.query(BalanceHold).filter(
                BalanceHold.wallet_id == wallet.id,
                BalanceHold.transaction_id == txn.id,
                BalanceHold.status == "active",
            ).first()
            if hold:
                hold.status = "released"
                hold.released_at = datetime.utcnow()
            _enqueue_webhook(db, wallet, "payment_failed", {"payment_intent_id": intent.id, "transaction_id": txn.id, "amount": intent.amount})
        else:
            # Capture: debit wallet + release reservation atomically.
            updated = db.execute(text(
                "UPDATE wallets SET "
                "  reserved_balance = COALESCE(reserved_balance, 0.0) - :amount, "
                "  balance = balance - :amount "
                "WHERE id = :wallet_id AND COALESCE(reserved_balance, 0.0) >= :amount AND balance >= :amount"
            ), {"amount": intent.amount, "wallet_id": wallet.id}).rowcount
            if not updated:
                intent.status = "failed"
                intent.failure_code = "capture_failed"
                txn.payment_status = "failed"
                _enqueue_webhook(db, wallet, "payment_failed", {"payment_intent_id": intent.id, "transaction_id": txn.id, "amount": intent.amount, "failure_code": intent.failure_code})
            else:
                intent.status = "succeeded"
                txn.payment_status = "succeeded"
                txn.settled_at = datetime.utcnow()
                hold = db.query(BalanceHold).filter(
                    BalanceHold.wallet_id == wallet.id,
                    BalanceHold.transaction_id == txn.id,
                    BalanceHold.status == "active",
                ).first()
                if hold:
                    hold.status = "captured"
                _enqueue_webhook(db, wallet, "payment_succeeded", {"payment_intent_id": intent.id, "transaction_id": txn.id, "amount": intent.amount})

        settled += 1

    db.commit()
    return settled


@app.post("/simulate/tick")
def simulate_tick(db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    """Advance simulated payment settlement and webhook delivery."""
    settled = _settle_payments(db)
    sent = _process_webhook_outbox(db)
    return {"settled": settled, "webhooks_sent": sent}


@app.get("/payments/intents")
def list_payment_intents(limit: int = 50, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    rows = db.query(PaymentIntent).order_by(PaymentIntent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": i.id,
            "wallet_id": i.wallet_id,
            "transaction_id": i.transaction_id,
            "amount": i.amount,
            "currency": i.currency,
            "status": i.status,
            "failure_code": i.failure_code,
            "idempotency_key": i.idempotency_key,
            "settle_after": i.settle_after.isoformat() if i.settle_after else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in rows
    ]


@app.get("/payments/webhooks")
def list_webhook_events(limit: int = 50, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    rows = db.query(WebhookEvent).order_by(WebhookEvent.created_at.desc()).limit(limit).all()
    return [
        {
            "id": e.id,
            "wallet_id": e.wallet_id,
            "destination_url": e.destination_url,
            "event_type": e.event_type,
            "status": e.status,
            "attempts": e.attempts,
            "next_attempt_at": e.next_attempt_at.isoformat() if e.next_attempt_at else None,
            "last_error": e.last_error,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
        }
        for e in rows
    ]


def _payments_worker_loop(poll_seconds: float = 0.5) -> None:
    while True:
        try:
            db = SessionLocal()
            try:
                _settle_payments(db)
                _process_webhook_outbox(db)
            finally:
                db.close()
        except Exception:
            # Best-effort worker; endpoints still work and /simulate/tick can be used.
            pass
        time.sleep(poll_seconds)


@app.on_event("startup")
def _start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    if os.environ.get("AGENT_COMMERCE_WORKER", "1") not in ("1", "true", "yes", "on"):
        return
    t = threading.Thread(target=_payments_worker_loop, name="payments-worker", daemon=True)
    t.start()
    _worker_started = True
