import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base


class ChatSession(Base):
    """Chat + sandbox unit. Table name stays `sessions` per the architecture plan."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("approval_mode in ('ask','auto')", name="ck_sessions_approval_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="active")
    approval_mode: Mapped[str] = mapped_column(String, nullable=False, server_default="ask")
    sandbox_container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    workspace_volume_name: Mapped[str | None] = mapped_column(String, nullable=True)
    forked_from_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    # FK to messages.id added via ALTER TABLE in the migration (messages is created after sessions).
    forked_from_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL", use_alter=True, name="fk_sessions_forked_from_message"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
