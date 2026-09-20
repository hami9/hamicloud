from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "HamiCloud Control API"
    VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/v1"
    # Environment mode: development | staging | production (fails closed to production when unset)
    ENVIRONMENT: str = Field(default="production")

    # PostgreSQL
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
    )
    DATABASE_URL_SYNC: str = Field(
        default="postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
    )

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6380/0")

    # NATS JetStream
    NATS_URL: str = Field(default="nats://localhost:4222")

    # CORS & Origin Security
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # Secret Encryption (32-byte url-safe base64 key for AES-GCM)
    SECRET_ENCRYPTION_KEY: str = Field(
        default="MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
    )

    # Admission Defaults
    DEFAULT_JOB_TIMEOUT_SECONDS: int = 600
    DEFAULT_BUILD_TIMEOUT_SECONDS: int = 900
    DEFAULT_WORKSPACE_JOB_CONCURRENCY: int = 2
    DEFAULT_WORKSPACE_SERVICE_CAP: int = 2


settings = Settings()
