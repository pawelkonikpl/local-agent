from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    environment: str = "development"
    session_cookie_name: str = "la_session"
    session_ttl_hours: int = 24 * 7

    llm_proxy_url: str
    internal_proxy_token: str
    chat_model: str = "claude-sonnet-5"
    # Models a user may pick per message; llm-proxy routes each by name (see its `_resolve_backend`).
    # `chat_model` is the default and is always offered, even if missing here.
    chat_models: list[str] = ["claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5-20251001", "local-model"]
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

    @property
    def available_chat_models(self) -> list[str]:
        return [self.chat_model, *(model for model in self.chat_models if model != self.chat_model)]

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"


settings = Settings()
