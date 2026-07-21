from __future__ import annotations

import ast
import json
import sys
from array import array
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Never, cast

import pytest
from multiplayer_fakes import (  # pyright: ignore[reportImplicitRelativeImport]
    FakeHeaders,
    FakeRequest,
    FakeRoomContext,
    FakeRoomStorage,
    FakeWebSocket,
    FakeWebSocketPair,
)

import retro_backend.multiplayer.room as room_module
from retro_backend.multiplayer.errors import PUBLIC_ERROR_MESSAGES, RoomErrorCode
from retro_backend.multiplayer.games.game_2048 import (
    Board,
    Game2048Adapter,
    Game2048State,
    Player2048State,
    highest_tile,
)
from retro_backend.multiplayer.protocol import GameDescriptor, ReadyCommand
from retro_backend.multiplayer.room import GameRoom, Response
from retro_backend.multiplayer.storage import (
    ACTIVE_ROOM_TTL_MS,
    WAITING_ROOM_TTL_MS,
    RoomStore,
    SocketIdentity,
)

ROOM_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_ROOM_ID = "223e4567-e89b-12d3-a456-426614174001"
CREATOR = "user-0"
GUEST = "user-1"
OUTSIDER = "user-2"
DESCRIPTOR = GameDescriptor(game_type="2048", mode="1v1")
ROOM_PATH = Path(__file__).parents[1] / "src" / "retro_backend" / "multiplayer" / "room.py"
READ_ROOM_SQL = (
    "SELECT room_id, game_type, mode, status, created_by, created_at, "
    "expires_at, version, state_json FROM room WHERE singleton = 1"
)


@dataclass(slots=True)
class MillisecondClock:
    value: int = 0

    def __call__(self) -> int:
        return self.value


class EntropySequence:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, size: int) -> bytes:
        offset = self.calls
        self.calls += 1
        return bytes((offset + index) % 256 for index in range(size))


class Uint32Sequence:
    def __init__(self, values: tuple[int, ...] = (1, 2, 3, 4)) -> None:
        self._values: Iterator[int] = iter(values)

    def __call__(self) -> int:
        return next(self._values)


class GameRoomHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.storage = FakeRoomStorage()
        self.context = FakeRoomContext(self.storage)
        self.pairs = FakeWebSocketPair(self.storage.events)
        monkeypatch.setattr(room_module, "WebSocketPair", self.pairs)
        self.clock = MillisecondClock()
        self.room = self._new_room()
        self.storage.reset_observations()

    def _new_room(self) -> GameRoom:
        room = GameRoom(self.context, SimpleNamespace())
        room.store = RoomStore(
            self.storage,
            entropy=EntropySequence(),
            random_uint32=Uint32Sequence(),
        )
        room.clock_ms = self.clock
        return room

    def hibernate(self) -> GameRoom:
        self.room = self._new_room()
        return self.room

    @staticmethod
    def create_payload(**changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "room_id": ROOM_ID,
            "created_by": CREATOR,
            "now_ms": 0,
            "game": {"game_type": "2048", "mode": "1v1"},
        }
        payload.update(changes)
        return payload

    def create_direct(self, *, now_ms: int = 0) -> None:
        self.room.store.create_room(
            room_id=ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=CREATOR,
            now_ms=now_ms,
        )

    def active_direct(self) -> None:
        self.create_direct()
        self.room.store.join(GUEST, 100)
        self.room.store.execute_command(
            SocketIdentity(CREATOR, 0),
            ReadyCommand(expected_version=0),
            200,
        )
        self.room.store.execute_command(
            SocketIdentity(GUEST, 1),
            ReadyCommand(expected_version=1),
            300,
        )
        self.clock.value = 400

    def connected_socket(
        self,
        user_id: str,
        seat: int,
        *,
        name: str | None = None,
        invalid_frame_count: int = 0,
        attachment: str | None = None,
        ready_state: int = 1,
    ) -> FakeWebSocket:
        socket = FakeWebSocket(
            name or f"{user_id}-{len(self.context.sockets)}",
            self.storage.events,
            ready_state=ready_state,
        )
        socket.seed_attachment(
            attachment
            or json.dumps(
                {
                    "user_id": user_id,
                    "seat": seat,
                    "invalid_frame_count": invalid_frame_count,
                },
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        self.context.add_socket(socket)
        return socket

    def issue_ticket(self, user_id: str, now_ms: int = 1_000) -> str:
        return self.room.store.issue_ticket(user_id, now_ms).ticket

    async def fetch_ticket(self, ticket: str) -> Response:
        self.clock.value = 1_001
        return await self.room.fetch(
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={ticket}",
                headers=FakeHeaders({"Upgrade": "websocket"}),
            )
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> GameRoomHarness:
    return GameRoomHarness(monkeypatch)


def _json_body(response: Response) -> dict[str, object]:
    body = response.body
    assert isinstance(body, str)
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def _messages(socket: FakeWebSocket) -> list[dict[str, object]]:
    return [cast(dict[str, object], json.loads(payload)) for payload in socket.sent]


def _participants(message: Mapping[str, object]) -> list[dict[str, object]]:
    snapshot = cast(Mapping[str, object], message["snapshot"])
    return cast(list[dict[str, object]], snapshot["participants"])


def _participant(message: Mapping[str, object], user_id: str) -> dict[str, object]:
    return next(item for item in _participants(message) if item["user_id"] == user_id)


def _row(storage: FakeRoomStorage, table: str) -> dict[str, object]:
    rows = storage.rows(table)
    assert len(rows) == 1
    return rows[0]


def _fresh_context_socket(
    harness: GameRoomHarness,
    socket: FakeWebSocket,
) -> FakeWebSocket:
    return next(
        candidate
        for candidate in harness.context.getWebSockets()
        if candidate.js_id == socket.js_id
    )


def _set_actor_noop_board(harness: GameRoomHarness) -> None:
    room_row = _row(harness.storage, "room")
    state = Game2048Adapter().decode_state(
        json.loads(cast(str, room_row["state_json"])),
        "active",
    )
    assert state.boards is not None
    board: Board = (
        (2, 4, 0, 0),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
    )
    actor = Player2048State(
        seat=0,
        board=board,
        score=0,
        highest_tile=highest_tile(board),
        board_version=0,
        finished=False,
        rng_state=state.boards[0].rng_state,
    )
    replacement = Game2048State(
        boards=(actor, state.boards[1]),
        winner_seat=None,
        draw=False,
    )
    harness.storage.execute(
        "UPDATE room SET state_json = ? WHERE singleton = 1",
        json.dumps(replacement.to_payload(), separators=(",", ":"), allow_nan=False),
    )


def test_cpython_import_uses_explicit_emscripten_runtime_guard() -> None:
    assert sys.platform != "emscripten"
    tree = ast.parse(ROOM_PATH.read_text())
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

    assert runtime_imports == {
        ("js", "WebSocketPair"),
        ("workers", "DurableObject"),
        ("workers", "Response"),
    }
    fallback_classes = {
        node.name for node in platform_branch.orelse if isinstance(node, ast.ClassDef)
    }
    assert {"DurableObject", "Response"} <= fallback_classes
    assert not any(
        isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.ImportFrom) and child.module in {"js", "workers"}
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )
    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("pyodide")
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("pyodide") for alias in node.names)
        )
        for node in ast.walk(tree)
    )


