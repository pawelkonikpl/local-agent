from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from api.tools.base import Capability, ToolInputError, ToolResult, parse_input

DEFAULT_TIMEZONE = "UTC"


class CurrentTimeInput(BaseModel):
    timezone: str = DEFAULT_TIMEZONE


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CurrentTimeTool:
    """`get_current_time`: the current date and time in a given IANA timezone.

    The model has no notion of "now", so this is the tool it needs for anything relative to the
    present (today's date, the time somewhere, how long until a date).
    """

    name = "get_current_time"
    description = (
        "Returns the current date and time, optionally in a given IANA timezone. "
        "Use it whenever the answer depends on the current date or time."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name, e.g. Europe/Warsaw. Defaults to UTC.",
            }
        },
        "additionalProperties": False,
    }
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now

    async def run(self, input: dict) -> ToolResult:
        timezone_name = parse_input(CurrentTimeInput, input).timezone
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ToolInputError(f"Unknown timezone: {timezone_name}") from exc

        local_now = self._now().astimezone(zone)
        return ToolResult(f"{local_now.isoformat(timespec='seconds')} ({local_now:%A}, {timezone_name})")
