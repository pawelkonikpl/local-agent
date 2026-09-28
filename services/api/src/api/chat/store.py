import uuid
from datetime import UTC, datetime

from shared.db.models import Message
from sqlalchemy import null
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.models.tool_call_event import ToolCallEvent
from api.tools.base import ToolResult


class GenerationStore:
    """What a chat generation persists: its messages and the audit trail of its tool calls.

    A generation outlives the HTTP request that started it, so it can't use the request's DB
    session: every write here opens and commits its own session from `session_factory`.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_message(
        self, session_id: uuid.UUID, sequence_number: int, role: str, content: list[dict]
    ) -> Message:
        message = Message(session_id=session_id, sequence_number=sequence_number, role=role, content=content)
        async with self._session_factory() as db:
            db.add(message)
            await db.commit()
        return message

    async def record_tool_event(
        self,
        event_id: uuid.UUID,
        *,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
        tool_name: str,
        tool_input: dict,
        result: ToolResult | None = None,
    ) -> None:
        """Insert or update a `tool_call_events` row: `running` without a `result`, finished with one.

        An upsert keyed by a pre-assigned id, so finishing a call works whether or not its `running`
        row made it to the DB (a cancel can land in between).
        """
        if result is None:
            # `null()` rather than `None`: a Core insert would store `None` as JSON `null` in JSONB.
            status, output, completed_at = "running", null(), None
        else:
            status = "failed" if result.is_error else "succeeded"
            output = {"content": result.content, "is_error": result.is_error}
            completed_at = datetime.now(UTC)
        statement = pg_insert(ToolCallEvent).values(
            id=event_id,
            session_id=session_id,
            message_id=message_id,
            tool_name=tool_name,
            input=tool_input,
            status=status,
            output=output,
            completed_at=completed_at,
        )
        async with self._session_factory() as db:
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ToolCallEvent.id],
                    set_={"status": status, "output": output, "completed_at": completed_at},
                )
            )
            await db.commit()
