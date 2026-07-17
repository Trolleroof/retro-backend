"""Shared, secret-safe aiokafka transport configuration."""

from __future__ import annotations

import ssl
from typing import Any

from .config import Settings


def kafka_client_options(settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {
        "bootstrap_servers": settings.kafka_bootstrap_servers,
        "security_protocol": settings.kafka_security_protocol,
    }
    if settings.kafka_security_protocol.startswith("SASL_"):
        assert settings.kafka_sasl_mechanism is not None
        assert settings.kafka_sasl_username is not None
        assert settings.kafka_sasl_password is not None
        options.update(
            {
                "sasl_mechanism": settings.kafka_sasl_mechanism,
                "sasl_plain_username": settings.kafka_sasl_username.get_secret_value(),
                "sasl_plain_password": settings.kafka_sasl_password.get_secret_value(),
            }
        )
    if settings.kafka_security_protocol in {"SSL", "SASL_SSL"}:
        context = ssl.create_default_context(cafile=settings.kafka_ssl_cafile)
        if settings.kafka_ssl_certfile is not None:
            context.load_cert_chain(
                certfile=settings.kafka_ssl_certfile,
                keyfile=settings.kafka_ssl_keyfile,
                password=(
                    settings.kafka_ssl_password.get_secret_value()
                    if settings.kafka_ssl_password is not None
                    else None
                ),
            )
        options["ssl_context"] = context
    return options
