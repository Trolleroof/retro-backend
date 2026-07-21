# Retro Multiplayer Backend Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the invite-only independent-board 2048 multiplayer backend in the Python `retro-backend` Cloudflare Worker without deploying or publishing a release.

**Architecture:** FastAPI handles four authenticated JSON routes and calls a room-scoped `GameRoom` Durable Object through RPC. The top-level Worker intercepts the ticketed WebSocket upgrade before ASGI and forwards it to the same object, whose SQLite storage, transactions, alarms, and hibernating sockets remain authoritative. A pure Python reducer implements and validates two independent 2048 boards without Cloudflare runtime dependencies.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, Cloudflare Python Workers, SQLite Durable Objects, hibernating WebSockets, pytest, Ruff, BasedPyright, pywrangler, Wrangler.

**Design:** `docs/superpowers/specs/2026-07-20-multiplayer-backend-port-design.md`

**Compatibility reference:** `tui-gamepigeon` tree `d0eefe28e19f99a31320aa2f8b8dd6e87a3ce9c3`, especially `cloudflare/auth-worker/src/multiplayer/`.

**Execution guardrail:** Do not run `make deploy`, `make migrate-remote`, `make apply-r2-lifecycle`, `make release`, `wrangler deploy`, or any credentialed production smoke command. Local tests, type generation, and Wrangler dry-runs are allowed.

**Scope correction:** Per the user's 2026-07-20 direction, this backend supports only `2048` / `1v1`. It must contain no Connect Four descriptor, command, snapshot, reducer, storage field, route behavior, or test surface.

---

## File Map

- `src/retro_backend/multiplayer/errors.py`: stable room fault codes, messages, HTTP mapping, and RPC envelopes.
- `src/retro_backend/multiplayer/protocol.py`: strict descriptors, commands, snapshots, identifiers, timestamps, frame parsing, and serialization.
- `src/retro_backend/multiplayer/tickets.py`: 32-byte base64url tickets and lowercase SHA-256 digests.
- `src/retro_backend/multiplayer/games/rng.py`: serializable Mulberry32 implementation.
- `src/retro_backend/multiplayer/games/game_2048.py`: independent-board 2048 reducer and stored-state validation.
- `src/retro_backend/multiplayer/storage.py`: room SQLite schema, rows, snapshots, atomic transitions, tickets, and expiry deletion.
- `src/retro_backend/multiplayer/bindings.py`: structural protocols for `GAME_ROOMS`, SQL, context, and WebSockets.
- `src/retro_backend/multiplayer/room.py`: `GameRoom` RPC, WebSocket hibernation handlers, replacement, broadcasts, and alarm.
- `src/retro_backend/multiplayer/client.py`: FastAPI-to-Durable-Object RPC adapter.
- `src/retro_backend/api/rooms.py`: authenticated room routes, origin policy, exact response envelopes, and CORS preflight.
- `src/retro_backend/worker_websocket.py`: socket route classification and pre-ASGI forwarding.
- `src/retro_backend/app.py`: room router registration.
- `src/retro_backend/entry.py`: socket interception and `GameRoom` export.
- `src/retro_backend/worker_http.py`: 4 KiB create-route body cap.
- `wrangler.toml`: `GAME_ROOMS` SQLite binding/export plus `PUBLIC_GAME_URL` and `ALLOWED_GAME_ORIGINS`.
- `scripts/deployment_preflight.py`: fail closed when multiplayer bindings or public variables are absent.
- `README.md`: route/runtime/local-verification documentation with an explicit no-deploy note.

### Task 1: Strict 2048-Only Protocol, Errors, And Tickets

**Files:**
- Create: `src/retro_backend/multiplayer/__init__.py`
- Create: `src/retro_backend/multiplayer/errors.py`
- Create: `src/retro_backend/multiplayer/protocol.py`
- Create: `src/retro_backend/multiplayer/tickets.py`
- Test: `tests/test_multiplayer_protocol.py`
- Test: `tests/test_multiplayer_tickets.py`

- [ ] **Step 1: Write failing protocol and ticket tests**

Cover the exact 2048/1v1 descriptor, forbidden extra keys, canonical UUIDs, nonnegative JavaScript-safe versions, exact command keys, optional move `request_id`, stable parser messages, error envelopes, 43-character tickets, digest-only storage, and `now >= expires_at` boundaries.

```python
def test_parse_move_preserves_valid_request_id() -> None:
    command = parse_client_frame(
        '{"type":"move","direction":"left",'
        '"expected_board_version":3,"request_id":"move_1"}',
        GameDescriptor(game_type="2048", mode="1v1"),
    )
    assert command == MoveCommand(
        direction="left", expected_board_version=3, request_id="move_1"
    )


def test_ticket_is_unpadded_base64url_and_only_digest_is_storable() -> None:
    raw = generate_ticket(lambda size: bytes(range(size)))
    assert raw == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert len(raw) == 43
    assert hash_ticket(raw) == sha256(raw.encode("ascii")).hexdigest()
```

- [ ] **Step 2: Run the focused tests and verify red**

Run: `uv run --frozen pytest -q tests/test_multiplayer_protocol.py tests/test_multiplayer_tickets.py`

Expected: collection fails because `retro_backend.multiplayer` does not exist.

- [ ] **Step 3: Implement strict contracts**

Use frozen strict Pydantic models and one parser boundary. `RoomFault` must retain public fields without leaking internal exceptions.

```python
MAX_SAFE_INTEGER = 9_007_199_254_740_991
REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MoveCommand(StrictModel):
    type: Literal["move"] = "move"
    direction: Literal["up", "down", "left", "right"]
    expected_board_version: Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
    request_id: Annotated[str, Field(pattern=REQUEST_ID_PATTERN)] | None = None


def parse_client_frame(frame: object, descriptor: GameDescriptor) -> ClientCommand:
    if not isinstance(frame, str):
        raise ProtocolFault("Client frames must be text")
    if len(frame.encode("utf-8")) > MAX_CLIENT_FRAME_BYTES:
        raise FrameTooLarge
    try:
        payload = json.loads(frame)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ProtocolFault("Client frame must contain valid JSON") from error
    return validate_command_payload(payload, descriptor)
```

Ticket generation must use injectable entropy for deterministic tests and `secrets.token_bytes` in production.

```python
def generate_ticket(entropy: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return base64.urlsafe_b64encode(entropy(32)).rstrip(b"=").decode("ascii")


def hash_ticket(raw_ticket: str) -> str:
    if TICKET_RE.fullmatch(raw_ticket) is None:
        raise RoomFault("invalid_ticket", "Invalid ticket")
    return hashlib.sha256(raw_ticket.encode("ascii")).hexdigest()
```

- [ ] **Step 4: Run focused tests, lint, and types**

Run: `uv run --frozen pytest -q tests/test_multiplayer_protocol.py tests/test_multiplayer_tickets.py`

Expected: all focused tests pass.

Run: `uv run --frozen ruff check src/retro_backend/multiplayer tests/test_multiplayer_protocol.py tests/test_multiplayer_tickets.py`

Expected: `All checks passed!`

Run: `uv run --frozen basedpyright src/retro_backend/multiplayer tests/test_multiplayer_protocol.py tests/test_multiplayer_tickets.py`

Expected: zero errors.

- [ ] **Step 5: Commit the protocol foundation**

```bash
git add src/retro_backend/multiplayer tests/test_multiplayer_protocol.py tests/test_multiplayer_tickets.py
git commit -m "feat: add multiplayer protocol contracts"
```

### Task 2: Independent-Board 2048 Reducer

**Files:**
- Create: `src/retro_backend/multiplayer/games/__init__.py`
- Create: `src/retro_backend/multiplayer/games/rng.py`
- Create: `src/retro_backend/multiplayer/games/game_2048.py`
- Test: `tests/test_multiplayer_2048.py`

- [ ] **Step 1: Write failing RNG, movement, no-op, and validation tests**

Cover Mulberry32 uint32 wrapping, two-draw spawning, the parity vector, one-merge-per-tile behavior, all directions, board-local versions, opponent independence, no-op conservation, finished-board no-op, both-board completion, score-only winner selection, tile 2048 continuing play, and malformed/unreachable stored state.

```python
def test_pr59_mulberry32_parity_vector() -> None:
    state = Game2048State.from_test_board(
        seat=0,
        board=((2, 2, 0, 0), (4, 0, 4, 0), (0, 0, 0, 0), (0, 0, 0, 0)),
        rng_state=123,
    )
    applied = Game2048Adapter().apply_move(state, seat=0, direction="left")
    assert applied.action_payload["score_delta"] == 12
    assert applied.action_payload["spawned"] == {"row": 3, "column": 1, "value": 2}
    assert applied.state.boards[0].rng_state == 3_663_131_749


def test_noop_changes_nothing_and_consumes_no_rng() -> None:
    state = fixed_two_board_state()
    applied = Game2048Adapter().apply_move(state, seat=0, direction="left")
    assert applied == GameNoop(state=state)
```

- [ ] **Step 2: Run the 2048 tests and verify red**

Run: `uv run --frozen pytest -q tests/test_multiplayer_2048.py`

Expected: collection fails because `game_2048` and `rng` are absent.

- [ ] **Step 3: Implement Mulberry32 and the 2048 reducer**

Persist RNG state after each draw. Empty-cell selection is row-major; value is 2 below 0.9 and 4 otherwise.

```python
UINT32_MASK = 0xFFFF_FFFF
MULBERRY_INCREMENT = 0x6D2B79F5


def next_random(state: int) -> tuple[int, float]:
    next_state = (state + MULBERRY_INCREMENT) & UINT32_MASK
    value = next_state
    value = ((value ^ (value >> 15)) * (value | 1)) & UINT32_MASK
    value ^= (value + (((value ^ (value >> 7)) * (value | 61)) & UINT32_MASK)) & UINT32_MASK
    value ^= value >> 14
    return next_state, (value & UINT32_MASK) / 4_294_967_296
```

Move only the actor's board. Check its expected version before detecting a no-op. A changed move merges, scores, spawns, advances only that RNG, increments only that board version, then marks the board finished only when no legal direction remains.

```python
@dataclass(frozen=True, slots=True)
class MoveNoop:
    state: Game2048State


@dataclass(frozen=True, slots=True)
class MoveError:
    code: Literal["stale_board_version", "match_not_active"]
    message: str
    current_board_version: int | None = None


if expected_board_version != board.version:
    return MoveError(
        "stale_board_version",
        "Board version is stale",
        current_board_version=board.version,
    )
moved_grid, score_delta = slide_and_merge(board.grid, direction)
if moved_grid == board.grid or board.finished:
    return MoveNoop(state=state)
spawned_grid, spawned, rng_state = spawn_tile(moved_grid, board.rng_state)
```

The reducer supports only `2048` / `1v1`; no registry or second game adapter is
introduced.

- [ ] **Step 4: Run focused tests, lint, and types**

Run: `uv run --frozen pytest -q tests/test_multiplayer_2048.py tests/test_multiplayer_protocol.py`

Expected: all 2048 reducer and protocol tests pass.

Run: `uv run --frozen ruff check src/retro_backend/multiplayer/games tests/test_multiplayer_2048.py`

Expected: `All checks passed!`

Run: `uv run --frozen basedpyright src/retro_backend/multiplayer/games tests/test_multiplayer_2048.py`

Expected: zero errors.

- [ ] **Step 5: Commit the 2048 reducer**

```bash
git add src/retro_backend/multiplayer/games tests/test_multiplayer_2048.py
git commit -m "feat: add independent board 2048 reducer"
```

### Task 3: Room SQLite State And Atomic Lifecycle

**Files:**
- Create: `src/retro_backend/multiplayer/bindings.py`
- Create: `src/retro_backend/multiplayer/storage.py`
- Test: `tests/multiplayer_fakes.py`
- Test: `tests/test_multiplayer_storage.py`

- [ ] **Step 1: Write failing schema, lifecycle, ticket, and expiry tests**

Build a synchronous fake SQL/storage boundary that records statements, fully consumed cursors, `transactionSync` commits/rollbacks, alarms, and `deleteAll`. Cover schema idempotency, creator seat zero, lowest-free-seat joins, idempotent rejoin, capacity, first-time and repeated ready, activation only when full and ready, action/version ordering, ticket issue/consume/expiry, GET wall-clock behavior, command wall-clock rejection, completed retention, and repeated expiration.

```python
def test_final_ready_activates_and_commits_state_action_together(fake_store: FakeRoomStore) -> None:
    room = seeded_2048_room(fake_store, ready_seats=(0,))
    result = room.mark_ready("user-1", expected_version=1, now_ms=1_000)
    assert result.snapshot.status == "active"
    assert result.snapshot.version == 2
    assert fake_store.transaction_events[-3:] == [
        "participant-ready", "room-updated", "action-inserted"
    ]


def test_transaction_rolls_back_room_when_action_insert_fails(fake_store: FakeRoomStore) -> None:
    fake_store.fail_next_action_insert = True
    before = fake_store.snapshot()
    with pytest.raises(StorageFault):
        fake_store.room().mark_ready("user-0", expected_version=0, now_ms=1_000)
    assert fake_store.snapshot() == before
```

- [ ] **Step 2: Run storage tests and verify red**

Run: `uv run --frozen pytest -q tests/test_multiplayer_storage.py`

Expected: collection fails because the room store is absent.

- [ ] **Step 3: Implement protocols, schema, and synchronous transactions**

Define dedicated structural protocols without changing the existing `ChunkGate` protocol types. Use the exact four tables from the design. Consume every SQL cursor before an `await`.

```python
ROOM_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS room (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        room_id TEXT NOT NULL UNIQUE,
        game_type TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        version INTEGER NOT NULL,
        state_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS participants (
        user_id TEXT PRIMARY KEY,
        seat INTEGER NOT NULL UNIQUE CHECK (seat BETWEEN 0 AND 1),
        ready INTEGER NOT NULL DEFAULT 0,
        joined_at INTEGER NOT NULL,
        disconnected_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS actions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        seat INTEGER NOT NULL,
        action_type TEXT NOT NULL,
        action_json TEXT NOT NULL,
        accepted_at INTEGER NOT NULL,
        resulting_version INTEGER NOT NULL UNIQUE
    )""",
)
```

Every readiness/game mutation must run inside `ctx.storage.transactionSync`. The callback is synchronous and includes participant updates, room state, action insertion, and the snapshot read.

```python
def execute_command(self, identity: SocketIdentity, command: ClientCommand, now_ms: int) -> CommandResult:
    def transition() -> CommandResult:
        room = self.require_room()
        participant = self.require_participant(identity)
        return self._apply_and_persist(room, participant, command, now_ms)

    return cast(CommandResult, self._storage.transactionSync(transition))
```

Use 30-minute waiting/active expiry, 24-hour completed retention, and 60-second tickets. Log first-time `ready` and legal `move` actions at their unique resulting global versions. Repeated ready broadcasts unchanged state; a 2048 no-op is actor-only and performs no SQL write.

- [ ] **Step 4: Run focused tests, lint, and types**

Run: `uv run --frozen pytest -q tests/test_multiplayer_storage.py tests/test_multiplayer_2048.py`

Expected: all storage and adapter tests pass.

Run: `uv run --frozen ruff check src/retro_backend/multiplayer tests/multiplayer_fakes.py tests/test_multiplayer_storage.py`

Expected: `All checks passed!`

Run: `uv run --frozen basedpyright src/retro_backend/multiplayer tests/multiplayer_fakes.py tests/test_multiplayer_storage.py`

Expected: zero errors.

- [ ] **Step 5: Commit room persistence**

```bash
git add src/retro_backend/multiplayer/bindings.py src/retro_backend/multiplayer/storage.py tests/multiplayer_fakes.py tests/test_multiplayer_storage.py
git commit -m "feat: persist multiplayer room lifecycle"
```

### Task 4: GameRoom RPC, Hibernating WebSockets, And Alarms

**Files:**
- Create: `src/retro_backend/multiplayer/room.py`
- Test: `tests/test_multiplayer_room.py`
- Modify: `tests/multiplayer_fakes.py`

- [ ] **Step 1: Write failing RPC and socket-handler tests**

Cover create/get/join/ticket RPC envelopes, generic internal failure mapping, ticket-only upgrade, one-use consume, initial snapshot, attachment JSON, replacement close, presence broadcasts, hibernated attachment restoration, persist-before-send ordering, actor-only errors/no-ops, correlated broadcasts, invalid-frame counter reset, third-invalid close, oversize close, transport failure, alarm rescheduling, final expired snapshot, stable close code, and idempotent deletion.

```python
@pytest.mark.asyncio
async def test_legal_move_persists_before_broadcast(fake_room: GameRoomHarness) -> None:
    socket = fake_room.connected_socket("user-0", seat=0)
    await fake_room.room.webSocketMessage(
        socket,
        '{"type":"move","direction":"left",'
        '"expected_board_version":0,"request_id":"m1"}',
    )
    assert fake_room.events.index("sql-commit") < fake_room.events.index("socket-send")
    assert all(message["request_id"] == "m1" for message in fake_room.broadcast_messages())


@pytest.mark.asyncio
async def test_third_invalid_frame_closes_policy_violation(fake_room: GameRoomHarness) -> None:
    socket = fake_room.connected_socket("user-0", seat=0)
    for _ in range(3):
        await fake_room.room.webSocketMessage(socket, "not-json")
    assert socket.closed_with == (1008, "policy violation")
```

- [ ] **Step 2: Run room tests and verify red**

Run: `uv run --frozen pytest -q tests/test_multiplayer_room.py`

Expected: collection fails because `GameRoom` is absent.

- [ ] **Step 3: Implement the runtime boundary with an emscripten branch**

Keep `workers` and `js` imports behind the repository's established platform guard so CPython can collect tests.

```python
if sys.platform == "emscripten":
    from js import WebSocketPair
    from workers import DurableObject, Response
else:
    WebSocketPair = None

    class DurableObject:
        def __init__(self, ctx: RoomContext, env: object) -> None:
            self.ctx = ctx
            self.env = env
```

Expose RPC methods that always return structured `{ok,value}` or `{ok,error}` dictionaries. Create room initialization synchronously and schedule its alarm after persistence.

```python
async def create_room_rpc(self, payload: object) -> dict[str, object]:
    try:
        snapshot = self.store.create_room(CreateRoomInput.model_validate(payload))
        await self.ctx.storage.setAlarm(epoch_ms(snapshot.expires_at))
        return rpc_success(snapshot.model_dump(mode="json"))
    except RoomFault as fault:
        return rpc_failure(fault)
    except Exception:
        return rpc_failure(RoomFault.service_unavailable())
```

The upgrade path must create `WebSocketPair.new().object_values()`, consume the ticket in `transactionSync`, call `ctx.acceptWebSocket(server)`, serialize a compact JSON attachment, send the initial snapshot, close the prior socket for that user with `4001`, then broadcast updated presence.

Handlers must be named exactly `webSocketMessage`, `webSocketClose`, and `webSocketError`. Use `ctx.getWebSockets()` on every event; attachments and SQLite are the only reconstruction sources. Alarm expiry sends the final snapshot, closes sockets with `4000`, and calls `deleteAll()` only after delivery attempts.

- [ ] **Step 4: Run focused tests, lint, and types**

Run: `uv run --frozen pytest -q tests/test_multiplayer_room.py tests/test_multiplayer_storage.py`

Expected: all room boundary tests pass.

Run: `uv run --frozen ruff check src/retro_backend/multiplayer tests/test_multiplayer_room.py tests/multiplayer_fakes.py`

Expected: `All checks passed!`

Run: `uv run --frozen basedpyright src/retro_backend/multiplayer tests/test_multiplayer_room.py tests/multiplayer_fakes.py`

Expected: zero errors.

- [ ] **Step 5: Commit the Durable Object**

```bash
git add src/retro_backend/multiplayer/room.py tests/test_multiplayer_room.py tests/multiplayer_fakes.py
git commit -m "feat: add multiplayer durable object"
```

### Task 5: Authenticated HTTP Routes And Pre-ASGI Socket Routing

**Files:**
- Create: `src/retro_backend/multiplayer/client.py`
- Create: `src/retro_backend/api/rooms.py`
- Create: `src/retro_backend/worker_websocket.py`
- Modify: `src/retro_backend/app.py`
- Modify: `src/retro_backend/entry.py`
- Modify: `src/retro_backend/worker_http.py`
- Test: `tests/test_rooms_api.py`
- Test: `tests/test_worker_websocket.py`
- Modify: `tests/test_worker_http.py`
- Modify: `tests/test_entry_contract.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write failing API, ordering, CORS, and routing tests**

Cover all five routes, exact methods, OPTIONS preflight, exact create JSON/content type/body cap, canonical UUIDs, neighboring paths, pathname/query caps, exact ticket query, originless native calls, canonical allowlist origins, `Vary: Origin`, origin rejection before auth, auth before room allocation, participant-only GET/ticket, RPC failure mapping, missing upgrade 426, socket forwarding before ASGI, and existing ingestion routes remaining unchanged.

```python
@pytest.mark.asyncio
async def test_origin_rejects_before_auth_or_room_allocation(room_client: RoomAppHarness) -> None:
    response = await room_client.post(
        "/v1/rooms",
        headers={"Origin": "https://evil.example", "Authorization": "Bearer secret"},
        json={"game_type": "2048", "mode": "1v1"},
    )
    assert response.status_code == 403
    assert response.json() == {"error": {"code": "unauthorized", "message": "Unauthorized"}}
    assert room_client.auth_lookups == []
    assert room_client.room_allocations == []


@pytest.mark.asyncio
async def test_socket_bypasses_asgi(worker_harness: WorkerHarness) -> None:
    response = await worker_harness.fetch(
        "GET",
        f"/v1/rooms/{ROOM_ID}/socket?ticket={VALID_TICKET}",
        headers={"Upgrade": "websocket"},
    )
    assert response is worker_harness.room_stub.response
    assert worker_harness.asgi_requests == []
```

- [ ] **Step 2: Run route tests and verify red**

Run: `uv run --frozen pytest -q tests/test_rooms_api.py tests/test_worker_websocket.py tests/test_worker_http.py tests/test_entry_contract.py`

Expected: failures show absent room router, socket classifier, and `GameRoom` export.

- [ ] **Step 3: Implement the RPC client and exact FastAPI routes**

Use the existing `require_user_id(request)` function inside each route only after origin, path, method, and body validation. Resolve the namespace through `request.scope["env"].GAME_ROOMS.getByName(room_id)`.

```python
@router.post("", status_code=201)
async def create_room(request: Request) -> JSONResponse:
    origin = validate_game_origin(request)
    body = await parse_exact_create_body(request)
    user_id = await require_user_id(request)
    room_id = str(uuid4())
    client = RoomClient.from_request(request, room_id)
    snapshot = await client.create(room_id, user_id, body)
    return game_json_response(
        {
            "room_id": room_id,
            "invite_url": invite_url(request, room_id),
            "expires_at": snapshot["expires_at"],
            "snapshot": snapshot,
        },
        status_code=201,
        origin=origin,
    )
```

Return only stable public envelopes. Convert binding/RPC exceptions to `503 service_unavailable`; never include exception text.

- [ ] **Step 4: Implement direct socket forwarding and entry integration**

Classify `/v1/rooms/{canonical_uuid}/socket` before `fetch_with_body_limit`. Reject disallowed origins and malformed queries before `getByName`.

```python
class Default(WorkerEntrypoint):
    async def fetch(self, request: object) -> object:
        environment = _environment_with_auth_cache(self.env)
        socket_response = await handle_room_socket(request, environment)
        if socket_response is not None:
            return socket_response
        return await fetch_with_body_limit(
            app, request, environment, request_factory=_rebuild_worker_request
        )
```

Import and export `GameRoom` beside `ChunkGate`, and set `__all__ = ["ChunkGate", "GameRoom", "Default"]`. Add the `/v1/rooms` collection path to `_body_limit` with a hard 4 KiB cap while preserving existing chunk behavior.

- [ ] **Step 5: Run focused and regression tests**

Run: `uv run --frozen pytest -q tests/test_rooms_api.py tests/test_worker_websocket.py tests/test_worker_http.py tests/test_entry_contract.py tests/test_ingestion_api.py tests/test_results_api.py`

Expected: all route, entry, and existing API regression tests pass.

Run: `uv run --frozen ruff check src/retro_backend tests/test_rooms_api.py tests/test_worker_websocket.py tests/test_worker_http.py tests/test_entry_contract.py`

Expected: `All checks passed!`

Run: `uv run --frozen basedpyright src/retro_backend tests/test_rooms_api.py tests/test_worker_websocket.py tests/test_worker_http.py tests/test_entry_contract.py`

Expected: zero errors.

- [ ] **Step 6: Commit HTTP and socket routing**

```bash
git add src/retro_backend/api/rooms.py src/retro_backend/multiplayer/client.py src/retro_backend/worker_websocket.py src/retro_backend/app.py src/retro_backend/entry.py src/retro_backend/worker_http.py tests/conftest.py tests/test_rooms_api.py tests/test_worker_websocket.py tests/test_worker_http.py tests/test_entry_contract.py
git commit -m "feat: expose multiplayer room routes"
```

### Task 6: Cloudflare Configuration, Preflight, And Documentation

**Files:**
- Modify: `wrangler.toml`
- Modify: `scripts/deployment_preflight.py`
- Modify: `tests/test_deployment_preflight.py`
- Modify: `tests/test_entry_contract.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing configuration and preflight tests**

Require one new binding/export without legacy migration blocks, validate both public variables, preserve existing D1/R2/Queue/ChunkGate/rate-limit configuration, and ensure no TypeScript or JavaScript source is introduced.

```python
def test_wrangler_declares_sqlite_game_room_export() -> None:
    config = _wrangler_config()
    assert {"name": "GAME_ROOMS", "class_name": "GameRoom"} in config[
        "durable_objects"
    ]["bindings"]
    assert config["exports"]["GameRoom"] == {
        "type": "durable-object",
        "storage": "sqlite",
    }
    assert "migrations" not in config


def test_preflight_requires_multiplayer_public_configuration(valid_env: dict[str, str]) -> None:
    valid_env.pop("PUBLIC_GAME_URL")
    result = run_preflight(valid_env)
    assert result.returncode == 1
    assert "PUBLIC_GAME_URL" in result.stderr
```

- [ ] **Step 2: Run config tests and verify red**

Run: `uv run --frozen pytest -q tests/test_entry_contract.py tests/test_deployment_preflight.py`

Expected: assertions fail because `GAME_ROOMS`, `GameRoom`, `PUBLIC_GAME_URL`, and `ALLOWED_GAME_ORIGINS` are not configured.

- [ ] **Step 3: Add declarative binding/export and validated variables**

```toml
[vars]
PUBLIC_GAME_URL = "https://gamepigeon.example"
ALLOWED_GAME_ORIGINS = "https://gamepigeon.example"

[[durable_objects.bindings]]
name = "GAME_ROOMS"
class_name = "GameRoom"

[exports.GameRoom]
type = "durable-object"
storage = "sqlite"
```

Validate `PUBLIC_GAME_URL` as one credential-free canonical HTTP(S) URL and every comma-separated `ALLOWED_GAME_ORIGINS` value as a canonical origin. Preflight must also inspect the parsed Wrangler binding/export contract, not just environment values.

- [ ] **Step 4: Document only implemented behavior and local commands**

Add route tables, the ticketed socket flow, game descriptors, the independent-board 2048 winner rule, and this exact delivery boundary:

```text
This repository contains the multiplayer backend implementation, but this change
does not deploy it, create remote Durable Object namespaces, run remote migrations,
or publish a release.
```

Document only `make verify`, `make worker-sync`, `make worker-types`, and `make worker-dry-run` for this branch. Keep live deployment commands in their existing guarded production section.

- [ ] **Step 5: Run config tests and the complete local suite**

Run: `uv run --frozen pytest -q tests/test_entry_contract.py tests/test_deployment_preflight.py`

Expected: all configuration tests pass.

Run: `make verify`

Expected: complete pytest suite, Ruff, BasedPyright, and `git diff --check` pass.

- [ ] **Step 6: Commit configuration and docs**

```bash
git add wrangler.toml scripts/deployment_preflight.py tests/test_deployment_preflight.py tests/test_entry_contract.py README.md docs/superpowers/specs/2026-07-20-multiplayer-backend-port-design.md
git commit -m "docs: configure multiplayer worker runtime"
```

### Task 7: Final Review, Rebase, And Non-Release Verification

**Files:**
- Review: every file changed on `codex/multiplayer-backend`

- [ ] **Step 1: Run contract-parity review against PR #59**

Compare the Python tests and public fields against these source paths at tree `d0eefe2`:

```text
cloudflare/auth-worker/src/multiplayer/routes.ts
cloudflare/auth-worker/src/multiplayer/protocol.ts
cloudflare/auth-worker/src/multiplayer/room.ts
cloudflare/auth-worker/src/multiplayer/games/g2048-adapter.ts
src/core/rng.ts
```

Record and fix every mismatch in routes, status codes, snapshots, commands, version increments, expiration, RNG, ticket use, close codes, or delivery scope. Do not port Better Auth or TypeScript runtime structure.

- [ ] **Step 2: Run complete verification before rebasing**

Run: `make verify`

Expected: all tests pass, Ruff is clean, BasedPyright reports zero errors, and `git diff --check` is clean.

Run: `make worker-sync`

Expected: the frozen pywrangler environment is synchronized.

Run: `make worker-types`

Expected: Workers type generation succeeds under the repository-pinned Node toolchain.

Run: `make worker-dry-run`

Expected: Wrangler accepts the Python Worker, both Durable Object exports, and all bindings without deployment.

- [ ] **Step 3: Fetch and rebase on the latest retro-backend main**

```bash
git fetch origin main
git rebase origin/main
```

Expected: the feature branch is based on the latest fetched `origin/main`. Resolve conflicts by preserving upstream changes and reapplying only this multiplayer port.

- [ ] **Step 4: Rerun verification after the rebase**

Run: `make verify`

Expected: the complete suite remains green after rebase.

Run: `make worker-types`

Expected: type generation remains green.

Run: `make worker-dry-run`

Expected: the dry-run remains green and performs no deployment.

- [ ] **Step 5: Audit git and remote side effects**

Run: `git status --short --branch`

Expected: `codex/multiplayer-backend` is clean and reports its relationship to `origin/main`.

Run: `git log --oneline --decorate origin/main..HEAD`

Expected: only scoped design, protocol, game, room, route, configuration, and documentation commits appear.

Confirm from the command history that no deploy, release, remote migration, remote smoke, or `tui-gamepigeon` revert command ran.
