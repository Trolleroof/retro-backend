"""Environment-backed service configuration."""

from functools import lru_cache
from typing import ClassVar, Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="RETRO_",
        env_file=".env",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://retro:retro@127.0.0.1:5432/retro"
    redis_url: str | None = "redis://127.0.0.1:6379/0"
    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    kafka_topic: str = "retro-game-events"
    max_request_bytes: int = 256 * 1024
    max_game_result_bytes: int = 16 * 1024
    chunk_publishing_ttl_seconds: int = 30
    chunk_dedup_ttl_seconds: int = 48 * 60 * 60
    kafka_send_timeout_seconds: float = 20.0
    readiness_timeout_seconds: float = 2.0
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_timeouts(self) -> Self:
        if self.kafka_send_timeout_seconds >= self.chunk_publishing_ttl_seconds:
            raise ValueError(
                "kafka_send_timeout_seconds must be shorter than chunk_publishing_ttl_seconds"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
