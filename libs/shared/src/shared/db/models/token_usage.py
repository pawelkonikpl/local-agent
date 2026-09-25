import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base


class TokenUsage(Base):
    """Append-only ledger — never updated or deleted, kept for audit/history even before Etap 6 visualizes it.

    `provider`/`token_source` exist from Etap 1 so a future local (self-hosted) model backend doesn't need a
    migration: Anthropic reports usage in its response (`token_source="api_reported"`); a local model has no
    such response and must have its tokens counted client-side (`token_source="computed"`).
    """

    __tablename__ = "token_usage"
    __table_args__ = (
        CheckConstraint("provider in ('anthropic','local')", name="ck_token_usage_provider"),
        CheckConstraint(
            "token_source in ('api_reported','computed')", name="ck_token_usage_token_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False, server_default="anthropic")
    model: Mapped[str] = mapped_column(nullable=False)
    token_source: Mapped[str] = mapped_column(String, nullable=False, server_default="api_reported")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
