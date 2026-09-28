from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    environment: str = "development"
    session_cookie_name: str = "la_session"
    session_ttl_hours: int = 24 * 7

    llm_proxy_url: str
    internal_proxy_token: str
    # The default of the chat's model picker, when llm-proxy's providers list it. The models on
    # offer, and the reasoning levels of each, come from the providers (llm-proxy `GET /v1/models`).
    chat_model: str = "claude-sonnet-5"
    chat_max_tokens: int = 4096

    chat_tools_enabled: bool = True
    # Each iteration is a full model call, so this is mostly a cost valve.
    chat_max_tool_iterations: int = 10
    tool_timeout_s: float = 30
    tool_output_max_chars: int = 16000
    # The web-agent service behind `web_search`; unset = the tool isn't offered. Its own search
    # timeout (25s) sits below this, which sits below `tool_timeout_s`.
    web_agent_url: str | None = None
    web_search_timeout_s: float = 28
    # The web-agent instance behind `site_search` (compose: `site-agent`, a headful Chrome in a
    # container); unset, or no sites, = the tool isn't offered. Sites must be names web-agent knows
    # (its `/v1/sites`); override with WEB_AGENT_SITES (JSON).
    web_agent_sites_url: str | None = None
    web_agent_sites: list[str] = ["allegro.pl"]
    # Where the user sees that browser's screen (compose: `site-vnc`) to solve a bot check.
    site_browser_view_url: str = "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"


settings = Settings()
