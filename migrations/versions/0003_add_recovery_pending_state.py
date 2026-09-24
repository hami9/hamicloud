"""add recovery pending state to job check constraints

Revision ID: 0003_add_recovery_pending_state
Revises: 0002_close_schema_gaps
Create Date: 2026-09-24 19:30:00.000000

"""
from typing import Sequence, Union
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_add_recovery_pending_state"
down_revision: Union[str, None] = "0002_close_schema_gaps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATES = "('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'SUCCEEDED', 'RETRY_WAIT', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED')"
NEW_STATES = "('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'SUCCEEDED', 'RETRY_WAIT', 'RECOVERY_PENDING', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED')"


def upgrade() -> None:
    op.drop_constraint("ck_job_attempts_state", "job_attempts", type_="check")
    op.create_check_constraint(
        "ck_job_attempts_state",
        "job_attempts",
        f"state IN {NEW_STATES}",
    )

    op.drop_constraint("ck_jobs_state", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_state",
        "jobs",
        f"state IN {NEW_STATES}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_state", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_state",
        "jobs",
        f"state IN {OLD_STATES}",
    )

    op.drop_constraint("ck_job_attempts_state", "job_attempts", type_="check")
    op.create_check_constraint(
        "ck_job_attempts_state",
        "job_attempts",
        f"state IN {OLD_STATES}",
    )
