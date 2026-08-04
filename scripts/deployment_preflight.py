"""Render and validate the account-specific production Worker configuration."""

import argparse
import ipaddress
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import idna

PRODUCTION_HOST = "api.runretroarcade.com"
PLACEHOLDER_DATABASE_ID = "00000000-0000-0000-0000-000000000000"
PLACEHOLDER_PUBLIC_GAME_URL = "https://gamepigeon.example"
PLACEHOLDER_ALLOWED_GAME_ORIGINS = "https://gamepigeon.example"
DATABASE_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    flags=re.IGNORECASE,
)
HOST_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
WHATWG_IPV4_NUMBER_PATTERN = re.compile(r"^(?:[0-9]+|0x[0-9a-f]+)$")
INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9a-fA-F]{2})")
PATH_SEGMENT_SAFE = "-._~!$&'()*+,;=:@"
RESERVED_DOCUMENTATION_ROOTS = ("example.com", "example.net", "example.org")
PUBLIC_GAME_URL_ERROR = (
    "PUBLIC_GAME_URL must be a canonical credential-free HTTP(S) URL"
)
ALLOWED_GAME_ORIGINS_ERROR = (
    "ALLOWED_GAME_ORIGINS must be a nonempty comma-separated list of unique "
    "canonical HTTP(S) origins"
)
PUBLIC_GAME_URL_RESERVED_HOST_ERROR = (
    "PUBLIC_GAME_URL must not use an inert or reserved hostname"
)
ALLOWED_GAME_ORIGINS_RESERVED_HOST_ERROR = (
    "ALLOWED_GAME_ORIGINS must not use an inert or reserved hostname"
)
LIFECYCLE_ERROR = (
    "R2 lifecycle must expire derived artifacts at 7 days and raw chunks at 90"
)
# Durable Objects whose binding name and sqlite export the deploy config must
# declare exactly. EPISODE_ASSEMBLER gates episode assembly (#29): without it
# GameplayConsumer silently skips assembly instead of failing.
REQUIRED_DURABLE_OBJECTS = (
    ("GAME_ROOMS", "GameRoom"),
    ("EPISODE_ASSEMBLER", "EpisodeAssembler"),
)
DAY_SECONDS = 86_400


def _age(days: int) -> dict[str, object]:
    return {"maxAge": days * DAY_SECONDS, "type": "Age"}


def _expire_after(rule_id: str, prefix: str, days: int) -> dict[str, object]:
    return {
        "id": rule_id,
        "enabled": True,
        "conditions": {"prefix": prefix},
        "deleteObjectsTransition": {"condition": _age(days)},
    }


