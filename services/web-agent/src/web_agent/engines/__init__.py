from web_agent.engines.base import SearchEngine
from web_agent.engines.duckduckgo import DuckDuckGoEngine
from web_agent.engines.searxng import SearxngEngine

ENGINE_NAMES = (DuckDuckGoEngine.name, SearxngEngine.name)


def get_engine(name: str = DuckDuckGoEngine.name, *, searxng_url: str | None = None) -> SearchEngine:
    if name == DuckDuckGoEngine.name:
        return DuckDuckGoEngine()
    if name == SearxngEngine.name:
        if not searxng_url:
            raise ValueError("The searxng engine needs a SearXNG URL (WEB_AGENT_SEARXNG_URL)")
        return SearxngEngine(searxng_url)
    raise ValueError(f"Unknown search engine: {name}")


__all__ = ["ENGINE_NAMES", "SearchEngine", "get_engine"]
