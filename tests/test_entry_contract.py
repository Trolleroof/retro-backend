import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "retro_backend"
ENTRY_PATH = SOURCE_ROOT / "entry.py"
CHUNK_GATE_PATH = SOURCE_ROOT / "chunk_gate.py"
WRANGLER_PATH = ROOT / "wrangler.toml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
PYLOCK_PATH = ROOT / "pylock.toml"
GITIGNORE_PATH = ROOT / ".gitignore"
MAKEFILE_PATH = ROOT / "Makefile"
README_PATH = ROOT / "README.md"
DESIGN_PATH = (
    ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-07-17-cloudflare-python-backend-design.md"
)
PLAN_PATH = (
    ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-17-cloudflare-python-backend.md"
)

LEGACY_PATHS = (
    ".dockerignore",
    "Dockerfile",
    "docker-compose.yml",
    "alembic.ini",
    "migrations/env.py",
    "migrations/script.py.mako",
    "migrations/versions",
    "src/retro_backend/api/routes.py",
    "src/retro_backend/config.py",
    "src/retro_backend/database.py",
    "src/retro_backend/main.py",
    "src/retro_backend/models.py",
    "src/retro_backend/redis_client.py",
    "src/retro_backend/worker.py",
    "tests/test_worker.py",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _async_method(class_node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    return next(
        node
        for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _wrangler_config() -> dict[str, object]:
    return tomllib.loads(_read(WRANGLER_PATH))


def test_entry_exposes_fastapi_fetch_queue_and_chunk_gate() -> None:
    tree = _parse_python(ENTRY_PATH)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_names = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "asgi" in imports
    assert ("workers", "WorkerEntrypoint") in imported_names
    assert ("retro_backend.app", "app") in imported_names
    assert (
        "retro_backend.consumers.dispatch",
        "dispatch_queue",
    ) in imported_names
    assert ("retro_backend.chunk_gate", "ChunkGate") in imported_names

    default = _class(tree, "Default")
    assert [base.id for base in default.bases if isinstance(base, ast.Name)] == [
        "WorkerEntrypoint"
    ]
    assert [argument.arg for argument in _async_method(default, "fetch").args.args] == [
        "self",
        "request",
    ]
    assert [argument.arg for argument in _async_method(default, "queue").args.args] == [
        "self",
        "batch",
    ]

    source = _read(ENTRY_PATH)
    assert "await asgi.fetch(app, request, self.env)" in source
    assert "await dispatch_queue(batch, self.env)" in source


def test_chunk_gate_uses_explicit_runtime_base_branch() -> None:
    tree = _parse_python(CHUNK_GATE_PATH)
    platform_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "sys.platform == 'emscripten'"
    )
    runtime_imports = {
        (node.module, alias.name)
        for node in platform_branch.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert runtime_imports == {("workers", "DurableObject")}
    assert _class(ast.Module(body=platform_branch.orelse, type_ignores=[]), "DurableObject")
    assert not any(
        isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.ImportFrom) and child.module == "workers"
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )


def test_chunk_gate_exports_runtime_durable_object_rpc() -> None:
    tree = _parse_python(CHUNK_GATE_PATH)
    chunk_gate = _class(tree, "ChunkGate")

    assert [base.id for base in chunk_gate.bases if isinstance(base, ast.Name)] == [
        "DurableObject"
    ]

    expected_signatures = {
        "reserve": [
            "self",
            "payload_sha256",
            "lease_token",
            "now_ms",
            "lease_ms",
        ],
        "accept": [
            "self",
            "payload_sha256",
            "lease_token",
            "acknowledgment",
            "now_ms",
            "dedupe_ttl_ms",
        ],
        "release": ["self", "payload_sha256", "lease_token"],
        "alarm": ["self"],
    }
    for method_name, arguments in expected_signatures.items():
        method = _async_method(chunk_gate, method_name)
        assert [argument.arg for argument in method.args.args] == arguments


def test_wrangler_declares_worker_identity_and_observability() -> None:
    config = _wrangler_config()

    assert config["name"] == "retro-backend"
    assert config["main"] == "src/retro_backend/entry.py"
    assert config["base_dir"] == "src"
    assert config["compatibility_date"] == "2026-07-17"
    assert config["compatibility_flags"] == ["python_workers"]
    assert config["observability"] == {"enabled": True}


def test_wrangler_binds_d1_r2_and_one_gameplay_queue() -> None:
    config = _wrangler_config()

    assert config["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "retro-backend",
            "database_id": "00000000-0000-0000-0000-000000000000",
            "migrations_dir": "migrations",
        }
    ]
    assert config["r2_buckets"] == [
        {"binding": "RAW_BUCKET", "bucket_name": "retro-gameplay-raw"}
    ]
    assert config["queues"] == {
        "producers": [
            {
                "binding": "GAMEPLAY_QUEUE",
                "queue": "retro-gameplay-events",
            }
        ],
        "consumers": [
            {
                "queue": "retro-gameplay-events",
                "max_batch_size": 10,
                "max_batch_timeout": 5,
                "max_retries": 5,
                "dead_letter_queue": "retro-gameplay-events-dlq",
            }
        ],
    }


def test_wrangler_uses_current_sqlite_chunk_gate_export() -> None:
    config = _wrangler_config()

    assert config["durable_objects"] == {
        "bindings": [{"name": "CHUNK_GATE", "class_name": "ChunkGate"}]
    }
    assert config["exports"] == {
        "ChunkGate": {"type": "durable-object", "storage": "sqlite"}
    }
    assert "migrations" not in config


def test_wrangler_binds_native_ingest_rate_limiter() -> None:
    config = _wrangler_config()

    assert config["ratelimits"] == [
        {
            "name": "INGEST_RATE_LIMITER",
            "namespace_id": "1001",
            "simple": {"limit": 120, "period": 60},
        }
    ]


def test_wrangler_contains_only_approved_nonsecret_vars() -> None:
    config = _wrangler_config()

    expected_vars = {
        "MAX_CHUNK_BYTES": 262_144,
        "CHUNK_DEDUP_TTL_MS": 172_800_000,
        "CHUNK_PUBLISHING_LEASE_MS": 30_000,
        "ENVIRONMENT": "development",
    }
    assert config["vars"] == expected_vars
    assert not any(
        marker in key.casefold()
        for key in expected_vars
        for marker in ("secret", "token", "password", "key")
    )


def test_deferred_analytics_and_billing_are_not_configured() -> None:
    config_source = _read(WRANGLER_PATH).casefold()
    source_paths = {
        path.relative_to(SOURCE_ROOT).as_posix().casefold()
        for path in SOURCE_ROOT.rglob("*.py")
    }

    for marker in ("posthog", "stripe", "analytics_queue", "billing_queue"):
        assert marker not in config_source
    assert "api/stripe.py" not in source_paths
    assert "consumers/analytics.py" not in source_paths
    assert "consumers/billing.py" not in source_paths
    assert "integrations/posthog.py" not in source_paths
    assert "integrations/stripe.py" not in source_paths


def test_backend_has_no_typescript_or_javascript_source() -> None:
    excluded_directories = {
        ".git",
        ".venv",
        ".venv-workers",
        ".wrangler",
        ".pytest_cache",
        ".ruff_cache",
        "js-stubs",
        "python_modules",
    }
    generated_source = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".js", ".ts"}
        and not any(part in excluded_directories for part in path.parts)
    ]

    assert generated_source == []


