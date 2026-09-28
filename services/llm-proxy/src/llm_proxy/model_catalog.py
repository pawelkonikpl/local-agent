import asyncio
import logging
import time
from collections.abc import Sequence

from llm_proxy.backends.base import LLMBackend, ModelInfo, UpstreamError

logger = logging.getLogger(__name__)


class ModelCatalog:
    """The models every configured provider serves, asked of the providers themselves (each
    backend's `list_models`) and reused for `ttl_s` so the chat's model picker doesn't cost an
    upstream round trip per request.

    A provider that can't be asked is left out (and logged) rather than failing the whole list;
    such a partial answer isn't cached, so the next request asks again.
    """

    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._models: list[ModelInfo] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def models(self, backends: Sequence[LLMBackend]) -> list[ModelInfo]:
        async with self._lock:
            if self._models is not None and time.monotonic() - self._fetched_at < self._ttl_s:
                return self._models
            results = await asyncio.gather(
                *(backend.list_models() for backend in backends), return_exceptions=True
            )
            models: list[ModelInfo] = []
            complete = True
            for backend, result in zip(backends, results, strict=True):
                if isinstance(result, UpstreamError):
                    logger.warning("Listing models of %s failed: %s", type(backend).__name__, result)
                    complete = False
                elif isinstance(result, BaseException):
                    raise result
                else:
                    models.extend(result)
            if complete:
                self._models, self._fetched_at = models, time.monotonic()
            return models
