from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str
    internal_proxy_token: str
    anthropic_base_url: str = "https://api.anthropic.com"
    # OpenAI is opt-in. Requests are routed by the `model` field of each request: "claude-*"
    # -> Anthropic (always available, above), anything else -> real OpenAI via openai_api_key.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    # OpenAI's model list has no capability data, so these two decide what of it is offered:
    # ids matching the pattern (whole id; the default takes gpt-5 and later, e.g. gpt-6-sol, and
    # skips dated snapshots, older generations and gpt-image-style ids), each with these efforts.
    # Override with OPENAI_MODELS_PATTERN / OPENAI_REASONING_EFFORTS (JSON).
    openai_models_pattern: str = r"gpt-[5-9](\.\d+)?(-[a-z]+)?"
    openai_reasoning_efforts: list[str] = ["low", "medium", "high"]
    # How long `GET /v1/models` reuses the providers' answer before asking them again.
    models_cache_ttl_s: float = 600
    environment: str = "development"


settings = Settings()