async def test_create_rpc_is_exact_json_safe_and_schedules_after_commit(
    harness: GameRoomHarness,
) -> None:
    result = await harness.room.create_room_rpc(harness.create_payload())

    assert result["ok"] is True
    value = cast(dict[str, object], result["value"])
    assert value["room_id"] == ROOM_ID
    assert value["game_type"] == "2048"
    assert value["mode"] == "1v1"
    assert value["created_at"] == "1970-01-01T00:00:00.000Z"
    assert value["expires_at"] == "1970-01-01T00:30:00.000Z"
    assert harness.storage.events.index("transaction:COMMIT") < harness.storage.events.index(
        f"alarm:set:{WAITING_ROOM_TTL_MS}"
    )
    assert harness.storage.alarm_at_ms == WAITING_ROOM_TTL_MS

    harness.storage.reset_observations()
    repeated = await harness.room.create_room_rpc(harness.create_payload())
    assert repeated == result
    assert harness.storage.alarm_at_ms == WAITING_ROOM_TTL_MS
    assert harness.storage.events[-1] == f"alarm:set:{WAITING_ROOM_TTL_MS}"


@pytest.mark.parametrize(
    ("retained_alarm", "expected_set_calls"),
    [
        (None, [1_001 + WAITING_ROOM_TTL_MS]),
        (999, [1_001 + WAITING_ROOM_TTL_MS]),
        (1_001 + WAITING_ROOM_TTL_MS, []),
    ],
    ids=["missing", "stale", "matching"],
)
def test_constructor_reconciles_retained_live_room_alarm(
    monkeypatch: pytest.MonkeyPatch,
    retained_alarm: int | None,
    expected_set_calls: list[int],
) -> None:
    storage = FakeRoomStorage()
    store = RoomStore(storage)
    store.initialize_schema()
    store.create_room(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=1_001,
    )
    storage.alarm_at_ms = retained_alarm
    storage.reset_observations()
    context = FakeRoomContext(storage)
    monkeypatch.setattr(room_module, "_wall_clock_ms", lambda: 2_000)

    GameRoom(context, SimpleNamespace())

    assert len(context.block_concurrency_callbacks) == 1
    assert [
        scheduled
        for event, scheduled in storage.alarm_events
        if event == "setAlarm"
    ] == expected_set_calls


@pytest.mark.parametrize("room_state", ["waiting", "active"])
def test_constructor_restores_due_retained_room_alarm(
    monkeypatch: pytest.MonkeyPatch,
    room_state: str,
) -> None:
    storage = FakeRoomStorage()
    store = RoomStore(storage)
    store.initialize_schema()
    store.create_room(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=0,
    )
    deadline = WAITING_ROOM_TTL_MS
    if room_state == "active":
        store.join(GUEST, 100)
        store.execute_command(
            SocketIdentity(CREATOR, 0),
            ReadyCommand(expected_version=0),
            200,
        )
        activated = store.execute_command(
            SocketIdentity(GUEST, 1),
            ReadyCommand(expected_version=1),
            300,
        )
        assert activated.snapshot.status == "active"
        deadline = 300 + ACTIVE_ROOM_TTL_MS

    storage.alarm_at_ms = None
    storage.reset_observations()
    context = FakeRoomContext(storage)
    monkeypatch.setattr(room_module, "_wall_clock_ms", lambda: deadline)

    GameRoom(context, SimpleNamespace())

    assert storage.alarm_events == [
        ("getAlarm", None),
        ("setAlarm", deadline),
    ]


@pytest.mark.parametrize("room_state", ["absent", "expired"])
def test_constructor_does_not_reconcile_alarm_without_a_live_room(
    monkeypatch: pytest.MonkeyPatch,
    room_state: str,
) -> None:
    storage = FakeRoomStorage()
    store = RoomStore(storage)
    store.initialize_schema()
    if room_state == "expired":
        store.create_room(
            room_id=ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=CREATOR,
            now_ms=1_001,
        )
        store.expire(1_001 + WAITING_ROOM_TTL_MS)
    storage.reset_observations()
    context = FakeRoomContext(storage)
    monkeypatch.setattr(
        room_module,
        "_wall_clock_ms",
        lambda: 1_001 + WAITING_ROOM_TTL_MS,
    )

    GameRoom(context, SimpleNamespace())

    assert len(context.block_concurrency_callbacks) == 1
    assert "alarm:get" not in storage.events
    assert not any(event.startswith("alarm:set:") for event in storage.events)


