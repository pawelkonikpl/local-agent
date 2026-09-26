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
    # `chat_model` is the default and is always offered, even if missing here. The `gpt-*` ones
    # need OPENAI_API_KEY set on llm-proxy (otherwise it answers 503); override with CHAT_MODELS.
    chat_models: list[str] = [
        "claude-sonnet-5",
        "claude-opus-5-5",
        "claude-haiku-4-5-20251001",
        "gpt-6-sol",
        "gpt-6-astra",
        "gpt-6-luna",
        "local-model",
    ]
    # Reasoning levels a user may pick per model, in Anthropic's `output_config.effort` vocabulary
    # (llm-proxy maps it onto OpenAI's `reasoning.effort`). A model missing here offers no choice
    # and runs at its provider's default. Only list levels the provider accepts for that model --
    # Haiku 4.5 takes a token budget rather than an effort level, so it has none. Override with
    # CHAT_MODEL_REASONING_EFFORTS (JSON).
    chat_model_reasoning_efforts: dict[str, list[str]] = {
        "claude-sonnet-5": ["low", "medium", "high", "xhigh", "max"],
        "claude-opus-5-5": ["low", "medium", "high", "xhigh", "max"],
        "gpt-6-sol": ["low", "medium", "high"],
        "gpt-6-astra": ["low", "medium", "high"],
        "gpt-6-luna": ["low", "medium", "high"],
    }
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

    def reasoning_efforts_for(self, model: str) -> list[str]:
        return self.chat_model_reasoning_efforts.get(model, [])

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"


settings = Settings()
