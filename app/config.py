from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agentic:agentic@localhost:5432/agentic"
    redis_url: str = "redis://localhost:6379/0"
    ollama_base_url: str = "http://localhost:11434"

    jwt_secret: str = "dev-secret-change-me-0123456789abcdef"
    jwt_ttl_minutes: int = 60

    model_tier: str = ""  # override auto-detection, e.g. "cpu"
    chat_model: str = ""  # override tier's chat model
    price_per_mtoken: float = 0.0

    approval_ttl_hours: int = 24

    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    otel_exporter_otlp_endpoint: str = ""


settings = Settings()
