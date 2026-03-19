"""init iteration2

Revision ID: 20260317_0001
Revises: None
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260317_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "memberships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("user_id", sa.String(), nullable=False, index=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("external_agent_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "external_agent_id", name="uq_agents_org_external"),
    )

    op.create_table(
        "wallets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("agent_id", sa.String(), nullable=False, index=True),
        sa.Column("purpose", sa.String(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="usd"),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("balance_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("auto_approve_limit_cents", sa.BigInteger(), nullable=False, server_default="2000"),
        sa.Column("spend_limit_max_cents", sa.BigInteger(), nullable=False, server_default="10000"),
        sa.Column("daily_limit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("weekly_limit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("velocity_max_txn", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_vendors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("webhook_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "agent_id", "purpose", name="uq_wallets_org_agent_purpose"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False, server_default="default"),
        sa.Column("prefix", sa.String(), nullable=False, unique=True),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
    )

    op.create_table(
        "products",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("vendor_name", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("price_cents", sa.BigInteger(), nullable=False),
        sa.Column("min_order", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("vendor_name", sa.String()),
        sa.Column("product_name", sa.String()),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("intent", sa.Text()),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text()),
        sa.Column("policy_trace", sa.Text()),
        sa.Column("reviewer_note", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ux_approvals_wallet_idempotency",
        "approval_requests",
        ["wallet_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "balance_holds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),  # approval | auth
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("approval_request_id", sa.String()),
        sa.Column("transaction_id", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime()),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("product_id", sa.String()),
        sa.Column("vendor_name", sa.String()),
        sa.Column("product_name", sa.String()),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("intent", sa.Text()),
        sa.Column("status", sa.String(), nullable=False),  # approved | blocked
        sa.Column("payment_status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("payment_ref", sa.String()),
        sa.Column("reason", sa.Text()),
        sa.Column("policy_trace", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime()),
    )
    op.create_index(
        "ux_transactions_wallet_idempotency",
        "transactions",
        ["wallet_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "payment_authorizations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("transaction_id", sa.String(), nullable=False, index=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="authorized"),
        sa.Column("failure_code", sa.String()),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "payment_captures",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("authorization_id", sa.String(), nullable=False, index=True),
        sa.Column("transaction_id", sa.String(), nullable=False, index=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="captured"),
        sa.Column("failure_code", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "ach_payouts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("approval_request_id", sa.String()),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="submitted"),  # submitted|processing|paid|failed|returned
        sa.Column("failure_code", sa.String()),
        sa.Column("idempotency_key", sa.Text()),
        sa.Column("settle_after", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, index=True),
        sa.Column("wallet_id", sa.String(), nullable=False, index=True),
        sa.Column("destination_url", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("ach_payouts")
    op.drop_table("payment_captures")
    op.drop_table("payment_authorizations")
    op.drop_index("ux_transactions_wallet_idempotency", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("balance_holds")
    op.drop_index("ux_approvals_wallet_idempotency", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_table("products")
    op.drop_table("api_keys")
    op.drop_table("wallets")
    op.drop_table("agents")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("organizations")

