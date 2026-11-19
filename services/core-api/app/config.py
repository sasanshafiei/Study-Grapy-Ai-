from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


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

    # Root of the uploaded-file store. docker-compose mounts the persistent
    # `uploads` volume here; nothing creates or touches it until the first upload.
    upload_dir: Path = Path("/data/uploads")
    # Largest accepted upload, in bytes (25 MiB). Lecture PDFs are normally far
    # below this; raise it for image-heavy slide exports.
    max_upload_bytes: int = Field(default=25 * MIB, gt=0, le=1024 * MIB)


settings = Settings()
