from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

# What a tool can do, for the Rule of Two: an agent session should combine at most two of reading
# untrusted content, touching sensitive data, and having an effect outside (sending, changing).
# `GenerationGuard` enforces the combinations; the model never sees these.
Capability = Literal["reads_untrusted", "sensitive_data", "external_effect"]


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
    capabilities: frozenset[Capability]

    async def run(self, input: dict) -> ToolResult: ...


def parse_input[InputT: BaseModel](model: type[InputT], input: dict) -> InputT:
    """The model's tool `input` as `model`, or `ToolInputError` naming each bad field.

    Strict: the model must send the JSON types its `input_schema` asks for, so "3" is not an
    integer here and `true` is not 1.
    """
    try:
        return model.model_validate(input, strict=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}" for error in exc.errors()
        )
        raise ToolInputError(problems) from exc
