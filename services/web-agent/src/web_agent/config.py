from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WEB_AGENT_", extra="ignore")

    # Shared with the other internal services, so no WEB_AGENT_ prefix. Optional here only so the
    # CLI runs without it; the HTTP app refuses to start when it's missing.
    internal_proxy_token: str | None = Field(default=None, validation_alias="INTERNAL_PROXY_TOKEN")

    # Attach to an already running Chrome (e.g. one started with --remote-debugging-port=9222)
    # instead of launching a bundled Chromium.
    cdp_url: str | None = None
    # "duckduckgo" (its HTML frontend; blocks headless browsers) or "searxng" (self-hosted, needs
    # searxng_url).
    engine: str = "duckduckgo"
    searxng_url: str | None = None
    # Egress proxy (compose: the allowlisting `egress-proxy`) every browser request goes through,
    # except hosts in `proxy_bypass` (Chromium's bypass-list syntax). None = direct, e.g. the CLI.
    proxy_url: str | None = None
    proxy_bypass: str | None = None
    headless: bool = True
    navigation_timeout_s: float = 15
    search_timeout_s: float = 25
    max_concurrent_searches: int = 2
    # Courtesy spacing between queries to the same engine -- not a way of looking human.
    min_interval_s: float = 1.0
    default_region: str = "pl-pl"

    # `site_search` runs in its own instance (compose: `site-agent`), attached to a headful Chrome.
    # `sites_enabled` serves /v1/site-search and /v1/sites; `sites_only` also switches /v1/search
    # off, so web searches never reach that browser. The `web-agent` instance keeps both off: sites
    # never reach the headless browser.
    sites_enabled: bool = False
    sites_only: bool = False
    # With `cdp_url`: a new tab in the browser's own (default) context per search instead of a
    # fresh context, so a bot-check cookie a person earned in that profile carries over.
    reuse_default_context: bool = False

    @property
    def serves_sites(self) -> bool:
        return self.sites_enabled or self.sites_only


settings = Settings()
