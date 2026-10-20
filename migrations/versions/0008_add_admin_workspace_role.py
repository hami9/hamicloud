"""add ADMIN role to workspace_memberships check constraint

Revision ID: 0008_add_admin_workspace_role
Revises: 0007_add_dead_letter_records
Create Date: 2026-10-20 12:00:00.000000

"""
from collections.abc import Sequence
from alembic import op

revision: str = "0008_add_admin_workspace_role"
down_revision: str | None = "0007_add_dead_letter_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE workspace_memberships DROP CONSTRAINT IF EXISTS ck_workspace_memberships_role;"
    )
    op.create_check_constraint(
        "ck_workspace_memberships_role",
        "workspace_memberships",
        "role IN ('OWNER', 'ADMIN', 'DEVELOPER', 'VIEWER')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM workspace_memberships WHERE role = 'ADMIN';")
    op.execute(
        "ALTER TABLE workspace_memberships DROP CONSTRAINT IF EXISTS ck_workspace_memberships_role;"
    )
    op.create_check_constraint(
        "ck_workspace_memberships_role",
        "workspace_memberships",
        "role IN ('OWNER', 'DEVELOPER', 'VIEWER')",
    )
