"""Render and validate the account-specific production Worker configuration."""

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

PRODUCTION_HOST = "api.runretroarcade.com"
PLACEHOLDER_DATABASE_ID = "00000000-0000-0000-0000-000000000000"
DATABASE_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    flags=re.IGNORECASE,
)
EXPECTED_LIFECYCLE = {
    "Rules": [
        {
            "ID": "transition-retro-artifacts-to-infrequent-access",
            "Status": "Enabled",
            "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}],
        }
    ]
}


class DeploymentConfigError(RuntimeError):
    """Raised when a release configuration is incomplete or unsafe."""


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_mapping(item) for item in value if isinstance(item, Mapping)]


def _load_toml(path: Path) -> Mapping[str, object]:
    return cast(Mapping[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _database_id(config: Mapping[str, object]) -> object:
    databases = _mapping_list(config.get("d1_databases"))
    if len(databases) != 1:
        return None
    database = databases[0]
    if database.get("binding") != "DB" or database.get("database_name") != "retro-backend":
        return None
    return database.get("database_id")


def _consumer_for(
    consumers: list[Mapping[str, object]],
    queue_name: str,
) -> Mapping[str, object] | None:
    return next(
        (consumer for consumer in consumers if consumer.get("queue") == queue_name),
        None,
    )


def deployment_errors(
    config: Mapping[str, object],
    lifecycle: object,
) -> list[str]:
    """Return every actionable production configuration error."""
    errors: list[str] = []

    if config.get("workers_dev") is not False:
        errors.append("workers_dev must be false for the production-only Worker")
    expected_routes = [{"pattern": PRODUCTION_HOST, "custom_domain": True}]
    if config.get("routes") != expected_routes:
        errors.append(f"custom domain must be exactly {PRODUCTION_HOST}")

    observability = _mapping(config.get("observability"))
    if observability.get("enabled") is not True:
        errors.append("Worker observability must be enabled")
    sampling_rate = observability.get("head_sampling_rate")
    if (
        isinstance(sampling_rate, bool)
        or not isinstance(sampling_rate, (int, float))
        or not 0 < sampling_rate <= 1
    ):
        errors.append("observability head_sampling_rate must be explicitly between 0 and 1")

    variables = _mapping(config.get("vars"))
    if variables.get("ENVIRONMENT") != "production":
        errors.append("ENVIRONMENT must be 'production'")

    database_id = _database_id(config)
    if database_id is None:
        errors.append("exactly one D1 DB binding for retro-backend is required")
    elif database_id == PLACEHOLDER_DATABASE_ID:
        errors.append("D1 database_id is still the all-zero placeholder")
    elif not isinstance(database_id, str) or DATABASE_ID_PATTERN.fullmatch(database_id) is None:
        errors.append("D1 database_id must be a valid UUID")

    buckets = _mapping_list(config.get("r2_buckets"))
    if not any(
        bucket.get("binding") == "RAW_BUCKET"
        and bucket.get("bucket_name") == "retro-gameplay-raw"
        for bucket in buckets
    ):
        errors.append("RAW_BUCKET must bind retro-gameplay-raw")

    queues = _mapping(config.get("queues"))
    producers = _mapping_list(queues.get("producers"))
    if not any(
        producer.get("binding") == "GAMEPLAY_QUEUE"
        and producer.get("queue") == "retro-gameplay-events"
        for producer in producers
    ):
        errors.append("GAMEPLAY_QUEUE must produce to retro-gameplay-events")

    consumers = _mapping_list(queues.get("consumers"))
    gameplay_consumer = _consumer_for(consumers, "retro-gameplay-events")
    if (
        gameplay_consumer is None
        or gameplay_consumer.get("dead_letter_queue")
        != "retro-gameplay-events-dlq"
    ):
        errors.append("gameplay consumer must dead-letter to retro-gameplay-events-dlq")
    dlq_consumer = _consumer_for(consumers, "retro-gameplay-events-dlq")
    if dlq_consumer is None:
        errors.append("retro-gameplay-events-dlq must have an active archival consumer")
    elif (
        dlq_consumer.get("dead_letter_queue")
        != "retro-gameplay-events-archive-dlq"
    ):
        errors.append(
            "DLQ archival failures must dead-letter to retro-gameplay-events-archive-dlq"
        )

    if lifecycle != EXPECTED_LIFECYCLE:
        errors.append(
            "R2 lifecycle must transition retained artifacts to STANDARD_IA after 30 days"
        )
    return errors


def _validate_or_raise(
    config: Mapping[str, object],
    lifecycle: object,
) -> None:
    errors = deployment_errors(config, lifecycle)
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise DeploymentConfigError(f"deployment preflight failed:\n{details}")


def _render_config(
    source_path: Path,
    output_path: Path,
    lifecycle: object,
) -> Mapping[str, object]:
    database_id = os.environ.get("CLOUDFLARE_D1_DATABASE_ID", "").strip()
    if not database_id:
        raise DeploymentConfigError("CLOUDFLARE_D1_DATABASE_ID is required")
    if (
        database_id == PLACEHOLDER_DATABASE_ID
        or DATABASE_ID_PATTERN.fullmatch(database_id) is None
    ):
        raise DeploymentConfigError(
            "CLOUDFLARE_D1_DATABASE_ID must be a non-zero UUID"
        )

    source = source_path.read_text(encoding="utf-8")
    if source.count(PLACEHOLDER_DATABASE_ID) != 1:
        raise DeploymentConfigError(
            "source configuration must contain exactly one all-zero D1 placeholder"
        )
    rendered_source = source.replace(PLACEHOLDER_DATABASE_ID, database_id)
    rendered = cast(Mapping[str, object], tomllib.loads(rendered_source))
    _validate_or_raise(rendered, lifecycle)

    output_path.write_text(rendered_source, encoding="utf-8")
    output_path.chmod(0o600)
    return rendered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("wrangler.toml"))
    parser.add_argument(
        "--lifecycle",
        type=Path,
        default=Path("cloudflare/r2-lifecycle.json"),
    )
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("wrangler.production.toml"))
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        lifecycle = _load_json(arguments.lifecycle)
        if arguments.render:
            _render_config(arguments.config, arguments.output, lifecycle)
        else:
            _validate_or_raise(_load_toml(arguments.config), lifecycle)
    except (DeploymentConfigError, OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Deployment configuration is ready for {PRODUCTION_HOST}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
