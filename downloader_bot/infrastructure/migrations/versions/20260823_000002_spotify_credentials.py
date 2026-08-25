"""Store the deployment-owned Spotify Premium session separately from media cache data."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_000002"
down_revision: str | Sequence[str] | None = "20260821_000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("spotify_credentials"):
        return
    op.create_table(
        "spotify_credentials",
        sa.Column("id", sa.Boolean(), primary_key=True, nullable=False),
        sa.Column("credentials", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("id", name="spotify_credentials_singleton"),
    )


def downgrade() -> None:
    op.drop_table("spotify_credentials")
