"""wallet webhook signing secret

Revision ID: 20260317_0002
Revises: 20260317_0001
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260317_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallets", sa.Column("webhook_signing_secret", sa.Text(), nullable=True))
    op.add_column("wallets", sa.Column("webhook_secret_rotated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("wallets", "webhook_secret_rotated_at")
    op.drop_column("wallets", "webhook_signing_secret")

