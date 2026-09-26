import asyncio
import logging
from collections.abc import Iterable

from api.config import settings
from api.tools.base import Tool, ToolInputError, ToolResult
from api.tools.current_time import CurrentTimeTool
from api.tools.web_search import WebSearchTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """The tools offered to the model, and the one place that runs them.

    `execute` never raises for a tool's own failure -- every failure becomes an error
    `ToolResult` the model can read and react to. Only `asyncio.CancelledError` propagates, so
    cancelling a generation still stops a running tool.
    """

    def __init__(self, tools: Iterable[Tool], *, timeout_s: float, max_output_chars: int) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        self._timeout_s = timeout_s
        self._max_output_chars = max_output_chars

    def definitions(self) -> list[dict]:
        """Tool definitions for the `tools` field of a Messages request, in registration order."""
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, input: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await tool.run(input)
        except ToolInputError as exc:
            return ToolResult(f"Invalid input: {exc}", is_error=True)
        except TimeoutError:
            return ToolResult(f"Tool timed out after {self._timeout_s:g}s", is_error=True)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return ToolResult(f"Tool failed: {type(exc).__name__}: {exc}", is_error=True)
        return self._truncate(result)

    def _truncate(self, result: ToolResult) -> ToolResult:
        total = len(result.content)
        if total <= self._max_output_chars:
            return result
        content = result.content[: self._max_output_chars] + f"\n[output truncated: {total} chars total]"
        return ToolResult(content, is_error=result.is_error)


def build_default_registry() -> ToolRegistry:
    tools: list[Tool] = [CurrentTimeTool()]
    if settings.web_agent_url:
        tools.append(
            WebSearchTool(
                base_url=settings.web_agent_url,
                token=settings.internal_proxy_token,
                timeout_s=settings.web_search_timeout_s,
            )
        )
    return ToolRegistry(
        tools,
        timeout_s=settings.tool_timeout_s,
        max_output_chars=settings.tool_output_max_chars,
    )
