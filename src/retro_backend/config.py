"""Environment-backed service configuration."""

from functools import lru_cache
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator
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
    kafka_consumer_group_id: str = "retro-parquet-writer-v1"
    kafka_dlq_topic: str = "retro-game-events-dlq"
    consumer_batch_max_records: int = Field(default=100, gt=0)
    consumer_batch_max_rows: int = Field(default=5_000, gt=0)
    consumer_batch_max_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    consumer_flush_interval_seconds: float = Field(default=10.0, gt=0)
    consumer_poll_timeout_ms: int = Field(default=1_000, gt=0)
    consumer_poll_max_records: int = Field(default=500, gt=0)
    consumer_retry_initial_seconds: float = Field(default=0.25, gt=0)
    consumer_retry_max_seconds: float = Field(default=10.0, gt=0)
    consumer_shutdown_retry_attempts: int = Field(default=3, gt=0)
    consumer_metrics_port: int = Field(default=9100, ge=1, le=65535)
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "retro-game-events"
    s3_raw_prefix: str = "raw"
    s3_compacted_prefix: str = "compacted"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_timeouts(self) -> Self:
        if self.kafka_send_timeout_seconds >= self.chunk_publishing_ttl_seconds:
            raise ValueError(
                "kafka_send_timeout_seconds must be shorter than chunk_publishing_ttl_seconds"
            )
        if self.consumer_retry_max_seconds < self.consumer_retry_initial_seconds:
            raise ValueError(
                "consumer_retry_max_seconds must be greater than or equal to consumer_retry_initial_seconds"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
