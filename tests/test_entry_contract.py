import ast
import re
import subprocess
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
NODE_VERSION_PATH = ROOT / ".node-version"
NODE_PREFLIGHT_PATH = ROOT / "scripts" / "node_preflight.py"
DEPLOYMENT_PREFLIGHT_PATH = ROOT / "scripts" / "deployment_preflight.py"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_PATH = ROOT / ".github" / "workflows" / "deploy.yml"
R2_LIFECYCLE_PATH = ROOT / "cloudflare" / "r2-lifecycle.json"
RESULTS_API_PATH = SOURCE_ROOT / "api" / "results.py"
AUTH_CACHE_PATH = SOURCE_ROOT / "auth_cache.py"
WORKER_HTTP_PATH = SOURCE_ROOT / "worker_http.py"
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


def _workflow_run_commands(source: str) -> list[str]:
    return [
        match.group("command").strip().strip("\"'")
        for match in re.finditer(
            r"^\s*-\s+run:\s*(?P<command>[^#\n]+)",
            source,
            flags=re.MULTILINE,
        )
    ]


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
    all_imported_names = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "asgi" in imports
    assert ("workers", "WorkerEntrypoint") in imported_names
    assert ("workers", "Blob") in imported_names
    assert ("workers", "Request") in imported_names
    assert ("workers", "Response") in imported_names
    assert ("retro_backend.app", "app") in imported_names
    assert (
        "retro_backend.consumers.dispatch",
        "dispatch_queue",
    ) in imported_names
    assert ("retro_backend.chunk_gate", "ChunkGate") in imported_names
    assert (
        "retro_backend.worker_http",
        "fetch_with_body_limit",
    ) in imported_names
    assert ("js", "caches") in all_imported_names
    assert (
        "retro_backend.auth_cache",
        "build_auth_cache_environment",
    ) in imported_names

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
    assert "return await fetch_with_body_limit(" in source
    assert "asgi_fetch=asgi.fetch" in source
    assert "request_factory=_rebuild_worker_request" in source
    assert "too_large_response=_too_large_response" in source
    assert "asgi.fetch(app, request" not in source
    assert "Blob(body).js_object" in source
    assert "Request(" in source
    assert ".js_object" in source
    assert "return bounded_request.js_object" in source
    assert 'getattr(request, "js_object", request)' in source
    assert "incoming = cast(_WorkerJsRequest, raw_request)" in source
    assert "incoming = cast(_WorkerJsRequest, request)" not in source
    assert "incoming.url" in source
    assert "method=cast(HTTPMethod, incoming.method)" in source
    assert "headers=headers" in source
    assert "redirect=incoming.redirect" in source
    assert "Response.from_json(" in source
    assert '{"detail": "request body exceeds maximum size"}' in source
    assert "status=413" in source
    assert "build_auth_cache_environment(" in source
    assert "Cache-Control" in source
    assert "max-age=" in source
    assert "await dispatch_queue(batch, self.env)" in source


def test_worker_http_boundary_is_packaged_as_python_runtime_source() -> None:
    source = _read(WORKER_HTTP_PATH)

    assert "async def fetch_with_body_limit(" in source
    assert "content-length" in source
    assert "getReader" in source
    assert "await _cancel(reader)" in source
    assert "byteLength" in source
    assert 'asgi_request = getattr(request, "js_object", request)' in source
    assert "await asgi_fetch(app, asgi_request, environment)" in source
    assert "request_factory(request, raw_body, headers)" in source
    assert "await asgi_fetch(app, bounded_request, environment)" in source


def test_docs_place_body_limit_before_asgi_buffering_and_fastapi_auth() -> None:
    readme = _read(README_PATH)
    design = _read(DESIGN_PATH)
    plan = _read(PLAN_PATH)

    for source in (readme, design, plan):
        normalized = source.casefold()
        assert "worker fetch boundary" in normalized
        assert "asgi" in normalized
        assert "fastapi" in normalized
    combined = "\n".join((readme, design, plan)).casefold()
    assert "authenticates before reading" not in combined
    assert "both routes authenticate before reading" not in combined
    assert "return await asgi.fetch(app, request, self.env)" not in combined
    assert "fetch_with_body_limit" in plan


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
    assert config["observability"] == {
        "enabled": True,
        "head_sampling_rate": 1.0,
    }
    assert config["workers_dev"] is False
    assert config["routes"] == [
        {"pattern": "api.runretroarcade.com", "custom_domain": True}
    ]


