"""The Solver's hands and senses: the only module that knows about Playwright and CDP.

Playwright is used as the browser process manager and the CDP transport; everything a tool
senses or does goes through `CDPSession.send(...)` and CDP events, so later rungs (trusted
`Input.*` events, screenshots for a vision Operator) plug in here without a new layer.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, CDPSession, Playwright, ProxySettings, async_playwright
from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

CONSOLE_ERRORS_KEPT = 20
BLOCKED_REQUESTS_KEPT = 20
LOGGED_URL_MAX_CHARS = 200
# Not needed to read a page's text, and a tracking/exfiltration channel: refused on every origin.
BLOCKED_RESOURCE_TYPES = frozenset({"Image", "Media", "Font"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> str:
    """`scheme://host:port` of `url`, with the default port made explicit ("" if not http(s))."""
    parsed = urlsplit(url)
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.hostname:
        return ""
    try:
        port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    except ValueError:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}:{port}"


class BrowserError(Exception):
    """The page or the browser failed (network error, crash, CDP error) -- not a bug in our code."""


@dataclass(frozen=True)
class NavigationOutcome:
    url: str
    # HTTP status of the main document as seen by the Network domain; None if no response was
    # observed (e.g. served from cache without a network event).
    status: int | None


@dataclass(frozen=True)
class BlockedRequest:
    url: str
    resource_type: str
    frame_id: str


class BrowserSession:
    """One page in its own clean `BrowserContext`, with the CDP domains the Solver senses through.

    Every request the page makes -- navigations, redirects, iframes, scripts -- is paused by the
    `Fetch` domain and let through only if its origin is in `allowed_origins` (Agent Origin Sets:
    a read-only set fixed by code for the task, which neither the page nor the model can extend).
    """

    def __init__(
        self, cdp: CDPSession, *, navigation_timeout_s: float, allowed_origins: frozenset[str]
    ) -> None:
        self._cdp = cdp
        self._navigation_timeout_s = navigation_timeout_s
        self._allowed_origins = allowed_origins
        self._document_status: dict[str, int] = {}
        self._loaded = asyncio.Event()
        # Handlers of paused requests, kept referenced until done so they aren't garbage-collected.
        self._fetch_tasks: set[asyncio.Task] = set()
        self._blocked: list[BlockedRequest] = []
        # URLs (shortened) the origin policy refused, oldest first; the same list for the session's
        # lifetime, so a caller holding it sees later refusals too.
        self.blocked_requests: list[str] = []
        self.console_errors: list[str] = []

    @classmethod
    async def open(
        cls, context: BrowserContext, *, navigation_timeout_s: float, allowed_origins: frozenset[str]
    ) -> "BrowserSession":
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        session = cls(cdp, navigation_timeout_s=navigation_timeout_s, allowed_origins=allowed_origins)
        cdp.on("Fetch.requestPaused", session._on_request_paused)
        cdp.on("Network.responseReceived", session._on_response)
        cdp.on("Page.loadEventFired", lambda _params: session._loaded.set())
        cdp.on("Runtime.consoleAPICalled", session._on_console)
        cdp.on("Runtime.exceptionThrown", session._on_exception)
        await session._send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        for domain in ("Network", "Page", "Runtime"):
            await session._send(f"{domain}.enable")
        return session

    async def navigate(self, url: str) -> NavigationOutcome:
        """Act: load `url` and wait for the load event. Verify through a different channel than the
        one that acted: the document status comes from `Network.responseReceived`, not from
        `Page.navigate`'s own answer."""
        self._loaded.clear()
        blocked_before = len(self._blocked)
        try:
            async with asyncio.timeout(self._navigation_timeout_s):
                result = await self._send("Page.navigate", {"url": url})
                self._raise_if_navigation_blocked(result.get("frameId", ""), blocked_before)
                if result.get("errorText"):
                    raise BrowserError(f"Navigation failed: {result['errorText']}")
                await self._loaded.wait()
        except TimeoutError:
            raise BrowserError(f"Navigation timed out after {self._navigation_timeout_s:g}s") from None
        # A redirect to a refused origin can surface only after the navigation itself started.
        self._raise_if_navigation_blocked(result.get("frameId", ""), blocked_before)
        return NavigationOutcome(url=url, status=self._document_status.get(result.get("loaderId", "")))

    def _raise_if_navigation_blocked(self, frame_id: str, since: int) -> None:
        for blocked in self._blocked[since:]:
            if blocked.resource_type == "Document" and blocked.frame_id == frame_id:
                raise BrowserError(f"navigation blocked by origin policy: {origin_of(blocked.url) or blocked.url}")

    async def evaluate(self, expression: str) -> Any:
        """Sense (rung 1 of the ladder): run JS in the page and return its JSON-serializable value."""
        result = await self._send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = details.get("exception", {}).get("description") or details.get("text")
            raise BrowserError(f"Page script failed: {description}")
        return result.get("result", {}).get("value")

    async def text_content(self) -> str:
        return await self.evaluate("document.body ? document.body.innerText : ''") or ""

    async def screenshot(self) -> bytes:
        result = await self._send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(result["data"])

    async def _send(self, method: str, params: dict | None = None) -> dict:
        try:
            return await self._cdp.send(method, params or {})
        except PlaywrightError as exc:
            raise BrowserError(f"{method} failed: {exc.message}") from exc

    def _on_request_paused(self, params: dict) -> None:
        task = asyncio.create_task(self._decide(params))
        self._fetch_tasks.add(task)
        task.add_done_callback(self._fetch_tasks.discard)

    async def _decide(self, params: dict) -> None:
        request_id = params["requestId"]
        url = params.get("request", {}).get("url", "")
        resource_type = params.get("resourceType", "")
        try:
            if origin_of(url) in self._allowed_origins and resource_type not in BLOCKED_RESOURCE_TYPES:
                await self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            if len(self._blocked) < BLOCKED_REQUESTS_KEPT:
                self._blocked.append(
                    BlockedRequest(url[:LOGGED_URL_MAX_CHARS], resource_type, params.get("frameId", ""))
                )
                self.blocked_requests.append(url[:LOGGED_URL_MAX_CHARS])
            await self._cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
        except PlaywrightError:
            # The page or the context went away while the request was paused; nothing to answer.
            pass

    def _on_response(self, params: dict) -> None:
        if params.get("type") == "Document":
            self._document_status[params["loaderId"]] = params["response"]["status"]

    def _on_console(self, params: dict) -> None:
        if params.get("type") != "error":
            return
        text = " ".join(
            str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])
        )
        self._record_error(f"console.error: {text}")

    def _on_exception(self, params: dict) -> None:
        details = params.get("exceptionDetails", {})
        self._record_error(details.get("exception", {}).get("description") or details.get("text", ""))

    def _record_error(self, message: str) -> None:
        if len(self.console_errors) < CONSOLE_ERRORS_KEPT:
            self.console_errors.append(message)


