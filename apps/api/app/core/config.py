import os
from typing import List, Optional
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

    # NATS JetStream & Outbox Dispatcher
    NATS_URL: str = Field(default="nats://localhost:4222")
    NATS_STREAM_NAME: str = Field(default="HAMICLOUD_EVENTS")
    OUTBOX_DISPATCHER_ENABLED: bool = Field(default=True)
    OUTBOX_POLL_INTERVAL_SECONDS: float = Field(default=1.0)
    OUTBOX_BATCH_SIZE: int = Field(default=100)
    OUTBOX_MAX_RETRIES: int = Field(default=5)

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

    # Approved Image Allowlist Policy (Decision D13, ADR-0004 §8)
    APPROVED_IMAGE_REGISTRIES: List[str] = Field(
        default_factory=lambda: [
            "registry.example.com/",
            "ghcr.io/hami9/",
            "docker.io/library/",
            "quay.io/",
            "gcr.io/",
            "sha256:",
        ]
    )

    # Approved Repository Allowlist Policy (Roadmap: Source builds allowlist)
    APPROVED_REPOSITORY_HOSTS: List[str] = Field(
        default_factory=lambda: [
            "github.com",
            "gitlab.com",
            "bitbucket.org",
        ]
    )

    # OIDC Authentication (Keycloak / standard OIDC)
    OIDC_ENABLED: bool = Field(default=True)
    OIDC_ISSUER_URL: str = Field(
        default="http://localhost:8080/realms/hamicloud"
    )
    OIDC_JWKS_URL: Optional[str] = Field(default=None)
    OIDC_AUDIENCE: Optional[str] = Field(default=None)
    OIDC_ALGORITHMS: List[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    OIDC_JWKS_CACHE_TTL: int = Field(default=3600)

    # Artifact storage directory
    ARTIFACTS_DIR: Optional[str] = Field(default=None)

    @property
    def effective_jwks_url(self) -> str:
        if self.OIDC_JWKS_URL:
            return self.OIDC_JWKS_URL
        return f"{self.OIDC_ISSUER_URL.rstrip('/')}/protocol/openid-connect/certs"

    @property
    def effective_artifacts_dir(self) -> str:
        if self.ARTIFACTS_DIR:
            return os.path.abspath(self.ARTIFACTS_DIR)
        if self.ENVIRONMENT == "development":
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            return os.path.join(repo_root, "var", "artifacts")
        return ""

    def validate_runtime_environment(self) -> None:
        """Validate production security requirements. Refuses to start if unconfigured."""
        if self.ENVIRONMENT != "development":
            if not self.OIDC_AUDIENCE:
                raise RuntimeError(
                    "OIDC_AUDIENCE is required when ENVIRONMENT is not development"
                )
            if not self.effective_artifacts_dir:
                raise RuntimeError(
                    "ARTIFACTS_DIR is required when ENVIRONMENT is not development"
                )


settings = Settings()