def test_wrangler_binds_d1_r2_and_gameplay_dlq_archiver() -> None:
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
            },
            {
                "queue": "retro-gameplay-events-dlq",
                "max_batch_size": 10,
                "max_batch_timeout": 5,
                "max_retries": 10,
                "dead_letter_queue": "retro-gameplay-events-archive-dlq",
            },
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
    readme = _read(README_PATH)

    expected_vars = {
        "AUTH_CACHE_TTL_SECONDS": 60,
        "MAX_CHUNK_BYTES": 262_144,
        "MAX_RESULT_BYTES": 16_384,
        "CHUNK_DEDUP_TTL_MS": 172_800_000,
        "CHUNK_PUBLISHING_LEASE_MS": 30_000,
        "ENVIRONMENT": "production",
    }
    assert config["vars"] == expected_vars
    assert "`AUTH_CACHE_TTL_SECONDS=60`" in readme
    assert "`MAX_RESULT_BYTES=16384`" in readme
    assert "`ENVIRONMENT=production`" in readme
    assert "16 KiB" in readme
    assert not any(
        marker in key.casefold()
        for key in expected_vars
        for marker in ("secret", "token", "password", "key")
    )


def test_auth_cache_uses_d1_misses_without_caching_leaderboards_or_using_r2() -> None:
    readme = _read(README_PATH)
    design = _read(DESIGN_PATH)
    config = _wrangler_config()
    auth_cache = _read(AUTH_CACHE_PATH)
    results_api = _read(RESULTS_API_PATH)

    documentation = " ".join("\n".join((readme, design)).casefold().split())
    assert "auth cache miss" in documentation
    assert "d1" in documentation
    assert "leaderboard responses are not cached" in documentation
    assert "r2" in documentation
    assert "raw and derived gameplay" in documentation
    assert "worker-level caching remains disabled" in documentation
    assert "custom domain" in documentation

    assert "cache" not in config
    assert "RAW_BUCKET" not in auth_cache
    assert "R2" not in auth_cache
    assert "EdgeAuthCache" not in results_api
    assert "AUTH_CACHE" not in results_api


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

    assert _read(NODE_VERSION_PATH).strip() == "22.23.1"
    assert NODE_PREFLIGHT_PATH.is_file()
    assert DEPLOYMENT_PREFLIGHT_PATH.is_file()
    assert "Node.js 22.x" in readme
    assert "22.23.1" in readme
    assert "Node.js 23 and newer are unsupported" in readme
    assert "Node.js 22 or newer" not in readme
    assert "make node-preflight" in readme
    assert "make worker-sync" in readme
    assert "make worker-types" in readme
    assert "make worker-dry-run" in readme
    assert "make migrate-local" in readme
    assert "make migrate-remote" in readme
    assert "make deployment-preflight" in readme
    assert "make apply-r2-lifecycle" in readme
    assert "uv run --frozen uv --version" in readme
    assert "UV_RUN := uv run --frozen" in makefile
    assert "WRANGLER_VERSION ?= 4.112.0" in makefile
    assert "WRANGLER := npx --yes wrangler@$(WRANGLER_VERSION)" in makefile


def test_release_make_targets_use_the_pinned_wrangler() -> None:
    for target in (
        "worker-dry-run",
        "dev",
        "deploy",
        "migrate-local",
        "migrate-remote",
        "apply-r2-lifecycle",
    ):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "npx --yes wrangler@4.112.0" in result.stdout


def test_worker_make_targets_run_node_preflight_first() -> None:
    for target in (
        "worker-sync",
        "worker-types",
        "worker-dry-run",
        "dev",
        "deploy",
        "migrate-local",
        "migrate-remote",
        "apply-r2-lifecycle",
    ):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        commands = result.stdout.splitlines()
        preflight_index = commands.index(
            "uv run --frozen python scripts/node_preflight.py"
        )
        pywrangler_index = next(
            index
            for index, command in enumerate(commands)
            if "pywrangler" in command or "npx --yes wrangler@" in command
        )
        assert preflight_index < pywrangler_index


def test_live_make_targets_require_deployment_preflight() -> None:
    for target in ("deploy", "migrate-remote", "apply-r2-lifecycle"):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        commands = result.stdout.splitlines()
        deployment_preflight_index = commands.index(
            "uv run --frozen python scripts/deployment_preflight.py --config wrangler.toml"
        )
        wrangler_index = next(
            index
            for index, command in enumerate(commands)
            if "npx --yes wrangler@" in command
        )
        assert deployment_preflight_index < wrangler_index


