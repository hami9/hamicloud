"""add next_attempt_at column to outbox_events

Revision ID: 0004_add_outbox_next_attempt_at
Revises: 0003_add_recovery_pending_state
Create Date: 2026-09-28 11:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_add_outbox_next_attempt_at"
down_revision: Union[str, None] = "0003_add_recovery_pending_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_outbox_events_next_attempt_at",
        "outbox_events",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_next_attempt_at", table_name="outbox_events")
    op.drop_column("outbox_events", "next_attempt_at")
