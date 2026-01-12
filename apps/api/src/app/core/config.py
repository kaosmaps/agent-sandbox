"""Configuration settings for Agent Sandbox API."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Security
    WEBHOOK_SECRET: str = ""

    # Docker
    DOCKER_NETWORK: str = "sandbox-network"
    CONTAINER_PREFIX: str = "sandbox"

    # Domain
    SANDBOX_DOMAIN: str = "sandbox.nanoswarm.kaosmaps.com"

    # CORS
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://nanoswarm.kaosmaps.com",
    ]

    # GitHub Container Registry
    GHCR_TOKEN: str = ""


settings = Settings()
