import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deployment_preflight.py"
CONFIG = ROOT / "wrangler.toml"
LIFECYCLE = ROOT / "cloudflare" / "r2-lifecycle.json"
PLACEHOLDER_DATABASE_ID = "00000000-0000-0000-0000-000000000000"
VALID_DATABASE_ID = "01234567-89ab-4cde-8f01-23456789abcd"


def _run_preflight(
    config: Path = CONFIG,
    lifecycle: Path = LIFECYCLE,
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
    )


def _production_config(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    source = CONFIG.read_text(encoding="utf-8").replace(
        PLACEHOLDER_DATABASE_ID,
        VALID_DATABASE_ID,
    )
    for old, new in replacements:
        source = source.replace(old, new)
    target = tmp_path / "wrangler.production.toml"
    target.write_text(source, encoding="utf-8")
    return target


def test_committed_config_cannot_deploy_with_placeholder_d1_id() -> None:
    result = _run_preflight()

    assert result.returncode == 1
    assert "D1 database_id is still the all-zero placeholder" in result.stderr


def test_account_rendered_production_config_passes(tmp_path: Path) -> None:
    result = _run_preflight(_production_config(tmp_path))

    assert result.returncode == 0
    assert result.stdout == "Deployment configuration is ready for api.runretroarcade.com.\n"
    assert result.stderr == ""


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


def test_config_renderer_requires_and_validates_account_d1_id(tmp_path: Path) -> None:
    output = tmp_path / "wrangler.production.toml"
    environment = os.environ.copy()
    environment.pop("CLOUDFLARE_D1_DATABASE_ID", None)

    missing = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--render",
            "--config",
            str(CONFIG),
            "--output",
            str(output),
            "--lifecycle",
            str(LIFECYCLE),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert missing.returncode == 1
    assert "CLOUDFLARE_D1_DATABASE_ID is required" in missing.stderr
    assert not output.exists()

    environment["CLOUDFLARE_D1_DATABASE_ID"] = VALID_DATABASE_ID
    rendered = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--render",
            "--config",
            str(CONFIG),
            "--output",
            str(output),
            "--lifecycle",
            str(LIFECYCLE),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert rendered.returncode == 0
    assert VALID_DATABASE_ID in output.read_text(encoding="utf-8")
    assert PLACEHOLDER_DATABASE_ID not in output.read_text(encoding="utf-8")
