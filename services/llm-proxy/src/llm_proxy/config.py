from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str
    internal_proxy_token: str
    anthropic_base_url: str = "https://api.anthropic.com"
    # Additional providers are opt-in. Requests are routed by the `model` field of each
    # request: "claude-*" -> Anthropic (always available, above), "local-model" ->
    # local_model_base_url, anything else -> real OpenAI via openai_api_key.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    local_model_base_url: str | None = None
    local_model_id: str = "Qwen/Qwen2.5-0.5B-Instruct"
    environment: str = "development"


settings = Settings()
