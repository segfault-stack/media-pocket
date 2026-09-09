"""Persist the direct-media compression choice.

Revision ID: 20260909_000004
Revises: 20260825_000003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_000004"
down_revision: str | Sequence[str] | None = "20260825_000003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "download_jobs",
        sa.Column("compression_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("download_jobs", "compression_json")
