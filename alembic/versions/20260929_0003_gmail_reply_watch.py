"""add durable Gmail reply notification state

Revision ID: 20260929_0003
Revises: 20260928_0002
Create Date: 2026-09-29 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260929_0003"
down_revision: str | Sequence[str] | None = "20260928_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create privacy-minimal state used to prevent duplicate Telegram alerts."""

    op.create_table(
        "gmail_reply_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uid_validity", sa.BigInteger(), nullable=False),
        sa.Column("message_uid", sa.BigInteger(), nullable=False),
        sa.Column("message_id_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_gmail_reply_notifications_attempts_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gmail_reply_notifications")),
        sa.UniqueConstraint(
            "uid_validity",
            "message_uid",
            name="uq_gmail_reply_notifications_mailbox_message",
        ),
    )


def downgrade() -> None:
    """Remove Gmail reply notification state."""

    op.drop_table("gmail_reply_notifications")
