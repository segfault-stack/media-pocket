"""Persist YouTube defaults and allow limited-use invite codes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_000003"
down_revision: str | Sequence[str] | None = "20260823_000002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column(
            "youtube_mode",
            sa.String(length=16),
            nullable=False,
            server_default="video",
        ),
    )
    op.execute(
        "UPDATE user_preferences SET youtube_mode = 'audio' "
        "WHERE default_audio_only IS TRUE"
    )
    op.alter_column("user_preferences", "youtube_mode", server_default=None)
    op.drop_constraint("ck_invite_policy", "invite_codes", type_="check")
    op.create_check_constraint(
        "ck_invite_policy",
        "invite_codes",
        "(kind = 'timed' AND expires_at IS NOT NULL AND max_uses IS NULL) OR "
        "(kind = 'one_time' AND expires_at IS NULL AND max_uses = 1) OR "
        "(kind = 'limited' AND expires_at IS NULL "
        "AND max_uses BETWEEN 2 AND 100000)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_invite_policy", "invite_codes", type_="check")
    op.execute("DELETE FROM invite_codes WHERE kind = 'limited'")
    op.create_check_constraint(
        "ck_invite_policy",
        "invite_codes",
        "(kind = 'timed' AND expires_at IS NOT NULL AND max_uses IS NULL) OR "
        "(kind = 'one_time' AND expires_at IS NULL AND max_uses = 1)",
    )
    op.drop_column("user_preferences", "youtube_mode")
