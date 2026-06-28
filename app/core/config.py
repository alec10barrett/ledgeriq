"""
app/core/config.py

All runtime configuration, read from environment variables or a .env file.
Access settings anywhere via:

    from app.core.config import settings
"""

from functools import lru_cache

from pydantic import AnyUrl, Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",         # silently ignore unknown env vars
    )

    # ------------------------------------------------------------------
    # App
    # ------------------------------------------------------------------
    app_name: str = "AssetAI"
    environment: str = Field(default="development")   # development | staging | production
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")

    # ------------------------------------------------------------------
    # Database (PostgreSQL)
    # ------------------------------------------------------------------
    database_url: PostgresDsn = Field(
        ...,
        description="Full PostgreSQL connection string, e.g. postgresql+asyncpg://user:pass@host/db",
    )

    # Connection pool settings — sensible defaults for a small app.
    db_pool_size: int = Field(default=5)
    db_max_overflow: int = Field(default=10)
    db_pool_timeout: int = Field(default=30)   # seconds

    # ------------------------------------------------------------------
    # AWS
    # ------------------------------------------------------------------
    aws_region: str = Field(default="us-east-1")
    aws_access_key_id: str = Field(...)
    aws_secret_access_key: str = Field(...)
    s3_bucket: str = Field(
        ...,
        description="S3 bucket name for document storage (no s3:// prefix)",
    )

    # ------------------------------------------------------------------
    # Textract
    # ------------------------------------------------------------------
    # Textract is regional; usually the same as aws_region but can differ.
    textract_region: str | None = Field(default=None)

    @field_validator("textract_region", mode="before")
    @classmethod
    def default_textract_region(cls, v: str | None, info) -> str:
        """Fall back to aws_region if textract_region isn't set."""
        if v:
            return v
        # info.data contains already-validated fields
        return info.data.get("aws_region", "us-east-1")

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    secret_key: str = Field(
        ...,
        description="Secret key for signing tokens. Generate with: openssl rand -hex 32",
    )
    access_token_expire_minutes: int = Field(default=60 * 24)  # 24 hours

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    # Comma-separated list of allowed origins, e.g. "http://localhost:3000,https://app.example.com"
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def effective_textract_region(self) -> str:
        return self.textract_region or self.aws_region


@lru_cache
def get_settings() -> Settings:
    """
    Return a cached Settings instance.
    Use this as a FastAPI dependency:

        from app.core.config import get_settings
        from fastapi import Depends

        def my_route(settings: Settings = Depends(get_settings)):
            ...

    Or import the module-level singleton for non-route code:

        from app.core.config import settings
    """
    return Settings()


# Module-level singleton — fine for services and non-test code.
settings = get_settings()
