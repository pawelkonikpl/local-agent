"""Server-sent events as every service here frames them: `event: <name>` plus one JSON `data:` line.

llm-proxy speaks Anthropic Messages SSE to api, and api speaks its own chat events to the browser;
both use this one framing.
"""

import json
from collections.abc import AsyncIterable, AsyncIterator

EVENT_PREFIX = "event:"
DATA_PREFIX = "data:"


def encode_sse(event: str, data: dict) -> bytes:
    return f"{EVENT_PREFIX} {event}\n{DATA_PREFIX} {json.dumps(data)}\n\n".encode()


async def iter_sse(lines: AsyncIterable[str]) -> AsyncIterator[tuple[str, dict]]:
    """`(event, data)` for every `data:` line that follows an `event:` line and holds a JSON object.

    Anything else (comments, keep-alive blank lines, malformed JSON) is skipped, not fatal: a
    stream is read for the events it carries, never rejected as a whole.
    """
    event: str | None = None
    async for line in lines:
        if line.startswith(EVENT_PREFIX):
            event = line[len(EVENT_PREFIX) :].strip()
            continue
        if event is None or not line.startswith(DATA_PREFIX):
            continue
        raw = line[len(DATA_PREFIX) :].strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield event, data
