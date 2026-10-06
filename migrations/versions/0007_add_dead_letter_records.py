"""add dead letter records table and failure trigger

Revision ID: 0007_add_dead_letter_records
Revises: 0006_add_superseded_status
Create Date: 2026-10-06 14:00:00.000000

"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007_add_dead_letter_records"
down_revision: str | None = "0006_add_superseded_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("last_attempt", sa.Integer(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_dead_letter_records_job_id"),
    )
    op.create_index(
        "ix_dead_letter_records_workspace_id",
        "dead_letter_records",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_dead_letter_records_workspace_created",
        "dead_letter_records",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )

    # Defense-in-depth trigger: ensure dead-letter record is guaranteed even if state is updated directly
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_jobs_insert_dead_letter()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.state = 'FAILED' AND (OLD.state IS NULL OR OLD.state != 'FAILED') THEN
                INSERT INTO dead_letter_records (id, job_id, workspace_id, last_attempt, exit_code, failure_reason, created_at)
                SELECT
                    gen_random_uuid(),
                    NEW.id,
                    NEW.workspace_id,
                    NEW.current_attempt_number,
                    ja.exit_code,
                    ja.failure_reason,
                    NOW()
                FROM (
                    SELECT exit_code, failure_reason
                    FROM job_attempts
                    WHERE job_id = NEW.id AND attempt_number = NEW.current_attempt_number
                    LIMIT 1
                ) ja
                ON CONFLICT (job_id) DO UPDATE SET
                    last_attempt = EXCLUDED.last_attempt,
                    exit_code = EXCLUDED.exit_code,
                    failure_reason = EXCLUDED.failure_reason,
                    created_at = EXCLUDED.created_at;

                IF NOT FOUND THEN
                    INSERT INTO dead_letter_records (id, job_id, workspace_id, last_attempt, exit_code, failure_reason, created_at)
                    VALUES (gen_random_uuid(), NEW.id, NEW.workspace_id, NEW.current_attempt_number, NULL, 'Job reached FAILED state', NOW())
                    ON CONFLICT (job_id) DO NOTHING;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_jobs_insert_dead_letter ON jobs;
        CREATE TRIGGER trg_jobs_insert_dead_letter
        AFTER UPDATE OF state ON jobs
        FOR EACH ROW
        EXECUTE FUNCTION fn_jobs_insert_dead_letter();
    """)

    # Backfill any existing failed jobs
    op.execute("""
        INSERT INTO dead_letter_records (id, job_id, workspace_id, last_attempt, exit_code, failure_reason, created_at)
        SELECT
            gen_random_uuid(),
            j.id,
            j.workspace_id,
            j.current_attempt_number,
            ja.exit_code,
            ja.failure_reason,
            j.updated_at
        FROM jobs j
        LEFT JOIN LATERAL (
            SELECT exit_code, failure_reason
            FROM job_attempts
            WHERE job_id = j.id AND attempt_number = j.current_attempt_number
            LIMIT 1
        ) ja ON true
        WHERE j.state = 'FAILED'
        ON CONFLICT (job_id) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_jobs_insert_dead_letter ON jobs;")
    op.execute("DROP FUNCTION IF EXISTS fn_jobs_insert_dead_letter();")
    op.drop_index("ix_dead_letter_records_workspace_created", table_name="dead_letter_records")
    op.drop_index("ix_dead_letter_records_workspace_id", table_name="dead_letter_records")
    op.drop_table("dead_letter_records")
