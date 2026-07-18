# Cloudflare Python Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope update (2026-07-17):** Stripe, PostHog, and the analytics and billing queues are deferred by user direction. Task 4 and related sections are retained as future work and are not part of the active implementation.

**Goal:** Replace the development FastAPI/PostgreSQL/Redis/Kafka stack with a deployable Cloudflare-native backend whose runtime logic is entirely Python.

**Architecture:** A Python Worker delegates HTTP requests to FastAPI through Cloudflare's ASGI adapter and exposes Queue plus Durable Object entrypoints from the same module. D1 stores small relational product state, R2 stores exact and validated gameplay objects, a per-chunk Durable Object coordinates publication, and Queues isolate gameplay processing, PostHog, and Stripe projections from request latency.

**Tech Stack:** Python 3.13, FastAPI, Pydantic 2, HTTPX, Cloudflare Python Workers, pywrangler, D1, R2, Queues, Durable Objects, pytest, Ruff, BasedPyright.

---

## File Map

The rewrite owns these files:

```text
src/retro_backend/
  __init__.py                  package version only
  app.py                       FastAPI composition and error mapping
  auth.py                      bearer parsing, hashing, and user dependency
  bindings.py                  binding protocols and JS/dict conversion helpers
  contracts.py                 Pydantic wire contracts and v2 semantic validation
  entry.py                     Worker fetch and queue handlers
  chunk_gate.py                Cloudflare Durable Object and gate client
  observability.py             allowlisted structured JSON logging
  api/
    __init__.py
    health.py                  health and current-user routes
    chunks.py                  chunk ingestion route
    results.py                 score submission and leaderboard routes
    stripe.py                  raw-body Stripe webhook route
  storage/
    __init__.py
    d1.py                      D1 auth, score, and billing repositories
    r2.py                      deterministic keys and conditional object I/O
    queues.py                  Queue envelopes and publishing helpers
  services/
    __init__.py
    ingestion.py               R2-first chunk publication orchestration
  consumers/
    __init__.py
    dispatch.py                queue-name routing and per-message ack/retry
    gameplay.py                semantic validation and derived R2 objects
    analytics.py               PostHog Capture API delivery
    billing.py                 Stripe event projection into D1
  integrations/
    __init__.py
    posthog.py                 HTTPX PostHog client and safe event mapping
    stripe.py                  manual Stripe signature verification
migrations/0001_core.sql       D1 product-state schema
scripts/issue_token.py         local opaque token and SQL generator
tests/                         pure Python unit and HTTP integration tests
wrangler.toml                  Python Worker resources and Queue triggers
pyproject.toml                 Python Worker and development dependencies
README.md                      Cloudflare setup, local development, and deploy
```

Legacy SQLAlchemy, Alembic, Redis, Kafka, Docker, and Uvicorn files are removed only after their replacements pass.

## Task 1: Python Worker Core, Contracts, D1, And HTTP Routes

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/retro_backend/__init__.py`
- Replace: `src/retro_backend/contracts.py`
- Replace: `src/retro_backend/auth.py`
- Create: `src/retro_backend/bindings.py`
- Create: `src/retro_backend/app.py`
- Create: `src/retro_backend/observability.py`
- Create: `src/retro_backend/api/health.py`
- Create: `src/retro_backend/api/results.py`
- Create: `src/retro_backend/storage/__init__.py`
- Create: `src/retro_backend/storage/d1.py`
- Create: `migrations/0001_core.sql`
- Create: `scripts/issue_token.py`
- Replace: `tests/conftest.py`
- Replace: `tests/test_contracts.py`
- Create: `tests/test_auth.py`
- Create: `tests/test_d1.py`
- Create: `tests/test_results_api.py`

- [ ] **Step 1: Write failing contract, auth, repository, and route tests**

Define a reusable fake D1 binding in `tests/conftest.py`. It must record prepared SQL and bound parameters and return configured rows from `first()` or `run()`.

Add tests with these exact behavioral assertions:

```python
def test_token_hash_is_stable_and_does_not_contain_plaintext() -> None:
    assert hash_token("retro_secret") == hashlib.sha256(b"retro_secret").hexdigest()
    assert "retro_secret" not in hash_token("retro_secret")


