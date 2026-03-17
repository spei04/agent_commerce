import uuid
import json
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, Text
from database import Base


def gen_id():
    return str(uuid.uuid4())


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(String, primary_key=True, default=gen_id)
    agent_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    balance = Column(Float, default=0.0)
    reserved_balance = Column(Float, default=0.0)       # reserved (holds) reduce available balance
    api_key = Column(Text)                             # shared secret for agent calls (MVP)
    auto_approve_limit = Column(Float, default=20.0)    # below this → auto-approve
    spend_limit_max = Column(Float, default=100.0)      # above this → hard block; middle tier → requires approval
    allowed_vendors = Column(Text, default="[]")        # JSON array; empty = allow all
    status = Column(String, default="active")           # active | suspended | review
    daily_limit = Column(Float, default=0.0)            # 0 = no limit
    weekly_limit = Column(Float, default=0.0)           # 0 = no limit
    velocity_max_txn = Column(Integer, default=0)       # max approved txn per 60s; 0 = no limit
    webhook_url = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=gen_id)
    vendor_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    price = Column(Float, nullable=False)
    min_order = Column(Integer, default=1)
    lead_time_days = Column(Integer, default=1)
    tags = Column(Text, default="[]")   # JSON array
    created_at = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False)
    agent_id = Column(String, nullable=False)
    product_id = Column(String)
    vendor_name = Column(String)
    product_name = Column(String)
    amount = Column(Float, nullable=False)
    idempotency_key = Column(Text)                      # unique per wallet to prevent double-charges
    quantity = Column(Integer, default=1)
    intent = Column(Text)
    status = Column(String, nullable=False)   # approved | blocked
    payment_status = Column(String, default="not_started")  # not_started | processing | succeeded | failed
    payment_intent_id = Column(String)
    reason = Column(Text)
    policy_trace = Column(Text)                         # JSON array of policy check dicts
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime)


class IntentLog(Base):
    __tablename__ = "intent_logs"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False)
    agent_id = Column(String, nullable=False)
    raw_intent = Column(Text)
    constraints = Column(Text, default="{}")  # JSON
    matched_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SkuInsight(Base):
    """
    Per-product AI visibility analytics.
    Updated every time resolve_intent() runs and when a purchase is executed.
    """
    __tablename__ = "sku_insights"

    id = Column(String, primary_key=True, default=gen_id)
    product_id = Column(String, nullable=False, unique=True)
    impressions = Column(Integer, default=0)        # times product appeared in resolve results
    selections = Column(Integer, default=0)         # times product was actually purchased
    rank_sum = Column(Float, default=0.0)           # sum of rank positions (1-indexed) for avg calc
    score_sum = Column(Float, default=0.0)          # sum of relevance scores seen
    last_seen = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CatalogSync(Base):
    """
    Audit log of bulk catalog sync jobs from external PIMs/ERPs.
    """
    __tablename__ = "catalog_syncs"

    id = Column(String, primary_key=True, default=gen_id)
    source = Column(String, nullable=False)         # e.g. "shopify", "sap", "csv"
    status = Column(String, default="complete")     # complete | partial | failed
    products_added = Column(Integer, default=0)
    products_updated = Column(Integer, default=0)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class ApprovalRequest(Base):
    """
    Created when a transaction falls in the middle policy tier
    (above auto_approve_limit but below spend_limit_max).
    A human reviewer approves or rejects via the dashboard.
    On approval, the transaction is executed and balance decremented.
    """
    __tablename__ = "approval_requests"

    id = Column(String, primary_key=True, default=gen_id)
    # Snapshot of what is being requested — transaction is created AFTER approval
    wallet_id = Column(String, nullable=False)
    agent_id = Column(String, nullable=False)
    product_id = Column(String, nullable=False)
    vendor_name = Column(String)
    product_name = Column(String)
    amount = Column(Float, nullable=False)
    idempotency_key = Column(Text)               # unique per wallet to de-dupe pending approvals
    quantity = Column(Integer, default=1)
    intent = Column(Text)
    status = Column(String, default="pending")   # pending | approved | rejected
    reason = Column(Text)                        # policy decision reason at request-time (snapshot)
    policy_trace = Column(Text)                  # JSON array of policy checks at request-time
    reviewer_note = Column(Text)
    reviewed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class BalanceHold(Base):
    """
    Reserved funds for an in-flight purchase.
    Used to model payment authorizations and approval guarantees (closest-to-real without real money).
    """
    __tablename__ = "balance_holds"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    kind = Column(String, nullable=False)        # approval | auth
    status = Column(String, default="active")    # active | released | captured
    approval_request_id = Column(String)
    transaction_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    released_at = Column(DateTime)


class PaymentIntent(Base):
    """
    Simulated payment processor intent. Settles async and emits webhook events.
    """
    __tablename__ = "payment_intents"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False)
    transaction_id = Column(String)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="usd")
    status = Column(String, default="processing")  # processing | succeeded | failed | canceled
    failure_code = Column(String)
    idempotency_key = Column(Text)
    settle_after = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WebhookEvent(Base):
    """
    Outbox for at-least-once webhook delivery (duplicates can happen).
    """
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False)
    destination_url = Column(Text, nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)       # JSON
    status = Column(String, default="pending")   # pending | sent | retry | failed
    attempts = Column(Integer, default=0)
    next_attempt_at = Column(DateTime)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime)
