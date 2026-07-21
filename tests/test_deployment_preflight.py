import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deployment_preflight.py"
CONFIG = ROOT / "wrangler.toml"
LIFECYCLE = ROOT / "cloudflare" / "r2-lifecycle.json"
PLACEHOLDER_DATABASE_ID = "00000000-0000-0000-0000-000000000000"
VALID_DATABASE_ID = "01234567-89ab-4cde-8f01-23456789abcd"
MISMATCH_DATABASE_ID = "fedcba98-7654-4abc-8def-0123456789ab"
PLACEHOLDER_PUBLIC_GAME_URL = "https://gamepigeon.example"
PLACEHOLDER_ALLOWED_GAME_ORIGINS = "https://gamepigeon.example"
VALID_PUBLIC_GAME_URL = "https://play.runretroarcade.com/arcade"
VALID_ALLOWED_GAME_ORIGINS = (
    "https://play.runretroarcade.com,https://www.runretroarcade.com"
)
PROTECTED_ENVIRONMENT_NAMES = (
    "CLOUDFLARE_D1_DATABASE_ID",
    "PUBLIC_GAME_URL",
    "ALLOWED_GAME_ORIGINS",
)
GAME_ROOMS_BINDING = """[[durable_objects.bindings]]
name = "GAME_ROOMS"
class_name = "GameRoom"
"""
GAME_ROOM_EXPORT = """[exports.GameRoom]
type = "durable-object"
storage = "sqlite"
"""