def test_v2_envelope_rejects_authenticated_identity_mismatch(fixture_payload):
    envelope = ChunkEnvelope.model_validate(fixture_payload)
    with pytest.raises(ContractError, match="user_id must match"):
        envelope.validate_authenticated_identity("usr_someone_else")


async def test_auth_repository_resolves_token_hash(fake_d1):
    fake_d1.first_rows.append({"id": "usr_example"})
    user_id = await D1Repository(fake_d1).resolve_user("opaque")
    assert user_id == "usr_example"
    assert fake_d1.bind_calls[-1] == (hash_token("opaque"),)


async def test_higher_score_updates_personal_best(fake_d1):
    fake_d1.first_rows.extend([{"score": 100}, {"score": 100}])
    result = await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=100,
        achieved_at="2026-07-17T18:42:00Z",
    )
    assert result.personal_best == 100
    assert result.updated is True


async def test_result_route_requires_auth_and_returns_personal_best(app_client, fake_env):
    fake_env.DB.first_rows.extend([{"id": "usr_example"}, {"score": 32768}, {"score": 32768}])
    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        json={
            "result_id": "res_01",
            "game_id": "game_01",
            "game_type": "2048",
            "score": 32768,
            "completed_at": "2026-07-17T18:42:00Z",
        },
    )
    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "personal_best": 32768,
        "high_score_updated": True,
    }
```

Also test missing bearer token (`401`), unknown token (`401`), invalid game type (`422`), lower/equal score preservation, leaderboard order and limit, `GET /health/live`, `GET /health/ready`, and `GET /v1/me`.

Test `log_event()` separately and assert that it emits one JSON object containing only the event name and explicitly supplied allowlisted identifiers. Its API must not accept request bodies, bearer tokens, Stripe signatures, or arbitrary mappings.

- [ ] **Step 2: Run tests and verify the new APIs fail for missing modules**

Run:

```bash
uv run pytest tests/test_contracts.py tests/test_auth.py tests/test_d1.py tests/test_results_api.py -q
```

Expected: collection fails because `retro_backend.bindings`, `retro_backend.storage.d1`, and `retro_backend.app` do not exist or the imported symbols are missing.

- [ ] **Step 3: Implement the minimal Python core and D1-backed routes**

Set `requires-python = ">=3.13,<3.14"`. Runtime dependencies are exactly `fastapi`, `httpx`, and `pydantic`; development dependencies include `workers-py`, `workers-runtime-sdk`, `pytest`, `pytest-asyncio`, `ruff`, and `basedpyright`.

Use these public contracts in `contracts.py`:

```python
GameType = Literal[
    "2048", "tetris", "snake", "minesweeper",
    "sokoban", "connect4", "lightsout", "sudoku",
]


class ChunkEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[2]
    user_id: str
    game_id: str
    game_type: str
    game_version: str
    chunk_id: str
    chunk_sequence: int = Field(ge=1)
    first_event_sequence: int = Field(ge=1)
    last_event_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1)
    created_at_ms: int = Field(ge=0)
    client: dict[str, object]
    events: list[dict[str, object]]

    def validate_authenticated_identity(self, user_id: str) -> None:
        if self.user_id != user_id:
            raise ContractError("user_id must match the authenticated user")
        for index, event in enumerate(self.events):
            if event.get("user_id") != user_id:
                raise ContractError(
                    f"events[{index}].user_id must match the authenticated user"
                )


class GameResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(pattern=r"^res_[A-Za-z0-9._-]+$")
    game_id: str = Field(pattern=r"^game_[A-Za-z0-9._-]+$")
    game_type: GameType
    score: int = Field(ge=0, le=2**63 - 1, strict=True)
    completed_at: AwareDatetime