# Wrangler's `r2 bucket lifecycle set --file` requires the Cloudflare-native
# shape: a lowercase "rules" array whose conditions are ages in seconds. The
# S3-style {"Rules": [{"Days": ...}]} spelling is rejected outright.
EXPECTED_LIFECYCLE = {
    "rules": [
        {
            "id": "raw-chunks-infrequent-access-then-expire",
            "enabled": True,
            "conditions": {"prefix": "raw/"},
            "storageClassTransitions": [
                {"condition": _age(30), "storageClass": "InfrequentAccess"}
            ],
            "deleteObjectsTransition": {"condition": _age(90)},
        },
        _expire_after("validated-artifacts-expire", "validated/", 7),
        _expire_after("rejected-artifacts-expire", "rejected/", 7),
        _expire_after("dlq-archives-expire", "dlq/", 7),
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


def _has_forbidden_url_character(value: str) -> bool:
    return "\\" in value or any(
        character.isspace()
        or ord(character) < 32
        or 127 <= ord(character) <= 159
        for character in value
    )


def _serialize_ipv6(address: ipaddress.IPv6Address) -> str:
    hextets = [
        int.from_bytes(address.packed[index : index + 2], "big")
        for index in range(0, len(address.packed), 2)
    ]
    best_start = -1
    best_length = 0
    index = 0
    while index < len(hextets):
        if hextets[index] != 0:
            index += 1
            continue
        run_start = index
        while index < len(hextets) and hextets[index] == 0:
            index += 1
        run_length = index - run_start
        if run_length >= 2 and run_length > best_length:
            best_start = run_start
            best_length = run_length

    serialized = [format(hextet, "x") for hextet in hextets]
    if best_start < 0:
        return ":".join(serialized)

    left = ":".join(serialized[:best_start])
    right = ":".join(serialized[best_start + best_length :])
    if left and right:
        return f"{left}::{right}"
    if left:
        return f"{left}::"
    if right:
        return f"::{right}"
    return "::"


def _valid_idna_a_label(label: str) -> bool:
    if not label.startswith("xn--"):
        return True
    try:
        decoded_label = idna.decode(label, uts46=True, std3_rules=True)
        encoded_label = idna.encode(
            decoded_label,
            uts46=True,
            std3_rules=True,
            transitional=False,
        ).decode("ascii")
    except idna.IDNAError:
        return False
    return encoded_label == label


def _canonical_host(hostname: str) -> str | None:
    if "%" in hostname:
        return None
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError:
        return None

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if (
            len(hostname) > 253
            or hostname.endswith(".")
            or all(character in "0123456789." for character in hostname)
        ):
            return None
        labels = hostname.split(".")
        if (
            not labels
            or any(HOST_LABEL_PATTERN.fullmatch(label) is None for label in labels)
            or WHATWG_IPV4_NUMBER_PATTERN.fullmatch(labels[-1]) is not None
        ):
            return None
        if any(not _valid_idna_a_label(label) for label in labels):
            return None
        return hostname

    if isinstance(address, ipaddress.IPv6Address):
        return f"[{_serialize_ipv6(address)}]"
    return address.compressed


def _canonical_base_path(path: str) -> str | None:
    if not path:
        return ""
    if not path.startswith("/") or path.endswith("/"):
        return None

    raw_segments = path[1:].split("/")
    if any(not segment for segment in raw_segments):
        return None
    for raw_segment in raw_segments:
        if INVALID_PERCENT_ESCAPE_PATTERN.search(raw_segment) is not None:
            return None
        try:
            decoded_segment = unquote(raw_segment, errors="strict")
        except UnicodeDecodeError:
            return None
        if (
            decoded_segment in {".", ".."}
            or "/" in decoded_segment
            or _has_forbidden_url_character(decoded_segment)
            or quote(decoded_segment, safe=PATH_SEGMENT_SAFE) != raw_segment
        ):
            return None
    return path


def _canonical_http_url(value: object, *, origin_only: bool) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _has_forbidden_url_character(value)
    ):
        return None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        return None

    canonical_host = _canonical_host(hostname)
    if canonical_host is None:
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    canonical_port = port if port is not None and port != default_port else None
    canonical_authority = canonical_host
    if canonical_port is not None:
        canonical_authority = f"{canonical_authority}:{canonical_port}"

    if origin_only:
        if parsed.path:
            return None
        canonical_path = ""
    else:
        canonical_path = _canonical_base_path(parsed.path)
        if canonical_path is None:
            return None

    canonical = urlunsplit(
        (parsed.scheme, canonical_authority, canonical_path, "", "")
    )
    return canonical if canonical == value else None


def _allowed_game_origins_are_valid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    origins = value.split(",")
    if any(not origin for origin in origins):
        return False
    canonical_origins = [
        _canonical_http_url(origin, origin_only=True) for origin in origins
    ]
    return all(origin is not None for origin in canonical_origins) and len(
        set(canonical_origins)
    ) == len(canonical_origins)


def _hostname_is_inert_or_reserved(hostname: str) -> bool:
    return hostname.endswith(".example") or any(
        hostname == root or hostname.endswith(f".{root}")
        for root in RESERVED_DOCUMENTATION_ROOTS
    )


def _url_uses_inert_or_reserved_hostname(value: str) -> bool:
    hostname = urlsplit(value).hostname
    return hostname is not None and _hostname_is_inert_or_reserved(hostname)


def _allowed_game_origins_use_inert_or_reserved_hostname(value: object) -> bool:
    return isinstance(value, str) and any(
        _url_uses_inert_or_reserved_hostname(origin) for origin in value.split(",")
    )


def deployment_errors(
    config: Mapping[str, object],
    lifecycle: object,
    *,
    expected_database_id: str | None = None,
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
    public_game_url = variables.get("PUBLIC_GAME_URL")
    canonical_public_game_url = _canonical_http_url(
        public_game_url,
        origin_only=False,
    )
    if canonical_public_game_url is None:
        errors.append(PUBLIC_GAME_URL_ERROR)
    elif _url_uses_inert_or_reserved_hostname(canonical_public_game_url):
        errors.append(PUBLIC_GAME_URL_RESERVED_HOST_ERROR)
    allowed_game_origins = variables.get("ALLOWED_GAME_ORIGINS")
    if not _allowed_game_origins_are_valid(allowed_game_origins):
        errors.append(ALLOWED_GAME_ORIGINS_ERROR)
    elif _allowed_game_origins_use_inert_or_reserved_hostname(
        allowed_game_origins
    ):
        errors.append(ALLOWED_GAME_ORIGINS_RESERVED_HOST_ERROR)

    database_id = _database_id(config)
    if database_id is None:
        errors.append("exactly one D1 DB binding for retro-backend is required")
    elif database_id == PLACEHOLDER_DATABASE_ID:
        errors.append("D1 database_id is still the all-zero placeholder")
    elif not isinstance(database_id, str) or DATABASE_ID_PATTERN.fullmatch(database_id) is None:
        errors.append("D1 database_id must be a valid UUID")
    if expected_database_id is not None and database_id != expected_database_id:
        errors.append("D1 database_id must match CLOUDFLARE_D1_DATABASE_ID")

    buckets = _mapping_list(config.get("r2_buckets"))
    if not any(
        bucket.get("binding") == "RAW_BUCKET"
        and bucket.get("bucket_name") == "retro-gameplay-raw"
        for bucket in buckets
    ):
        errors.append("RAW_BUCKET must bind retro-gameplay-raw")

    queues = _mapping(config.get("queues"))
    producers = _mapping_list(queues.get("producers"))
    for binding_name, queue_name in (
        ("GAMEPLAY_QUEUE", "retro-gameplay-events"),
        ("EPISODE_QUEUE", "retro-episode-events"),
        ("EPISODE_DLQ", "retro-episode-events-dlq"),
    ):
        if not any(
            producer.get("binding") == binding_name
            and producer.get("queue") == queue_name
            for producer in producers
        ):
            errors.append(f"{binding_name} must produce to {queue_name}")

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
    episode_consumer = _consumer_for(consumers, "retro-episode-events")
    if (
        episode_consumer is None
        or episode_consumer.get("dead_letter_queue") != "retro-episode-events-dlq"
    ):
        errors.append("episode consumer must dead-letter to retro-episode-events-dlq")

    durable_objects = _mapping(config.get("durable_objects"))
    durable_object_bindings = _mapping_list(durable_objects.get("bindings"))
    exports = _mapping(config.get("exports"))
    for name, class_name in REQUIRED_DURABLE_OBJECTS:
        declared = [
            binding
            for binding in durable_object_bindings
            if binding.get("name") == name
        ]
        if declared != [{"name": name, "class_name": class_name}]:
            errors.append(
                f"{name} must bind Durable Object class {class_name} exactly once"
            )
        if _mapping(exports.get(class_name)) != {
            "type": "durable-object",
            "storage": "sqlite",
        }:
            errors.append(
                f"exports.{class_name} must declare a sqlite durable-object"
            )
    if "migrations" in config:
        errors.append(
            "top-level migrations must be absent for the undeployed GameRoom export"
        )

    if lifecycle != EXPECTED_LIFECYCLE:
        errors.append(LIFECYCLE_ERROR)
    return errors


def _validate_or_raise(
    config: Mapping[str, object],
    lifecycle: object,
    *,
    expected_database_id: str | None = None,
) -> None:
    errors = deployment_errors(
        config,
        lifecycle,
        expected_database_id=expected_database_id,
    )
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise DeploymentConfigError(f"deployment preflight failed:\n{details}")


def _replace_template_string_assignment(
    source: str,
    *,
    table_header: str,
    key: str,
    expected: str,
    replacement: str,
    field_name: str,
) -> str:
    lines = source.splitlines(keepends=True)
    table_indices = [
        index for index, line in enumerate(lines) if line.strip() == table_header
    ]
    if len(table_indices) != 1:
        raise DeploymentConfigError(
            f"source {field_name} assignment could not be rendered safely"
        )

    section_start = table_indices[0] + 1
    section_end = next(
        (
            index
            for index in range(section_start, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    assignment_pattern = re.compile(
        "".join(
            (
                rf"^(?P<prefix>[ \t]*{re.escape(key)}[ \t]*=[ \t]*)",
                rf"{re.escape(json.dumps(expected))}",
                r"(?P<suffix>[ \t]*(?:#[^\r\n]*)?(?:\r?\n)?)$",
            )
        )
    )
    assignment_indices = [
        index
        for index in range(section_start, section_end)
        if assignment_pattern.fullmatch(lines[index]) is not None
    ]
    if len(assignment_indices) != 1:
        raise DeploymentConfigError(
            f"source {field_name} assignment could not be rendered safely"
        )

    assignment_index = assignment_indices[0]
    assignment = assignment_pattern.fullmatch(lines[assignment_index])
    if assignment is None:  # pragma: no cover - guarded by assignment_indices
        raise DeploymentConfigError(
            f"source {field_name} assignment could not be rendered safely"
        )
    lines[assignment_index] = (
        f"{assignment.group('prefix')}{json.dumps(replacement)}"
        f"{assignment.group('suffix')}"
    )
    return "".join(lines)


def _render_config(
    source_path: Path,
    output_path: Path,
    lifecycle: object,
) -> Mapping[str, object]:
    source = source_path.read_text(encoding="utf-8")
    source_config = cast(Mapping[str, object], tomllib.loads(source))
    if _database_id(source_config) != PLACEHOLDER_DATABASE_ID:
        raise DeploymentConfigError(
            "source D1 DB database_id must be the all-zero template placeholder"
        )
    source_variables = _mapping(source_config.get("vars"))
    for field_name, placeholder in (
        ("PUBLIC_GAME_URL", PLACEHOLDER_PUBLIC_GAME_URL),
        ("ALLOWED_GAME_ORIGINS", PLACEHOLDER_ALLOWED_GAME_ORIGINS),
    ):
        if source_variables.get(field_name) != placeholder:
            raise DeploymentConfigError(
                f"source {field_name} must be the committed template placeholder"
            )

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

    public_game_url = os.environ.get("PUBLIC_GAME_URL", "")
    if not public_game_url:
        raise DeploymentConfigError("PUBLIC_GAME_URL is required")
    canonical_public_game_url = _canonical_http_url(
        public_game_url,
        origin_only=False,
    )
    if canonical_public_game_url is None:
        raise DeploymentConfigError(PUBLIC_GAME_URL_ERROR)
    if _url_uses_inert_or_reserved_hostname(canonical_public_game_url):
        raise DeploymentConfigError(PUBLIC_GAME_URL_RESERVED_HOST_ERROR)

    allowed_game_origins = os.environ.get("ALLOWED_GAME_ORIGINS", "")
    if not allowed_game_origins:
        raise DeploymentConfigError("ALLOWED_GAME_ORIGINS is required")
    if not _allowed_game_origins_are_valid(allowed_game_origins):
        raise DeploymentConfigError(ALLOWED_GAME_ORIGINS_ERROR)
    if _allowed_game_origins_use_inert_or_reserved_hostname(allowed_game_origins):
        raise DeploymentConfigError(ALLOWED_GAME_ORIGINS_RESERVED_HOST_ERROR)

    rendered_source = _replace_template_string_assignment(
        source,
        table_header="[[d1_databases]]",
        key="database_id",
        expected=PLACEHOLDER_DATABASE_ID,
        replacement=database_id,
        field_name="D1 DB database_id",
    )
    rendered_source = _replace_template_string_assignment(
        rendered_source,
        table_header="[vars]",
        key="PUBLIC_GAME_URL",
        expected=PLACEHOLDER_PUBLIC_GAME_URL,
        replacement=public_game_url,
        field_name="PUBLIC_GAME_URL",
    )
    rendered_source = _replace_template_string_assignment(
        rendered_source,
        table_header="[vars]",
        key="ALLOWED_GAME_ORIGINS",
        expected=PLACEHOLDER_ALLOWED_GAME_ORIGINS,
        replacement=allowed_game_origins,
        field_name="ALLOWED_GAME_ORIGINS",
    )
    rendered = cast(Mapping[str, object], tomllib.loads(rendered_source))
    _validate_or_raise(
        rendered,
        lifecycle,
        expected_database_id=database_id,
    )

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
            expected_database_id = os.environ.get("CLOUDFLARE_D1_DATABASE_ID")
            if expected_database_id is not None:
                expected_database_id = expected_database_id.strip()
            _validate_or_raise(
                _load_toml(arguments.config),
                lifecycle,
                expected_database_id=expected_database_id,
            )
    except (DeploymentConfigError, OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Deployment configuration is ready for {PRODUCTION_HOST}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
