import asyncio
import logging
from collections.abc import Iterable

from api.config import settings
from api.tools.base import Tool, ToolInputError, ToolResult
from api.tools.current_time import CurrentTimeTool
from api.tools.guard import GenerationGuard
from api.tools.site_search import SiteSearchTool
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

    def reads_untrusted(self) -> bool:
        """Whether any offered tool brings untrusted (web) content into the conversation."""
        return any("reads_untrusted" in tool.capabilities for tool in self._tools.values())

    async def execute(self, name: str, input: dict, *, guard: GenerationGuard | None = None) -> ToolResult:
        """Run `name`; with a `guard`, only if its policy allows the call.

        A refused call never reaches the tool and comes back as an error result; the alarm is
        logged without the input, which may hold the user's data.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        if guard is not None and (reason := guard.check(tool, input)):
            logger.warning("security_alert tool=%s reason=%s", name, reason)
            return ToolResult(f"Blocked by security policy: {reason}", is_error=True)
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await tool.run(input)
        except ToolInputError as exc:
            return ToolResult(f"Invalid input: {exc}", is_error=True)
        except TimeoutError:
            result = ToolResult(f"Tool timed out after {self._timeout_s:g}s", is_error=True)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            result = ToolResult(f"Tool failed: {type(exc).__name__}: {exc}", is_error=True)
        if guard is not None:
            guard.after(tool, result)
        return self._truncate(result)

    def _truncate(self, result: ToolResult) -> ToolResult:
        total = len(result.content)
        if total <= self._max_output_chars:
            return result
        content = result.content[: self._max_output_chars] + f"\n[output truncated: {total} chars total]"
        return ToolResult(content, is_error=result.is_error)


def build_default_registry() -> ToolRegistry:
    tools: list[Tool] = [CurrentTimeTool()]
    sites = settings.web_agent_sites if settings.web_agent_sites_url else []
    if settings.web_agent_url:
        tools.append(
            WebSearchTool(
                base_url=settings.web_agent_url,
                token=settings.internal_proxy_token,
                timeout_s=settings.web_search_timeout_s,
                site_search_sites=sites,
            )
        )
    if settings.web_agent_sites_url and sites:
        tools.append(
            SiteSearchTool(
                base_url=settings.web_agent_sites_url,
                token=settings.internal_proxy_token,
                timeout_s=settings.web_search_timeout_s,
                sites=sites,
                view_url=settings.site_browser_view_url,
            )
        )
    return ToolRegistry(
        tools,
        timeout_s=settings.tool_timeout_s,
        max_output_chars=settings.tool_output_max_chars,
    )