def test_ci_uses_pinned_toolchains_and_canonical_worker_verification() -> None:
    workflow = _read(CI_PATH)
    commands = _workflow_run_commands(workflow)

    assert re.search(r"actions/setup-python@v\d+", workflow)
    assert re.search(r"python-version:\s*[\"']?3\.13[\"']?", workflow)
    assert re.search(r"actions/setup-node@v\d+", workflow)
    assert re.search(r"node-version-file:\s*[\"']?\.node-version[\"']?", workflow)
    assert not re.search(r"^\s+node-version:\s*", workflow, flags=re.MULTILINE)

    expected_commands = {
        "uv sync --frozen",
        "make verify",
        "make worker-sync",
        "make worker-dry-run",
    }
    assert expected_commands <= set(commands)
    assert commands.index("make worker-sync") < commands.index("make worker-dry-run")
    assert "make deploy" not in commands
    assert all("--remote" not in command for command in commands)
    assert all(
        "pywrangler deploy" not in command or "--dry-run" in command
        for command in commands
    )


def test_manual_production_workflow_is_guarded_and_smoke_tests_the_custom_domain() -> None:
    workflow = _read(DEPLOY_PATH)
    commands = _workflow_run_commands(workflow)

    assert re.search(r"^\s*workflow_dispatch:\s*$", workflow, flags=re.MULTILINE)
    assert not re.search(r"^\s+push:\s*$", workflow, flags=re.MULTILINE)
    assert "needs: verify" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "environment: production" in workflow
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in workflow
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}" in workflow
    assert (
        "CLOUDFLARE_D1_DATABASE_ID: ${{ secrets.CLOUDFLARE_D1_DATABASE_ID }}"
        in workflow
    )

    expected_commands = [
        "uv sync --frozen",
        "make verify",
        "make prepare-deployment-config",
        "make worker-dry-run DEPLOY_CONFIG=wrangler.production.toml",
        "make migrate-remote DEPLOY_CONFIG=wrangler.production.toml",
        "make apply-r2-lifecycle DEPLOY_CONFIG=wrangler.production.toml",
        "make deploy DEPLOY_CONFIG=wrangler.production.toml",
        (
            "curl --fail --silent --show-error --retry 8 --retry-all-errors "
            "--retry-delay 5 https://api.runretroarcade.com/health/ready"
        ),
    ]
    previous_index = -1
    for expected in expected_commands:
        previous_index = commands.index(expected, previous_index + 1)


def test_r2_lifecycle_preserves_artifacts_and_transitions_them_after_30_days() -> None:
    lifecycle = __import__("json").loads(_read(R2_LIFECYCLE_PATH))

    assert lifecycle == {
        "Rules": [
            {
                "ID": "transition-retro-artifacts-to-infrequent-access",
                "Status": "Enabled",
                "Transitions": [
                    {
                        "Days": 30,
                        "StorageClass": "STANDARD_IA",
                    }
                ],
            }
        ]
    }


def test_readme_lists_the_implemented_high_scores_route() -> None:
    readme = _read(README_PATH)
    results_api = _read(RESULTS_API_PATH)

    assert '"/v1/high-scores/{game_type}"' in results_api
    assert "`GET /v1/high-scores/{game_type}`" in readme
    assert "/v1/leaderboards/{game_type}" not in readme


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
    assert "**Files:**" not in task_4
    assert "- [ ]" not in task_4


def test_deferred_integrations_have_no_schema_or_advertised_resources() -> None:
    migration = _read(ROOT / "migrations" / "0001_core.sql")
    readme = _read(README_PATH)
    active_docs = "\n".join((_read(DESIGN_PATH), _read(PLAN_PATH)))

    for marker in ("stripe", "posthog", "billing", "analytics"):
        assert marker not in migration.casefold()
    for marker in (
        "/v1/webhooks/stripe",
        "retro-analytics-events",
        "retro-billing-events",
        "ANALYTICS_QUEUE",
        "BILLING_QUEUE",
        "stripe_customers",
        "processed_stripe_events",
        "POSTHOG_API_KEY",
        "STRIPE_SECRET_KEY",
        "src/retro_backend/api/stripe.py",
        "src/retro_backend/integrations/posthog.py",
    ):
        assert marker not in active_docs
    assert "no active tables or persistence" in readme.casefold()


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
