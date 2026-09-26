from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolResult:
    """What a tool hands back to the model as a `tool_result` block."""

    content: str
    is_error: bool = False


class ToolInputError(Exception):
    """Raised by a tool when the model passed input it can't act on.

    `ToolRegistry.execute` turns it into an error `ToolResult`, so the model sees the message and
    can retry with corrected input.
    """


class Tool(Protocol):
    """A tool the model can call.

    Deliberately the same shape as the session-agent registry planned for Stage 5, so that
    registry can take over these tools unchanged. `input_schema` is a JSON Schema, passed 1:1 as
    an entry of Anthropic's `tools=[...]`.
    """

    name: str
    description: str
    input_schema: dict

    async def run(self, input: dict) -> ToolResult: ...