@pytest.mark.parametrize(
    "payload",
    [
        GameRoomHarness.create_payload(extra=True),
        GameRoomHarness.create_payload(roomId=ROOM_ID),
        GameRoomHarness.create_payload(now_ms=True),
        GameRoomHarness.create_payload(game={"game_type": "2048", "mode": "1v1", "x": 1}),
        GameRoomHarness.create_payload(game={"game_type": "2048", "mode": "solo"}),
        GameRoomHarness.create_payload(game={"game_type": 2048, "mode": "1v1"}),
    ],
)
async def test_create_rpc_rejects_nonexact_input_without_allocating_room(
    harness: GameRoomHarness,
    payload: dict[str, object],
) -> None:
    result = await harness.room.create_room_rpc(payload)

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": PUBLIC_ERROR_MESSAGES["invalid_request"],
        },
    }
    assert harness.storage.rows("room") == []
    assert harness.storage.alarm_at_ms is None


async def test_new_create_alarm_failure_compensates_then_explicit_retry_recovers(
    harness: GameRoomHarness,
) -> None:
    harness.storage.fail_next_set_alarm = True

    failed = await harness.room.create_room_rpc(harness.create_payload())

    assert failed == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": PUBLIC_ERROR_MESSAGES["service_unavailable"],
        },
    }
    commit = harness.storage.events.index("transaction:COMMIT")
    alarm = harness.storage.events.index(f"alarm:set:{WAITING_ROOM_TTL_MS}")
    delete = harness.storage.events.index("storage:deleteAll")
    assert commit < alarm < delete
    assert harness.storage.table_names() == set()
    assert harness.storage.delete_all_count == 1
    assert harness.storage.alarm_at_ms is None

    recovered = await harness.room.create_room_rpc(harness.create_payload())
    assert recovered["ok"] is True
    assert _row(harness.storage, "room")["room_id"] == ROOM_ID
    assert harness.storage.rows("participants")[0]["user_id"] == CREATOR
    assert harness.storage.rows("tickets") == []
    assert harness.storage.rows("actions") == []
    assert harness.storage.alarm_at_ms == WAITING_ROOM_TTL_MS


async def test_idempotent_create_alarm_failure_preserves_live_room_and_prior_alarm(
    harness: GameRoomHarness,
) -> None:
    created = await harness.room.create_room_rpc(harness.create_payload())
    assert created["ok"] is True
    harness.room.store.join(GUEST, 100)
    harness.room.store.execute_command(
        SocketIdentity(CREATOR, 0),
        ReadyCommand(expected_version=0),
        150,
    )
    harness.room.store.issue_ticket(CREATOR, 200)
    before = harness.storage.database_snapshot()
    alarm_before = harness.storage.alarm_at_ms
    harness.storage.reset_observations()
    harness.storage.fail_next_set_alarm = True

    failed = await harness.room.create_room_rpc(harness.create_payload(now_ms=999))

    assert failed == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": PUBLIC_ERROR_MESSAGES["service_unavailable"],
        },
    }
    assert harness.storage.database_snapshot() == before
    assert harness.storage.alarm_at_ms == alarm_before == WAITING_ROOM_TTL_MS
    assert "storage:deleteAll" not in harness.storage.events
    assert harness.storage.delete_all_count == 0


async def test_new_create_alarm_and_compensation_failure_preserves_retryable_room(
    harness: GameRoomHarness,
) -> None:
    harness.storage.fail_next_set_alarm = True
    harness.storage.fail_next_delete_all = True

    failed = await harness.room.create_room_rpc(harness.create_payload())

    assert failed == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": PUBLIC_ERROR_MESSAGES["service_unavailable"],
        },
    }
    assert harness.storage.events.index("transaction:COMMIT") < harness.storage.events.index(
        f"alarm:set:{WAITING_ROOM_TTL_MS}"
    ) < harness.storage.events.index("storage:deleteAll")
    assert _row(harness.storage, "room")["room_id"] == ROOM_ID
    assert harness.storage.delete_all_count == 0

    recovered = await harness.room.create_room_rpc(harness.create_payload())
    assert recovered["ok"] is True
    assert harness.storage.delete_all_count == 0
    assert harness.storage.alarm_at_ms == WAITING_ROOM_TTL_MS


async def test_rpc_get_join_and_ticket_include_live_presence_and_json_datetime(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    creator_socket = harness.connected_socket(CREATOR, 0)

    joined = await harness.room.join_rpc(GUEST, 100)
    fetched = await harness.room.get_snapshot_rpc(GUEST)
    issued = await harness.room.issue_ticket_rpc(GUEST, 1_000)

    assert joined["ok"] is fetched["ok"] is issued["ok"] is True
    snapshot = cast(dict[str, object], fetched["value"])
    participants = cast(list[dict[str, object]], snapshot["participants"])
    assert [(item["user_id"], item["connected"]) for item in participants] == [
        (CREATOR, True),
        (GUEST, False),
    ]
    assert creator_socket.readyState == 1
    ticket_value = cast(dict[str, object], issued["value"])
    assert set(ticket_value) == {"ticket", "expires_at"}
    assert isinstance(ticket_value["expires_at"], str)
    assert ticket_value["expires_at"].endswith("Z")
    assert isinstance(ticket_value["ticket"], str)
    assert ticket_value["ticket"] not in str(harness.storage.database_snapshot())


async def test_rpc_public_fault_and_unknown_failure_mapping_do_not_leak(
    harness: GameRoomHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.create_direct()

    public = await harness.room.get_snapshot_rpc(OUTSIDER)
    assert public == {
        "ok": False,
        "error": {
            "code": "not_joined",
            "message": PUBLIC_ERROR_MESSAGES["not_joined"],
        },
    }

    secret = "injected SQL failure"
    harness.storage.inject_sql_failure(READ_ROOM_SQL)
    internal = await harness.room.get_snapshot_rpc(CREATOR)
    assert internal == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": PUBLIC_ERROR_MESSAGES["service_unavailable"],
        },
    }
    assert secret not in json.dumps(internal)

    unknown_secret = "private unknown store failure"

    def explode(*_args: object, **_kwargs: object) -> Never:
        raise RuntimeError(unknown_secret)

    monkeypatch.setattr(harness.room.store, "get_snapshot", explode)
    unknown = await harness.room.get_snapshot_rpc(CREATOR)
    assert unknown == internal
    assert unknown_secret not in json.dumps(unknown)


