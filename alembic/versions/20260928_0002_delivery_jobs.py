"""add persistent delivery jobs

Revision ID: 20260928_0002
Revises: 20260928_0001
Create Date: 2026-09-28 22:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260928_0002"
down_revision: str | Sequence[str] | None = "20260928_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the durable, lockable delivery outbox."""

    op.create_table(
        "delivery_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "olx_to_telegram",
                "telegram_to_olx",
                name="delivery_job_kind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "retry",
                "succeeded",
                "failed",
                "dead_letter",
                name="delivery_job_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("olx_message_id", sa.Integer(), nullable=True),
        sa.Column("outbound_message_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_token", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_delivery_jobs_attempts_non_negative"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name=op.f("ck_delivery_jobs_max_attempts_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry', 'succeeded', 'failed', 'dead_letter')",
            name=op.f("ck_delivery_jobs_status_values"),
        ),
        sa.CheckConstraint(
            "(kind = 'olx_to_telegram' AND olx_message_id IS NOT NULL "
            "AND outbound_message_id IS NULL) OR "
            "(kind = 'telegram_to_olx' AND outbound_message_id IS NOT NULL "
            "AND olx_message_id IS NULL)",
            name=op.f("ck_delivery_jobs_reference_matches_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["olx_message_id"],
            ["olx_messages.id"],
            name=op.f("fk_delivery_jobs_olx_message_id_olx_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"],
            ["outbound_messages.id"],
            name=op.f("fk_delivery_jobs_outbound_message_id_outbound_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_jobs")),
        sa.UniqueConstraint(
            "kind",
            "olx_message_id",
            name="uq_delivery_jobs_kind_olx_message",
        ),
        sa.UniqueConstraint(
            "kind",
            "outbound_message_id",
            name="uq_delivery_jobs_kind_outbound_message",
        ),
    )
    op.create_index(
        "ix_delivery_jobs_due",
        "delivery_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_jobs_lock",
        "delivery_jobs",
        ["status", "locked_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the persistent delivery outbox."""

    op.drop_index("ix_delivery_jobs_lock", table_name="delivery_jobs")
    op.drop_index("ix_delivery_jobs_due", table_name="delivery_jobs")
    op.drop_table("delivery_jobs")
