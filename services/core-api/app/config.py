from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables (see .env.example).

    Field names map to env vars case-insensitively, so `database_url`
    is read from DATABASE_URL.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://studygraph:studygraph@db:5432/studygraph"
    redis_url: str = "redis://redis:6379/0"
    ai_service_url: str = "http://ai-service:8000"
    environment: str = "development"


settings = Settings()