@pytest.mark.parametrize(
    ("fake_request", "status", "header", "code", "message"),
    [
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket/extra?ticket={'A' * 43}",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            404,
            None,
            "room_not_found",
            "Room route not found",
        ),
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={'A' * 43}",
                method="POST",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            405,
            ("allow", "GET"),
            "invalid_request",
            "Method not allowed",
        ),
        (
            FakeRequest(f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={'A' * 43}"),
            426,
            ("upgrade", "websocket"),
            "invalid_request",
            "WebSocket upgrade required",
        ),
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            401,
            None,
            "invalid_ticket",
            "Invalid ticket",
        ),
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket=",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            401,
            None,
            "invalid_ticket",
            "Invalid ticket",
        ),
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={'A' * 43}"
                + f"&ticket={'B' * 43}",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            401,
            None,
            "invalid_ticket",
            "Invalid ticket",
        ),
        (
            FakeRequest(
                f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={'A' * 43}&x=1",
                headers=FakeHeaders({"upgrade": "websocket"}),
            ),
            401,
            None,
            "invalid_ticket",
            "Invalid ticket",
        ),
    ],
)
async def test_fetch_rejects_neighbor_method_upgrade_and_nonexact_ticket_query(
    harness: GameRoomHarness,
    fake_request: FakeRequest,
    status: int,
    header: tuple[str, str] | None,
    code: RoomErrorCode,
    message: str,
) -> None:
    response = await harness.room.fetch(fake_request)

    assert response.status == status
    assert _json_body(response)["error"] == {"code": code, "message": message}
    headers = cast(Mapping[str, str], response.headers)
    assert headers.get("content-type") == "application/json"
    if header is not None:
        assert headers.get(header[0]) == header[1]
    assert harness.pairs.created == []


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/socket", "Room route not found"),
        (f"/wrong/v1/rooms/{ROOM_ID}/socket", "Room route not found"),
        (f"/v1/rooms/{ROOM_ID}/extra/socket", "Room route not found"),
        (f"/v1/rooms/{OTHER_ROOM_ID}/socket", "Room not found"),
        (f"/v1/rooms/{ROOM_ID.upper()}/socket", "Room not found"),
        ("/v1/rooms/not-a-uuid/socket", "Room not found"),
        (f"/v1/rooms/{ROOM_ID}%2Fneighbor/socket", "Room not found"),
    ],
)
async def test_fetch_requires_exact_matching_canonical_path_before_ticket_consumption(
    harness: GameRoomHarness,
    path: str,
    message: str,
) -> None:
    harness.create_direct()
    ticket = harness.issue_ticket(CREATOR)
    harness.storage.reset_observations()

    rejected = await harness.room.fetch(
        FakeRequest(
            f"https://game.example{path}?ticket={ticket}",
            headers=FakeHeaders({"upgrade": "websocket"}),
        )
    )

    assert rejected.status == 404
    assert _json_body(rejected) == {
        "error": {
            "code": "room_not_found",
            "message": message,
        }
    }
    assert _row(harness.storage, "tickets")["consumed_at"] is None
    assert harness.pairs.created == []

    accepted = await harness.fetch_ticket(ticket)
    assert accepted.status == 101
    assert _row(harness.storage, "tickets")["consumed_at"] == 1_001


