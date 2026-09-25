"""initial schema: users, auth_sessions, sessions, messages, tool_call_events,
token_usage, user_usage_limits, user_usage_counters

Revision ID: 0001
Revises:
Create Date: 2026-09-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.CheckConstraint("role in ('admin','user')", name="ck_users_role"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("approval_mode", sa.String(), server_default="ask", nullable=False),
        sa.Column("sandbox_container_id", sa.String(), nullable=True),
        sa.Column("workspace_volume_name", sa.String(), nullable=True),
        sa.Column("forked_from_session_id", sa.Uuid(), nullable=True),
        sa.Column("forked_from_message_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["forked_from_session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("approval_mode in ('ask','auto')", name="ck_sessions_approval_mode"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "sequence_number", name="uq_messages_session_seq"),
        sa.CheckConstraint("role in ('user','assistant','system')", name="ck_messages_role"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])

    # sessions.forked_from_message_id -> messages.id: added now that messages exists,
    # avoiding a circular dependency between sessions and messages at creation time.
    op.create_foreign_key(
        "fk_sessions_forked_from_message",
        "sessions",
        "messages",
        ["forked_from_message_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "tool_call_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), server_default="proposed", nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status in ('proposed','approved','rejected','running','succeeded','failed')",
            name="ck_tool_call_events_status",
        ),
    )
    op.create_index("ix_tool_call_events_session_id", "tool_call_events", ["session_id"])

    op.create_table(
        "token_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), server_default="anthropic", nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("token_source", sa.String(), server_default="api_reported", nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("provider in ('anthropic','local')", name="ck_token_usage_provider"),
        sa.CheckConstraint(
            "token_source in ('api_reported','computed')", name="ck_token_usage_token_source"
        ),
    )
    op.create_index("ix_token_usage_session_id", "token_usage", ["session_id"])
    op.create_index("ix_token_usage_user_id", "token_usage", ["user_id"])

    op.create_table(
        "user_usage_limits",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "user_usage_counters",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("spent_usd", sa.Numeric(12, 4), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "period_start"),
    )


def downgrade() -> None:
    op.drop_table("user_usage_counters")
    op.drop_table("user_usage_limits")
    op.drop_table("token_usage")
    op.drop_table("tool_call_events")
    op.drop_constraint("fk_sessions_forked_from_message", "sessions", type_="foreignkey")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