```

`bindings.py` defines small `Protocol` types for `D1Database`, `D1PreparedStatement`, `R2Bucket`, `QueueBinding`, and `DurableObjectNamespace`. It also defines `value_field(value, name, default=None)` that reads from mappings or object attributes so production JS proxies and Python fakes share the same adapters.

`auth.py` exposes `hash_token`, strict bearer parsing, and a FastAPI `require_user_id(request)` dependency. It reads the binding from `request.scope["env"]` in Cloudflare and falls back to `request.app.state.env` in tests.

`D1Repository` uses prepared statements with numbered parameters. The high-score upsert is concurrency-safe:

```sql
INSERT INTO high_scores (user_id, game_type, score, achieved_at)
VALUES (?1, ?2, ?3, ?4)
ON CONFLICT(user_id, game_type) DO UPDATE SET
    score = excluded.score,
    achieved_at = excluded.achieved_at
WHERE excluded.score > high_scores.score
RETURNING score
```

When `RETURNING` produces no row, select the existing score and report `updated=False`.

Create `app = FastAPI(title="Retro Backend", version="0.2.0")`, include health and result routers, and return `202` for accepted game results.

`migrations/0001_core.sql` creates `users`, `high_scores`, `stripe_customers`, and `processed_stripe_events` exactly as approved, enables foreign keys, and adds a descending leaderboard index.

`scripts/issue_token.py` uses `secrets.token_urlsafe(32)`, prints the one-time token to stderr, and prints a parameter-safe SQL statement containing only its hash to stdout. It has no Cloudflare credential dependency.

`observability.py` exposes `log_event(event, *, request_id=None, user_id=None, game_id=None, chunk_id=None, outcome=None)`. It omits `None` values and writes one compact JSON line through `print()`.

- [ ] **Step 4: Run the focused tests until green**

Run:

```bash
uv run pytest tests/test_contracts.py tests/test_auth.py tests/test_d1.py tests/test_results_api.py -q
uv run ruff check src tests scripts
uv run basedpyright src tests
```

Expected: all focused tests pass and static checks emit no errors.

- [ ] **Step 5: Commit the core**

```bash
git add pyproject.toml uv.lock src/retro_backend tests migrations/0001_core.sql scripts/issue_token.py
git commit -m "feat: add Cloudflare Python API core"
```

## Task 2: R2-First Ingestion And ChunkGate Coordination

**Files:**
- Create: `src/retro_backend/storage/r2.py`
- Create: `src/retro_backend/storage/queues.py`
- Replace: `src/retro_backend/services/ingestion.py`
- Create: `src/retro_backend/chunk_gate.py`
- Create: `src/retro_backend/api/chunks.py`
- Modify: `src/retro_backend/app.py`
- Replace: `contracts/ingest-response-v1.schema.json`
- Create: `tests/test_r2.py`
- Create: `tests/test_chunk_gate.py`
- Replace: `tests/test_ingestion_api.py`
- Create: `tests/test_ingestion_service.py`

- [ ] **Step 1: Write failing storage, gate, service, and HTTP tests**

Tests must exercise real orchestration with fake bindings:

```python
async def test_first_publish_stores_exact_bytes_before_queue(
    fake_env, fixture_bytes, fixture_payload
):
    result = await ingest_chunk(
        env=fake_env,
        user_id=fixture_payload["user_id"],
        raw_body=fixture_bytes,
        now_ms=1784241600000,
    )
    assert fake_env.RAW_BUCKET.put_calls[0].body == fixture_bytes
    assert fake_env.operation_order == ["gate.reserve", "r2.put", "queue.send", "gate.accept"]
    assert result.queued is True
    assert result.duplicate is False


async def test_duplicate_returns_cached_ack_without_r2_or_queue(fake_env, fixture_bytes):
    fake_env.CHUNK_GATE.reserve_result = {
        "status": "duplicate",
        "ack": {"accepted": True, "duplicate": False, "game_id": "game_01",
                "chunk_id": "chk_01", "chunk_sequence": 1,
                "accepted_event_range": {"first": 1, "last": 4},
                "queued": True, "server_received_at_ms": 1784241600000},
    }
    result = await ingest_chunk(fake_env, "usr_example", fixture_bytes, 1784241601000)
    assert result.duplicate is True
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_queue_failure_releases_gate_and_returns_retryable_error(
    fake_env, fixture_bytes
):
    fake_env.GAMEPLAY_QUEUE.failure = RuntimeError("unavailable")
    with pytest.raises(IngestionUnavailable):
        await ingest_chunk(fake_env, "usr_example", fixture_bytes, 1784241600000)
    assert fake_env.CHUNK_GATE.release_calls == 1


async def test_chunk_route_ack_matches_schema(app_client, fake_env, fixture_bytes):
    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )
    assert response.status_code == 202
    Draft202012Validator(INGEST_RESPONSE_SCHEMA).validate(response.json())
    assert "broker_partition" not in response.json()
    assert "broker_offset" not in response.json()
```

Also test empty body (`400`), invalid JSON (`400`), schema v1 (`400`), authenticated identity mismatch (`400`), body larger than 256 KiB (`413`), a streaming body that stops reading immediately after crossing the limit, chunk hash conflict (`409`), active publishing lease (`503`), R2 failure (`503`), pre-existing matching R2 object reuse, and pre-existing mismatched R2 metadata (`409`).

Test a denied `INGEST_RATE_LIMITER` result as `429` and assert that the route performs no Durable Object, R2, or Queue operation after the denial.

For `ChunkGateState`, unit test new reservation, same-hash in-flight status, expired lease takeover, accepted duplicate, different-hash conflict, accept, release, and expiration.

- [ ] **Step 2: Run tests and verify the ingestion modules fail**

Run:

```bash
uv run pytest tests/test_r2.py tests/test_chunk_gate.py tests/test_ingestion_service.py tests/test_ingestion_api.py -q
```

Expected: failures identify missing `R2Store`, `ChunkGateClient`, `ingest_chunk`, and chunk router behavior.

- [ ] **Step 3: Implement deterministic R2, Queue envelopes, and ingestion**

Use these immutable Pydantic envelopes:

```python
class GameplayPointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    r2_key: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: str
    game_id: str
    chunk_id: str
    received_at_ms: int = Field(ge=0)


class IngestAcknowledgment(BaseModel):
    accepted: Literal[True] = True
    duplicate: bool
    game_id: str
    chunk_id: str
    chunk_sequence: int
    accepted_event_range: EventRange
    queued: Literal[True] = True
    server_received_at_ms: int
```

`R2Store.raw_key()` returns exactly:

```python
f"raw/user={user_id}/game={game_id}/chunk={chunk_id}.json"
```

`put_raw()` first calls `head()`. A matching `payload_sha256` is a successful replay; a different hash raises `ObjectConflict`. If absent, call `put()` with `onlyIf={"etagDoesNotMatch": "*"}`, `sha256=<hex digest>`, `httpMetadata={"contentType": "application/json"}`, and string-only custom metadata.

`ChunkGateState` is pure Python and serializable. `ChunkGateClient` addresses the namespace with `sha256(f"{user_id}:{chunk_id}")`, then calls the object's RPC methods `reserve`, `accept`, and `release`.

`ingest_chunk()` performs this exact order:

```python
envelope = parse_chunk_envelope(raw_body, user_id)
digest = hashlib.sha256(raw_body).hexdigest()
reservation = await gate.reserve(user_id, envelope.chunk_id, digest, now_ms)
if reservation.status == "duplicate":
    return IngestAcknowledgment.model_validate(reservation.ack).model_copy(
        update={"duplicate": True}
    )
if reservation.status == "conflict":
    raise ChunkConflict("chunk ID already contains different bytes")
if reservation.status == "publishing":
    raise IngestionUnavailable("chunk is currently publishing; retry later")
try:
    r2_key = await store.put_raw(...)
    await queue.publish(GameplayPointer(...))
    acknowledgment = IngestAcknowledgment(...)
    await gate.accept(digest, acknowledgment.model_dump(mode="json"), now_ms)
    return acknowledgment
except ChunkConflict:
    await gate.release(digest)
    raise
except Exception as error:
    await gate.release(digest)
    raise IngestionUnavailable("event ingestion unavailable") from error
```

The FastAPI route uses a `_read_limited_body()` helper over `request.stream()`. It rejects `Content-Length` above the limit before reading, stops as soon as the accumulated bytes cross the limit, and maps contract, conflict, and availability errors to `400`, `409`, and `503`.

After authentication and before parsing or persistence, the route calls the Cloudflare Rate Limiting binding with `{"key": f"{user_id}:game-event-chunks"}`. A false `success` result returns `429` with `Retry-After: 60`.

Replace the response schema with the approved Cloudflare acknowledgment and disallow additional properties.

- [ ] **Step 4: Run ingestion tests and the whole suite**

Run:

```bash
uv run pytest tests/test_r2.py tests/test_chunk_gate.py tests/test_ingestion_service.py tests/test_ingestion_api.py -q
uv run pytest -q
uv run ruff check src tests scripts
uv run basedpyright src tests
```

Expected: all tests pass and no Kafka offset field remains in runtime acknowledgments.

- [ ] **Step 5: Commit ingestion**

```bash
git add src/retro_backend contracts/ingest-response-v1.schema.json tests
git commit -m "feat: add R2-first gameplay ingestion"
```

## Task 3: Gameplay Queue Consumer And Deterministic Artifacts

**Files:**
- Create: `src/retro_backend/consumers/__init__.py`
- Create: `src/retro_backend/consumers/gameplay.py`
- Create: `src/retro_backend/consumers/dispatch.py`
- Create: `tests/test_gameplay_consumer.py`
- Create: `tests/test_queue_dispatch.py`

- [ ] **Step 1: Write failing consumer and dispatch tests**

```python
async def test_valid_pointer_writes_deterministic_validated_artifact(
    fake_env, fixture_bytes, fixture_payload
):
    fake_env.RAW_BUCKET.seed_raw(pointer.r2_key, fixture_bytes, pointer.payload_sha256)
    await GameplayConsumer(fake_env).process(pointer.model_dump())
    put = fake_env.RAW_BUCKET.put_calls[-1]
    assert put.key == (
        "validated/date=2026-07-17/user=usr_example/"
        "game=game_01/chunk=chk_01.json"
    )
    body = json.loads(put.body)
    assert body["authenticated_user_id"] == "usr_example"
    assert body["payload_sha256"] == pointer.payload_sha256


async def test_semantically_invalid_chunk_writes_rejection_and_acks(fake_batch):
    await dispatch_queue(fake_batch, fake_env)
    assert fake_batch.messages[0].acked is True
    assert fake_batch.messages[0].retried is False
    assert fake_env.RAW_BUCKET.put_calls[-1].key.startswith("rejected/")


async def test_transient_r2_failure_retries_only_failed_message(fake_batch):
    await dispatch_queue(fake_batch, fake_env)
    assert fake_batch.messages[0].acked is True
    assert fake_batch.messages[1].retried is True
```

Also test missing raw object as transient, raw hash mismatch as permanent rejection, pointer validation failure as permanent rejection, authenticated identity mismatch, deterministic replay with matching derived hash, derived-object conflict, and unknown queue name raising a visible error.

- [ ] **Step 2: Run tests and verify consumer symbols are missing**

```bash
uv run pytest tests/test_gameplay_consumer.py tests/test_queue_dispatch.py -q
```

Expected: import or behavior failures for `GameplayConsumer` and `dispatch_queue`.

- [ ] **Step 3: Implement semantic validation and per-message Queue outcomes**

The gameplay consumer:

1. Validates `GameplayPointer`.
2. Reads raw bytes and metadata from R2.
3. Confirms the content hash and authoritative user identity.
4. Runs complete v2 event validation, including event IDs, strict increasing sequence, event count/range, supported event types and visibility, and start-event game metadata.
5. Serializes a canonical validated object with sorted keys and compact separators.
6. Writes it with deterministic R2 conditional semantics.

Use UTC date from the chunk's immutable `created_at_ms` for validated and rejected keys. Rejection artifacts contain pointer identifiers, reason code, reason text, and rejection time, but never the raw request body.

`dispatch_queue()` switches on `batch.queue` and processes messages sequentially. It calls `message.ack()` after success or permanent rejection and `message.retry()` after transient failures, preserving already-acknowledged messages if a later message fails.

- [ ] **Step 4: Verify consumer behavior**

```bash
uv run pytest tests/test_gameplay_consumer.py tests/test_queue_dispatch.py -q
uv run pytest -q
uv run ruff check src tests scripts
uv run basedpyright src tests
```

Expected: all tests and checks pass.

- [ ] **Step 5: Commit consumer**

```bash
git add src/retro_backend/consumers src/retro_backend/contracts.py src/retro_backend/storage/r2.py tests
git commit -m "feat: validate queued gameplay chunks"
```

## Task 4: PostHog Analytics And Stripe Billing (Deferred)

This task is future work and is not part of the active implementation under the 2026-07-17 scope update.

**Files:**
- Create: `src/retro_backend/integrations/__init__.py`
- Create: `src/retro_backend/integrations/posthog.py`
- Create: `src/retro_backend/integrations/stripe.py`
- Create: `src/retro_backend/consumers/analytics.py`
- Create: `src/retro_backend/consumers/billing.py`
- Create: `src/retro_backend/api/stripe.py`
- Modify: `src/retro_backend/consumers/dispatch.py`
- Modify: `src/retro_backend/storage/d1.py`
- Modify: `src/retro_backend/app.py`
- Create: `tests/test_posthog.py`
- Create: `tests/test_stripe.py`
- Create: `tests/test_billing_consumer.py`
- Create: `tests/test_stripe_api.py`

- [ ] **Step 1: Write failing analytics, signature, webhook, and billing tests**

```python
def test_posthog_mapping_is_sanitized_and_deterministic():
    event = AnalyticsEvent(
        id="evt_01",
        type="gamepigeon.chunk.accepted",
        source="retro-api",
        time="2026-07-17T18:00:00Z",
        subject="usr_example/chk_01",
        data={"user_id": "usr_example", "game_type": "2048"},
    )
    payload = to_posthog_payload(event, "ph_project")
    assert payload["api_key"] == "ph_project"
    assert payload["distinct_id"] == "usr_example"
    assert payload["properties"]["$insert_id"] == "evt_01"
    assert "token" not in json.dumps(payload).lower()


def test_stripe_signature_verification_accepts_valid_raw_body():
    body = b'{"id":"evt_stripe_01","type":"customer.subscription.updated"}'
    signature = make_test_signature(body, secret="whsec_test", timestamp=1784241600)
    assert verify_stripe_signature(body, signature, "whsec_test", 1784241600).id == "evt_stripe_01"


def test_stripe_signature_rejects_stale_timestamp():
    with pytest.raises(StripeSignatureError, match="timestamp"):
        verify_stripe_signature(body, signature, "whsec_test", 1784242001)


async def test_verified_webhook_is_queued_and_returns_quickly(app_client, fake_env):
    response = await app_client.post(
        "/v1/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": signature},
    )
    assert response.status_code == 202
    assert fake_env.BILLING_QUEUE.send_calls[0]["event_id"] == "evt_stripe_01"


async def test_replayed_billing_event_is_noop(fake_d1):
    fake_d1.first_rows.append({"event_id": "evt_stripe_01"})
    result = await BillingConsumer(fake_env).process(event)
    assert result.duplicate is True
    assert fake_d1.batch_calls == []
```

Also test unsupported PostHog event rejection, PostHog non-2xx retry, missing analytics key as permanent no-op in development, invalid Stripe header (`400`), invalid JSON after valid signature (`400`), Queue failure (`503` so Stripe retries), subscription created/updated/deleted projections, and one D1 batch containing idempotency insert plus customer upsert.

- [ ] **Step 2: Run tests and verify integrations fail**

```bash
uv run pytest tests/test_posthog.py tests/test_stripe.py tests/test_billing_consumer.py tests/test_stripe_api.py -q
```

Expected: imports or behavior fail for the new integration modules.

- [ ] **Step 3: Implement PostHog and Stripe without SDK runtime dependencies**

`AnalyticsEvent` permits only an explicit event type allowlist and an explicit property allowlist per type. `to_posthog_payload()` sends to `{POSTHOG_HOST}/i/v0/e/` using HTTPX and includes project `api_key`, event name, `distinct_id`, ISO timestamp, and deterministic `$insert_id`.

Stripe verification is manual HMAC-SHA256 over exact bytes:

```python
signed = f"{timestamp}.".encode() + raw_body
expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
if not any(hmac.compare_digest(expected, signature) for signature in v1_values):
    raise StripeSignatureError("signature mismatch")
if abs(now_seconds - timestamp) > tolerance_seconds:
    raise StripeSignatureError("timestamp outside tolerance")
```

The webhook verifies before JSON parsing, publishes only the event ID, event type, creation time, and required subscription/customer fields, and returns `202` only after Queue acceptance.

The billing consumer handles `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. It uses a D1 preflight lookup for fast replay detection, then one `DB.batch()` containing:

```sql
INSERT INTO processed_stripe_events (event_id, event_type) VALUES (?1, ?2)
ON CONFLICT(event_id) DO NOTHING
```

and an upsert or delete-status projection for `stripe_customers`. The event payload must carry `metadata.user_id`; events without a valid `usr_` identity are permanent rejections and are acknowledged.

Extend queue dispatch for `retro-analytics-events` and `retro-billing-events`.

- [ ] **Step 4: Verify integrations and complete suite**

```bash
uv run pytest tests/test_posthog.py tests/test_stripe.py tests/test_billing_consumer.py tests/test_stripe_api.py -q
uv run pytest -q
uv run ruff check src tests scripts
uv run basedpyright src tests
```

Expected: all tests and static checks pass with no network calls in tests.

- [ ] **Step 5: Commit integrations**

```bash
git add src/retro_backend tests
git commit -m "feat: add PostHog and Stripe consumers"
```

## Task 5: Worker Entrypoint, Wrangler Resources, Cleanup, And Smoke Verification

**Files:**
- Create: `src/retro_backend/entry.py`
- Create: `wrangler.toml`
- Replace: `README.md`
- Replace: `Makefile`
- Modify: `.env.example`
- Create: `tests/test_entry_contract.py`
- Delete: `Dockerfile`
- Delete: `docker-compose.yml`
- Delete: `alembic.ini`
- Delete: `migrations/env.py`
- Delete: `migrations/script.py.mako`
- Delete: `migrations/versions/0001_initial_backend.py`
- Delete: `src/retro_backend/config.py`
- Delete: `src/retro_backend/database.py`
- Delete: `src/retro_backend/main.py`
- Delete: `src/retro_backend/models.py`
- Delete: `src/retro_backend/redis_client.py`
- Delete: `src/retro_backend/worker.py`
- Delete: `tests/test_worker.py`

- [ ] **Step 1: Write failing entrypoint and configuration contract tests**

Tests inspect source/config without importing Pyodide-only modules under CPython:

```python
def test_worker_entrypoint_exposes_fetch_queue_and_chunk_gate():
    source = Path("src/retro_backend/entry.py").read_text()
    assert "class Default(WorkerEntrypoint)" in source
    assert "async def fetch" in source
    assert "async def queue" in source
    assert "class ChunkGate(DurableObject)" in Path(
        "src/retro_backend/chunk_gate.py"
    ).read_text()


def test_wrangler_binds_every_runtime_resource():
    config = tomllib.loads(Path("wrangler.toml").read_text())
    assert config["main"] == "src/retro_backend/entry.py"
    assert "python_workers" in config["compatibility_flags"]
    assert config["compatibility_date"] == "2026-07-17"
    assert config["observability"]["enabled"] is True
    assert {item["binding"] for item in config["queues"]["producers"]} == {
        "GAMEPLAY_QUEUE", "ANALYTICS_QUEUE", "BILLING_QUEUE"
    }
```

Also assert D1 binding `DB`, R2 binding `RAW_BUCKET`, Durable Object binding `CHUNK_GATE`, rate-limit binding `INGEST_RATE_LIMITER`, SQLite class export, all three Queue consumers and DLQs, non-secret vars only, no `.ts` or `.js` source files, and absence of PostgreSQL/Redis/Kafka/Docker dependencies and legacy files.

- [ ] **Step 2: Run tests and verify entry/config are absent**

```bash
uv run pytest tests/test_entry_contract.py -q
```

Expected: assertions fail because `entry.py` and `wrangler.toml` do not exist and legacy files remain.

- [ ] **Step 3: Implement Worker entrypoint and Cloudflare configuration**

`entry.py` is exactly the runtime bridge:

```python
import asgi
from workers import WorkerEntrypoint

from retro_backend.app import app
from retro_backend.consumers.dispatch import dispatch_queue


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)

    async def queue(self, batch):
        await dispatch_queue(batch, self.env)
```

The exported `ChunkGate` class remains in `chunk_gate.py` and extends `workers.DurableObject` with `reserve`, `accept`, `release`, and `alarm` RPC methods backed by storage key `receipt`. It persists before returning and calls `deleteAll()` in the alarm.

`wrangler.toml` uses `compatibility_date = "2026-07-17"`, `compatibility_flags = ["python_workers"]`, enables observability, declares D1/R2/Queue/DO bindings, and uses current class exports:

```toml
[exports.ChunkGate]
type = "durable-object"
storage = "sqlite"

[[durable_objects.bindings]]
name = "CHUNK_GATE"
class_name = "ChunkGate"
```

Queue consumers use batch size 10, timeout 5 seconds, five retries, and dedicated DLQs. Config vars include body limits, deduplication TTL, publishing lease, PostHog host, and environment name. Secrets are documented but absent from the file.

Configure `INGEST_RATE_LIMITER` as a native `[[ratelimits]]` binding with namespace `"1001"`, 120 requests per 60-second period. The README notes that the namespace must remain unique within the Cloudflare account.

README commands use only `uv run pywrangler` for Worker development and deployment, plus D1 migration commands. The Makefile exposes `test`, `lint`, `typecheck`, `verify`, `dev`, `deploy`, `migrate-local`, and `migrate-remote` targets.

Delete all listed legacy files after tests for their replacements pass.

- [ ] **Step 4: Read the Wrangler skill and validate the local Worker**

Run:

```bash
uv sync
uv run pywrangler types
uv run pywrangler deploy --dry-run
uv run pytest -q
uv run ruff check src tests scripts
uv run basedpyright src tests
git diff --check
```

Expected: pywrangler generates/validates Python Worker bindings, dry-run packaging succeeds without account mutation, all tests pass, static checks are clean, and the diff has no whitespace errors.

- [ ] **Step 5: Run a local HTTP smoke test**

Start:

```bash
uv run pywrangler dev --local --port 8787
```

Probe:

```bash
curl -fsS http://127.0.0.1:8787/health/live
```

Expected JSON:

```json
{"status":"healthy"}
```

Stop the development server after the probe.

- [ ] **Step 6: Commit deployment-ready rewrite**

```bash
git add -A
git commit -m "feat: deploy Retro on Cloudflare Python Workers"
```

## Final Review

- [ ] Confirm every approved HTTP route exists and is covered by tests.
- [ ] Confirm exact raw bytes reach R2 before Queue acceptance.
- [ ] Confirm every asynchronous path uses per-message ack/retry and deterministic keys.
- [ ] Confirm no secret, raw token, raw Stripe body, or gameplay trajectory reaches logs or PostHog.
- [ ] Confirm D1 contains only product and billing state.
- [ ] Confirm no runtime source file uses TypeScript or JavaScript.
- [ ] Run an independent spec-compliance review and an independent code-quality review.
- [ ] Run `make verify` from a clean working tree and report the exact result.
