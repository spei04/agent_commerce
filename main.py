import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import jwt
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import text, func
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from database import get_db, SessionLocal
from models import (
    Organization,
    User,
    Membership,
    Agent,
    Wallet,
    ApiKey,
    Product,
    ApprovalRequest,
    BalanceHold,
    Transaction,
    PaymentAuthorization,
    PaymentCapture,
    AchPayout,
    WebhookEvent,
)


JWT_SECRET = os.environ.get("JWT_SECRET", "dev-jwt-secret-change-me-please-use-32-bytes")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "agent-commerce")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "3600"))
DEMO_MODE = os.environ.get("DEMO_MODE", "1") in ("1", "true", "yes", "on")

# Use PBKDF2 for the MVP to avoid bcrypt backend compatibility issues across environments.
# In production: consider argon2id (with passlib[argon2]) or bcrypt with pinned versions.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

app = FastAPI(title="Agent Commerce", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _now() -> datetime:
    return datetime.utcnow()


def _to_cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def _from_cents(cents: int) -> float:
    return round(float(cents) / 100.0, 2)


def _hash_api_secret(secret: str) -> str:
    pepper = os.environ.get("API_KEY_PEPPER", "dev-pepper-change-me")
    return hashlib.sha256((pepper + secret).encode("utf-8")).hexdigest()


def _parse_wallet_key(raw: str) -> tuple[str, str]:
    """
    Wallet key format: wk_<key_id>.<secret>
    """
    if not raw or not raw.startswith("wk_") or "." not in raw:
        raise HTTPException(status_code=401, detail="missing/invalid wallet key")
    prefix, secret = raw.split(".", 1)
    key_id = prefix.removeprefix("wk_")
    if not key_id or not secret:
        raise HTTPException(status_code=401, detail="missing/invalid wallet key")
    return key_id, secret


def _sign_webhook(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _issue_jwt(user_id: str, org_id: str, roles: list[str]) -> str:
    now = int(time.time())
    payload = {
        "iss": JWT_ISSUER,
        "sub": user_id,
        "org_id": org_id,
        "roles": roles,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], issuer=JWT_ISSUER)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")


class AuthLogin(BaseModel):
    email: str
    password: str
    org_id: Optional[str] = None


def _require_bearer(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return _decode_jwt(token)


def require_user(
    claims: dict = Depends(_require_bearer),
    db: Session = Depends(get_db),
) -> tuple[User, str, list[str]]:
    user = db.query(User).filter(User.id == claims["sub"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="unknown user")
    org_id = claims.get("org_id")
    roles = claims.get("roles") or []
    if not org_id:
        raise HTTPException(status_code=401, detail="missing org scope")
    return user, org_id, roles


def require_role(required: str):
    def _dep(ctx=Depends(require_user)):
        _, _, roles = ctx
        if required not in roles and "admin" not in roles:
            raise HTTPException(status_code=403, detail="insufficient role")
        return ctx

    return _dep


def require_wallet_from_key(
    x_wallet_key: Optional[str] = Header(None, alias="X-Wallet-Key"),
    db: Session = Depends(get_db),
) -> tuple[Wallet, ApiKey]:
    if not x_wallet_key:
        raise HTTPException(status_code=401, detail="missing wallet key")
    key_id, secret = _parse_wallet_key(x_wallet_key)
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key or api_key.revoked_at:
        raise HTTPException(status_code=401, detail="missing/invalid wallet key")
    expected = api_key.secret_hash
    actual = _hash_api_secret(secret)
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=401, detail="missing/invalid wallet key")
    api_key.last_used_at = _now()
    wallet = db.query(Wallet).filter(Wallet.id == api_key.wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=401, detail="wallet not found for key")
    db.commit()
    return wallet, api_key


def _ensure_demo_seed(db: Session) -> None:
    if not DEMO_MODE:
        return
    org = db.query(Organization).filter(Organization.name == "Demo Org").first()
    if not org:
        org = Organization(id=str(uuid.uuid4()), name="Demo Org")
        db.add(org)
        db.commit()
    demo_user = db.query(User).filter(User.email == "demo@agentcommerce.local").first()
    if not demo_user:
        demo_user = User(
            id=str(uuid.uuid4()),
            email="demo@agentcommerce.local",
            password_hash=pwd_context.hash("demo-password"),
        )
        db.add(demo_user)
        db.commit()
    else:
        # If the demo user already exists from an older run, ensure the password matches the current hasher.
        # This prevents "mysterious 401s" in the UI when the local DB persists across iterations.
        try:
            ok = pwd_context.verify("demo-password", demo_user.password_hash)
        except Exception:
            ok = False
        if not ok:
            demo_user.password_hash = pwd_context.hash("demo-password")
            db.commit()

    # Ensure demo memberships exist (admin + approver).
    existing_roles = {
        m.role
        for m in db.query(Membership)
        .filter(Membership.org_id == org.id, Membership.user_id == demo_user.id)
        .all()
    }
    for role in ("admin", "approver"):
        if role not in existing_roles:
            db.add(Membership(id=str(uuid.uuid4()), org_id=org.id, user_id=demo_user.id, role=role))
    db.commit()


@app.on_event("startup")
def _startup() -> None:
    db = SessionLocal()
    try:
        _ensure_demo_seed(db)
    finally:
        db.close()


def _require_admin_or_auditor(ctx):
    _, _, roles = ctx
    if "admin" not in roles and "auditor" not in roles:
        raise HTTPException(status_code=403, detail="admin/auditor required")
    return ctx


# ─── Auth ────────────────────────────────────────────────────────────────────


@app.post("/auth/login")
def auth_login(data: AuthLogin, db: Session = Depends(get_db)):
    _ensure_demo_seed(db)
    user = db.query(User).filter(func.lower(User.email) == data.email.lower()).first()
    if not user or not pwd_context.verify(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")

    memberships = db.query(Membership).filter(Membership.user_id == user.id).all()
    if not memberships:
        raise HTTPException(status_code=403, detail="no org membership")

    org_id = data.org_id or memberships[0].org_id
    roles = [m.role for m in memberships if m.org_id == org_id]
    if not roles:
        raise HTTPException(status_code=403, detail="not a member of that org")
    token = _issue_jwt(user.id, org_id, roles)
    return {"access_token": token, "token_type": "bearer", "org_id": org_id, "roles": roles}


@app.post("/auth/logout")
def auth_logout(ctx=Depends(require_user)):
    # Stateless JWTs: logout is a client-side concern in this MVP.
    # vNext: add token revocation list / refresh tokens.
    return {"ok": True}


@app.get("/me")
def me(ctx=Depends(require_user), db: Session = Depends(get_db)):
    user, org_id, roles = ctx
    memberships = db.query(Membership).filter(Membership.user_id == user.id).all()
    return {
        "user": {"id": user.id, "email": user.email},
        "active_org_id": org_id,
        "active_roles": roles,
        "memberships": [{"org_id": m.org_id, "role": m.role} for m in memberships],
    }


# ─── Orgs / Agents / Wallets ────────────────────────────────────────────────


class OrgCreate(BaseModel):
    name: str


@app.post("/orgs")
def create_org(data: OrgCreate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    user, _, _ = ctx
    org = Organization(id=str(uuid.uuid4()), name=data.name)
    db.add(org)
    db.commit()
    db.add(Membership(id=str(uuid.uuid4()), org_id=org.id, user_id=user.id, role="admin"))
    db.commit()
    return {"id": org.id, "name": org.name}


class AgentCreate(BaseModel):
    external_agent_id: str
    name: str
    metadata: dict = Field(default_factory=dict)


@app.get("/agents")
def list_agents(ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    rows = db.query(Agent).filter(Agent.org_id == org_id).order_by(Agent.created_at.desc()).all()
    return [_agent_dict(a) for a in rows]


@app.post("/agents")
def create_agent(data: AgentCreate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    existing = db.query(Agent).filter(Agent.org_id == org_id, Agent.external_agent_id == data.external_agent_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="external_agent_id already exists")
    a = Agent(
        id=str(uuid.uuid4()),
        org_id=org_id,
        external_agent_id=data.external_agent_id,
        name=data.name,
        agent_metadata=json.dumps(data.metadata),
    )
    db.add(a)
    db.commit()
    return _agent_dict(a)


class WalletCreate(BaseModel):
    agent_id: str
    purpose: str
    name: str
    currency: str = "usd"
    balance_cents: Optional[int] = None
    balance: Optional[float] = None
    auto_approve_limit_cents: Optional[int] = None
    auto_approve_limit: Optional[float] = None
    spend_limit_max_cents: Optional[int] = None
    spend_limit_max: Optional[float] = None
    allowed_vendors: List[str] = Field(default_factory=list)
    daily_limit_cents: int = 0
    weekly_limit_cents: int = 0
    velocity_max_txn: int = 0
    webhook_url: Optional[str] = None


@app.post("/wallets")
def create_wallet(data: WalletCreate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    agent = db.query(Agent).filter(Agent.org_id == org_id, Agent.id == data.agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    purpose = data.purpose
    if purpose not in {"procurement", "cloud", "data", "travel", "other"}:
        raise HTTPException(status_code=400, detail="invalid purpose")

    balance_cents = data.balance_cents if data.balance_cents is not None else (_to_cents(data.balance) if data.balance is not None else 0)
    auto_cents = data.auto_approve_limit_cents if data.auto_approve_limit_cents is not None else (_to_cents(data.auto_approve_limit) if data.auto_approve_limit is not None else 2000)
    max_cents = data.spend_limit_max_cents if data.spend_limit_max_cents is not None else (_to_cents(data.spend_limit_max) if data.spend_limit_max is not None else 10000)

    webhook_secret = None
    if data.webhook_url:
        webhook_secret = secrets.token_urlsafe(24)

    w = Wallet(
        id=str(uuid.uuid4()),
        org_id=org_id,
        agent_id=agent.id,
        purpose=purpose,
        name=data.name,
        currency=data.currency,
        balance_cents=balance_cents,
        reserved_cents=0,
        auto_approve_limit_cents=auto_cents,
        spend_limit_max_cents=max_cents,
        allowed_vendors=json.dumps(data.allowed_vendors),
        daily_limit_cents=data.daily_limit_cents,
        weekly_limit_cents=data.weekly_limit_cents,
        velocity_max_txn=data.velocity_max_txn,
        webhook_url=data.webhook_url,
        webhook_signing_secret=webhook_secret,
        webhook_secret_rotated_at=_now() if webhook_secret else None,
    )
    db.add(w)
    db.commit()
    resp = _wallet_dict(w)
    # Only returned once, on creation (or via explicit rotation endpoint).
    if webhook_secret:
        resp["webhook_signing_secret"] = webhook_secret
    return resp


@app.get("/agents/{agent_id}/wallets")
def agent_wallets(agent_id: str, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    agent = db.query(Agent).filter(Agent.org_id == org_id, Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    wallets = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.agent_id == agent.id).all()
    return [_wallet_dict(w) for w in wallets]


class ApiKeyCreate(BaseModel):
    name: str = "default"


@app.post("/wallets/{wallet_id}/keys")
def create_wallet_key(wallet_id: str, data: ApiKeyCreate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")

    secret = secrets.token_urlsafe(24)
    key = ApiKey(
        id=str(uuid.uuid4()),
        wallet_id=wallet.id,
        name=data.name,
        prefix=secrets.token_hex(4),
        secret_hash=_hash_api_secret(secret),
    )
    db.add(key)
    db.commit()
    return {"id": key.id, "wallet_id": wallet.id, "name": key.name, "wallet_key": f"wk_{key.id}.{secret}"}


@app.get("/wallets/{wallet_id}/keys")
def list_wallet_keys(wallet_id: str, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")
    keys = db.query(ApiKey).filter(ApiKey.wallet_id == wallet.id).order_by(ApiKey.created_at.desc()).all()
    return [
        {"id": k.id, "name": k.name, "created_at": k.created_at.isoformat(), "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None, "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}
        for k in keys
    ]


@app.post("/wallets/{wallet_id}/keys/{key_id}/revoke")
def revoke_wallet_key(wallet_id: str, key_id: str, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")
    k = db.query(ApiKey).filter(ApiKey.wallet_id == wallet.id, ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(status_code=404, detail="key not found")
    k.revoked_at = _now()
    db.commit()
    return {"revoked": True}


@app.get("/wallets/{wallet_id}")
def get_wallet(wallet_id: str, ctx=Depends(require_user), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    w = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="wallet not found")
    return _wallet_dict(w)


class WebhookConfigUpdate(BaseModel):
    webhook_url: Optional[str] = None
    rotate_secret: bool = False


@app.post("/wallets/{wallet_id}/webhook")
def configure_wallet_webhook(wallet_id: str, data: WebhookConfigUpdate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")
    if data.webhook_url is not None:
        wallet.webhook_url = data.webhook_url
    secret = None
    if data.rotate_secret or (wallet.webhook_url and not wallet.webhook_signing_secret):
        secret = secrets.token_urlsafe(24)
        wallet.webhook_signing_secret = secret
        wallet.webhook_secret_rotated_at = _now()
    db.commit()
    resp = _wallet_dict(wallet)
    if secret:
        resp["webhook_signing_secret"] = secret
    return resp


@app.post("/wallets/{wallet_id}/webhook/rotate-secret")
def rotate_wallet_webhook_secret(wallet_id: str, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")
    secret = secrets.token_urlsafe(24)
    wallet.webhook_signing_secret = secret
    wallet.webhook_secret_rotated_at = _now()
    db.commit()
    return {"wallet_id": wallet.id, "webhook_signing_secret": secret}


# ─── Products ────────────────────────────────────────────────────────────────


class ProductCreate(BaseModel):
    vendor_name: str
    name: str
    description: Optional[str] = None
    price_cents: Optional[int] = None
    price: Optional[float] = None
    min_order: int = 1
    lead_time_days: int = 1
    tags: List[str] = Field(default_factory=list)


@app.post("/products")
def create_product(data: ProductCreate, ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    price_cents = data.price_cents if data.price_cents is not None else _to_cents(data.price or 0.0)
    p = Product(
        id=str(uuid.uuid4()),
        org_id=org_id,
        vendor_name=data.vendor_name,
        name=data.name,
        description=data.description,
        price_cents=price_cents,
        min_order=data.min_order,
        lead_time_days=data.lead_time_days,
        tags=json.dumps(data.tags),
    )
    db.add(p)
    db.commit()
    return _product_dict(p)


@app.get("/products")
def list_products(ctx=Depends(require_user), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    rows = db.query(Product).filter(Product.org_id == org_id).order_by(Product.created_at.desc()).all()
    return [_product_dict(p) for p in rows]


# ─── Resolve + Purchase (Agent) ──────────────────────────────────────────────


class IntentRequest(BaseModel):
    intent: str
    budget_cents: Optional[int] = None
    budget: Optional[float] = None
    constraints: dict = Field(default_factory=dict)
    limit: int = 5


@app.post("/resolve")
def resolve_intent(data: IntentRequest, auth=Depends(require_wallet_from_key), db: Session = Depends(get_db)):
    wallet, _ = auth
    budget_cents = data.budget_cents if data.budget_cents is not None else (_to_cents(data.budget) if data.budget is not None else None)
    # Simple resolver: string matching on tags/name/desc (kept from v1, org-scoped products).
    products = db.query(Product).filter(Product.org_id == wallet.org_id).all()
    intent_lower = data.intent.lower()
    scored = []
    for p in products:
        tags = json.loads(p.tags)
        if budget_cents is not None and p.price_cents > budget_cents:
            continue
        score = 0.0
        for t in tags:
            if t.lower() in intent_lower:
                score += 2.0
        for word in intent_lower.split():
            if len(word) > 3:
                if word in p.name.lower():
                    score += 1.5
                if word in (p.description or "").lower():
                    score += 0.5
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: data.limit]
    return {"matches": [{"product_id": p.id, "vendor_name": p.vendor_name, "name": p.name, "price_cents": p.price_cents, "price": _from_cents(p.price_cents), "score": round(s, 2)} for s, p in top]}


class PurchaseRequest(BaseModel):
    product_id: str
    quantity: int = 1
    intent: Optional[str] = None
    idempotency_key: Optional[str] = None
    rail: str = "card"  # card|ach


@app.post("/purchase")
def purchase(data: PurchaseRequest, auth=Depends(require_wallet_from_key), db: Session = Depends(get_db)):
    wallet, _ = auth
    if data.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")

    if data.idempotency_key:
        existing = db.query(Transaction).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.idempotency_key == data.idempotency_key,
        ).first()
        if existing:
            return _txn_dict(existing)

    product = db.query(Product).filter(Product.org_id == wallet.org_id, Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="product not found")

    amount_cents = int(product.price_cents) * int(data.quantity)
    policy = _evaluate_policy(db=db, wallet=wallet, amount_cents=amount_cents, vendor_name=product.vendor_name)

    if policy["decision"] == "requires_approval":
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
                    "reason": existing_req.reason,
                    "amount_cents": existing_req.amount_cents,
                    "amount": _from_cents(existing_req.amount_cents),
                    "vendor_name": existing_req.vendor_name,
                    "product_name": existing_req.product_name,
                    "policy_trace": json.loads(existing_req.policy_trace or "[]"),
                }

        _reserve_or_block(db, wallet, amount_cents, trace=policy["trace"])
        req = ApprovalRequest(
            id=str(uuid.uuid4()),
            org_id=wallet.org_id,
            wallet_id=wallet.id,
            agent_id=wallet.agent_id,
            product_id=product.id,
            vendor_name=product.vendor_name,
            product_name=product.name,
            amount_cents=amount_cents,
            idempotency_key=data.idempotency_key,
            quantity=data.quantity,
            intent=data.intent,
            status="pending",
            reason=policy["reason"],
            policy_trace=json.dumps(policy["trace"]),
        )
        db.add(req)
        db.add(BalanceHold(
            id=str(uuid.uuid4()),
            org_id=wallet.org_id,
            wallet_id=wallet.id,
            amount_cents=amount_cents,
            kind="approval",
            status="active",
            approval_request_id=req.id,
        ))
        _enqueue_webhook(db, wallet, "approval_required", {"approval_request_id": req.id, "amount_cents": amount_cents})
        db.commit()
        return {
            "status": "pending_approval",
            "approval_request_id": req.id,
            "reason": req.reason,
            "amount_cents": amount_cents,
            "amount": _from_cents(amount_cents),
            "vendor_name": product.vendor_name,
            "product_name": product.name,
            "policy_trace": policy["trace"],
        }

    # Auto-approve or blocked => create transaction now.
    txn = Transaction(
        id=str(uuid.uuid4()),
        org_id=wallet.org_id,
        wallet_id=wallet.id,
        agent_id=wallet.agent_id,
        product_id=product.id,
        vendor_name=product.vendor_name,
        product_name=product.name,
        amount_cents=amount_cents,
        idempotency_key=data.idempotency_key,
        quantity=data.quantity,
        intent=data.intent,
        status="approved" if policy["decision"] == "auto_approve" else "blocked",
        payment_status="not_started",
        reason=policy["reason"],
        policy_trace=json.dumps(policy["trace"]),
    )
    db.add(txn)

    if txn.status == "approved":
        _reserve_or_block(db, wallet, amount_cents, trace=policy["trace"])
        hold = BalanceHold(
            id=str(uuid.uuid4()),
            org_id=wallet.org_id,
            wallet_id=wallet.id,
            amount_cents=amount_cents,
            kind="auth",
            status="active",
            transaction_id=txn.id,
        )
        db.add(hold)
        authz = _rail_authorize(db, wallet, txn, amount_cents, idempotency_key=data.idempotency_key)
        txn.payment_status = "processing"
        txn.payment_ref = authz.id
        _enqueue_webhook(db, wallet, "payment.authorization.created", {"authorization_id": authz.id, "transaction_id": txn.id, "amount_cents": amount_cents})

    db.commit()
    return _txn_dict(txn)


@app.get("/agent/transactions")
def agent_transactions(limit: int = 50, auth=Depends(require_wallet_from_key), db: Session = Depends(get_db)):
    wallet, _ = auth
    rows = (
        db.query(Transaction)
        .filter(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [_txn_dict(t) for t in rows]


# ─── Approvals (Human) ───────────────────────────────────────────────────────


@app.get("/approvals")
def list_approvals(status: str = "pending", ctx=Depends(require_role("approver")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    q = db.query(ApprovalRequest).filter(ApprovalRequest.org_id == org_id)
    if status:
        q = q.filter(ApprovalRequest.status == status)
    return [_approval_dict(r) for r in q.order_by(ApprovalRequest.created_at.desc()).all()]


class ApprovalAction(BaseModel):
    note: Optional[str] = None
    rail: str = "card"  # card|ach


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, data: ApprovalAction, ctx=Depends(require_role("approver")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    req = db.query(ApprovalRequest).filter(ApprovalRequest.org_id == org_id, ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="approval not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"already {req.status}")

    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == req.wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")

    hold = db.query(BalanceHold).filter(BalanceHold.approval_request_id == req.id, BalanceHold.status == "active").first()
    if not hold:
        raise HTTPException(status_code=409, detail="missing hold for approval")

    txn = Transaction(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=req.wallet_id,
        agent_id=req.agent_id,
        product_id=req.product_id,
        vendor_name=req.vendor_name,
        product_name=req.product_name,
        amount_cents=req.amount_cents,
        idempotency_key=req.idempotency_key,
        quantity=req.quantity,
        intent=req.intent,
        status="approved",
        payment_status="processing",
        reason="approved by reviewer",
        policy_trace=req.policy_trace,
    )
    db.add(txn)
    hold.transaction_id = txn.id

    if data.rail == "ach":
        payout = _rail_ach_submit(db, wallet, req, idempotency_key=req.idempotency_key)
        txn.payment_ref = payout.id
        _enqueue_webhook(db, wallet, "payout.submitted", {"payout_id": payout.id, "approval_request_id": req.id, "amount_cents": req.amount_cents})
    else:
        authz = _rail_authorize(db, wallet, txn, req.amount_cents, idempotency_key=req.idempotency_key)
        txn.payment_ref = authz.id
        _enqueue_webhook(db, wallet, "payment.authorization.created", {"authorization_id": authz.id, "transaction_id": txn.id, "amount_cents": req.amount_cents})

    req.status = "approved"
    req.reviewer_note = data.note
    req.reviewed_at = _now()
    db.commit()
    return {"approved": True, "transaction": _txn_dict(txn)}


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, data: ApprovalAction, ctx=Depends(require_role("approver")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    req = db.query(ApprovalRequest).filter(ApprovalRequest.org_id == org_id, ApprovalRequest.id == approval_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="approval not found")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail=f"already {req.status}")

    wallet = db.query(Wallet).filter(Wallet.org_id == org_id, Wallet.id == req.wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet not found")

    hold = db.query(BalanceHold).filter(BalanceHold.approval_request_id == req.id, BalanceHold.status == "active").first()
    if hold:
        _release_hold(db, wallet, hold.amount_cents)
        hold.status = "released"
        hold.released_at = _now()

    txn = Transaction(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=req.wallet_id,
        agent_id=req.agent_id,
        product_id=req.product_id,
        vendor_name=req.vendor_name,
        product_name=req.product_name,
        amount_cents=req.amount_cents,
        idempotency_key=req.idempotency_key,
        quantity=req.quantity,
        intent=req.intent,
        status="blocked",
        payment_status="not_started",
        reason=f"rejected by reviewer: {data.note or 'no reason given'}",
        policy_trace=req.policy_trace,
    )
    db.add(txn)

    req.status = "rejected"
    req.reviewer_note = data.note
    req.reviewed_at = _now()
    db.commit()
    return {"rejected": True, "transaction": _txn_dict(txn)}


# ─── Transactions / Payments / Webhooks (Human) ──────────────────────────────


@app.get("/transactions")
def list_transactions(wallet_id: Optional[str] = None, limit: int = 50, ctx=Depends(require_user), db: Session = Depends(get_db)):
    _, org_id, roles = ctx
    q = db.query(Transaction).filter(Transaction.org_id == org_id)
    if wallet_id:
        q = q.filter(Transaction.wallet_id == wallet_id)
    else:
        if "admin" not in roles and "auditor" not in roles:
            raise HTTPException(status_code=403, detail="wallet_id required unless admin/auditor")
    rows = q.order_by(Transaction.created_at.desc()).limit(limit).all()
    return [_txn_dict(t) for t in rows]


@app.get("/dashboard/summary")
def dashboard_summary(ctx=Depends(require_user), db: Session = Depends(get_db)):
    _require_admin_or_auditor(ctx)
    _, org_id, _ = ctx
    all_txns = db.query(Transaction).filter(Transaction.org_id == org_id).all()
    approved = [t for t in all_txns if t.status == "approved"]
    blocked = [t for t in all_txns if t.status == "blocked"]
    pending = db.query(ApprovalRequest).filter(ApprovalRequest.org_id == org_id, ApprovalRequest.status == "pending").count()
    total_volume = sum(t.amount_cents for t in approved if (t.payment_status or "") != "failed")
    return {
        "total_volume_cents": int(total_volume),
        "total_volume": _from_cents(int(total_volume)),
        "total_transactions": len(all_txns),
        "approved_count": len(approved),
        "blocked_count": len(blocked),
        "pending_approvals": pending,
    }


@app.get("/intents")
def list_intents(limit: int = 50, ctx=Depends(require_user)):
    # vNext: persist /resolve calls for analytics. This stub keeps the demo dashboard simple.
    _require_admin_or_auditor(ctx)
    return []


@app.post("/demo/reset")
def demo_reset(ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    """
    Seed a small-scale demo dataset inside the caller's active org.
    Returns wallet ids + fresh wallet keys for the demo UI.
    """
    _, org_id, _ = ctx

    # Clear org data (demo-only convenience).
    db.query(WebhookEvent).filter(WebhookEvent.org_id == org_id).delete()
    db.query(AchPayout).filter(AchPayout.org_id == org_id).delete()
    db.query(PaymentCapture).filter(PaymentCapture.org_id == org_id).delete()
    db.query(PaymentAuthorization).filter(PaymentAuthorization.org_id == org_id).delete()
    db.query(Transaction).filter(Transaction.org_id == org_id).delete()
    db.query(BalanceHold).filter(BalanceHold.org_id == org_id).delete()
    db.query(ApprovalRequest).filter(ApprovalRequest.org_id == org_id).delete()
    db.query(ApiKey).filter(ApiKey.wallet_id.in_(db.query(Wallet.id).filter(Wallet.org_id == org_id))).delete(synchronize_session=False)
    db.query(Product).filter(Product.org_id == org_id).delete()
    db.query(Wallet).filter(Wallet.org_id == org_id).delete()
    db.query(Agent).filter(Agent.org_id == org_id).delete()
    db.commit()

    # Agents
    procurement = Agent(id=str(uuid.uuid4()), org_id=org_id, external_agent_id="demo-procure-bot", name="Demo Procurement Agent", metadata="{}")
    research = Agent(id=str(uuid.uuid4()), org_id=org_id, external_agent_id="demo-research-bot", name="Demo Research Agent", metadata="{}")
    db.add_all([procurement, research])
    db.commit()

    # Wallets
    w_proc = Wallet(
        id=str(uuid.uuid4()),
        org_id=org_id,
        agent_id=procurement.id,
        purpose="procurement",
        name="Procurement Wallet",
        balance_cents=50_000,
        reserved_cents=0,
        auto_approve_limit_cents=5_000,
        spend_limit_max_cents=30_000,
        daily_limit_cents=7_000,
        allowed_vendors="[]",
        velocity_max_txn=3,
        webhook_url=None,
    )
    w_res = Wallet(
        id=str(uuid.uuid4()),
        org_id=org_id,
        agent_id=research.id,
        purpose="data",
        name="Research Wallet",
        balance_cents=20_000,
        reserved_cents=0,
        auto_approve_limit_cents=2_000,
        spend_limit_max_cents=10_000,
        allowed_vendors=json.dumps(["arxiv-reports", "data-warehouse.io"]),
        webhook_url=None,
    )
    db.add_all([w_proc, w_res])
    db.commit()

    # Keys
    def mk_key(wallet: Wallet) -> str:
        secret = secrets.token_urlsafe(24)
        k = ApiKey(id=str(uuid.uuid4()), wallet_id=wallet.id, name="demo", prefix=secrets.token_hex(4), secret_hash=_hash_api_secret(secret))
        db.add(k)
        db.commit()
        return f"wk_{k.id}.{secret}"

    keys = {"procurement": mk_key(w_proc), "research": mk_key(w_res)}

    # Products
    products = [
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="packright.com", name="Demo: Corrugated Mailer Box", description=None, price_cents=800, tags=json.dumps(["packaging", "boxes"]), lead_time_days=3, min_order=1),
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="arxiv-reports", name="Demo: Market Research Report", description=None, price_cents=4500, tags=json.dumps(["research", "report"]), lead_time_days=1, min_order=1),
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="cloudsoft.io", name="Demo: Enterprise License", description=None, price_cents=32000, tags=json.dumps(["software", "license"]), lead_time_days=1, min_order=1),
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="exfil-data.io", name="Demo: Restricted Dataset", description=None, price_cents=1500, tags=json.dumps(["data", "dataset"]), lead_time_days=1, min_order=1),
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="shipthat.com", name="Demo: Express Shipping Bundle", description=None, price_cents=3000, tags=json.dumps(["shipping", "bundle"]), lead_time_days=1, min_order=1),
        Product(id=str(uuid.uuid4()), org_id=org_id, vendor_name="cloud-provider.com", name="Demo: API Credits 1000 units", description=None, price_cents=1200, tags=json.dumps(["api", "credits"]), lead_time_days=0, min_order=1),
    ]
    db.add_all(products)
    db.commit()

    # Seed one pending approval (reserve + hold).
    approval_amount = 6_500
    _reserve_or_block(db, w_res, approval_amount, trace=[])
    req = ApprovalRequest(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=w_res.id,
        agent_id=w_res.agent_id,
        product_id=products[1].id,
        vendor_name=products[1].vendor_name,
        product_name=products[1].name,
        amount_cents=approval_amount,
        idempotency_key="demo-approval-1",
        quantity=1,
        intent="purchase competitor analysis report bundle",
        status="pending",
        reason="requires approval",
        policy_trace=json.dumps([]),
    )
    db.add(req)
    db.add(BalanceHold(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=w_res.id,
        amount_cents=approval_amount,
        kind="approval",
        status="active",
        approval_request_id=req.id,
    ))
    db.commit()

    # Seed a couple of transactions so the demo has real data immediately.
    # 1) Auto-approved procurement purchase (authorized, will capture on next tick).
    auto_amount = products[0].price_cents  # packright.com $8.00
    _reserve_or_block(db, w_proc, int(auto_amount), trace=[])
    t1 = Transaction(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=w_proc.id,
        agent_id=w_proc.agent_id,
        product_id=products[0].id,
        vendor_name=products[0].vendor_name,
        product_name=products[0].name,
        amount_cents=int(auto_amount),
        idempotency_key="demo-txn-1",
        quantity=1,
        intent="reorder packaging supplies (demo seed)",
        status="approved",
        payment_status="processing",
        reason="within policy",
        policy_trace=json.dumps([{"check": "demo_seed", "passed": True, "detail": "seeded transaction"}]),
    )
    db.add(t1)
    db.add(BalanceHold(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=w_proc.id,
        amount_cents=int(auto_amount),
        kind="auth",
        status="active",
        transaction_id=t1.id,
    ))
    a1 = _rail_authorize(db, w_proc, t1, int(auto_amount), idempotency_key=t1.idempotency_key)
    t1.payment_ref = a1.id
    _enqueue_webhook(db, w_proc, "payment.authorization.created", {"authorization_id": a1.id, "transaction_id": t1.id, "amount_cents": int(auto_amount)})

    # 2) Blocked research purchase (vendor not allowed).
    blocked_amount = products[3].price_cents  # exfil-data.io restricted dataset
    t2 = Transaction(
        id=str(uuid.uuid4()),
        org_id=org_id,
        wallet_id=w_res.id,
        agent_id=w_res.agent_id,
        product_id=products[3].id,
        vendor_name=products[3].vendor_name,
        product_name=products[3].name,
        amount_cents=int(blocked_amount),
        idempotency_key="demo-txn-2",
        quantity=1,
        intent="attempt to purchase restricted dataset (demo seed)",
        status="blocked",
        payment_status="not_started",
        reason="vendor not allowed",
        policy_trace=json.dumps([{"check": "vendor_allowlist", "passed": False, "detail": "exfil-data.io not in allowlist"}]),
    )
    db.add(t2)
    db.commit()

    return {
        "org_id": org_id,
        "agents": {"procurement": _agent_dict(procurement), "research": _agent_dict(research)},
        "wallets": {"procurement": _wallet_dict(w_proc), "research": _wallet_dict(w_res)},
        "wallet_keys": keys,
        "products": [_product_dict(p) for p in products],
        "pending_approval_id": req.id,
    }


@app.get("/payments/authorizations")
def list_authorizations(limit: int = 50, ctx=Depends(require_role("auditor")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    rows = db.query(PaymentAuthorization).filter(PaymentAuthorization.org_id == org_id).order_by(PaymentAuthorization.created_at.desc()).limit(limit).all()
    return [{"id": a.id, "transaction_id": a.transaction_id, "amount_cents": a.amount_cents, "status": a.status, "created_at": a.created_at.isoformat()} for a in rows]


@app.get("/payments/payouts")
def list_payouts(limit: int = 50, ctx=Depends(require_role("auditor")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    rows = db.query(AchPayout).filter(AchPayout.org_id == org_id).order_by(AchPayout.created_at.desc()).limit(limit).all()
    return [{"id": p.id, "amount_cents": p.amount_cents, "status": p.status, "created_at": p.created_at.isoformat()} for p in rows]


class WebhookReplay(BaseModel):
    event_id: str
    destination_url: Optional[str] = None


@app.post("/webhooks/replay")
def replay_webhook(data: WebhookReplay, ctx=Depends(require_role("approver")), db: Session = Depends(get_db)):
    _, org_id, _ = ctx
    evt = db.query(WebhookEvent).filter(WebhookEvent.org_id == org_id, WebhookEvent.id == data.event_id).first()
    if not evt:
        raise HTTPException(status_code=404, detail="event not found")
    if data.destination_url:
        evt.destination_url = data.destination_url
    evt.status = "pending"
    evt.attempts = 0
    evt.next_attempt_at = _now()
    db.commit()
    return {"replayed": True}


@app.get("/webhooks/events")
def list_webhook_events(
    wallet_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    ctx=Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_admin_or_auditor(ctx)
    _, org_id, _ = ctx
    q = db.query(WebhookEvent).filter(WebhookEvent.org_id == org_id)
    if wallet_id:
        q = q.filter(WebhookEvent.wallet_id == wallet_id)
    if status:
        q = q.filter(WebhookEvent.status == status)
    rows = q.order_by(WebhookEvent.created_at.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": e.id,
            "wallet_id": e.wallet_id,
            "destination_url": e.destination_url,
            "event_type": e.event_type,
            "event_version": e.event_version,
            "status": e.status,
            "attempts": e.attempts,
            "next_attempt_at": e.next_attempt_at.isoformat() if e.next_attempt_at else None,
            "last_error": e.last_error,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "sent_at": e.sent_at.isoformat() if e.sent_at else None,
        }
        for e in rows
    ]


# ─── Simulation / Worker Helpers ─────────────────────────────────────────────


def _enqueue_webhook(db: Session, wallet: Wallet, event_type: str, data: dict) -> None:
    if not wallet.webhook_url:
        return
    event_id = str(uuid.uuid4())
    payload = {
        "id": event_id,
        "type": event_type,
        "event_version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "org_id": wallet.org_id,
        "wallet_id": wallet.id,
        "data": data,
    }
    evt = WebhookEvent(
        id=event_id,
        org_id=wallet.org_id,
        wallet_id=wallet.id,
        destination_url=wallet.webhook_url,
        event_type=event_type,
        event_version=1,
        payload=json.dumps(payload),
        status="pending",
        attempts=0,
        next_attempt_at=_now(),
    )
    db.add(evt)


def _process_webhooks(db: Session, limit: int = 50) -> int:
    import requests

    now = _now()
    rows = db.query(WebhookEvent).filter(
        WebhookEvent.status.in_(["pending", "retry"]),
        WebhookEvent.next_attempt_at <= now,
    ).order_by(WebhookEvent.created_at.asc()).limit(limit).all()
    sent = 0
    for evt in rows:
        wallet = db.query(Wallet).filter(Wallet.id == evt.wallet_id).first()
        # Per-wallet secret (preferred). Fallback keeps demo running for older wallets.
        secret = (wallet.webhook_signing_secret if wallet else None) or os.environ.get("WEBHOOK_SIGNING_SECRET", "dev-webhook-secret")
        payload = evt.payload
        try:
            r = requests.post(
                evt.destination_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-Commerce-Event": evt.event_type,
                    "X-Agent-Commerce-Signature": _sign_webhook(payload, secret),
                },
                timeout=3,
            )
            if 200 <= r.status_code < 300:
                evt.status = "sent"
                evt.sent_at = _now()
                evt.last_error = None
                sent += 1
            else:
                raise Exception(f"http {r.status_code}")
        except Exception as e:
            evt.attempts += 1
            evt.status = "retry" if evt.attempts < 8 else "failed"
            backoff = min(60, 2 ** min(evt.attempts, 6))
            evt.next_attempt_at = _now() + timedelta(seconds=backoff)
            evt.last_error = str(e)
    db.commit()
    return sent


def _rail_authorize(db: Session, wallet: Wallet, txn: Transaction, amount_cents: int, idempotency_key: Optional[str]) -> PaymentAuthorization:
    authz = PaymentAuthorization(
        id=str(uuid.uuid4()),
        org_id=wallet.org_id,
        wallet_id=wallet.id,
        transaction_id=txn.id,
        amount_cents=amount_cents,
        status="authorized",
        idempotency_key=idempotency_key,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(authz)
    return authz


def _rail_capture(db: Session, wallet: Wallet, txn: Transaction, authz: PaymentAuthorization) -> None:
    # Capture: reserved decreases and balance decreases.
    updated = db.execute(text(
        "UPDATE wallets SET reserved_cents = reserved_cents - :amt, balance_cents = balance_cents - :amt "
        "WHERE id = :wallet_id AND reserved_cents >= :amt AND balance_cents >= :amt"
    ), {"amt": authz.amount_cents, "wallet_id": wallet.id}).rowcount
    if not updated:
        authz.status = "failed"
        authz.failure_code = "capture_failed"
        txn.payment_status = "failed"
        # Release the hold so funds don't get stuck reserved forever.
        hold = db.query(BalanceHold).filter(BalanceHold.transaction_id == txn.id, BalanceHold.status == "active").first()
        if hold:
            _release_hold(db, wallet, hold.amount_cents)
            hold.status = "released"
            hold.released_at = _now()
        _enqueue_webhook(
            db,
            wallet,
            "payment.failed",
            {
                "authorization_id": authz.id,
                "transaction_id": txn.id,
                "amount_cents": authz.amount_cents,
                "failure_code": authz.failure_code,
            },
        )
        return
    cap = PaymentCapture(
        id=str(uuid.uuid4()),
        org_id=wallet.org_id,
        authorization_id=authz.id,
        transaction_id=txn.id,
        amount_cents=authz.amount_cents,
        status="captured",
    )
    db.add(cap)
    authz.status = "captured"
    authz.updated_at = _now()
    txn.payment_status = "succeeded"
    txn.settled_at = _now()
    hold = db.query(BalanceHold).filter(BalanceHold.transaction_id == txn.id, BalanceHold.status == "active").first()
    if hold:
        hold.status = "captured"
        hold.released_at = _now()
    _enqueue_webhook(db, wallet, "payment.captured", {"authorization_id": authz.id, "capture_id": cap.id, "transaction_id": txn.id, "amount_cents": authz.amount_cents})


def _rail_ach_submit(db: Session, wallet: Wallet, req: ApprovalRequest, idempotency_key: Optional[str]) -> AchPayout:
    payout = AchPayout(
        id=str(uuid.uuid4()),
        org_id=wallet.org_id,
        wallet_id=wallet.id,
        approval_request_id=req.id,
        amount_cents=req.amount_cents,
        status="submitted",
        idempotency_key=idempotency_key,
        settle_after=_now() + timedelta(seconds=3),
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(payout)
    return payout


def _settle_simulated_payments(db: Session, limit: int = 50) -> int:
    """
    Simulate "async" capture for card authorizations and ACH payouts.
    """
    now = _now()

    # Card: capture any authorizations older than ~1s.
    authzs = db.query(PaymentAuthorization).filter(PaymentAuthorization.status == "authorized").order_by(PaymentAuthorization.created_at.asc()).limit(limit).all()
    for a in authzs:
        if a.created_at and (now - a.created_at).total_seconds() < 1.0:
            continue
        txn = db.query(Transaction).filter(Transaction.id == a.transaction_id).first()
        wallet = db.query(Wallet).filter(Wallet.id == a.wallet_id).first()
        if txn and wallet:
            _rail_capture(db, wallet, txn, a)
    db.commit()

    # ACH: settle payouts whose time has come.
    payouts = db.query(AchPayout).filter(AchPayout.status.in_(["submitted", "processing"])).order_by(AchPayout.created_at.asc()).limit(limit).all()
    for p in payouts:
        if p.settle_after and p.settle_after > now:
            continue
        wallet = db.query(Wallet).filter(Wallet.id == p.wallet_id).first()
        if not wallet:
            p.status = "failed"
            p.failure_code = "missing_wallet"
            continue
        txn = db.query(Transaction).filter(Transaction.payment_ref == p.id).first()
        hold = db.query(BalanceHold).filter(BalanceHold.transaction_id == (txn.id if txn else None), BalanceHold.status == "active").first() if txn else None
        # Capture funds now (payout "paid").
        updated = db.execute(text(
            "UPDATE wallets SET reserved_cents = reserved_cents - :amt, balance_cents = balance_cents - :amt "
            "WHERE id = :wallet_id AND reserved_cents >= :amt AND balance_cents >= :amt"
        ), {"amt": p.amount_cents, "wallet_id": wallet.id}).rowcount
        if not updated:
            p.status = "failed"
            p.failure_code = "insufficient_funds"
            if txn:
                txn.payment_status = "failed"
                txn.settled_at = _now()
            if hold:
                _release_hold(db, wallet, hold.amount_cents)
                hold.status = "released"
                hold.released_at = _now()
            _enqueue_webhook(db, wallet, "payout.failed", {"payout_id": p.id, "amount_cents": p.amount_cents, "failure_code": p.failure_code})
        else:
            p.status = "paid"
            if txn:
                txn.payment_status = "succeeded"
                txn.settled_at = _now()
            if hold:
                hold.status = "captured"
                hold.released_at = _now()
            _enqueue_webhook(db, wallet, "payout.paid", {"payout_id": p.id, "amount_cents": p.amount_cents})
        p.updated_at = _now()
    db.commit()
    return len(authzs) + len(payouts)


@app.post("/simulate/tick")
def simulate_tick(ctx=Depends(require_role("admin")), db: Session = Depends(get_db)):
    settled = _settle_simulated_payments(db)
    sent = _process_webhooks(db)
    return {"settled": settled, "webhooks_sent": sent}


# ─── Policy + Balance Operations ─────────────────────────────────────────────


def _evaluate_policy(db: Session, wallet: Wallet, amount_cents: int, vendor_name: str) -> dict:
    trace = []
    if wallet.status == "suspended":
        trace.append({"check": "wallet_status", "passed": False, "detail": "wallet is suspended"})
        return {"decision": "blocked", "reason": "wallet is suspended", "trace": trace}
    trace.append({"check": "wallet_status", "passed": True, "detail": f"wallet status {wallet.status}"})

    allowed = json.loads(wallet.allowed_vendors or "[]")
    if allowed and vendor_name not in allowed:
        trace.append({"check": "vendor_allowlist", "passed": False, "detail": f"{vendor_name} not in allowlist"})
        return {"decision": "blocked", "reason": "vendor not allowed", "trace": trace}
    trace.append({"check": "vendor_allowlist", "passed": True, "detail": "allowed"})

    # Velocity: count recent approved txns (including in-flight) in last 60s.
    if int(wallet.velocity_max_txn or 0) > 0:
        since = _now() - timedelta(seconds=60)
        count = db.query(func.count(Transaction.id)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.payment_status.in_(["processing", "succeeded"]),
            Transaction.created_at >= since,
        ).scalar() or 0
        if count >= int(wallet.velocity_max_txn):
            wallet.status = "suspended"
            trace.append({"check": "velocity", "passed": False, "detail": f"{count} txns in 60s >= limit {wallet.velocity_max_txn} (wallet suspended)"})
            return {"decision": "blocked", "reason": "velocity limit exceeded (wallet suspended)", "trace": trace}
        trace.append({"check": "velocity", "passed": True, "detail": f"{count} txns in 60s (limit {wallet.velocity_max_txn})"})
    else:
        trace.append({"check": "velocity", "passed": True, "detail": "no velocity limit"})

    available = int(wallet.balance_cents) - int(wallet.reserved_cents)
    if available < amount_cents:
        trace.append({"check": "balance", "passed": False, "detail": f"available {available} < {amount_cents}"})
        return {"decision": "blocked", "reason": "insufficient balance", "trace": trace}
    trace.append({"check": "balance", "passed": True, "detail": f"available {available} >= {amount_cents}"})

    # Daily + weekly windows (rolling). Count in-flight approvals too so limits can’t be bypassed
    # by spamming many purchases before settlement finishes.
    if int(wallet.daily_limit_cents or 0) > 0:
        since = _now() - timedelta(hours=24)
        spent = db.query(func.coalesce(func.sum(Transaction.amount_cents), 0)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.payment_status.in_(["processing", "succeeded"]),
            Transaction.created_at >= since,
        ).scalar() or 0
        if int(spent) + amount_cents > int(wallet.daily_limit_cents):
            trace.append({"check": "daily_limit", "passed": False, "detail": f"spent {spent} + {amount_cents} > {wallet.daily_limit_cents}"})
            return {"decision": "blocked", "reason": "daily limit exceeded", "trace": trace}
        trace.append({"check": "daily_limit", "passed": True, "detail": f"spent {spent} + {amount_cents} <= {wallet.daily_limit_cents}"})
    else:
        trace.append({"check": "daily_limit", "passed": True, "detail": "no daily limit"})

    if int(wallet.weekly_limit_cents or 0) > 0:
        since = _now() - timedelta(days=7)
        spent = db.query(func.coalesce(func.sum(Transaction.amount_cents), 0)).filter(
            Transaction.wallet_id == wallet.id,
            Transaction.status == "approved",
            Transaction.payment_status.in_(["processing", "succeeded"]),
            Transaction.created_at >= since,
        ).scalar() or 0
        if int(spent) + amount_cents > int(wallet.weekly_limit_cents):
            trace.append({"check": "weekly_limit", "passed": False, "detail": f"spent {spent} + {amount_cents} > {wallet.weekly_limit_cents}"})
            return {"decision": "blocked", "reason": "weekly limit exceeded", "trace": trace}
        trace.append({"check": "weekly_limit", "passed": True, "detail": f"spent {spent} + {amount_cents} <= {wallet.weekly_limit_cents}"})
    else:
        trace.append({"check": "weekly_limit", "passed": True, "detail": "no weekly limit"})

    if amount_cents > int(wallet.spend_limit_max_cents):
        trace.append({"check": "spend_limit_max", "passed": False, "detail": "over max"})
        return {"decision": "blocked", "reason": "over hard ceiling", "trace": trace}
    trace.append({"check": "spend_limit_max", "passed": True, "detail": "within max"})

    if amount_cents > int(wallet.auto_approve_limit_cents):
        trace.append({"check": "auto_approve_limit", "passed": False, "detail": "requires approval"})
        return {"decision": "requires_approval", "reason": "requires approval", "trace": trace}

    trace.append({"check": "auto_approve_limit", "passed": True, "detail": "auto-approve"})
    return {"decision": "auto_approve", "reason": "within policy", "trace": trace}


def _reserve_or_block(db: Session, wallet: Wallet, amount_cents: int, trace: list[dict]) -> None:
    updated = db.execute(text(
        "UPDATE wallets SET reserved_cents = reserved_cents + :amt "
        "WHERE id = :wallet_id AND (balance_cents - reserved_cents) >= :amt"
    ), {"amt": amount_cents, "wallet_id": wallet.id}).rowcount
    if not updated:
        raise HTTPException(status_code=400, detail="insufficient balance (concurrent spend)")
    db.refresh(wallet)


def _release_hold(db: Session, wallet: Wallet, amount_cents: int) -> None:
    db.execute(text(
        "UPDATE wallets SET reserved_cents = reserved_cents - :amt "
        "WHERE id = :wallet_id AND reserved_cents >= :amt"
    ), {"amt": amount_cents, "wallet_id": wallet.id})
    db.refresh(wallet)


# ─── Discovery Manifests / Docs ─────────────────────────────────────────────


@app.get("/.well-known/agent-commerce.json")
def agent_manifest():
    return {
        "name": "Agent Commerce",
        "openapi": {"path": "/openapi.json"},
        "docs": {"html": "/docs-ref", "text": "/docs.txt"},
        "auth": {
            "human": {"type": "bearer_jwt", "login": "/auth/login"},
            "agent": {"type": "wallet_api_key", "header": "X-Wallet-Key", "format": "wk_<key_id>.<secret>"},
        },
        "tenancy": {"model": "org_scoped", "agent_unique": "org_id + external_agent_id"},
        "wallets": {"multiple_per_agent": True, "purpose_required": True},
        "idempotency": {"field": "idempotency_key", "scope": "wallet"},
        "payments": {"card_semantics": "authorize_then_capture", "ach": "payout_flow"},
        "webhooks": {
            "delivery": "at_least_once",
            "event_version": 1,
            "headers": ["X-Agent-Commerce-Event", "X-Agent-Commerce-Signature"],
            "signature": {"algo": "hmac_sha256", "format": "v1=<hex>", "secret": "wallet.webhook_signing_secret"},
        },
    }


@app.get("/docs.txt", response_class=PlainTextResponse)
def docs_txt():
    return """AGENT COMMERCE — ITERATION 2 (PLAIN TEXT)

Quick Links:
- OpenAPI: /openapi.json
- Agent manifest: /.well-known/agent-commerce.json
- Human docs: /docs-ref

Human Auth:
- POST /auth/login -> {access_token}
- Use: Authorization: Bearer <token>

Agent Auth:
- Use header: X-Wallet-Key: wk_<key_id>.<secret>
- Keys are wallet-scoped; rotate/revoke via human admin APIs.

Core Flow:
- POST /resolve (agent)
- POST /purchase (agent) with idempotency_key
- If pending_approval: human approves via POST /approvals/{id}/approve

Guarantee:
- Approval requests reserve funds immediately (available = balance - reserved).

Payments:
- Card-like: authorize -> capture (async simulated)
- ACH: payout (async simulated)

Webhooks:
- At-least-once delivery; verify signature; dedupe by event.id
- Signature header: X-Agent-Commerce-Signature = v1=<hex(hmac_sha256(webhook_signing_secret, raw_body))>
"""


# ─── Static Pages (demo UI is updated in a later patch) ──────────────────────


@app.get("/")
def serve_dashboard():
    return FileResponse("dashboard.html", headers={"Cache-Control": "no-store"})


@app.get("/demo")
def serve_demo():
    return FileResponse("demo.html", headers={"Cache-Control": "no-store"})


@app.get("/docs-ref")
def serve_docs():
    return FileResponse("docs.html", headers={"Cache-Control": "no-store"})


# ─── Serialization ──────────────────────────────────────────────────────────


def _agent_dict(a: Agent) -> dict:
    return {
        "id": a.id,
        "org_id": a.org_id,
        "external_agent_id": a.external_agent_id,
        "name": a.name,
        "metadata": json.loads(a.agent_metadata or "{}"),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _wallet_dict(w: Wallet) -> dict:
    available = int(w.balance_cents) - int(w.reserved_cents)
    return {
        "id": w.id,
        "org_id": w.org_id,
        "agent_id": w.agent_id,
        "purpose": w.purpose,
        "name": w.name,
        "currency": w.currency,
        "status": w.status,
        "balance_cents": int(w.balance_cents),
        "reserved_cents": int(w.reserved_cents),
        "available_cents": available,
        "balance": _from_cents(int(w.balance_cents)),
        "reserved_balance": _from_cents(int(w.reserved_cents)),
        "available_balance": _from_cents(available),
        "auto_approve_limit_cents": int(w.auto_approve_limit_cents),
        "auto_approve_limit": _from_cents(int(w.auto_approve_limit_cents)),
        "spend_limit_max_cents": int(w.spend_limit_max_cents),
        "spend_limit_max": _from_cents(int(w.spend_limit_max_cents)),
        "daily_limit_cents": int(w.daily_limit_cents),
        "daily_limit": _from_cents(int(w.daily_limit_cents)),
        "weekly_limit_cents": int(w.weekly_limit_cents),
        "weekly_limit": _from_cents(int(w.weekly_limit_cents)),
        "velocity_max_txn": int(w.velocity_max_txn),
        "allowed_vendors": json.loads(w.allowed_vendors or "[]"),
        "webhook_url": w.webhook_url,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def _product_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "org_id": p.org_id,
        "vendor_name": p.vendor_name,
        "name": p.name,
        "description": p.description,
        "price_cents": int(p.price_cents),
        "price": _from_cents(int(p.price_cents)),
        "min_order": p.min_order,
        "lead_time_days": p.lead_time_days,
        "tags": json.loads(p.tags or "[]"),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _txn_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
        "org_id": t.org_id,
        "wallet_id": t.wallet_id,
        "agent_id": t.agent_id,
        "product_id": t.product_id,
        "vendor_name": t.vendor_name,
        "product_name": t.product_name,
        "amount_cents": int(t.amount_cents),
        "amount": _from_cents(int(t.amount_cents)),
        "quantity": t.quantity,
        "intent": t.intent,
        "status": t.status,
        "payment_status": t.payment_status,
        "payment_ref": t.payment_ref,
        "reason": t.reason,
        "policy_trace": json.loads(t.policy_trace or "[]"),
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
        "amount_cents": int(r.amount_cents),
        "amount": _from_cents(int(r.amount_cents)),
        "quantity": r.quantity,
        "intent": r.intent,
        "status": r.status,
        "reason": r.reason,
        "policy_trace": json.loads(r.policy_trace or "[]"),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
