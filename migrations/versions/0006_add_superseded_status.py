"""add superseded status to release check constraints

Revision ID: 0006_add_superseded_status
Revises: 0005_source_builds_and_repos
Create Date: 2026-10-01 14:00:00.000000

"""
from typing import Sequence, Union
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_add_superseded_status"
down_revision: Union[str, None] = "0005_source_builds_and_repos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUSES = "('REQUESTED', 'BUILDING', 'IMAGE_READY', 'DEPLOYING', 'HEALTHY', 'BUILD_FAILED', 'DEPLOY_FAILED')"
NEW_STATUSES = "('REQUESTED', 'BUILDING', 'IMAGE_READY', 'DEPLOYING', 'HEALTHY', 'BUILD_FAILED', 'DEPLOY_FAILED', 'SUPERSEDED')"


def upgrade() -> None:
    op.drop_constraint("ck_releases_status", "releases", type_="check")
    op.create_check_constraint(
        "ck_releases_status",
        "releases",
        f"status IN {NEW_STATUSES}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_releases_status", "releases", type_="check")
    op.create_check_constraint(
        "ck_releases_status",
        "releases",
        f"status IN {OLD_STATUSES}",
    )