def test_worker_lock_is_committed_while_generated_modules_are_ignored() -> None:
    ignored = {
        line.strip()
        for line in _read(GITIGNORE_PATH).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".venv-workers/",
        ".wrangler/",
        "python_modules/",
        "src/js-stubs/",
    } <= ignored
    assert "pylock.toml" not in ignored
    assert PYLOCK_PATH.is_file()


def test_worker_toolchain_and_pyodide_dependencies_are_reproducibly_pinned() -> None:
    project = tomllib.loads(_read(PYPROJECT_PATH))
    runtime_dependencies = project["project"]["dependencies"]
    development_dependencies = project["dependency-groups"]["dev"]

    assert "pydantic==2.10.6" in runtime_dependencies
    assert "uv==0.10.9" in development_dependencies
    assert "workers-py==1.15.0" in development_dependencies
    assert "workers-runtime-sdk==1.6.3" in development_dependencies

    worker_lock = tomllib.loads(_read(PYLOCK_PATH))
    locked_packages = {
        package["name"]: package for package in worker_lock["packages"]
    }
    assert locked_packages["pydantic"]["version"] == "2.10.6"
    assert locked_packages["pydantic-core"]["version"] == "2.27.2"
    assert locked_packages["workers-runtime-sdk"]["version"] == "1.6.3"
    assert any(
        "pydantic_core-2.27.2-cp313-cp313-pyodide_2025_0_wasm32.whl"
        in wheel["url"]
        for wheel in locked_packages["pydantic-core"]["wheels"]
    )


def test_documented_worker_commands_use_the_frozen_project_toolchain() -> None:
    readme = _read(README_PATH)
    makefile = _read(MAKEFILE_PATH)

    assert "uv run --frozen uv --version" in readme
    assert "uv run --frozen pywrangler deploy --dry-run" in readme
    assert "UV_RUN := uv run --frozen" in makefile
    assert "$(UV_RUN) pywrangler dev" in makefile
    assert "$(UV_RUN) pywrangler deploy" in makefile


def test_design_and_plan_mark_deferred_integrations_as_future_work() -> None:
    design = _read(DESIGN_PATH)
    plan = _read(PLAN_PATH)
    design_preamble = "\n".join(design.splitlines()[:15])
    plan_preamble = "\n".join(plan.splitlines()[:15])

    for preamble in (design_preamble, plan_preamble):
        assert "Scope update (2026-07-17)" in preamble
        assert "deferred by user direction" in preamble
        assert "future work" in preamble

    assert "## Task 4: PostHog Analytics And Stripe Billing (Deferred)" in plan
    task_4 = plan.split("## Task 4:", maxsplit=1)[1].split("## Task 5:", maxsplit=1)[0]
    assert "not part of the active implementation" in task_4


def test_legacy_infrastructure_is_absent() -> None:
    assert [path for path in LEGACY_PATHS if (ROOT / path).exists()] == []


def test_runtime_dependencies_and_imports_are_cloudflare_native() -> None:
    project = tomllib.loads(_read(ROOT / "pyproject.toml"))["project"]
    dependency_names = {
        re.split(r"[<>=!~\[]", dependency, maxsplit=1)[0]
        for dependency in project["dependencies"]
    }
    assert dependency_names == {"fastapi", "httpx", "pydantic"}

    forbidden = (
        "alembic",
        "docker",
        "kafka",
        "psycopg",
        "redis",
        "sqlalchemy",
        "uvicorn",
    )
    pyproject_source = _read(ROOT / "pyproject.toml").casefold()
    runtime_source = "\n".join(
        _read(path).casefold() for path in SOURCE_ROOT.rglob("*.py")
    )
    for marker in forbidden:
        assert marker not in pyproject_source
        assert marker not in runtime_source
