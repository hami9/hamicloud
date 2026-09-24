"""close schema gaps

Revision ID: 0002_close_schema_gaps
Revises: 0001_baseline_schema
Create Date: 2026-09-22 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_close_schema_gaps"
down_revision: Union[str, None] = "0001_baseline_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------
    # 1. outbox_events: schema_version, workspace_id, backfills
    # ---------------------------------------------------------
    op.add_column("outbox_events", sa.Column("schema_version", sa.Integer(), nullable=True))
    op.add_column("outbox_events", sa.Column("workspace_id", sa.Uuid(), nullable=True))

    op.execute("UPDATE outbox_events SET schema_version = 1 WHERE schema_version IS NULL")
    op.execute("""
        UPDATE outbox_events 
        SET workspace_id = (payload_json->>'workspace_id')::uuid 
        WHERE workspace_id IS NULL AND payload_json->>'workspace_id' IS NOT NULL
    """)

    op.alter_column("outbox_events", "schema_version", nullable=False)
    op.alter_column("outbox_events", "workspace_id", nullable=False)

    op.create_foreign_key(
        "fk_outbox_events_workspace_id_workspaces",
        "outbox_events",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_outbox_events_workspace_id", "outbox_events", ["workspace_id"])
    op.create_check_constraint(
        "ck_outbox_events_status",
        "outbox_events",
        "status IN ('PENDING', 'PUBLISHED', 'FAILED')",
    )

    # ---------------------------------------------------------
    # 2. consumed_events: replace consumer_group with handler
    # ---------------------------------------------------------
    op.add_column("consumed_events", sa.Column("handler", sa.String(length=100), nullable=True))
    op.execute("UPDATE consumed_events SET handler = consumer_group WHERE handler IS NULL")
    op.alter_column("consumed_events", "handler", nullable=False)

    op.drop_constraint("uq_consumed_event_group", "consumed_events", type_="unique")
    op.drop_column("consumed_events", "consumer_group")

    op.create_unique_constraint("uq_consumed_event_handler", "consumed_events", ["event_id", "handler"])
    op.create_index("ix_consumed_events_handler", "consumed_events", ["handler"])

    # ---------------------------------------------------------
    # 3. job_attempts: workspace_id backfilled from jobs
    # ---------------------------------------------------------
    op.add_column("job_attempts", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE job_attempts 
        SET workspace_id = jobs.workspace_id 
        FROM jobs 
        WHERE job_attempts.job_id = jobs.id AND job_attempts.workspace_id IS NULL
    """)
    op.alter_column("job_attempts", "workspace_id", nullable=False)

    op.create_foreign_key(
        "fk_job_attempts_workspace_id_workspaces",
        "job_attempts",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_job_attempts_workspace_id", "job_attempts", ["workspace_id"])
    op.create_check_constraint(
        "ck_job_attempts_state",
        "job_attempts",
        "state IN ('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'SUCCEEDED', 'RETRY_WAIT', 'RECOVERY_PENDING', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED')",
    )

    # ---------------------------------------------------------
    # 4. execution_intents: typed FKs, resource_uid, constraints
    # ---------------------------------------------------------
    op.add_column("execution_intents", sa.Column("resource_uid", sa.String(length=100), nullable=True))
    op.add_column("execution_intents", sa.Column("job_attempt_id", sa.Uuid(), nullable=True))
    op.add_column("execution_intents", sa.Column("release_id", sa.Uuid(), nullable=True))

    op.execute("""
        UPDATE execution_intents 
        SET job_attempt_id = resource_id 
        WHERE resource_type = 'JOB_ATTEMPT' AND job_attempt_id IS NULL
    """)
    op.execute("""
        UPDATE execution_intents 
        SET release_id = resource_id 
        WHERE resource_type = 'SERVICE_RELEASE' AND release_id IS NULL
    """)

    op.drop_index("ix_execution_intents_resource_id", table_name="execution_intents")
    op.drop_column("execution_intents", "resource_id")

    op.create_foreign_key(
        "fk_execution_intents_job_attempt_id_job_attempts",
        "execution_intents",
        "job_attempts",
        ["job_attempt_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_execution_intents_release_id_releases",
        "execution_intents",
        "releases",
        ["release_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_index("ix_execution_intents_job_attempt_id", "execution_intents", ["job_attempt_id"])
    op.create_index("ix_execution_intents_release_id", "execution_intents", ["release_id"])

    # Align index names to match model declarations
    op.execute("ALTER INDEX ix_execution_intents_name RENAME TO ix_execution_intents_deterministic_resource_name")
    op.execute("ALTER INDEX ix_execution_intents_lease_expires RENAME TO ix_execution_intents_lease_expires_at")

    op.create_check_constraint(
        "ck_execution_intents_typed_resource",
        "execution_intents",
        "(resource_type = 'JOB_ATTEMPT' AND job_attempt_id IS NOT NULL AND release_id IS NULL) OR "
        "(resource_type = 'SERVICE_RELEASE' AND release_id IS NOT NULL AND job_attempt_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_execution_intents_resource_type",
        "execution_intents",
        "resource_type IN ('JOB_ATTEMPT', 'SERVICE_RELEASE')",
    )
    op.create_check_constraint(
        "ck_execution_intents_status",
        "execution_intents",
        "status IN ('PENDING', 'CLAIMED', 'APPLIED', 'TERMINATED')",
    )

    # Unique constraint with NULLS NOT DISTINCT (PostgreSQL 16)
    op.execute("""
        ALTER TABLE execution_intents 
        ADD CONSTRAINT uq_execution_intents_target 
        UNIQUE NULLS NOT DISTINCT (resource_type, job_attempt_id, release_id, target_generation)
    """)

    # ---------------------------------------------------------
    # 5. applications: deferred FK current_release_id -> releases.id
    # ---------------------------------------------------------
    op.create_foreign_key(
        "fk_applications_current_release_id_releases",
        "applications",
        "releases",
        ["current_release_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_applications_workload_type",
        "applications",
        "workload_type IN ('HTTP_SERVICE')",
    )

    # ---------------------------------------------------------
    # 6. Index renames & additions for model agreement (T17)
    # ---------------------------------------------------------
    op.execute("ALTER INDEX ix_audit_events_actor RENAME TO ix_audit_events_actor_subject")
    op.execute("ALTER INDEX ix_audit_events_created RENAME TO ix_audit_events_created_at")

    op.execute("ALTER INDEX ix_idempotency_records_expires RENAME TO ix_idempotency_records_expires_at")

    op.execute("ALTER INDEX ix_quota_reservations_expires RENAME TO ix_quota_reservations_expires_at")
    op.create_index("ix_quota_reservations_resource_class", "quota_reservations", ["resource_class"])
    op.create_check_constraint(
        "ck_quota_reservations_resource_class",
        "quota_reservations",
        "resource_class IN ('CONCURRENT_JOB', 'CONCURRENT_BUILD', 'DEPLOYED_SERVICE')",
    )
    op.create_check_constraint(
        "ck_quota_reservations_status",
        "quota_reservations",
        "status IN ('ACTIVE', 'RELEASED', 'EXPIRED')",
    )

    # ---------------------------------------------------------
    # 7. CHECK constraints on remaining state/status columns
    # ---------------------------------------------------------
    op.create_check_constraint(
        "ck_releases_status",
        "releases",
        "status IN ('REQUESTED', 'BUILDING', 'IMAGE_READY', 'DEPLOYING', 'HEALTHY', 'BUILD_FAILED', 'DEPLOY_FAILED')",
    )
    op.create_check_constraint(
        "ck_jobs_state",
        "jobs",
        "state IN ('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'SUCCEEDED', 'RETRY_WAIT', 'RECOVERY_PENDING', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED')",
    )
    op.create_check_constraint(
        "ck_workspace_memberships_role",
        "workspace_memberships",
        "role IN ('OWNER', 'DEVELOPER', 'VIEWER')",
    )


def downgrade() -> None:
    # 7. Remaining CHECK constraints
    op.drop_constraint("ck_workspace_memberships_role", "workspace_memberships", type_="check")
    op.drop_constraint("ck_jobs_state", "jobs", type_="check")
    op.drop_constraint("ck_releases_status", "releases", type_="check")

    # 6. Quota reservations, idempotency, audit index renames
    op.drop_constraint("ck_quota_reservations_status", "quota_reservations", type_="check")
    op.drop_constraint("ck_quota_reservations_resource_class", "quota_reservations", type_="check")
    op.drop_index("ix_quota_reservations_resource_class", table_name="quota_reservations")
    op.execute("ALTER INDEX ix_quota_reservations_expires_at RENAME TO ix_quota_reservations_expires")

    op.execute("ALTER INDEX ix_idempotency_records_expires_at RENAME TO ix_idempotency_records_expires")

    op.execute("ALTER INDEX ix_audit_events_created_at RENAME TO ix_audit_events_created")
    op.execute("ALTER INDEX ix_audit_events_actor_subject RENAME TO ix_audit_events_actor")

    # 5. applications
    op.drop_constraint("ck_applications_workload_type", "applications", type_="check")
    op.drop_constraint("fk_applications_current_release_id_releases", "applications", type_="foreignkey")

    # 4. execution_intents
    op.drop_constraint("uq_execution_intents_target", "execution_intents", type_="unique")
    op.drop_constraint("ck_execution_intents_status", "execution_intents", type_="check")
    op.drop_constraint("ck_execution_intents_resource_type", "execution_intents", type_="check")
    op.drop_constraint("ck_execution_intents_typed_resource", "execution_intents", type_="check")

    op.execute("ALTER INDEX ix_execution_intents_deterministic_resource_name RENAME TO ix_execution_intents_name")
    op.execute("ALTER INDEX ix_execution_intents_lease_expires_at RENAME TO ix_execution_intents_lease_expires")

    op.drop_index("ix_execution_intents_release_id", table_name="execution_intents")
    op.drop_index("ix_execution_intents_job_attempt_id", table_name="execution_intents")

    op.drop_constraint("fk_execution_intents_release_id_releases", "execution_intents", type_="foreignkey")
    op.drop_constraint("fk_execution_intents_job_attempt_id_job_attempts", "execution_intents", type_="foreignkey")

    op.add_column("execution_intents", sa.Column("resource_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE execution_intents 
        SET resource_id = COALESCE(job_attempt_id, release_id)
    """)
    op.alter_column("execution_intents", "resource_id", nullable=False)
    op.create_index("ix_execution_intents_resource_id", "execution_intents", ["resource_id"])

    op.drop_column("execution_intents", "release_id")
    op.drop_column("execution_intents", "job_attempt_id")
    op.drop_column("execution_intents", "resource_uid")

    # 3. job_attempts
    op.drop_constraint("ck_job_attempts_state", "job_attempts", type_="check")
    op.drop_index("ix_job_attempts_workspace_id", table_name="job_attempts")
    op.drop_constraint("fk_job_attempts_workspace_id_workspaces", "job_attempts", type_="foreignkey")
    op.drop_column("job_attempts", "workspace_id")

    # 2. consumed_events
    op.add_column("consumed_events", sa.Column("consumer_group", sa.String(length=100), nullable=True))
    op.execute("UPDATE consumed_events SET consumer_group = handler WHERE consumer_group IS NULL")
    op.alter_column("consumed_events", "consumer_group", nullable=False)

    op.drop_index("ix_consumed_events_handler", table_name="consumed_events")
    op.drop_constraint("uq_consumed_event_handler", "consumed_events", type_="unique")
    op.drop_column("consumed_events", "handler")

    op.create_unique_constraint("uq_consumed_event_group", "consumed_events", ["event_id", "consumer_group"])

    # 1. outbox_events
    op.drop_constraint("ck_outbox_events_status", "outbox_events", type_="check")
    op.drop_index("ix_outbox_events_workspace_id", table_name="outbox_events")
    op.drop_constraint("fk_outbox_events_workspace_id_workspaces", "outbox_events", type_="foreignkey")
    op.drop_column("outbox_events", "workspace_id")
    op.drop_column("outbox_events", "schema_version")
