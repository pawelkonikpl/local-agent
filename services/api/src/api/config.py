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
    chat_max_tokens: int = 4096

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production"


settings = Settings()
