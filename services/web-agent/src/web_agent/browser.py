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

from playwright.async_api import Browser, BrowserContext, CDPSession, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

CONSOLE_ERRORS_KEPT = 20


class BrowserError(Exception):
    """The page or the browser failed (network error, crash, CDP error) -- not a bug in our code."""


@dataclass(frozen=True)
class NavigationOutcome:
    url: str
    # HTTP status of the main document as seen by the Network domain; None if no response was
    # observed (e.g. served from cache without a network event).
    status: int | None


class BrowserSession:
    """One page in its own clean `BrowserContext`, with the CDP domains the Solver senses through."""

    def __init__(self, cdp: CDPSession, *, navigation_timeout_s: float) -> None:
        self._cdp = cdp
        self._navigation_timeout_s = navigation_timeout_s
        self._document_status: dict[str, int] = {}
        self._loaded = asyncio.Event()
        self.console_errors: list[str] = []

    @classmethod
    async def open(cls, context: BrowserContext, *, navigation_timeout_s: float) -> "BrowserSession":
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)
        session = cls(cdp, navigation_timeout_s=navigation_timeout_s)
        cdp.on("Network.responseReceived", session._on_response)
        cdp.on("Page.loadEventFired", lambda _params: session._loaded.set())
        cdp.on("Runtime.consoleAPICalled", session._on_console)
        cdp.on("Runtime.exceptionThrown", session._on_exception)
        for domain in ("Network", "Page", "Runtime"):
            await session._send(f"{domain}.enable")
        return session

    async def navigate(self, url: str) -> NavigationOutcome:
        """Act: load `url` and wait for the load event. Verify through a different channel than the
        one that acted: the document status comes from `Network.responseReceived`, not from
        `Page.navigate`'s own answer."""
        self._loaded.clear()
        try:
            async with asyncio.timeout(self._navigation_timeout_s):
                result = await self._send("Page.navigate", {"url": url})
                if result.get("errorText"):
                    raise BrowserError(f"Navigation failed: {result['errorText']}")
                await self._loaded.wait()
        except TimeoutError:
            raise BrowserError(f"Navigation timed out after {self._navigation_timeout_s:g}s") from None
        return NavigationOutcome(url=url, status=self._document_status.get(result.get("loaderId", "")))

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

    def __init__(self, *, cdp_url: str | None, headless: bool, navigation_timeout_s: float) -> None:
        self._cdp_url = cdp_url
        self._headless = headless
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
    async def session(self) -> AsyncGenerator[BrowserSession]:
        browser = await self._ensure_browser()
        try:
            context = await browser.new_context()
        except PlaywrightError as exc:
            raise BrowserError(f"Could not open a browser context: {exc.message}") from exc
        self.open_contexts += 1
        try:
            yield await BrowserSession.open(context, navigation_timeout_s=self._navigation_timeout_s)
        finally:
            self.open_contexts -= 1
            try:
                await context.close()
            except PlaywrightError:
                logger.warning("Closing a browser context failed", exc_info=True)

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
                        headless=self._headless, args=["--disable-dev-shm-usage"]
                    )
            except PlaywrightError as exc:
                raise BrowserError(f"Could not start the browser: {exc.message}") from exc
            return self._browser
