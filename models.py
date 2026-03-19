import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, BigInteger, DateTime, Text

from database import Base


def gen_id() -> str:
    return str(uuid.uuid4())


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)  # admin|approver|auditor
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    external_agent_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    # "metadata" is reserved by SQLAlchemy Declarative, so map to a different attribute name.
    agent_metadata = Column("metadata", Text, default="{}", nullable=False)  # JSON
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    agent_id = Column(String, nullable=False, index=True)
    purpose = Column(String, nullable=False, index=True)  # procurement|cloud|data|travel|other
    name = Column(String, nullable=False)
    currency = Column(String, default="usd", nullable=False)
    status = Column(String, default="active", nullable=False)  # active|suspended|review

    balance_cents = Column(BigInteger, default=0, nullable=False)
    reserved_cents = Column(BigInteger, default=0, nullable=False)

    auto_approve_limit_cents = Column(BigInteger, default=2000, nullable=False)
    spend_limit_max_cents = Column(BigInteger, default=10000, nullable=False)
    daily_limit_cents = Column(BigInteger, default=0, nullable=False)
    weekly_limit_cents = Column(BigInteger, default=0, nullable=False)
    velocity_max_txn = Column(Integer, default=0, nullable=False)
    allowed_vendors = Column(Text, default="[]", nullable=False)  # JSON array

    webhook_url = Column(Text)
    # Demo-friendly: stored in plaintext so we can sign outgoing webhooks.
    # In production: store encrypted (KMS) and support rotation + separation from API keys.
    webhook_signing_secret = Column(Text)
    webhook_secret_rotated_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=gen_id)
    wallet_id = Column(String, nullable=False, index=True)
    name = Column(String, default="default", nullable=False)
    prefix = Column(String, unique=True, nullable=False)
    secret_hash = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime)
    revoked_at = Column(DateTime)


class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    vendor_name = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    price_cents = Column(BigInteger, nullable=False)
    min_order = Column(Integer, default=1, nullable=False)
    lead_time_days = Column(Integer, default=1, nullable=False)
    tags = Column(Text, default="[]", nullable=False)  # JSON
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    agent_id = Column(String, nullable=False)
    product_id = Column(String, nullable=False)
    vendor_name = Column(String)
    product_name = Column(String)
    amount_cents = Column(BigInteger, nullable=False)
    idempotency_key = Column(Text)
    quantity = Column(Integer, default=1, nullable=False)
    intent = Column(Text)
    status = Column(String, default="pending", nullable=False)  # pending|approved|rejected
    reason = Column(Text)
    policy_trace = Column(Text)
    reviewer_note = Column(Text)
    reviewed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class BalanceHold(Base):
    __tablename__ = "balance_holds"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    amount_cents = Column(BigInteger, nullable=False)
    kind = Column(String, nullable=False)  # approval|auth
    status = Column(String, default="active", nullable=False)  # active|released|captured
    approval_request_id = Column(String)
    transaction_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    released_at = Column(DateTime)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    agent_id = Column(String, nullable=False)
    product_id = Column(String)
    vendor_name = Column(String)
    product_name = Column(String)
    amount_cents = Column(BigInteger, nullable=False)
    idempotency_key = Column(Text)
    quantity = Column(Integer, default=1, nullable=False)
    intent = Column(Text)
    status = Column(String, nullable=False)  # approved|blocked
    payment_status = Column(String, default="not_started", nullable=False)  # not_started|processing|succeeded|failed
    payment_ref = Column(String)
    reason = Column(Text)
    policy_trace = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    settled_at = Column(DateTime)


class PaymentAuthorization(Base):
    __tablename__ = "payment_authorizations"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    transaction_id = Column(String, nullable=False, index=True)
    amount_cents = Column(BigInteger, nullable=False)
    status = Column(String, default="authorized", nullable=False)  # authorized|voided|captured|failed
    failure_code = Column(String)
    idempotency_key = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaymentCapture(Base):
    __tablename__ = "payment_captures"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    authorization_id = Column(String, nullable=False, index=True)
    transaction_id = Column(String, nullable=False, index=True)
    amount_cents = Column(BigInteger, nullable=False)
    status = Column(String, default="captured", nullable=False)  # captured|failed
    failure_code = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AchPayout(Base):
    __tablename__ = "ach_payouts"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    approval_request_id = Column(String)
    amount_cents = Column(BigInteger, nullable=False)
    status = Column(String, default="submitted", nullable=False)  # submitted|processing|paid|failed|returned
    failure_code = Column(String)
    idempotency_key = Column(Text)
    settle_after = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(String, primary_key=True, default=gen_id)
    org_id = Column(String, nullable=False, index=True)
    wallet_id = Column(String, nullable=False, index=True)
    destination_url = Column(Text, nullable=False)
    event_type = Column(String, nullable=False)
    event_version = Column(Integer, default=1, nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(String, default="pending", nullable=False)  # pending|sent|retry|failed
    attempts = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime)