async def test_fetch_consumes_ticket_attaches_redacted_json_and_sends_initial_snapshot(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.storage.execute(
        "UPDATE participants SET disconnected_at = 10 WHERE user_id = ?",
        CREATOR,
    )
    ticket = harness.issue_ticket(CREATOR)
    harness.storage.reset_observations()

    response = await harness.fetch_ticket(ticket)

    assert response.status == 101
    assert response.body is None
    pair = harness.pairs.created[0]
    assert response.web_socket is pair.client
    assert pair.server in harness.context.sockets
    assert pair.server.attachment_json == ('{"user_id":"user-0","seat":0,"invalid_frame_count":0}')
    assert ticket not in cast(str, pair.server.attachment_json)
    assert ticket not in "".join(pair.server.sent)
    initial = _messages(pair.server)[0]
    assert initial["type"] == "snapshot"
    assert _participant(initial, CREATOR)["connected"] is True
    assert _row(harness.storage, "participants")["disconnected_at"] is None
    assert _row(harness.storage, "tickets")["consumed_at"] == 1_001

    repeated = await harness.fetch_ticket(ticket)
    assert repeated.status == 401
    assert cast(dict[str, object], _json_body(repeated)["error"])["code"] == ("ticket_consumed")


async def test_fetch_sends_one_snapshot_before_replacement_close_and_broadcasts_to_peer(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    old = harness.connected_socket(CREATOR, 0, name="old")
    peer = harness.connected_socket(GUEST, 1, name="peer")
    ticket = harness.issue_ticket(CREATOR)
    harness.storage.reset_observations()
    old.sent.clear()
    peer.sent.clear()

    response = await harness.fetch_ticket(ticket)

    assert response.status == 101
    replacement = harness.pairs.created[0].server
    assert old.closed_with == (4001, "replaced")
    first_send = harness.storage.events.index(f"socket:{replacement.name}:send")
    old_close = harness.storage.events.index("socket:old:close:4001:replaced")
    assert first_send < old_close
    assert len(replacement.sent) == 1
    assert len(peer.sent) == 1
    for message in (_messages(replacement)[0], _messages(peer)[0]):
        assert _participant(message, CREATOR)["connected"] is True
        assert _participant(message, GUEST)["connected"] is True


async def test_fresh_socket_wrappers_do_not_replace_new_server_or_duplicate_broadcast(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    old = harness.connected_socket(CREATOR, 0, name="old")
    peer = harness.connected_socket(GUEST, 1, name="peer")
    harness.context.fresh_socket_wrappers = True
    ticket = harness.issue_ticket(CREATOR)
    harness.storage.reset_observations()
    old.sent.clear()
    peer.sent.clear()

    response = await harness.fetch_ticket(ticket)

    assert response.status == 101
    pair = harness.pairs.created[0]
    server_wrapper = _fresh_context_socket(harness, pair.server)
    assert server_wrapper is not pair.server
    assert server_wrapper.js_id == pair.server.js_id
    assert old.closed_with == (4001, "replaced")
    assert pair.server.closed_with is None
    assert len(pair.server.sent) == 1
    assert len(peer.sent) == 1
    assert harness.storage.events.count(f"socket:{pair.server.name}:send") == 1
    assert harness.storage.events.count("socket:peer:send") == 1


async def test_replacement_close_failure_does_not_abort_successful_replacement(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    old = harness.connected_socket(CREATOR, 0, name="old")
    peer = harness.connected_socket(GUEST, 1, name="peer")
    old.fail_next_close = True
    ticket = harness.issue_ticket(CREATOR)
    harness.storage.reset_observations()
    old.sent.clear()
    peer.sent.clear()

    response = await harness.fetch_ticket(ticket)

    assert response.status == 101
    pair = harness.pairs.created[0]
    assert old.readyState == 1
    assert old.close_calls == []
    assert pair.server.closed_with is None
    assert pair.client.closed_with is None
    assert len(pair.server.sent) == 1
    assert len(old.sent) == 1
    assert len(peer.sent) == 1
    assert [event for event in harness.storage.events if ":close:" in event] == [
        "socket:old:close:4001:replaced",
    ]
    assert _row(harness.storage, "tickets")["consumed_at"] == 1_001

    reused = await harness.fetch_ticket(ticket)
    assert reused.status == 401
    assert cast(dict[str, object], _json_body(reused)["error"])["code"] == (
        "ticket_consumed"
    )


@pytest.mark.parametrize(
    "failure",
    ["pair", "accept", "attachment", "send", "response"],
)
async def test_fetch_setup_failure_rolls_back_and_allows_same_ticket_retry(
    harness: GameRoomHarness,
    failure: Literal["pair", "accept", "attachment", "send", "response"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.create_direct()
    harness.storage.execute(
        "UPDATE participants SET disconnected_at = 500 WHERE user_id = ?",
        CREATOR,
    )
    old = harness.connected_socket(CREATOR, 0, name="old")
    ticket = harness.issue_ticket(CREATOR)
    if failure == "pair":
        harness.pairs.fail_next_new = True
    elif failure == "accept":
        harness.context.fail_next_accept = True
    elif failure == "attachment":
        harness.pairs.fail_next_server_attachment = True
    elif failure == "send":
        harness.pairs.fail_next_server_send = True
    else:
        original_response = room_module.Response

        class FailOnceUpgradeResponse(original_response):
            fail_next_upgrade = True

            def __init__(
                self,
                body: str | None = None,
                status: int = 200,
                headers: Mapping[str, str] | None = None,
                web_socket: FakeWebSocket | None = None,
            ) -> None:
                if status == 101 and self.fail_next_upgrade:
                    type(self).fail_next_upgrade = False
                    raise RuntimeError("injected response construction failure")
                super().__init__(body, status, headers, web_socket)

        monkeypatch.setattr(room_module, "Response", FailOnceUpgradeResponse)

    response = await harness.fetch_ticket(ticket)

    assert response.status == 500
    error = cast(dict[str, object], _json_body(response)["error"])
    assert error == {
        "code": "service_unavailable",
        "message": "WebSocket connection failed",
    }
    for pair in harness.pairs.created:
        assert pair.client.closed_with is not None
        assert pair.server.closed_with is not None
    assert old.readyState == 1
    assert old.closed_with is None
    assert _row(harness.storage, "tickets")["consumed_at"] is None
    assert _row(harness.storage, "participants")["disconnected_at"] == 500
    assert ticket not in json.dumps(error)

    retry = await harness.fetch_ticket(ticket)
    assert retry.status == 101
    assert _row(harness.storage, "tickets")["consumed_at"] == 1_001
    assert _row(harness.storage, "participants")["disconnected_at"] is None


async def test_ready_commit_precedes_alarm_and_broadcast_without_request_id(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    harness.room.store.execute_command(
        SocketIdentity(CREATOR, 0),
        ReadyCommand(expected_version=0),
        200,
    )
    creator = harness.connected_socket(CREATOR, 0)
    guest = harness.connected_socket(GUEST, 1)
    harness.clock.value = 300
    harness.storage.reset_observations()
    creator.sent.clear()
    guest.sent.clear()

    await harness.room.webSocketMessage(
        guest,
        '{"type":"ready","expected_version":1}',
    )

    commit = harness.storage.events.index("transaction:COMMIT")
    alarm = harness.storage.events.index(f"alarm:set:{300 + ACTIVE_ROOM_TTL_MS}")
    sends = [index for index, event in enumerate(harness.storage.events) if event.endswith(":send")]
    assert sends and commit < alarm < min(sends)
    for message in (_messages(creator)[0], _messages(guest)[0]):
        assert message["type"] == "snapshot"
        assert "request_id" not in message


async def test_ready_alarm_failure_still_delivers_committed_activation(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    harness.room.store.execute_command(
        SocketIdentity(CREATOR, 0),
        ReadyCommand(expected_version=0),
        200,
    )
    creator = harness.connected_socket(CREATOR, 0)
    guest = harness.connected_socket(GUEST, 1)
    harness.clock.value = 300
    harness.storage.reset_observations()
    harness.storage.fail_next_set_alarm = True
    creator.sent.clear()
    guest.sent.clear()

    await harness.room.webSocketMessage(
        guest,
        '{"type":"ready","expected_version":1}',
    )

    room = _row(harness.storage, "room")
    assert room["status"] == "active"
    assert room["version"] == 2
    assert room["expires_at"] == 300 + ACTIVE_ROOM_TTL_MS
    assert json.loads(cast(str, room["state_json"]))["boards"] is not None
    messages = _messages(creator) + _messages(guest)
    assert len(messages) == 2
    for message in messages:
        assert message["type"] == "snapshot"
        assert "request_id" not in message
        snapshot = cast(dict[str, object], message["snapshot"])
        assert snapshot["status"] == "active"
        assert snapshot["version"] == 2
    assert all(message["type"] != "error" for message in messages)
    commit = harness.storage.events.index("transaction:COMMIT")
    alarm = harness.storage.events.index(f"alarm:set:{300 + ACTIVE_ROOM_TTL_MS}")
    sends = [
        index
        for index, event in enumerate(harness.storage.events)
        if event.endswith(":send")
    ]
    assert sends and commit < alarm < min(sends)
    assert harness.storage.alarm_at_ms is None

    harness.clock.value = 301
    await harness.room.alarm()

    assert harness.storage.alarm_at_ms == 300 + ACTIVE_ROOM_TTL_MS
    assert len(creator.sent) == len(guest.sent) == 1


async def test_legal_move_persists_before_correlated_broadcast(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0,"request_id":"m1"}',
    )

    commit = harness.storage.events.index("transaction:COMMIT")
    first_send = next(
        index for index, event in enumerate(harness.storage.events) if event.endswith(":send")
    )
    assert commit < first_send
    for message in _messages(actor) + _messages(peer):
        assert message["type"] == "snapshot"
        assert message["request_id"] == "m1"
        assert cast(dict[str, object], message["snapshot"])["version"] == 3


async def test_move_noop_is_actor_only_and_correlated_without_writes(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    _set_actor_noop_board(harness)
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0,"request_id":"noop"}',
    )

    assert len(actor.sent) == 1
    assert peer.sent == []
    assert _messages(actor)[0]["request_id"] == "noop"
    assert all("UPDATE room SET state_json" not in event for event in harness.storage.events)


async def test_stale_fault_is_actor_only_with_room_board_versions_and_request_id(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":99,"request_id":"stale"}',
    )

    assert peer.sent == []
    message = _messages(actor)[0]
    assert message == {
        "type": "error",
        "code": "stale_board_version",
        "message": PUBLIC_ERROR_MESSAGES["stale_board_version"],
        "current_version": 2,
        "current_board_version": 0,
        "request_id": "stale",
    }
    assert harness.storage.transaction_events[-1] == "ROLLBACK"


async def test_valid_frame_resets_invalid_count_before_room_fault(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0, invalid_frame_count=2)

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":99}',
    )

    assert actor.attachment_json == ('{"user_id":"user-0","seat":0,"invalid_frame_count":0}')
    assert _messages(actor)[0]["code"] == "stale_board_version"


async def test_bad_text_and_binary_frames_error_twice_then_close_policy_violation(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)

    await harness.room.webSocketMessage(socket, "not-json")
    assert _messages(socket)[0]["code"] == "invalid_request"
    assert json.loads(cast(str, socket.attachment_json))["invalid_frame_count"] == 1

    await harness.room.webSocketMessage(socket, b"\x00")
    assert _messages(socket)[1]["code"] == "invalid_request"
    assert json.loads(cast(str, socket.attachment_json))["invalid_frame_count"] == 2

    await harness.room.webSocketMessage(socket, "still-not-json")
    assert socket.closed_with == (1008, "policy violation")
    assert len(socket.sent) == 2


async def test_hibernation_restores_invalid_counter_and_closes_on_next_bad_frame(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0, invalid_frame_count=2)
    harness.hibernate()

    await harness.room.webSocketMessage(socket, "not-json")

    assert socket.closed_with == (1008, "policy violation")
    assert socket.sent == []


@pytest.mark.parametrize(
    "attachment",
    [
        "{}",
        "[]",
        '{"user_id":"user-0","seat":0,"invalid_frame_count":0,"extra":1}',
        '{"user_id":"","seat":0,"invalid_frame_count":0}',
        '{"user_id":"user-0","seat":true,"invalid_frame_count":0}',
        '{"user_id":"user-0","seat":2,"invalid_frame_count":0}',
        '{"user_id":"user-0","seat":0,"invalid_frame_count":false}',
        '{"user_id":"user-0","seat":0,"invalid_frame_count":4}',
        '{"user_id":1,"seat":0,"invalid_frame_count":0}',
    ],
)
async def test_invalid_attachment_closes_without_trusting_identity(
    harness: GameRoomHarness,
    attachment: str,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0, attachment=attachment)
    harness.storage.reset_observations()

    await harness.room.webSocketMessage(socket, "not-json")

    assert socket.closed_with == (1008, "policy violation")
    assert socket.sent == []
    assert not any(event.startswith("sql:") for event in harness.storage.events)


async def test_oversized_frame_closes_immediately_without_attachment_read(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)
    harness.storage.reset_observations()

    await harness.room.webSocketMessage(socket, "x" * 4_097)

    assert socket.closed_with == (1009, "message too large")
    assert not any("attachment:deserialize" in event for event in harness.storage.events)


async def test_oversized_wide_memoryview_uses_byte_size_not_element_count(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)
    frame = memoryview(array("I", [0] * 1_025))
    assert len(frame) == 1_025
    assert frame.nbytes == 4_100

    await harness.room.webSocketMessage(socket, frame)

    assert socket.closed_with == (1009, "message too large")
    assert socket.sent == []


async def test_hibernated_room_restores_identity_state_and_broadcast_delivery(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    expected_state = _row(harness.storage, "room")["state_json"]
    harness.hibernate()
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0}',
    )

    assert _row(harness.storage, "room")["state_json"] != expected_state
    assert len(actor.sent) == len(peer.sent) == 1
    assert cast(dict[str, object], _messages(actor)[0]["snapshot"])["version"] == 3


async def test_hibernated_room_broadcasts_once_per_socket_with_fresh_wrappers(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    harness.context.fresh_socket_wrappers = True
    harness.hibernate()
    actor_event = _fresh_context_socket(harness, actor)
    assert actor_event is not actor
    assert _fresh_context_socket(harness, actor) is not actor_event
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor_event,
        '{"type":"move","direction":"left","expected_board_version":0}',
    )

    assert len(actor.sent) == len(peer.sent) == 1
    assert harness.storage.events.count(f"socket:{actor.name}:send") == 1
    assert harness.storage.events.count(f"socket:{peer.name}:send") == 1
    assert cast(dict[str, object], _messages(actor)[0]["snapshot"])["version"] == 3


async def test_recipient_send_failure_is_contained_and_healthy_recipient_receives(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    peer.fail_next_send = True
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0}',
    )

    assert len(actor.sent) == 1
    assert peer.sent == []
    assert peer.closed_with == (1011, "send failed")
    assert _row(harness.storage, "room")["version"] == 3


async def test_internal_command_failure_rolls_back_and_errors_only_to_actor(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0, invalid_frame_count=2)
    peer = harness.connected_socket(GUEST, 1)
    state_before = _row(harness.storage, "room").copy()
    harness.storage.fail_next_action_insert = True
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0,"request_id":"internal"}',
    )

    assert peer.sent == []
    assert _messages(actor)[0] == {
        "type": "error",
        "code": "service_unavailable",
        "message": "Room command failed",
        "current_version": 2,
        "request_id": "internal",
    }
    assert _row(harness.storage, "room") == state_before
    assert json.loads(cast(str, actor.attachment_json))["invalid_frame_count"] == 0
    assert "injected action insert failure" not in actor.sent[0]


async def test_corrupt_socket_does_not_poison_healthy_broadcasts(
    harness: GameRoomHarness,
) -> None:
    harness.active_direct()
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    corrupt = harness.connected_socket(CREATOR, 0, attachment="{}", name="corrupt")
    actor.sent.clear()
    peer.sent.clear()
    corrupt.sent.clear()

    await harness.room.webSocketMessage(
        actor,
        '{"type":"move","direction":"left","expected_board_version":0}',
    )

    assert len(actor.sent) == len(peer.sent) == 1
    assert corrupt.sent == []


async def test_transport_error_persists_final_disconnect_and_broadcasts_presence(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    actor = harness.connected_socket(CREATOR, 0)
    peer = harness.connected_socket(GUEST, 1)
    version = _row(harness.storage, "room")["version"]
    harness.clock.value = 500
    harness.storage.reset_observations()
    peer.sent.clear()

    await harness.room.webSocketError(actor, RuntimeError("private transport detail"))

    assert actor.closed_with == (1011, "transport error")
    creator_row = next(
        row for row in harness.storage.rows("participants") if row["user_id"] == CREATOR
    )
    assert creator_row["disconnected_at"] == 500
    assert _row(harness.storage, "room")["version"] == version
    assert harness.storage.rows("actions") == []
    assert _participant(_messages(peer)[-1], CREATOR)["connected"] is False


async def test_close_uses_event_code_and_replacement_does_not_mark_disconnected(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    old = harness.connected_socket(CREATOR, 0, name="old")
    replacement = harness.connected_socket(CREATOR, 0, name="replacement")
    harness.clock.value = 700

    await harness.room.webSocketClose(old, 4001, "replaced", True)

    assert old.closed_with == (4001, "replaced")
    assert replacement.readyState == 1
    assert _row(harness.storage, "participants")["disconnected_at"] is None


async def test_final_close_persists_disconnect_and_contains_close_failure(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)
    socket.fail_next_close = True
    harness.clock.value = 800

    await harness.room.webSocketClose(socket, 1000, "done", True)

    assert _row(harness.storage, "participants")["disconnected_at"] == 800
    assert "socket:user-0-0:close:1000:done" in harness.storage.events


async def test_fresh_wrapper_exclusion_persists_final_disconnect_and_targets_peer(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    actor = harness.connected_socket(CREATOR, 0, name="actor")
    peer = harness.connected_socket(GUEST, 1, name="peer")
    harness.context.fresh_socket_wrappers = True
    actor_event = _fresh_context_socket(harness, actor)
    actor.fail_next_close = True
    harness.clock.value = 800
    harness.storage.reset_observations()
    actor.sent.clear()
    peer.sent.clear()

    await harness.room.webSocketClose(actor_event, 1000, "done", True)

    creator = next(
        row for row in harness.storage.rows("participants") if row["user_id"] == CREATOR
    )
    assert actor.readyState == 1
    assert creator["disconnected_at"] == 800
    assert actor.sent == []
    assert len(peer.sent) == 1
    assert _participant(_messages(peer)[0], CREATOR)["connected"] is False


async def test_alarm_none_has_no_side_effects(harness: GameRoomHarness) -> None:
    await harness.room.alarm()

    assert harness.storage.alarm_events == []
    assert harness.storage.delete_all_count == 0
    assert harness.pairs.created == []


async def test_alarm_reschedules_without_send_or_delete(harness: GameRoomHarness) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)
    harness.clock.value = WAITING_ROOM_TTL_MS - 1
    harness.storage.reset_observations()
    socket.sent.clear()

    await harness.room.alarm()

    assert harness.storage.alarm_at_ms == WAITING_ROOM_TTL_MS
    assert socket.sent == []
    assert socket.close_calls == []
    assert harness.storage.delete_all_count == 0


async def test_alarm_expiry_commits_then_sends_open_closes_every_socket_and_deletes(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    open_socket = harness.connected_socket(CREATOR, 0, name="open")
    closed_socket = harness.connected_socket(
        CREATOR,
        0,
        name="closed",
        ready_state=3,
    )
    harness.clock.value = WAITING_ROOM_TTL_MS
    harness.storage.reset_observations()
    open_socket.sent.clear()
    closed_socket.sent.clear()

    await harness.room.alarm()

    assert cast(dict[str, object], _messages(open_socket)[0]["snapshot"])["status"] == ("expired")
    assert closed_socket.sent == []
    assert open_socket.closed_with == (4000, "room expired")
    assert closed_socket.closed_with == (4000, "room expired")
    commit = harness.storage.events.index("transaction:COMMIT")
    send = harness.storage.events.index("socket:open:send")
    first_close = min(
        harness.storage.events.index("socket:open:close:4000:room expired"),
        harness.storage.events.index("socket:closed:close:4000:room expired"),
    )
    delete = harness.storage.events.index("storage:deleteAll")
    assert commit < send < first_close < delete
    assert harness.storage.table_names() == set()

    observed = list(harness.storage.events)
    await harness.room.alarm()
    assert harness.storage.events == observed
    assert harness.storage.delete_all_count == 1


async def test_alarm_attempts_every_socket_despite_send_and_close_failures(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    first = harness.connected_socket(CREATOR, 0, name="first")
    second = harness.connected_socket(CREATOR, 0, name="second")
    third = harness.connected_socket(CREATOR, 0, name="third")
    first.fail_next_send = True
    second.fail_next_close = True
    harness.clock.value = WAITING_ROOM_TTL_MS
    harness.storage.reset_observations()

    await harness.room.alarm()

    for socket in (first, second, third):
        assert f"socket:{socket.name}:send" in harness.storage.events
        assert f"socket:{socket.name}:close:4000:room expired" in harness.storage.events
    assert harness.storage.events[-1] == "storage:deleteAll"
    assert harness.storage.delete_all_count == 1


async def test_alarm_delete_failure_escapes_and_retry_is_idempotent(
    harness: GameRoomHarness,
) -> None:
    harness.create_direct()
    socket = harness.connected_socket(CREATOR, 0)
    harness.clock.value = WAITING_ROOM_TTL_MS
    harness.storage.fail_next_delete_all = True

    with pytest.raises(RuntimeError, match="injected deleteAll failure"):
        await harness.room.alarm()

    assert _row(harness.storage, "room")["status"] == "expired"
    assert socket.closed_with == (4000, "room expired")
    assert harness.storage.delete_all_count == 0

    await harness.room.alarm()
    assert harness.storage.table_names() == set()
    assert harness.storage.delete_all_count == 1

    await harness.room.alarm()
    assert harness.storage.delete_all_count == 1


@pytest.mark.parametrize("instance_mode", ["same", "fresh"])
async def test_deleted_room_entrypoints_match_empty_instance_until_explicit_create(
    harness: GameRoomHarness,
    instance_mode: Literal["same", "fresh"],
) -> None:
    harness.create_direct()
    harness.clock.value = WAITING_ROOM_TTL_MS
    await harness.room.alarm()
    assert harness.storage.table_names() == set()
    if instance_mode == "fresh":
        harness.hibernate()
    harness.storage.reset_observations()

    missing = {
        "ok": False,
        "error": {
            "code": "room_not_found",
            "message": PUBLIC_ERROR_MESSAGES["room_not_found"],
        },
    }
    assert await harness.room.get_snapshot_rpc(CREATOR) == missing
    assert await harness.room.join_rpc(GUEST, WAITING_ROOM_TTL_MS + 1) == missing
    assert await harness.room.issue_ticket_rpc(CREATOR, WAITING_ROOM_TTL_MS + 1) == missing
    fetched = await harness.room.fetch(
        FakeRequest(
            f"https://game.example/v1/rooms/{ROOM_ID}/socket?ticket={'A' * 43}",
            headers=FakeHeaders({"upgrade": "websocket"}),
        )
    )
    assert fetched.status == 404
    assert _json_body(fetched) == {"error": missing["error"]}
    assert harness.storage.table_names() == {"room", "participants", "tickets", "actions"}
    assert all(
        harness.storage.rows(table) == []
        for table in ("room", "participants", "tickets", "actions")
    )

    created = await harness.room.create_room_rpc(
        harness.create_payload(now_ms=WAITING_ROOM_TTL_MS + 2)
    )
    assert created["ok"] is True
    assert len(harness.storage.rows("room")) == 1
    assert len(harness.storage.rows("participants")) == 1
    assert harness.storage.rows("tickets") == []
    assert harness.storage.rows("actions") == []


@pytest.mark.parametrize("instance_mode", ["same", "fresh"])
async def test_late_socket_handlers_after_delete_match_empty_instance_without_sql_errors(
    harness: GameRoomHarness,
    instance_mode: Literal["same", "fresh"],
) -> None:
    harness.create_direct()
    harness.room.store.join(GUEST, 100)
    first = harness.connected_socket(CREATOR, 0, name="first")
    second = harness.connected_socket(GUEST, 1, name="second")
    first.fail_next_close = True
    second.fail_next_close = True
    harness.clock.value = WAITING_ROOM_TTL_MS
    await harness.room.alarm()
    assert harness.storage.table_names() == set()
    assert first.readyState == second.readyState == 1
    if instance_mode == "fresh":
        harness.hibernate()
    harness.storage.reset_observations()
    first.sent.clear()
    second.sent.clear()

    await harness.room.webSocketMessage(
        _fresh_context_socket(harness, first),
        "not-json",
    )
    await harness.room.webSocketClose(
        _fresh_context_socket(harness, first),
        1000,
        "late close",
        True,
    )
    await harness.room.webSocketError(
        _fresh_context_socket(harness, second),
        RuntimeError("private late transport detail"),
    )

    assert _messages(first) == [
        {
            "type": "error",
            "code": "room_not_found",
            "message": PUBLIC_ERROR_MESSAGES["room_not_found"],
            "current_version": 0,
        }
    ]
    assert first.closed_with == (1000, "late close")
    assert second.closed_with == (1011, "transport error")
    assert "service_unavailable" not in "".join(first.sent + second.sent)
    if instance_mode == "same":
        assert not any(event.startswith("sql:") for event in harness.storage.events)


def test_room_scope_contains_only_2048_one_on_one_commands() -> None:
    owned = (
        ROOM_PATH,
        Path(__file__).parents[1] / "src" / "retro_backend" / "multiplayer" / "bindings.py",
    )
    markers = tuple("".join(parts) for parts in (("te", "am"), ("connect", "4"), ('"dr', 'op"')))

    for path in owned:
        source = path.read_text().lower()
        assert all(marker not in source for marker in markers), path
