"""Environment-backed service configuration."""

from functools import lru_cache
from typing import ClassVar, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
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
    kafka_security_protocol: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = (
        "PLAINTEXT"
    )
    kafka_sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] | None = None
    kafka_sasl_username: SecretStr | None = None
    kafka_sasl_password: SecretStr | None = None
    kafka_ssl_cafile: str | None = None
    kafka_ssl_certfile: str | None = None
    kafka_ssl_keyfile: str | None = None
    kafka_ssl_password: SecretStr | None = None
    max_request_bytes: int = 256 * 1024
    max_game_result_bytes: int = 16 * 1024
    chunk_publishing_ttl_seconds: int = 30
    chunk_dedup_ttl_seconds: int = 48 * 60 * 60
    kafka_send_timeout_seconds: float = 20.0
    readiness_timeout_seconds: float = 2.0
    kafka_consumer_group_id: str = "retro-parquet-writer-v1"
    kafka_dlq_topic: str = "retro-game-events-dlq"
    consumer_flush_interval_seconds: float = Field(default=10.0, gt=0)
    consumer_poll_timeout_ms: int = Field(default=1_000, gt=0)
    consumer_poll_max_records: int = Field(default=500, gt=0)
    consumer_retry_initial_seconds: float = Field(default=0.25, gt=0)
    consumer_retry_max_seconds: float = Field(default=10.0, gt=0)
    consumer_shutdown_retry_attempts: int = Field(default=3, gt=0)
    consumer_metrics_port: int = Field(default=9100, ge=1, le=65535)
    consumer_health_stale_seconds: float = Field(default=30.0, gt=0)
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "retro-game-events"
    s3_raw_prefix: str = "raw"
    s3_compacted_prefix: str = "compacted"
    compaction_read_batch_rows: int = Field(default=1_000, gt=0)
    compaction_part_max_rows: int = Field(default=500, gt=0)
    log_level: str = "INFO"

    @field_validator(
        "kafka_sasl_mechanism",
        "kafka_sasl_username",
        "kafka_sasl_password",
        "kafka_ssl_cafile",
        "kafka_ssl_certfile",
        "kafka_ssl_keyfile",
        "kafka_ssl_password",
        mode="before",
    )
    @classmethod
    def empty_kafka_security_value_is_unset(cls, value: object) -> object:
        return None if value == "" else value

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
        sasl_values = (
            self.kafka_sasl_mechanism,
            self.kafka_sasl_username,
            self.kafka_sasl_password,
        )
        if self.kafka_security_protocol.startswith("SASL_"):
            if any(value is None for value in sasl_values):
                raise ValueError("SASL protocol requires mechanism, username, and password")
        elif any(value is not None for value in sasl_values):
            raise ValueError("SASL credentials require a SASL security protocol")
        if (self.kafka_ssl_certfile is None) != (self.kafka_ssl_keyfile is None):
            raise ValueError("Kafka SSL certificate and key files must be configured together")
        ssl_values = (
            self.kafka_ssl_cafile,
            self.kafka_ssl_certfile,
            self.kafka_ssl_keyfile,
            self.kafka_ssl_password,
        )
        if self.kafka_security_protocol not in {"SSL", "SASL_SSL"} and any(
            value is not None for value in ssl_values
        ):
            raise ValueError("Kafka SSL settings require an SSL security protocol")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
