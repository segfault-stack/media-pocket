"""Create the clean modular-monolith schema.

Revision ID: 20260821_000001
Revises:
"""

from collections.abc import Sequence

from alembic import op

from downloader_bot.infrastructure.database import Base

revision: str = "20260821_000001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
