from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    internal_proxy_token: str
    model_id: str = "Qwen/Qwen2.5-0.5B-Instruct"
    max_new_tokens_cap: int = 512


settings = Settings()
