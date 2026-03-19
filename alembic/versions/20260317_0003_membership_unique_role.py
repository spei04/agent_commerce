"""membership uniqueness includes role

Revision ID: 20260317_0003
Revises: 20260317_0002
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260317_0003"
down_revision = "20260317_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # We need multiple roles per user per org (admin + approver, etc).
    # Replace uq(org_id, user_id) with uq(org_id, user_id, role).
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.drop_constraint("uq_membership_org_user", type_="unique")
        batch_op.create_unique_constraint("uq_membership_org_user_role", ["org_id", "user_id", "role"])


def downgrade() -> None:
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.drop_constraint("uq_membership_org_user_role", type_="unique")
        batch_op.create_unique_constraint("uq_membership_org_user", ["org_id", "user_id"])

