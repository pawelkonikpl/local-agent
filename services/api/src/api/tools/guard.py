"""A deterministic monitor over one generation's tool calls, outside the model.

It sees only which tool is called, what that tool can do, and the call's input -- never the
content tools returned -- so a web page can't talk it into anything. It enforces policy that no
prompt can: a canary token must never leave in a tool input, and once the generation has read
untrusted content, outbound requests carrying encoded blobs and tools touching sensitive data are
refused (the Rule of Two).
"""

import json
import logging
import re
import secrets

from api.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# 40+ characters of base64/hex alphabet: the shape of data smuggled out in a query or URL.
ENCODED_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


class GenerationGuard:
    """Per-generation state: its canary and whether it has read untrusted content yet.

    Created once per `run_generation`; the tool registry is shared across generations, so this
    state can't live there.
    """

    def __init__(self) -> None:
        self.canary = f"cnry-{secrets.token_hex(8)}"
        self.tainted = False

    def check(self, tool: Tool, input: dict) -> str | None:
        """Why this call must not run, or None if it may."""
        serialized = json.dumps(input, ensure_ascii=False)
        if self.canary in serialized:
            return "canary leaked into tool input"
        if not self.tainted:
            return None
        if "sensitive_data" in tool.capabilities:
            return "sensitive tool blocked after reading web content"
        if "external_effect" in tool.capabilities and any(
            ENCODED_BLOB.search(value) for value in _strings(input)
        ):
            return "encoded data in an outbound request after reading web content"
        return None

    def after(self, tool: Tool, result: ToolResult) -> None:
        # Even an error result may carry page text, so any call of such a tool taints.
        if "reads_untrusted" in tool.capabilities:
            self.tainted = True

    def check_output(self, text: str) -> None:
        """Alarm (only -- it's already been streamed) if the model's text contains the canary."""
        if self.canary in text:
            logger.warning("security_alert reason=%s", "canary in assistant text")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []
