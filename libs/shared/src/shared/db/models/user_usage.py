import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base


class UserUsageLimit(Base):
    """Per-user budget, checked synchronously by llm-proxy (Etap 2+) before forwarding a request."""

    __tablename__ = "user_usage_limits"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    monthly_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserUsageCounter(Base):
    """Running spend for the current period, incremented by llm-proxy from each response's usage block."""

    __tablename__ = "user_usage_counters"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, server_default="0")