def _run_preflight(
    config: Path = CONFIG,
    lifecycle: Path = LIFECYCLE,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--lifecycle",
            str(lifecycle),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=_clean_environment() if environment is None else environment,
    )


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in PROTECTED_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _production_config(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    source = CONFIG.read_text(encoding="utf-8")
    production_replacements = (
        (
            f'database_id = "{PLACEHOLDER_DATABASE_ID}"',
            f'database_id = "{VALID_DATABASE_ID}"',
        ),
        (
            f'PUBLIC_GAME_URL = "{PLACEHOLDER_PUBLIC_GAME_URL}"',
            f'PUBLIC_GAME_URL = "{VALID_PUBLIC_GAME_URL}"',
        ),
        (
            f'ALLOWED_GAME_ORIGINS = "{PLACEHOLDER_ALLOWED_GAME_ORIGINS}"',
            f'ALLOWED_GAME_ORIGINS = "{VALID_ALLOWED_GAME_ORIGINS}"',
        ),
        *replacements,
    )
    for old, new in production_replacements:
        source = source.replace(old, new)
    target = tmp_path / "wrangler.production.toml"
    target.write_text(source, encoding="utf-8")
    return target


def _string_var_replacement(name: str, old: str, new: str) -> tuple[str, str]:
    return (
        f"{name} = {json.dumps(old)}",
        f"{name} = {json.dumps(new)}",
    )


def _render_environment() -> dict[str, str]:
    environment = _clean_environment()
    environment.update(
        {
            "CLOUDFLARE_D1_DATABASE_ID": VALID_DATABASE_ID,
            "PUBLIC_GAME_URL": VALID_PUBLIC_GAME_URL,
            "ALLOWED_GAME_ORIGINS": VALID_ALLOWED_GAME_ORIGINS,
        }
    )
    return environment


def _run_render(
    tmp_path: Path,
    *,
    config: Path = CONFIG,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    output = tmp_path / "wrangler.production.toml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--render",
            "--config",
            str(config),
            "--output",
            str(output),
            "--lifecycle",
            str(LIFECYCLE),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=_render_environment() if environment is None else environment,
    )
    return result, output


def _write_config(tmp_path: Path, source: str, name: str = "wrangler.source.toml") -> Path:
    config = tmp_path / name
    config.write_text(source, encoding="utf-8")
    return config


def test_committed_config_cannot_deploy_with_placeholder_d1_id() -> None:
    result = _run_preflight()

    assert result.returncode == 1
    assert "D1 database_id is still the all-zero placeholder" in result.stderr


def test_account_rendered_production_config_passes(tmp_path: Path) -> None:
    result = _run_preflight(_production_config(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "Deployment configuration is ready for api.runretroarcade.com.\n"
    assert result.stderr == ""


def test_preflight_requires_database_id_to_match_protected_environment(
    tmp_path: Path,
) -> None:
    matching_environment = _clean_environment()
    matching_environment["CLOUDFLARE_D1_DATABASE_ID"] = VALID_DATABASE_ID
    matching = _run_preflight(
        _production_config(tmp_path),
        environment=matching_environment,
    )
    assert matching.returncode == 0, matching.stderr

    mismatched_environment = _clean_environment()
    mismatched_environment["CLOUDFLARE_D1_DATABASE_ID"] = MISMATCH_DATABASE_ID
    mismatched = _run_preflight(
        _production_config(tmp_path),
        environment=mismatched_environment,
    )

    assert mismatched.returncode == 1
    assert (
        "D1 database_id must match CLOUDFLARE_D1_DATABASE_ID"
        in mismatched.stderr
    )
    assert VALID_DATABASE_ID not in mismatched.stderr
    assert MISMATCH_DATABASE_ID not in mismatched.stderr


def test_preflight_rejects_committed_multiplayer_placeholders(
    tmp_path: Path,
) -> None:
    config = _production_config(
        tmp_path,
        _string_var_replacement(
            "PUBLIC_GAME_URL",
            VALID_PUBLIC_GAME_URL,
            PLACEHOLDER_PUBLIC_GAME_URL,
        ),
        _string_var_replacement(
            "ALLOWED_GAME_ORIGINS",
            VALID_ALLOWED_GAME_ORIGINS,
            PLACEHOLDER_ALLOWED_GAME_ORIGINS,
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert "PUBLIC_GAME_URL must not use an inert or reserved hostname" in result.stderr
    assert (
        "ALLOWED_GAME_ORIGINS must not use an inert or reserved hostname"
        in result.stderr
    )
    assert PLACEHOLDER_PUBLIC_GAME_URL not in result.stderr


@pytest.mark.parametrize(
    "reserved_host",
    [
        "gamepigeon.example",
        "gamepigeon.example:8443",
        "docs.example",
        "example.com",
        "play.example.com",
        "example.net",
        "assets.example.net",
        "example.org",
        "www.example.org",
    ],
)
@pytest.mark.parametrize(
    ("field", "default_value"),
    [
        ("PUBLIC_GAME_URL", VALID_PUBLIC_GAME_URL),
        ("ALLOWED_GAME_ORIGINS", VALID_ALLOWED_GAME_ORIGINS),
    ],
)
def test_preflight_rejects_inert_and_reserved_public_hostnames_without_leaking_them(
    tmp_path: Path,
    field: str,
    default_value: str,
    reserved_host: str,
) -> None:
    reserved_url = f"https://{reserved_host}"
    configured_value = (
        f"{reserved_url}/arcade"
        if field == "PUBLIC_GAME_URL"
        else f"{VALID_ALLOWED_GAME_ORIGINS},{reserved_url}"
    )
    config = _production_config(
        tmp_path,
        _string_var_replacement(field, default_value, configured_value),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert f"{field} must not use an inert or reserved hostname" in result.stderr
    assert reserved_host not in result.stderr


def test_preflight_reads_public_configuration_from_toml_not_process_env(
    tmp_path: Path,
) -> None:
    environment = _clean_environment()
    environment["PUBLIC_GAME_URL"] = "https://user:process-secret@evil.example"
    environment.pop("ALLOWED_GAME_ORIGINS", None)

    result = _run_preflight(_production_config(tmp_path), environment=environment)

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize("field", ["PUBLIC_GAME_URL", "ALLOWED_GAME_ORIGINS"])
def test_preflight_requires_public_vars_in_parsed_toml(
    tmp_path: Path,
    field: str,
) -> None:
    defaults = {
        "PUBLIC_GAME_URL": VALID_PUBLIC_GAME_URL,
        "ALLOWED_GAME_ORIGINS": VALID_ALLOWED_GAME_ORIGINS,
    }
    environment = _clean_environment()
    environment["PUBLIC_GAME_URL"] = VALID_PUBLIC_GAME_URL
    environment["ALLOWED_GAME_ORIGINS"] = VALID_ALLOWED_GAME_ORIGINS
    config = _production_config(
        tmp_path,
        (f"{field} = {json.dumps(defaults[field])}\n", ""),
    )

    result = _run_preflight(config, environment=environment)

    assert result.returncode == 1
    assert field in result.stderr


@pytest.mark.parametrize("field", ["PUBLIC_GAME_URL", "ALLOWED_GAME_ORIGINS"])
def test_preflight_rejects_non_string_public_vars(tmp_path: Path, field: str) -> None:
    defaults = {
        "PUBLIC_GAME_URL": VALID_PUBLIC_GAME_URL,
        "ALLOWED_GAME_ORIGINS": VALID_ALLOWED_GAME_ORIGINS,
    }
    config = _production_config(
        tmp_path,
        (f"{field} = {json.dumps(defaults[field])}", f"{field} = 17"),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert field in result.stderr


@pytest.mark.parametrize(
    "public_game_url",
    [
        "https://play.runretroarcade.com",
        "http://localhost:5173",
        "https://play.runretroarcade.com:8443/arcade/2048",
        "http://127.0.0.1:8787/arcade",
        "http://[::1]:8787/play",
        "http://[fe80::1]:8787/play",
        "http://[::ffff:7f00:1]:8787/play",
        "https://xn--bcher-kva.de/arcade",
        "https://xn--fa-hia.de/arcade",
        "https://xn--strae-oqa.de/arcade",
    ],
)
def test_preflight_accepts_canonical_public_game_urls(
    tmp_path: Path,
    public_game_url: str,
) -> None:
    config = _production_config(
        tmp_path,
        _string_var_replacement(
            "PUBLIC_GAME_URL",
            VALID_PUBLIC_GAME_URL,
            public_game_url,
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "public_game_url",
    [
        pytest.param("", id="empty"),
        pytest.param(" https://gamepigeon.example", id="leading-whitespace"),
        pytest.param("https://gamepigeon.example ", id="trailing-whitespace"),
        pytest.param("https://game pigeon.example", id="internal-whitespace"),
        pytest.param(
            "https://user:password@gamepigeon.example",
            id="credentials",
        ),
        pytest.param("https://gamepigeon.example?ticket=secret", id="query"),
        pytest.param("https://gamepigeon.example#secret", id="fragment"),
        pytest.param("https://gamepigeon.example\\arcade", id="backslash"),
        pytest.param("https://gamepigeon.example/\x01", id="control"),
        pytest.param("ftp://gamepigeon.example", id="non-http-scheme"),
        pytest.param("https:///arcade", id="missing-host"),
        pytest.param("https://bad_host.example", id="invalid-hostname"),
        pytest.param("https://[::1", id="malformed-ipv6"),
        pytest.param("https://gamepigeon.example:nope", id="malformed-port"),
        pytest.param("https://gamepigeon.example:0", id="zero-port"),
        pytest.param("https://gamepigeon.example:65536", id="oversized-port"),
        pytest.param("https://gamepigeon.example:443", id="default-port"),
        pytest.param("https://GAMEPIGEON.example", id="uppercase-host"),
        pytest.param("https://gamepigeon.example/", id="root-slash"),
        pytest.param("https://gamepigeon.example/arcade/", id="trailing-slash"),
        pytest.param("https://gamepigeon.example/arcade//2048", id="empty-segment"),
        pytest.param("https://gamepigeon.example/arcade/../admin", id="dot-segment"),
        pytest.param("https://gamepigeon.example/arcade%2Fadmin", id="encoded-slash"),
        pytest.param("https://gamepigeon.example/arcade%5Cadmin", id="encoded-backslash"),
        pytest.param("https://gamepigeon.example/%7eplayer", id="noncanonical-escape"),
        pytest.param("http://0x7f000001", id="legacy-hex-ipv4"),
        pytest.param("http://0x7f.1", id="legacy-mixed-ipv4"),
        pytest.param("https://foo.123", id="numeric-ending-dns"),
        pytest.param("https://xn--a.de", id="invalid-a-label"),
        pytest.param(
            "http://[::ffff:127.0.0.1]:8787/play",
            id="dotted-ipv4-mapped-ipv6",
        ),
    ],
)
def test_preflight_rejects_invalid_public_game_urls(
    tmp_path: Path,
    public_game_url: str,
) -> None:
    config = _production_config(
        tmp_path,
        _string_var_replacement(
            "PUBLIC_GAME_URL",
            VALID_PUBLIC_GAME_URL,
            public_game_url,
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert (
        "PUBLIC_GAME_URL must be a canonical credential-free HTTP(S) URL"
        in result.stderr
    )


@pytest.mark.parametrize(
    ("field", "default_value", "path", "expected_error"),
    [
        (
            "PUBLIC_GAME_URL",
            VALID_PUBLIC_GAME_URL,
            "/play",
            "PUBLIC_GAME_URL must be a canonical credential-free HTTP(S) URL",
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            VALID_ALLOWED_GAME_ORIGINS,
            "",
            "ALLOWED_GAME_ORIGINS must be a nonempty comma-separated list of unique "
            + "canonical HTTP(S) origins",
        ),
    ],
)
@pytest.mark.parametrize(
    "scoped_host",
    [
        pytest.param("fe80::1%eth0", id="raw-zone"),
        pytest.param("fe80::1%", id="empty-zone"),
        pytest.param("fe80::1%25eth0", id="encoded-zone"),
        pytest.param("fe80::1%2525eth0", id="double-encoded-zone"),
        pytest.param("fe80::1%25", id="encoded-empty-zone"),
        pytest.param("fe80::1%eth 0", id="zone-whitespace"),
        pytest.param("fe80::1%eth\x01", id="zone-control"),
        pytest.param("fe80::1%25eth%200", id="encoded-zone-whitespace"),
        pytest.param("fe80::1%25eth%010", id="encoded-zone-control"),
        pytest.param("fe80::1%%eth0", id="double-zone-delimiter"),
        pytest.param("fe80::1%2Geth0", id="malformed-zone-escape"),
    ],
)
def test_preflight_rejects_all_scoped_ipv6_public_hosts(
    tmp_path: Path,
    field: str,
    default_value: str,
    path: str,
    expected_error: str,
    scoped_host: str,
) -> None:
    configured_value = f"http://[{scoped_host}]:8787{path}"
    config = _production_config(
        tmp_path,
        _string_var_replacement(field, default_value, configured_value),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert expected_error in result.stderr


@pytest.mark.parametrize(
    "allowed_origins",
    [
        "https://play.runretroarcade.com",
        "http://localhost:5173",
        "https://play.runretroarcade.com:8443,http://127.0.0.1:8787",
        "http://[::1]:8787",
        "http://[fe80::1]:8787",
        "http://[::ffff:7f00:1]:8787",
        "https://xn--bcher-kva.de",
        "https://xn--fa-hia.de",
        "https://xn--strae-oqa.de",
    ],
)
def test_preflight_accepts_canonical_allowed_game_origins(
    tmp_path: Path,
    allowed_origins: str,
) -> None:
    config = _production_config(
        tmp_path,
        _string_var_replacement(
            "ALLOWED_GAME_ORIGINS",
            VALID_ALLOWED_GAME_ORIGINS,
            allowed_origins,
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "allowed_origins",
    [
        pytest.param("", id="empty"),
        pytest.param(" https://gamepigeon.example", id="leading-whitespace"),
        pytest.param("https://gamepigeon.example ", id="trailing-whitespace"),
        pytest.param(
            "https://gamepigeon.example, https://other.example",
            id="member-whitespace",
        ),
        pytest.param(
            "https://user:password@gamepigeon.example",
            id="credentials",
        ),
        pytest.param("https://gamepigeon.example/", id="root-path"),
        pytest.param("https://gamepigeon.example/play", id="path"),
        pytest.param("https://gamepigeon.example?ticket=secret", id="query"),
        pytest.param("https://gamepigeon.example#secret", id="fragment"),
        pytest.param("https://gamepigeon.example\\play", id="backslash"),
        pytest.param("https://gamepigeon.example/\x01", id="control"),
        pytest.param("ftp://gamepigeon.example", id="non-http-scheme"),
        pytest.param("https://[::1", id="malformed-ipv6"),
        pytest.param("https://gamepigeon.example:nope", id="malformed-port"),
        pytest.param("https://gamepigeon.example:65536", id="oversized-port"),
        pytest.param("https://gamepigeon.example:443", id="default-port"),
        pytest.param(
            "https://gamepigeon.example,https://gamepigeon.example",
            id="duplicate",
        ),
        pytest.param(
            "https://gamepigeon.example,,https://other.example",
            id="empty-middle-member",
        ),
        pytest.param("https://gamepigeon.example,", id="empty-final-member"),
        pytest.param("http://0x7f000001", id="legacy-hex-ipv4"),
        pytest.param("http://0x7f.1", id="legacy-mixed-ipv4"),
        pytest.param("https://foo.123", id="numeric-ending-dns"),
        pytest.param("https://xn--a.de", id="invalid-a-label"),
        pytest.param(
            "http://[::ffff:127.0.0.1]:8787",
            id="dotted-ipv4-mapped-ipv6",
        ),
    ],
)
def test_preflight_rejects_invalid_allowed_game_origins(
    tmp_path: Path,
    allowed_origins: str,
) -> None:
    config = _production_config(
        tmp_path,
        _string_var_replacement(
            "ALLOWED_GAME_ORIGINS",
            VALID_ALLOWED_GAME_ORIGINS,
            allowed_origins,
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    expected_error = (
        "ALLOWED_GAME_ORIGINS must be a nonempty comma-separated list of unique "
        + "canonical HTTP(S) origins"
    )
    assert expected_error in result.stderr


@pytest.mark.parametrize(
    ("field", "credentialed_value"),
    [
        (
            "PUBLIC_GAME_URL",
            "https://alice:public-url-secret@gamepigeon.example",
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            "https://bob:origin-secret@gamepigeon.example",
        ),
    ],
)
def test_preflight_errors_never_echo_url_credentials(
    tmp_path: Path,
    field: str,
    credentialed_value: str,
) -> None:
    defaults = {
        "PUBLIC_GAME_URL": VALID_PUBLIC_GAME_URL,
        "ALLOWED_GAME_ORIGINS": VALID_ALLOWED_GAME_ORIGINS,
    }
    config = _production_config(
        tmp_path,
        _string_var_replacement(field, defaults[field], credentialed_value),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert field in result.stderr
    assert credentialed_value.split("@", maxsplit=1)[0] not in result.stderr


def test_preflight_requires_exact_game_rooms_binding(tmp_path: Path) -> None:
    missing = _run_preflight(
        _production_config(tmp_path, (GAME_ROOMS_BINDING, ""))
    )
    wrong_class = _run_preflight(
        _production_config(
            tmp_path,
            ('class_name = "GameRoom"', 'class_name = "WrongRoom"'),
        )
    )

    for result in (missing, wrong_class):
        assert result.returncode == 1
        assert (
            "GAME_ROOMS must bind Durable Object class GameRoom exactly once"
            in result.stderr
        )


def test_preflight_requires_exact_sqlite_game_room_export(tmp_path: Path) -> None:
    missing = _run_preflight(
        _production_config(tmp_path, (GAME_ROOM_EXPORT, ""))
    )
    wrong_storage = _run_preflight(
        _production_config(
            tmp_path,
            (
                GAME_ROOM_EXPORT,
                GAME_ROOM_EXPORT.replace('storage = "sqlite"', 'storage = "kv"'),
            ),
        )
    )

    for result in (missing, wrong_storage):
        assert result.returncode == 1
        assert (
            "exports.GameRoom must declare a sqlite durable-object"
            in result.stderr
        )


def test_preflight_rejects_top_level_durable_object_migrations(tmp_path: Path) -> None:
    config = _production_config(
        tmp_path,
        (
            "workers_dev = false\n",
            "".join(
                (
                    "workers_dev = false\n\n",
                    "[[migrations]]\n",
                    'tag = "v1"\n',
                    'new_sqlite_classes = ["GameRoom"]\n',
                )
            ),
        ),
    )

    result = _run_preflight(config)

    assert result.returncode == 1
    assert (
        "top-level migrations must be absent for the undeployed GameRoom export"
        in result.stderr
    )


def test_preflight_rejects_development_mode_and_wrong_route(tmp_path: Path) -> None:
    config = _production_config(
        tmp_path,
        ("ENVIRONMENT = \"production\"", "ENVIRONMENT = \"development\""),
        ("api.runretroarcade.com", "wrong.runretroarcade.com"),
    )
    result = _run_preflight(config)

    assert result.returncode == 1
    assert "ENVIRONMENT must be 'production'" in result.stderr
    assert "custom domain must be exactly api.runretroarcade.com" in result.stderr


def test_preflight_rejects_missing_dlq_consumer(tmp_path: Path) -> None:
    config = _production_config(
        tmp_path,
        (
            'queue = "retro-gameplay-events-dlq"\nmax_batch_size = 10',
            'queue = "missing-dlq-consumer"\nmax_batch_size = 10',
        ),
    )
    result = _run_preflight(config)

    assert result.returncode == 1
    assert "retro-gameplay-events-dlq must have an active archival consumer" in result.stderr


@pytest.mark.parametrize("field", PROTECTED_ENVIRONMENT_NAMES)
def test_config_renderer_requires_protected_environment_values(
    tmp_path: Path,
    field: str,
) -> None:
    environment = _render_environment()
    environment.pop(field)

    missing, output = _run_render(tmp_path, environment=environment)

    assert missing.returncode == 1
    assert f"{field} is required" in missing.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value", "expected_error", "protected_fragment"),
    [
        (
            "PUBLIC_GAME_URL",
            "https://render-user:render-public-secret@play.runretroarcade.com",
            "PUBLIC_GAME_URL must be a canonical credential-free HTTP(S) URL",
            "render-public-secret",
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            "https://render-user:render-origin-secret@play.runretroarcade.com",
            "ALLOWED_GAME_ORIGINS must be a nonempty comma-separated list of unique "
            + "canonical HTTP(S) origins",
            "render-origin-secret",
        ),
        (
            "PUBLIC_GAME_URL",
            PLACEHOLDER_PUBLIC_GAME_URL,
            "PUBLIC_GAME_URL must not use an inert or reserved hostname",
            PLACEHOLDER_PUBLIC_GAME_URL,
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            PLACEHOLDER_ALLOWED_GAME_ORIGINS,
            "ALLOWED_GAME_ORIGINS must not use an inert or reserved hostname",
            PLACEHOLDER_ALLOWED_GAME_ORIGINS,
        ),
        (
            "PUBLIC_GAME_URL",
            "https://gamepigeon.example:8443/arcade",
            "PUBLIC_GAME_URL must not use an inert or reserved hostname",
            "gamepigeon.example",
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            "https://play.runretroarcade.com,https://www.example.com",
            "ALLOWED_GAME_ORIGINS must not use an inert or reserved hostname",
            "www.example.com",
        ),
    ],
)
def test_config_renderer_rejects_unsafe_protected_public_values_without_leaking_them(
    tmp_path: Path,
    field: str,
    value: str,
    expected_error: str,
    protected_fragment: str,
) -> None:
    environment = _render_environment()
    environment[field] = value

    result, output = _run_render(tmp_path, environment=environment)

    assert result.returncode == 1
    assert expected_error in result.stderr
    assert protected_fragment not in result.stderr
    assert not output.exists()


def test_config_renderer_updates_only_the_bound_d1_assignment(
    tmp_path: Path,
) -> None:
    source = CONFIG.read_text(encoding="utf-8").replace(
        f'database_id = "{PLACEHOLDER_DATABASE_ID}"',
        "\n".join(
            (
                f'# deployment-note = "{PLACEHOLDER_DATABASE_ID}"',
                f'database_id = "{PLACEHOLDER_DATABASE_ID}"',
            )
        ),
    )
    config = _write_config(tmp_path, source)

    result, output = _run_render(tmp_path, config=config)

    assert result.returncode == 0, result.stderr
    rendered_source = output.read_text(encoding="utf-8")
    rendered = tomllib.loads(rendered_source)
    assert rendered["d1_databases"][0]["database_id"] == VALID_DATABASE_ID
    assert f'# deployment-note = "{PLACEHOLDER_DATABASE_ID}"' in rendered_source


def test_config_renderer_rejects_stale_bound_d1_assignment_even_with_decoy(
    tmp_path: Path,
) -> None:
    source = CONFIG.read_text(encoding="utf-8").replace(
        "[[d1_databases]]",
        f'# deployment-note = "{PLACEHOLDER_DATABASE_ID}"\n[[d1_databases]]',
        1,
    ).replace(
        f'database_id = "{PLACEHOLDER_DATABASE_ID}"',
        f'database_id = "{MISMATCH_DATABASE_ID}"',
    )
    config = _write_config(tmp_path, source)

    result, output = _run_render(tmp_path, config=config)

    assert result.returncode == 1
    assert (
        "source D1 DB database_id must be the all-zero template placeholder"
        in result.stderr
    )
    assert PLACEHOLDER_DATABASE_ID not in result.stderr
    assert MISMATCH_DATABASE_ID not in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "placeholder", "replacement"),
    [
        (
            "PUBLIC_GAME_URL",
            PLACEHOLDER_PUBLIC_GAME_URL,
            VALID_PUBLIC_GAME_URL,
        ),
        (
            "ALLOWED_GAME_ORIGINS",
            PLACEHOLDER_ALLOWED_GAME_ORIGINS,
            VALID_ALLOWED_GAME_ORIGINS,
        ),
    ],
)
def test_config_renderer_rejects_stale_public_template_assignments(
    tmp_path: Path,
    field: str,
    placeholder: str,
    replacement: str,
) -> None:
    source = CONFIG.read_text(encoding="utf-8").replace(
        f"{field} = {json.dumps(placeholder)}",
        f"{field} = {json.dumps(replacement)}",
    )
    config = _write_config(tmp_path, source)

    result, output = _run_render(tmp_path, config=config)

    assert result.returncode == 1
    assert f"source {field} must be the committed template placeholder" in result.stderr
    assert replacement not in result.stderr
    assert not output.exists()


def test_config_renderer_writes_validated_account_values(tmp_path: Path) -> None:
    rendered, output = _run_render(tmp_path)

    assert rendered.returncode == 0
    assert rendered.stderr == ""
    config = tomllib.loads(output.read_text(encoding="utf-8"))
    assert config["d1_databases"][0]["database_id"] == VALID_DATABASE_ID
    assert (
        config["vars"]["AUTH_SESSION_URL"]
        == "https://auth.runretroarcade.com/api/auth/get-session"
    )
    assert config["vars"]["PUBLIC_GAME_URL"] == VALID_PUBLIC_GAME_URL
    assert config["vars"]["ALLOWED_GAME_ORIGINS"] == VALID_ALLOWED_GAME_ORIGINS
