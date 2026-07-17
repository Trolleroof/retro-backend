import pytest
from pydantic import SecretStr, ValidationError

from retro_backend.config import Settings
from retro_backend.kafka_config import kafka_client_options


def test_kafka_send_timeout_must_be_shorter_than_publication_lease() -> None:
    with pytest.raises(ValidationError, match="kafka_send_timeout_seconds"):
        Settings(chunk_publishing_ttl_seconds=5, kafka_send_timeout_seconds=5)


def test_sasl_settings_are_validated_as_a_complete_secret_bundle() -> None:
    with pytest.raises(ValidationError, match="SASL"):
        Settings(kafka_security_protocol="SASL_SSL")

    with pytest.raises(ValidationError, match="SASL"):
        Settings(
            kafka_sasl_username=SecretStr("user"),
            kafka_sasl_password=SecretStr("secret"),
        )


def test_shared_kafka_options_unwrap_secrets_for_clients_only() -> None:
    settings = Settings(
        kafka_bootstrap_servers="broker:9093",
        kafka_security_protocol="SASL_PLAINTEXT",
        kafka_sasl_mechanism="SCRAM-SHA-256",
        kafka_sasl_username=SecretStr("retro-user"),
        kafka_sasl_password=SecretStr("do-not-log"),
    )

    options = kafka_client_options(settings)

    assert options == {
        "bootstrap_servers": "broker:9093",
        "security_protocol": "SASL_PLAINTEXT",
        "sasl_mechanism": "SCRAM-SHA-256",
        "sasl_plain_username": "retro-user",
        "sasl_plain_password": "do-not-log",
    }
    assert "do-not-log" not in repr(settings)


def test_empty_optional_kafka_security_environment_values_are_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETRO_KAFKA_SASL_MECHANISM", "")
    monkeypatch.setenv("RETRO_KAFKA_SASL_USERNAME", "")
    monkeypatch.setenv("RETRO_KAFKA_SASL_PASSWORD", "")
    monkeypatch.setenv("RETRO_KAFKA_SSL_CAFILE", "")

    settings = Settings()

    assert settings.kafka_sasl_mechanism is None
    assert settings.kafka_sasl_username is None
    assert settings.kafka_ssl_cafile is None
