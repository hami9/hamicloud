"""add repositories, source builds, and webhook deliveries

Revision ID: 0005_source_builds_and_repos
Revises: 0004_add_outbox_next_attempt_at
Create Date: 2026-09-30 17:40:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_source_builds_and_repos"
down_revision: Union[str, None] = "0004_add_outbox_next_attempt_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create repositories table
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("repo_url", sa.String(length=255), nullable=False),
        sa.Column("webhook_secret", sa.String(length=255), nullable=False),
        sa.Column("default_branch", sa.String(length=100), nullable=False, server_default="main"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_repository_workspace_name"),
    )
    op.create_index("ix_repositories_workspace_id", "repositories", ["workspace_id"], unique=False)

    # 2. Add repository columns to applications table
    op.add_column("applications", sa.Column("repository_id", sa.Uuid(), nullable=True))
    op.add_column("applications", sa.Column("dockerfile_path", sa.String(length=255), server_default="Dockerfile", nullable=False))
    op.add_column("applications", sa.Column("context_dir", sa.String(length=255), server_default=".", nullable=False))
    op.add_column("applications", sa.Column("git_branch", sa.String(length=100), server_default="main", nullable=True))
    op.create_foreign_key(
        "fk_applications_repository_id",
        "applications",
        "repositories",
        ["repository_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_applications_repository_id", "applications", ["repository_id"], unique=False)

    # 3. Add source build tracking columns to releases table
    op.add_column("releases", sa.Column("repository_id", sa.Uuid(), nullable=True))
    op.add_column("releases", sa.Column("git_ref", sa.String(length=100), nullable=True))
    op.add_column("releases", sa.Column("commit_message", sa.String(length=255), nullable=True))
    op.add_column("releases", sa.Column("build_duration_ms", sa.Integer(), nullable=True))
    op.add_column("releases", sa.Column("build_logs", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_releases_repository_id",
        "releases",
        "repositories",
        ["repository_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_releases_repository_id", "releases", ["repository_id"], unique=False)

    # 4. Create webhook_deliveries table for deduplication
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "delivery_id", name="uq_webhook_deliveries_repo_delivery"),
    )
    op.create_index("ix_webhook_deliveries_workspace_id", "webhook_deliveries", ["workspace_id"], unique=False)
    op.create_index("ix_webhook_deliveries_repository_id", "webhook_deliveries", ["repository_id"], unique=False)
    op.create_index("ix_webhook_deliveries_delivery_id", "webhook_deliveries", ["delivery_id"], unique=False)


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_constraint("fk_releases_repository_id", "releases", type_="foreignkey")
    op.drop_index("ix_releases_repository_id", table_name="releases")
    op.drop_column("releases", "build_logs")
    op.drop_column("releases", "build_duration_ms")
    op.drop_column("releases", "commit_message")
    op.drop_column("releases", "git_ref")
    op.drop_column("releases", "repository_id")
    op.drop_constraint("fk_applications_repository_id", "applications", type_="foreignkey")
    op.drop_index("ix_applications_repository_id", table_name="applications")
    op.drop_column("applications", "git_branch")
    op.drop_column("applications", "context_dir")
    op.drop_column("applications", "dockerfile_path")
    op.drop_column("applications", "repository_id")
    op.drop_table("repositories")
