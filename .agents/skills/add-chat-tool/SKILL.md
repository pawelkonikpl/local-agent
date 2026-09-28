---
name: add-chat-tool
description: How to add a new tool the chat model can call in local-agent (the `api` service's tool registry). Use when asked to add, implement or register a chat tool / function the model can use, e.g. "dodaj narzędzie", "add a tool for X", "let the model look up Y".
---

# Adding a chat tool

Tools live in the `api` service and run inside its process, in the model/tool loop in
`services/api/src/api/chat/streaming.py` (`run_generation`). The loop, llm-proxy, the
provider translation (Claude and `local-model`) and the GUI are all generic: **a new tool
only needs a new class and one line in the registry.** Do not touch `streaming.py`,
llm-proxy or the GUI to add a tool.

Read these before writing anything:

- `services/api/src/api/tools/base.py`: the `Tool` protocol, `ToolResult`, `ToolInputError`
- `services/api/src/api/tools/registry.py`: `ToolRegistry` and `build_default_registry()`
- `services/api/src/api/tools/current_time.py`: the reference tool; copy its structure

## Steps

1. **Create `services/api/src/api/tools/<tool_name>.py`** with one class that satisfies `Tool`:

   ```python
   from api.tools.base import ToolInputError, ToolResult


   class ExampleTool:
       """`example_lookup`: one line on what it does and why the model needs it."""

       name = "example_lookup"
       description = (
           "What it returns, in one sentence. "
           "When to use it (and when not to), in one sentence."
       )
       input_schema = {
           "type": "object",
           "properties": {
               "query": {"type": "string", "description": "What to look up, e.g. ..."},
           },
           "required": ["query"],
           "additionalProperties": False,
       }

       def __init__(self, ...) -> None:  # inject clocks, HTTP clients, settings, so tests can fake them
           ...

       async def run(self, input: dict) -> ToolResult:
           query = input.get("query")
           if not isinstance(query, str) or not query.strip():
               raise ToolInputError("query must be a non-empty string")
           ...
           return ToolResult("compact, readable text for the model")
   ```

2. **Register it** in `build_default_registry()` in `registry.py`: add an instance to the
   list. Order is the order the model sees the tools in.

3. **Configuration (only if needed):** add fields to `Settings` in
   `services/api/src/api/config.py` (env var = upper-cased field name), and pass values
   into the tool's constructor from `build_default_registry()`. Don't read `settings`
   inside `run()`. Secrets go through env / podman secrets, never into the DB.

4. **New dependencies (only if needed):** add them to `services/api/pyproject.toml`, then run
   `uv lock` at the repo root.

5. **Tests:** add `services/api/tests/test_<tool_name>_tool.py` with plain unit tests
   that call `await tool.run({...})` directly (no DB, no HTTP app needed). Cover: the
   happy path, each `ToolInputError` case, and each expected upstream failure. Fake time or HTTP via
   constructor injection (`httpx.MockTransport` for HTTP clients). Optionally add a case
   through `ToolRegistry.execute` to check the error text the model will actually see.

6. **Verify and ship:** see "Verification" below.

## The contract

**`name`**
- snake_case, a verb or a noun describing the action (`get_current_time`, `web_search`),
  matching `^[a-zA-Z0-9_-]{1,64}$` (both the Anthropic and the OpenAI format require this).
- Must be unique: a duplicate raises `ValueError` when the app starts.

**`description`**
- In English, 1–3 sentences: what the tool returns, and when the model should call it.
- This text is the only thing the model knows about the tool. The small `local-model`
  (Qwen3.5 4B) decides whether to call a tool almost entirely from it. Make the trigger
  explicit ("Use it whenever the answer depends on …").

**`input_schema`**
- A JSON Schema object, sent 1:1 as Anthropic `input_schema` and translated to an OpenAI
  function `parameters` for `local-model`.
- Keep it flat and small: a few primitive properties, each with a `description` that includes an
  example, `required` listed, `"additionalProperties": false`. Nested objects and
  `oneOf`/`anyOf` are handled badly by small models.
- **Nothing validates input against the schema.** `run()` must check every field it uses
  (type, range, allowed values) and raise `ToolInputError` with a message the model can act
  on. Optional fields need a default (see `DEFAULT_TIMEZONE` in `current_time.py`).

**`run(input) -> ToolResult`**
- `async`, and must not block the event loop: it shares the process with every user's
  chat. Use `httpx.AsyncClient` for HTTP, and `asyncio.to_thread(...)` for sync or CPU-heavy code.
- Return `ToolResult(content: str)`: concise, human-readable text, not a raw JSON dump.
  Output over `tool_output_max_chars` (16000 by default) is truncated by the registry; aim
  well below that, because every character costs input tokens on every later turn.
- Error handling, so the model gets a useful `tool_result` instead of a crash:
  - bad input from the model → `raise ToolInputError("…")`, which the model sees as `Invalid input: …`
    and can retry.
  - an expected failure (not found, upstream 4xx/5xx, no results) → `return ToolResult("…", is_error=True)`
    with a plain explanation.
  - anything else that raises becomes `Tool failed: <Type>: <msg>` and is logged. That's a
    fallback, not a way to report errors.
- Never catch and swallow `asyncio.CancelledError`: the Stop button cancels a running tool through it.
- The registry enforces `tool_timeout_s` (30s by default). A slow tool should set its own
  shorter timeouts on network calls.

**State and safety**
- One instance is built at startup (`create_app()` → `build_default_registry()`) and
  shared by all users and sessions. Keep no per-user or per-call state on `self`.
- `run()` gets no user or session context. If a tool ever needs it, that's a design change to
  `Tool` and `ToolRegistry.execute`: raise it with the user, don't work around it.
- `approval_mode` is **not honoured yet**: every tool call runs automatically. Only add
  read-only, side-effect-free tools (lookups, calculations, fetches). Anything that writes,
  deletes, sends, or spends money needs an approval flow first: stop and ask.
- Treat `input` as untrusted, since the model can be prompt-injected by earlier tool output. No shell
  commands, no filesystem paths built from input, no fetching arbitrary URLs from the `api`
  container (SSRF into `db`/`llm-proxy`). Internet-facing tools go through a separate
  service; see `.github/task/narzedzie-web-search.md` for the `web-agent` design.

## Verification

1. Unit tests, from the repo root, against a **dedicated test database** (the suite drops
   the schema, so never point it at `local_agent`):
   ```bash
   DATABASE_URL=postgresql+asyncpg://local_agent:local_agent@127.0.0.1:5432/local_agent_test \
   LLM_PROXY_URL=http://unused INTERNAL_PROXY_TOKEN=test \
   uv run pytest services/api/tests -q
   ```
   `test_post_message_streams_deltas_and_persists_both_messages` fails regardless of any tool change
   (`run_generation` writes outside the test transaction), so don't try to fix it as part of a tool task.
2. Rebuild and restart only `api`:
   ```bash
   podman-compose build api && podman-compose up -d --no-deps --force-recreate api
   ```
3. In the chat, ask something that should trigger the tool, once with a Claude model and once with
   `local-model`. The GUI shows each call as a collapsible box with its input and result. Also
   check the audit trail:
   ```bash
   podman exec local-agent_db_1 psql -U local_agent -d local_agent -c \
     "select tool_name, status, input, left(output::text, 100) from tool_call_events order by created_at desc limit 5"
   ```
4. If `local-model` doesn't call the tool, improve the `description` first (explicit trigger,
   example input). If it calls the tool with bad arguments, tighten the property `description`s and
   the `ToolInputError` messages. Don't change the loop.

`CHAT_TOOLS_ENABLED=false` on `api` turns all tools off, which is useful to check a regression isn't
caused by a tool.