class BrowserManager:
    """One browser per process, started lazily and relaunched if it goes away.

    Every `session()` gets a fresh `BrowserContext`: no cookies or storage carry over between
    searches or users.
    """

    def __init__(
        self,
        *,
        cdp_url: str | None,
        headless: bool,
        navigation_timeout_s: float,
        proxy_url: str | None = None,
        proxy_bypass: str | None = None,
    ) -> None:
        self._cdp_url = cdp_url
        self._headless = headless
        self._proxy_url = proxy_url
        self._proxy_bypass = proxy_bypass
        self._navigation_timeout_s = navigation_timeout_s
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self.open_contexts = 0

    async def start(self) -> None:
        await self._ensure_browser()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    @asynccontextmanager
    async def session(self, *, allowed_origins: frozenset[str]) -> AsyncGenerator[BrowserSession]:
        browser = await self._ensure_browser()
        try:
            context = await browser.new_context()
        except PlaywrightError as exc:
            raise BrowserError(f"Could not open a browser context: {exc.message}") from exc
        self.open_contexts += 1
        try:
            yield await BrowserSession.open(
                context, navigation_timeout_s=self._navigation_timeout_s, allowed_origins=allowed_origins
            )
        finally:
            self.open_contexts -= 1
            try:
                await context.close()
            except PlaywrightError:
                logger.warning("Closing a browser context failed", exc_info=True)

    def _proxy(self) -> ProxySettings | None:
        """The egress proxy every request goes through, except hosts in `proxy_bypass`."""
        if not self._proxy_url:
            return None
        proxy: ProxySettings = {"server": self._proxy_url}
        if self._proxy_bypass:
            proxy["bypass"] = self._proxy_bypass
        return proxy

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                if self._cdp_url:
                    self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
                else:
                    self._browser = await self._playwright.chromium.launch(
                        headless=self._headless, args=["--disable-dev-shm-usage"], proxy=self._proxy()
                    )
            except PlaywrightError as exc:
                raise BrowserError(f"Could not start the browser: {exc.message}") from exc
            return self._browser
