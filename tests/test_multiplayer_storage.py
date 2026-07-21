import base64
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import pytest
from multiplayer_fakes import (  # pyright: ignore[reportImplicitRelativeImport]
    FakeRoomStorage,
    SqlExecution,
)

from retro_backend.multiplayer.errors import (
    PUBLIC_ERROR_MESSAGES,
    RoomErrorCode,
    RoomFault,
)
from retro_backend.multiplayer.games.game_2048 import (
    Board,
    Game2048Adapter,
    Game2048State,
    Player2048State,
    highest_tile,
)
from retro_backend.multiplayer.protocol import (
    MAX_SAFE_INTEGER,
    Game2048Snapshot,
    GameDescriptor,
    MoveCommand,
    ReadyCommand,
)
from retro_backend.multiplayer.storage import (
    ACTIVE_ROOM_TTL_MS,
    COMPLETED_ROOM_TTL_MS,
    MAX_TIMESTAMP_MS,
    ROOM_SCHEMA,
    WAITING_ROOM_TTL_MS,
    CommandResult,
    ExpirationResult,
    IssuedTicket,
    RoomStore,
    SocketIdentity,
    StorageFault,
)
from retro_backend.multiplayer.tickets import TICKET_TTL_MS, hash_ticket

ROOM_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_ROOM_ID = "223e4567-e89b-12d3-a456-426614174001"
DESCRIPTOR = GameDescriptor(game_type="2048", mode="1v1")
CREATOR = "user-0"
GUEST = "user-1"
OUTSIDER = "user-2"

WAITING_STATE_JSON = '{"boards":null,"winner_seat":null,"draw":false}'
MOVE_ACTION_JSON = (
    '{"direction":"left","score_delta":0,'
    '"spawned":{"row":3,"column":3,"value":2},'
    '"resulting_board_version":1}'
)
MOVED_STATE_JSON = '{"boards":[{"seat":0,"board":[[0,0,0,0],[4,0,0,0],[2,0,0,0],[0,0,0,2]],"score":0,"highest_tile":4,"board_version":1,"finished":false,"rng_state":2399460287},{"seat":1,"board":[[0,0,0,0],[2,0,0,0],[0,0,0,2],[0,0,0,0]],"score":0,"highest_tile":2,"board_version":0,"finished":false,"rng_state":3031295958}],"winner_seat":null,"draw":false}'
FIRST_TICKET_HASH = "ea866a757e4c38babfa8127cbe9a409d3e1f93a00ff1488ff735fcf917afffd0"
SECOND_TICKET_HASH = "eb9f16800c9029ffca85695763d23c3ace71011cf40e9354acd810205e250f87"

CREATE_ROOM_INSERT_SQL = "INSERT INTO room (singleton, room_id, game_type, mode, status, created_by, created_at, expires_at, version, state_json) VALUES (1, ?, ?, ?, 'waiting', ?, ?, ?, 0, ?) RETURNING singleton"
CREATE_PARTICIPANT_INSERT_SQL = "INSERT INTO participants (user_id, seat, ready, joined_at, disconnected_at) VALUES (?, 0, 0, ?, NULL) RETURNING seat"
JOIN_PARTICIPANT_INSERT_SQL = "INSERT INTO participants (user_id, seat, ready, joined_at, disconnected_at) VALUES (?, ?, 0, ?, NULL) RETURNING seat"
TICKET_CLEANUP_SQL = "DELETE FROM tickets WHERE expires_at <= ?"
TICKET_INSERT_SQL = "INSERT INTO tickets (token_hash, user_id, issued_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, NULL) RETURNING token_hash"
TICKET_CONSUME_SQL = "UPDATE tickets SET consumed_at = ? WHERE token_hash = ? AND consumed_at IS NULL RETURNING token_hash"
READY_PARTICIPANT_UPDATE_SQL = "UPDATE participants SET ready = 1 WHERE user_id = ? AND seat = ? AND ready = 0 RETURNING user_id"
READY_ROOM_UPDATE_SQL = "UPDATE room SET version = ?, status = ?, state_json = ?, expires_at = ? WHERE singleton = 1 AND version = ? RETURNING version"
MOVE_ROOM_UPDATE_SQL = "UPDATE room SET state_json = ?, status = ?, version = ?, expires_at = ? WHERE singleton = 1 AND version = ? RETURNING version"
ACTION_INSERT_SQL = "INSERT INTO actions (user_id, seat, action_type, action_json, accepted_at, resulting_version) VALUES (?, ?, ?, ?, ?, ?) RETURNING resulting_version"
EXPIRY_ROOM_UPDATE_SQL = "UPDATE room SET status = 'expired', version = ?, state_json = ? WHERE singleton = 1 AND status != 'expired' AND version = ? RETURNING version"
DELETE_TICKETS_SQL = "DELETE FROM tickets"
MARK_CONNECTED_SQL = "UPDATE participants SET disconnected_at = NULL WHERE user_id = ? AND seat = ? RETURNING user_id"
MARK_DISCONNECTED_SQL = (
    "UPDATE participants SET disconnected_at = ? WHERE user_id = ? AND seat = ? RETURNING user_id"
)
READ_ROOM_SQL = "SELECT room_id, game_type, mode, status, created_by, created_at, expires_at, version, state_json FROM room WHERE singleton = 1"
READ_PARTICIPANTS_SQL = "SELECT user_id, seat, ready, joined_at, disconnected_at FROM participants ORDER BY seat"

EDGE_PACKED_BOARD: Board = (
    (2, 4, 8, 16),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
)
DEAD_BOARD: Board = (
    (2, 4, 2, 4),
    (4, 2, 4, 2),
    (2, 4, 2, 4),
    (4, 2, 4, 2),
)
ALMOST_DEAD_BOARD: Board = (
    (2, 4, 2, 4),
    (4, 2, 4, 2),
    (2, 4, 2, 4),
    (4, 2, 4, 4),
)

type Seat2048 = Literal[0, 1]
type TopologyCorruption = Literal["missing_opponent", "unready_opponent"]
type TopologyOperation = Literal[
    "create",
    "get",
    "join",
    "issue_ticket",
    "consume_ticket",
    "move",
    "noop",
    "expire",
]


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
        self.calls: list[int] = []

    def __call__(self) -> int:
        value = next(self._values)
        self.calls.append(value)
        return value


@pytest.fixture
def fake_storage() -> FakeRoomStorage:
    return FakeRoomStorage()


@pytest.fixture
def store(fake_storage: FakeRoomStorage) -> RoomStore:
    room_store = RoomStore(
        fake_storage,
        entropy=EntropySequence(),
        random_uint32=Uint32Sequence(),
    )
    room_store.initialize_schema()
    fake_storage.reset_observations()
    return room_store


def _create(store: RoomStore, *, now_ms: int = 0) -> Game2048Snapshot:
    return store.create_room(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=now_ms,
    )


def _activate(store: RoomStore, *, final_ready_at: int = 300) -> CommandResult:
    _create(store)
    store.join(GUEST, 100)
    first = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 200
    )
    assert first.snapshot.status == "waiting"
    return store.execute_command(
        SocketIdentity(GUEST, 1), ReadyCommand(expected_version=1), final_ready_at
    )


def _room_row(fake_storage: FakeRoomStorage) -> dict[str, object]:
    rows = fake_storage.rows("room")
    assert len(rows) == 1
    return rows[0]


def _private_state(fake_storage: FakeRoomStorage) -> Game2048State:
    row = _room_row(fake_storage)
    status = cast(Literal["waiting", "active", "completed", "expired"], row["status"])
    return Game2048Adapter().decode_state(json.loads(cast(str, row["state_json"])), status)


def _replace_player(
    state: Game2048State,
    seat: Seat2048,
    *,
    board: Board,
    score: int,
    board_version: int,
    finished: bool,
    rng_state: int,
) -> Game2048State:
    assert state.boards is not None
    replacement = Player2048State(
        seat=seat,
        board=board,
        score=score,
        highest_tile=highest_tile(board),
        board_version=board_version,
        finished=finished,
        rng_state=rng_state,
    )
    boards = list(state.boards)
    boards[seat] = replacement
    return Game2048State(
        boards=(boards[0], boards[1]),
        winner_seat=state.winner_seat,
        draw=state.draw,
    )


def _set_private_state(
    fake_storage: FakeRoomStorage,
    state: Game2048State,
    *,
    status: str = "active",
    version: int | None = None,
    expires_at: int | None = None,
) -> None:
    assignments = ["state_json = ?", "status = ?"]
    bindings: list[object] = [
        json.dumps(state.to_payload(), separators=(",", ":"), allow_nan=False),
        status,
    ]
    if version is not None:
        assignments.append("version = ?")
        bindings.append(version)
    if expires_at is not None:
        assignments.append("expires_at = ?")
        bindings.append(expires_at)
    fake_storage.execute(
        f"UPDATE room SET {', '.join(assignments)} WHERE singleton = 1",
        *bindings,
    )


def _assert_fault(code: RoomErrorCode, operation: Callable[[], object]) -> RoomFault:
    with pytest.raises(RoomFault) as raised:
        operation()
    fault = raised.value
    assert fault.code == code
    assert fault.message == PUBLIC_ERROR_MESSAGES[code]
    return fault


def _assert_public_snapshot(snapshot: Game2048Snapshot) -> None:
    assert Game2048Snapshot.model_validate(snapshot.model_dump()) == snapshot
    assert "rng_state" not in snapshot.model_dump_json()
    assert snapshot.created_at.tzinfo is not None
    assert snapshot.expires_at.tzinfo is not None


def _assert_sql_execution(
    fake_storage: FakeRoomStorage,
    normalized_sql: str,
    bindings: tuple[object, ...],
    *,
    failed: bool = False,
) -> SqlExecution:
    matches = [
        execution
        for execution in fake_storage.executions
        if execution.normalized_sql == normalized_sql
    ]
    assert len(matches) == 1
    execution = matches[0]
    assert execution.bindings == bindings
    assert execution.failed is failed
    return execution


def _assert_transaction_encloses_sql(
    fake_storage: FakeRoomStorage,
    outcome: Literal["COMMIT", "ROLLBACK"],
) -> None:
    assert fake_storage.transaction_events == ["BEGIN", outcome]
    assert fake_storage.events[0] == "transaction:BEGIN"
    assert fake_storage.events[-1] == f"transaction:{outcome}"
    assert all(event.startswith("sql:") for event in fake_storage.events[1:-1])


def _assert_final_snapshot_read_before_commit(fake_storage: FakeRoomStorage) -> None:
    assert fake_storage.events[-3:] == [
        f"sql:{READ_ROOM_SQL}",
        f"sql:{READ_PARTICIPANTS_SQL}",
        "transaction:COMMIT",
    ]


def test_schema_is_idempotent_exact_and_drains_empty_cursors(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    store.initialize_schema()
    store.initialize_schema()

    assert fake_storage.table_names() == {"room", "participants", "tickets", "actions"}
    assert fake_storage.table_columns("room") == (
        "singleton",
        "room_id",
        "game_type",
        "mode",
        "status",
        "created_by",
        "created_at",
        "expires_at",
        "version",
        "state_json",
    )
    assert fake_storage.table_columns("participants") == (
        "user_id",
        "seat",
        "ready",
        "joined_at",
        "disconnected_at",
    )
    assert fake_storage.table_columns("tickets") == (
        "token_hash",
        "user_id",
        "issued_at",
        "expires_at",
        "consumed_at",
    )
    assert fake_storage.table_columns("actions") == (
        "sequence",
        "user_id",
        "seat",
        "action_type",
        "action_json",
        "accepted_at",
        "resulting_version",
    )
    assert len(ROOM_SCHEMA) == 4
    assert WAITING_ROOM_TTL_MS == ACTIVE_ROOM_TTL_MS == 30 * 60 * 1_000
    assert COMPLETED_ROOM_TTL_MS == 24 * 60 * 60 * 1_000
    assert fake_storage.all_cursors_consumed
    assert all(cursor.rows == () for cursor in fake_storage.cursors)

    participant_sql = " ".join(fake_storage.table_sql("participants").split()).lower()
    action_sql = " ".join(fake_storage.table_sql("actions").split()).lower()
    assert "seat between 0 and 1" in participant_sql
    assert "ready in (0, 1)" in participant_sql
    assert "seat between 0 and 1" in action_sql
    assert "action_type in ('ready', 'move')" in action_sql


def test_fake_transaction_sync_rejects_async_callbacks_and_results(
    fake_storage: FakeRoomStorage,
) -> None:
    async def callback() -> int:
        return 1

    with pytest.raises(TypeError, match="callback must be synchronous"):
        _ignored = fake_storage.transactionSync(callback)

    coroutine = callback()
    with pytest.raises(TypeError, match="result must not be awaitable"):
        _ignored = fake_storage.transactionSync(lambda: coroutine)

    assert fake_storage.transaction_events == ["BEGIN", "ROLLBACK"]
    assert not fake_storage.connection.in_transaction


def test_fake_cursor_becomes_consumed_only_after_stop_iteration(
    fake_storage: FakeRoomStorage,
) -> None:
    cursor = fake_storage.sql.exec("SELECT 7 AS value")

    assert cursor.consumed is False
    assert next(cursor) == {"value": 7}
    assert cursor.consumed is False
    with pytest.raises(StopIteration):
        next(cursor)
    assert cursor.consumed is True


def test_production_sql_contract_does_not_depend_on_rows_written() -> None:
    root = Path(__file__).parents[1]
    for relative_path in (
        "src/retro_backend/multiplayer/bindings.py",
        "src/retro_backend/multiplayer/storage.py",
    ):
        assert "rows" + "Written" not in (root / relative_path).read_text()


def test_lifecycle_uses_returning_when_write_counts_are_inflated() -> None:
    fake_storage = FakeRoomStorage(inflate_rows_written=True)
    store = RoomStore(
        fake_storage,
        entropy=EntropySequence(),
        random_uint32=Uint32Sequence(),
    )
    store.initialize_schema()

    created = _create(store)
    joined = store.join(GUEST, 100)
    ready = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 200
    )
    active = store.execute_command(
        SocketIdentity(GUEST, 1), ReadyCommand(expected_version=1), 300
    )
    issued = store.issue_ticket(CREATOR, 400)
    identity = store.consume_ticket(issued.ticket, 401)
    moved = store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(direction="left", expected_board_version=0),
        500,
    )
    expired = store.expire(300 + ACTIVE_ROOM_TTL_MS)

    assert created.status == joined.status == ready.snapshot.status == "waiting"
    assert active.snapshot.status == moved.snapshot.status == "active"
    assert identity == SocketIdentity(CREATOR, 0)
    assert expired.kind == "expired"
    assert expired.snapshot is not None
    assert expired.snapshot.status == "expired"
    inflated = [cursor.rowsWritten for cursor in fake_storage.cursors if cursor.rowsWritten]
    assert inflated
    assert all(rows_written > 1 for rows_written in inflated)
    assert fake_storage.all_cursors_consumed


async def test_fake_tracks_alarm_and_delete_all_hooks(
    fake_storage: FakeRoomStorage,
) -> None:
    fake_storage.execute("CREATE TABLE temporary_state (value INTEGER)")

    await fake_storage.setAlarm(123)
    await fake_storage.deleteAll()

    assert fake_storage.alarm_at_ms == 123
    assert fake_storage.alarm_events == [("setAlarm", 123), ("deleteAll", None)]
    assert fake_storage.delete_all_count == 1
    assert fake_storage.table_names() == set()


def test_general_sql_failure_injector_matches_normalized_statement_occurrence(
    fake_storage: FakeRoomStorage,
) -> None:
    statement = "INSERT INTO sample (value) VALUES (?)"
    fake_storage.execute("CREATE TABLE sample (value INTEGER)")
    fake_storage.reset_observations()
    fake_storage.inject_sql_failure(
        "  INSERT   INTO sample\n(value) VALUES (?)  ",
        occurrence=2,
    )

    def insert_twice() -> None:
        fake_storage.execute(statement, 1)
        fake_storage.execute(statement, 2)

    with pytest.raises(sqlite3.OperationalError, match="injected SQL failure"):
        fake_storage.transactionSync(insert_twice)

    assert fake_storage.rows("sample") == []
    assert [
        (execution.normalized_sql, execution.bindings, execution.failed)
        for execution in fake_storage.executions
    ] == [
        (statement, (1,), False),
        (statement, (2,), True),
    ]
    assert fake_storage.all_cursors_consumed
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_actions_reject_duplicate_resulting_version_and_roll_back(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 10
    )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    def insert_duplicate() -> None:
        fake_storage.execute(ACTION_INSERT_SQL, CREATOR, 0, "ready", "{}", 11, 1)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        fake_storage.transactionSync(insert_duplicate)

    assert fake_storage.database_snapshot() == before
    assert [row["resulting_version"] for row in fake_storage.rows("actions")] == [1]
    duplicate = _assert_sql_execution(
        fake_storage,
        ACTION_INSERT_SQL,
        (CREATOR, 0, "ready", "{}", 11, 1),
        failed=True,
    )
    assert duplicate.cursor is None
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_create_assigns_creator_seat_zero_and_compact_waiting_state(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    fake_storage.reset_observations()
    snapshot = _create(store, now_ms=1_000)

    assert snapshot.status == "waiting"
    assert snapshot.version == 0
    assert snapshot.participants[0].model_dump() == {
        "user_id": CREATOR,
        "seat": 0,
        "ready": False,
        "connected": False,
    }
    assert snapshot.boards is None
    assert snapshot.expires_at == datetime.fromtimestamp(
        (1_000 + WAITING_ROOM_TTL_MS) / 1_000, UTC
    )
    assert _room_row(fake_storage)["state_json"] == (
        '{"boards":null,"winner_seat":null,"draw":false}'
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")
    _assert_final_snapshot_read_before_commit(fake_storage)
    assert fake_storage.all_cursors_consumed
    _assert_public_snapshot(snapshot)


def test_create_is_idempotent_only_for_same_room_descriptor_and_creator(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    original = _create(store)
    fake_storage.reset_observations()

    repeated = _create(store, now_ms=999)

    assert repeated == original
    assert fake_storage.writes == 0
    _assert_fault(
        "invalid_request",
        lambda: store.create_room(
            room_id=ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=GUEST,
            now_ms=0,
        ),
    )
    _assert_fault(
        "invalid_request",
        lambda: store.create_room(
            room_id=OTHER_ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=CREATOR,
            now_ms=0,
        ),
    )
    _assert_fault(
        "invalid_request",
        lambda: store.create_room(
            room_id=ROOM_ID,
            descriptor={"game_type": "2048", "mode": "1v1"},
            created_by=CREATOR,
            now_ms=0,
        ),
    )


def test_create_with_status_distinguishes_new_and_idempotent_atomically(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    created = store.create_room_with_status(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=0,
    )
    fake_storage.reset_observations()

    existing = store.create_room_with_status(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=999,
    )

    assert created.created is True
    assert existing.created is False
    assert created.snapshot == existing.snapshot
    assert store.create_room(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=1_000,
    ) == created.snapshot
    with pytest.raises(FrozenInstanceError):
        created.created = False  # pyright: ignore[reportAttributeAccessIssue]
    assert fake_storage.writes == 0


def test_join_uses_lowest_free_seat_rejoins_without_writes_and_rejects_third_user(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()
    joined = store.join(GUEST, 10)

    assert [(participant.user_id, participant.seat) for participant in joined.participants] == [
        (CREATOR, 0),
        (GUEST, 1),
    ]
    assert joined.version == 0
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")
    _assert_final_snapshot_read_before_commit(fake_storage)

    fake_storage.reset_observations()
    rejoined = store.join(GUEST, 11)
    assert rejoined == joined
    assert fake_storage.writes == 0
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")

    fake_storage.reset_observations()
    _assert_fault("room_full", lambda: store.join(OUTSIDER, 12))
    assert _room_row(fake_storage)["version"] == 0
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_join_rejects_at_expiry_boundary_without_writing(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store, now_ms=0)
    fake_storage.reset_observations()

    _assert_fault("room_expired", lambda: store.join(GUEST, WAITING_ROOM_TTL_MS))

    assert fake_storage.writes == 0
    assert fake_storage.rows("participants")[0]["user_id"] == CREATOR


def test_get_is_participant_only_but_ignores_unpersisted_wall_clock_expiry(
    store: RoomStore,
) -> None:
    created = _create(store, now_ms=0)

    assert store.get_snapshot(CREATOR) == created
    _assert_fault("not_joined", lambda: store.get_snapshot(GUEST))


def test_first_ready_increments_once_and_repeated_ready_checks_version_first(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()
    first = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 10
    )

    assert first.changed is True
    assert first.audience == "all"
    assert first.next_alarm is None
    assert first.snapshot.status == "waiting"
    assert first.snapshot.version == 1
    assert first.snapshot.participants[0].ready is True
    assert fake_storage.rows("actions") == [
        {
            "sequence": 1,
            "user_id": CREATOR,
            "seat": 0,
            "action_type": "ready",
            "action_json": "{}",
            "accepted_at": 10,
            "resulting_version": 1,
        }
    ]
    _assert_sql_execution(
        fake_storage,
        READY_PARTICIPANT_UPDATE_SQL,
        (CREATOR, 0),
    )
    _assert_sql_execution(
        fake_storage,
        READY_ROOM_UPDATE_SQL,
        (1, "waiting", WAITING_STATE_JSON, WAITING_ROOM_TTL_MS, 0),
    )
    _assert_sql_execution(
        fake_storage,
        ACTION_INSERT_SQL,
        (CREATOR, 0, "ready", "{}", 10, 1),
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")
    _assert_final_snapshot_read_before_commit(fake_storage)

    fake_storage.reset_observations()
    stale = _assert_fault(
        "stale_version",
        lambda: store.execute_command(
            SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 11
        ),
    )
    assert stale.details == {"current_version": 1}
    assert fake_storage.writes == 0

    repeated = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=1), 12
    )
    assert repeated.changed is False
    assert repeated.audience == "all"
    assert repeated.snapshot == first.snapshot
    assert fake_storage.writes == 0


def test_activation_requires_two_seated_ready_users_and_uses_two_seeds(
    fake_storage: FakeRoomStorage,
) -> None:
    seeds = Uint32Sequence((0x11111111, 0x22222222))
    store = RoomStore(fake_storage, entropy=EntropySequence(), random_uint32=seeds)
    store.initialize_schema()
    _create(store)

    first = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 100
    )
    assert first.snapshot.status == "waiting"
    assert first.snapshot.boards is None
    assert seeds.calls == []

    store.join(GUEST, 150)
    activated = store.execute_command(
        SocketIdentity(GUEST, 1), ReadyCommand(expected_version=1), 200
    )

    assert activated.changed is True
    assert activated.audience == "all"
    assert activated.snapshot.status == "active"
    assert activated.snapshot.version == 2
    assert activated.next_alarm == 200 + ACTIVE_ROOM_TTL_MS
    assert _room_row(fake_storage)["expires_at"] == 200 + ACTIVE_ROOM_TTL_MS
    assert seeds.calls == [0x11111111, 0x22222222]
    assert _private_state(fake_storage) == Game2048Adapter().start_with_seeds(seeds.calls)
    assert activated.snapshot.boards is not None
    assert {board.seat for board in activated.snapshot.boards} == {0, 1}
    assert activated.snapshot.boards[0].board != activated.snapshot.boards[1].board
    _assert_public_snapshot(activated.snapshot)


def test_ready_status_precedes_version_and_identity_must_match_persisted_seat(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    activated = _activate(store)
    fake_storage.reset_observations()

    _assert_fault(
        "match_not_active",
        lambda: store.execute_command(
            SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=999), 301
        ),
    )
    _assert_fault(
        "not_joined",
        lambda: store.execute_command(
            SocketIdentity(CREATOR, 1), MoveCommand(direction="left", expected_board_version=0), 301
        ),
    )
    assert activated.snapshot.version == 2
    assert fake_storage.writes == 0


def test_command_rejects_wall_clock_expiry_before_mutating(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()

    _assert_fault(
        "room_expired",
        lambda: store.execute_command(
            SocketIdentity(CREATOR, 0),
            ReadyCommand(expected_version=0),
            WAITING_ROOM_TTL_MS,
        ),
    )
    assert fake_storage.writes == 0


def test_legal_move_changes_only_actor_private_board_and_logs_exact_payload(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    activated = _activate(store)
    before = _private_state(fake_storage)
    assert before.boards is not None
    fake_storage.reset_observations()

    moved = store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(direction="left", expected_board_version=0),
        400,
    )

    after = _private_state(fake_storage)
    assert after.boards is not None
    assert moved.changed is True
    assert moved.audience == "all"
    assert moved.next_alarm is None
    assert moved.snapshot.version == 3
    assert moved.snapshot.expires_at == activated.snapshot.expires_at
    assert after.boards[0] != before.boards[0]
    assert after.boards[1] == before.boards[1]
    action = fake_storage.rows("actions")[-1]
    assert action == {
        "sequence": 3,
        "user_id": CREATOR,
        "seat": 0,
        "action_type": "move",
        "action_json": MOVE_ACTION_JSON,
        "accepted_at": 400,
        "resulting_version": 3,
    }
    assert json.loads(MOVE_ACTION_JSON) == {
        "direction": "left",
        "score_delta": 0,
        "spawned": {"row": 3, "column": 3, "value": 2},
        "resulting_board_version": 1,
    }
    _assert_sql_execution(
        fake_storage,
        MOVE_ROOM_UPDATE_SQL,
        (MOVED_STATE_JSON, "active", 3, 300 + ACTIVE_ROOM_TTL_MS, 2),
    )
    _assert_sql_execution(
        fake_storage,
        ACTION_INSERT_SQL,
        (CREATOR, 0, "move", MOVE_ACTION_JSON, 400, 3),
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")
    _assert_final_snapshot_read_before_commit(fake_storage)
    assert [row["resulting_version"] for row in fake_storage.rows("actions")] == [1, 2, 3]
    assert len({row["resulting_version"] for row in fake_storage.rows("actions")}) == 3
    _assert_public_snapshot(moved.snapshot)


@pytest.mark.parametrize("finished", [False, True])
def test_noop_and_finished_move_perform_zero_writes(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
    finished: bool,
) -> None:
    activated = _activate(store)
    state = _private_state(fake_storage)
    state = _replace_player(
        state,
        0,
        board=DEAD_BOARD if finished else EDGE_PACKED_BOARD,
        score=0 if finished else 44,
        board_version=14 if finished else 7,
        finished=finished,
        rng_state=987,
    )
    _set_private_state(fake_storage, state)
    before = _room_row(fake_storage).copy()
    action_count = len(fake_storage.rows("actions"))
    fake_storage.reset_observations()

    result = store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(
            direction="left" if finished else "up",
            expected_board_version=14 if finished else 7,
        ),
        500,
    )

    assert result.changed is False
    assert result.audience == "actor"
    assert result.next_alarm is None
    assert result.snapshot.version == activated.snapshot.version
    assert fake_storage.writes == 0
    assert _room_row(fake_storage) == before
    assert len(fake_storage.rows("actions")) == action_count


def test_stale_board_fault_includes_current_room_and_board_versions(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _activate(store)
    fake_storage.reset_observations()

    fault = _assert_fault(
        "stale_board_version",
        lambda: store.execute_command(
            SocketIdentity(CREATOR, 0),
            MoveCommand(direction="left", expected_board_version=9),
            400,
        ),
    )

    assert fault.details == {"current_version": 2, "current_board_version": 0}
    assert fake_storage.writes == 0


def test_corrupt_private_state_fails_closed_without_leaking_contents(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    secret = "private-sql-state"
    fake_storage.execute(
        "UPDATE room SET state_json = ? WHERE singleton = 1",
        f'{{"corrupt":"{secret}"}}',
    )

    with pytest.raises(StorageFault) as raised:
        store.get_snapshot(CREATOR)

    assert secret not in str(raised.value)
    assert "corrupt" not in str(raised.value).lower()


@pytest.mark.parametrize("corruption", ["missing_creator", "creator_wrong_seat"])
def test_waiting_room_creator_topology_fails_closed(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
    corruption: Literal["missing_creator", "creator_wrong_seat"],
) -> None:
    _create(store)
    if corruption == "missing_creator":
        fake_storage.execute("DELETE FROM participants WHERE user_id = ?", CREATOR)
    else:
        fake_storage.execute(
            "UPDATE participants SET seat = 1 WHERE user_id = ?",
            CREATOR,
        )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    with pytest.raises(StorageFault):
        store.get_snapshot(CREATOR)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0


@pytest.mark.parametrize("corruption", ["missing_opponent", "unready_opponent"])
@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "get",
        "join",
        "issue_ticket",
        "consume_ticket",
        "move",
        "noop",
        "expire",
    ],
)
def test_corrupt_active_topology_fails_before_every_room_operation(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
    corruption: TopologyCorruption,
    operation: TopologyOperation,
) -> None:
    _activate(store)
    if operation == "noop":
        state = _replace_player(
            _private_state(fake_storage),
            0,
            board=EDGE_PACKED_BOARD,
            score=44,
            board_version=7,
            finished=False,
            rng_state=987,
        )
        _set_private_state(fake_storage, state)
    issued = store.issue_ticket(CREATOR, 400)
    if corruption == "missing_opponent":
        fake_storage.execute("DELETE FROM participants WHERE user_id = ?", GUEST)
    else:
        fake_storage.execute(
            "UPDATE participants SET ready = 0 WHERE user_id = ?",
            GUEST,
        )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    def invoke() -> object:
        if operation == "create":
            return _create(store, now_ms=401)
        if operation == "get":
            return store.get_snapshot(CREATOR)
        if operation == "join":
            return store.join(OUTSIDER, 401)
        if operation == "issue_ticket":
            return store.issue_ticket(CREATOR, 401)
        if operation == "consume_ticket":
            return store.consume_ticket(issued.ticket, 401)
        if operation == "move":
            return store.execute_command(
                SocketIdentity(CREATOR, 0),
                MoveCommand(direction="left", expected_board_version=0),
                401,
            )
        if operation == "noop":
            return store.execute_command(
                SocketIdentity(CREATOR, 0),
                MoveCommand(direction="up", expected_board_version=7),
                401,
            )
        return store.expire(300 + ACTIVE_ROOM_TTL_MS)

    with pytest.raises(StorageFault):
        invoke()

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0
    if operation == "get":
        assert fake_storage.transaction_events == []
    else:
        assert fake_storage.transaction_events == ["BEGIN", "ROLLBACK"]


@pytest.mark.parametrize("corruption", ["missing_opponent", "unready_opponent"])
def test_expired_active_origin_requires_complete_ready_topology(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
    corruption: TopologyCorruption,
) -> None:
    _activate(store)
    expired = store.expire(300 + ACTIVE_ROOM_TTL_MS)
    assert expired.snapshot is not None
    assert expired.snapshot.status == "expired"
    if corruption == "missing_opponent":
        fake_storage.execute("DELETE FROM participants WHERE user_id = ?", GUEST)
    else:
        fake_storage.execute(
            "UPDATE participants SET ready = 0 WHERE user_id = ?",
            GUEST,
        )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    with pytest.raises(StorageFault):
        store.get_snapshot(CREATOR)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0


def test_participant_disconnection_cannot_precede_join(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store, now_ms=100)
    fake_storage.execute(
        "UPDATE participants SET disconnected_at = 99 WHERE user_id = ?",
        CREATOR,
    )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    with pytest.raises(StorageFault):
        store.get_snapshot(CREATOR)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0


def test_ticket_persists_only_digest_with_issue_and_expiry_times(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()

    issued = store.issue_ticket(CREATOR, 1_000)

    expected_raw = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
    assert hash_ticket(expected_raw) == FIRST_TICKET_HASH
    assert issued == IssuedTicket(
        ticket=expected_raw,
        expires_at=datetime.fromtimestamp((1_000 + TICKET_TTL_MS) / 1_000, UTC),
    )
    assert fake_storage.rows("tickets") == [
        {
            "token_hash": hash_ticket(expected_raw),
            "user_id": CREATOR,
            "issued_at": 1_000,
            "expires_at": 1_000 + TICKET_TTL_MS,
            "consumed_at": None,
        }
    ]
    assert all(expected_raw not in repr(execution.bindings) for execution in fake_storage.executions)
    assert all(expected_raw not in line for line in fake_storage.database_snapshot())
    assert issued.expires_at.tzinfo is UTC
    _assert_sql_execution(fake_storage, TICKET_CLEANUP_SQL, (1_000,))
    _assert_sql_execution(
        fake_storage,
        TICKET_INSERT_SQL,
        (FIRST_TICKET_HASH, CREATOR, 1_000, 1_000 + TICKET_TTL_MS),
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")


def test_ticket_issue_deletes_expired_rows_and_requires_active_participant(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    first = store.issue_ticket(CREATOR, 0)

    _assert_fault("not_joined", lambda: store.issue_ticket(GUEST, 1))
    second = store.issue_ticket(CREATOR, TICKET_TTL_MS)

    rows = fake_storage.rows("tickets")
    assert first.ticket != second.ticket
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hash_ticket(second.ticket)


def test_ticket_consumption_is_atomic_immutable_and_consumed_precedes_expired(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    issued = store.issue_ticket(CREATOR, 0)
    fake_storage.reset_observations()

    identity = store.consume_ticket(issued.ticket, 1)

    assert identity == SocketIdentity(CREATOR, 0)
    with pytest.raises(FrozenInstanceError):
        identity.seat = 1  # pyright: ignore[reportAttributeAccessIssue]
    assert fake_storage.rows("tickets")[0]["consumed_at"] == 1
    _assert_sql_execution(
        fake_storage,
        TICKET_CONSUME_SQL,
        (1, FIRST_TICKET_HASH),
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")

    fake_storage.reset_observations()
    _assert_fault(
        "ticket_consumed",
        lambda: store.consume_ticket(issued.ticket, TICKET_TTL_MS),
    )


@pytest.mark.parametrize(
    ("assignments", "bindings", "now_ms", "room_expires_at"),
    [
        ("issued_at = ?, expires_at = ?", (10, 10 + TICKET_TTL_MS), 9, None),
        ("expires_at = ?", (TICKET_TTL_MS + 1,), 1, None),
        ("expires_at = ?", (TICKET_TTL_MS - 1,), 1, None),
        (
            "issued_at = ?, expires_at = ?, consumed_at = ?",
            (10, 10 + TICKET_TTL_MS, 9),
            10,
            None,
        ),
        ("consumed_at = ?", (10,), 9, None),
        ("consumed_at = ?", (TICKET_TTL_MS,), TICKET_TTL_MS, None),
        (
            "issued_at = ?, expires_at = ?",
            (MAX_TIMESTAMP_MS - TICKET_TTL_MS + 1, MAX_TIMESTAMP_MS),
            MAX_TIMESTAMP_MS - TICKET_TTL_MS + 1,
            MAX_TIMESTAMP_MS,
        ),
    ],
    ids=(
        "future-issued",
        "extended-ttl",
        "short-ttl",
        "consumed-before-issued",
        "future-consumed",
        "consumed-at-expiry",
        "deadline-overflow",
    ),
)
def test_corrupt_ticket_timestamps_fail_closed_without_writes(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
    assignments: str,
    bindings: tuple[int, ...],
    now_ms: int,
    room_expires_at: int | None,
) -> None:
    _create(store)
    issued = store.issue_ticket(CREATOR, 0)
    if room_expires_at is not None:
        fake_storage.execute(
            "UPDATE room SET expires_at = ? WHERE singleton = 1",
            room_expires_at,
        )
    fake_storage.execute(
        f"UPDATE tickets SET {assignments} WHERE token_hash = ?",
        *bindings,
        FIRST_TICKET_HASH,
    )
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    with pytest.raises(StorageFault):
        store.consume_ticket(issued.ticket, now_ms)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0
    assert fake_storage.transaction_events == ["BEGIN", "ROLLBACK"]


def test_ticket_expiry_missing_and_removed_participant_ordering(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    issued = store.issue_ticket(CREATOR, 0)

    _assert_fault(
        "ticket_expired",
        lambda: store.consume_ticket(issued.ticket, TICKET_TTL_MS),
    )

    store.join(GUEST, TICKET_TTL_MS + 1)
    another = store.issue_ticket(GUEST, TICKET_TTL_MS + 1)
    fake_storage.execute("DELETE FROM participants WHERE user_id = ?", GUEST)
    _assert_fault("invalid_ticket", lambda: store.consume_ticket(another.ticket, TICKET_TTL_MS + 2))


def test_ticket_shape_is_validated_before_lookup_and_room_expiry_precedes_digest_lookup(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()
    _assert_fault("invalid_ticket", lambda: store.consume_ticket("bad", 0))
    assert fake_storage.executions == []

    valid_but_missing = base64.urlsafe_b64encode(bytes(reversed(range(32)))).rstrip(b"=").decode(
        "ascii"
    )
    _assert_fault(
        "room_expired",
        lambda: store.consume_ticket(valid_but_missing, WAITING_ROOM_TTL_MS),
    )


def test_tickets_are_scoped_to_one_room_database() -> None:
    first_storage = FakeRoomStorage()
    second_storage = FakeRoomStorage()
    first = RoomStore(first_storage, entropy=EntropySequence(), random_uint32=Uint32Sequence())
    second = RoomStore(second_storage, entropy=EntropySequence(), random_uint32=Uint32Sequence())
    first.initialize_schema()
    second.initialize_schema()
    first.create_room(room_id=ROOM_ID, descriptor=DESCRIPTOR, created_by=CREATOR, now_ms=0)
    second.create_room(
        room_id=OTHER_ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=0,
    )
    issued = first.issue_ticket(CREATOR, 0)

    _assert_fault("invalid_ticket", lambda: second.consume_ticket(issued.ticket, 1))


def test_completion_sets_full_retention_and_preserves_private_rng(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _activate(store)
    state = _private_state(fake_storage)
    state = _replace_player(
        state,
        0,
        board=ALMOST_DEAD_BOARD,
        score=20,
        board_version=19,
        finished=False,
        rng_state=123,
    )
    state = _replace_player(
        state,
        1,
        board=DEAD_BOARD,
        score=12,
        board_version=17,
        finished=True,
        rng_state=456,
    )
    _set_private_state(fake_storage, state)
    fake_storage.reset_observations()

    completed = store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(direction="left", expected_board_version=19),
        5_000,
    )

    assert completed.snapshot.status == "completed"
    assert completed.snapshot.version == 3
    assert completed.snapshot.winner_seat == 0
    assert completed.next_alarm == 5_000 + COMPLETED_ROOM_TTL_MS
    assert _room_row(fake_storage)["expires_at"] == 5_000 + COMPLETED_ROOM_TTL_MS
    assert "rng_state" in cast(str, _room_row(fake_storage)["state_json"])
    _assert_public_snapshot(completed.snapshot)


def test_expiration_reschedules_then_expires_once_and_deletes_tickets(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    created = _create(store)
    store.issue_ticket(CREATOR, 0)
    fake_storage.reset_observations()

    rescheduled = store.expire(WAITING_ROOM_TTL_MS - 1)
    assert rescheduled == ExpirationResult(
        kind="reschedule",
        snapshot=None,
        next_alarm=WAITING_ROOM_TTL_MS,
    )
    assert fake_storage.writes == 0

    fake_storage.reset_observations()
    expired = store.expire(WAITING_ROOM_TTL_MS)
    assert expired.kind == "expired"
    assert expired.next_alarm is None
    assert expired.snapshot is not None
    assert expired.snapshot.status == "expired"
    assert expired.snapshot.version == created.version + 1
    assert fake_storage.rows("tickets") == []
    assert fake_storage.rows("actions") == []
    _assert_sql_execution(
        fake_storage,
        EXPIRY_ROOM_UPDATE_SQL,
        (1, WAITING_STATE_JSON, 0),
    )
    _assert_sql_execution(fake_storage, DELETE_TICKETS_SQL, ())
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")
    _assert_final_snapshot_read_before_commit(fake_storage)
    _assert_public_snapshot(expired.snapshot)

    fake_storage.reset_observations()
    repeated = store.expire(WAITING_ROOM_TTL_MS + 1)
    assert repeated == expired
    assert fake_storage.writes == 0


def test_expiration_of_missing_room_is_a_noop(fake_storage: FakeRoomStorage) -> None:
    store = RoomStore(fake_storage, entropy=EntropySequence(), random_uint32=Uint32Sequence())
    store.initialize_schema()
    fake_storage.reset_observations()

    assert store.expire(0) == ExpirationResult(kind="none", snapshot=None, next_alarm=None)
    assert fake_storage.writes == 0


def test_create_rolls_back_room_and_participant_when_second_insert_fails(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    before = fake_storage.database_snapshot()
    fake_storage.inject_sql_failure(CREATE_PARTICIPANT_INSERT_SQL)

    with pytest.raises(StorageFault):
        _create(store)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.rows("room") == []
    assert fake_storage.rows("participants") == []
    _assert_sql_execution(
        fake_storage,
        CREATE_ROOM_INSERT_SQL,
        (ROOM_ID, "2048", "1v1", CREATOR, 0, WAITING_ROOM_TTL_MS, WAITING_STATE_JSON),
    )
    _assert_sql_execution(
        fake_storage,
        CREATE_PARTICIPANT_INSERT_SQL,
        (CREATOR, 0),
        failed=True,
    )
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_join_rolls_back_inserted_participant_on_injected_failure(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.inject_sql_failure(JOIN_PARTICIPANT_INSERT_SQL)

    with pytest.raises(StorageFault):
        store.join(GUEST, 100)

    assert fake_storage.database_snapshot() == before
    assert [row["user_id"] for row in fake_storage.rows("participants")] == [CREATOR]
    _assert_sql_execution(
        fake_storage,
        JOIN_PARTICIPANT_INSERT_SQL,
        (GUEST, 1, 100),
        failed=True,
    )
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_ticket_issue_rolls_back_cleanup_and_insert_on_injected_failure(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    first = store.issue_ticket(CREATOR, 0)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.inject_sql_failure(TICKET_INSERT_SQL)

    with pytest.raises(StorageFault):
        store.issue_ticket(CREATOR, TICKET_TTL_MS)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.rows("tickets") == [
        {
            "token_hash": FIRST_TICKET_HASH,
            "user_id": CREATOR,
            "issued_at": 0,
            "expires_at": TICKET_TTL_MS,
            "consumed_at": None,
        }
    ]
    assert hash_ticket(first.ticket) == FIRST_TICKET_HASH
    _assert_sql_execution(fake_storage, TICKET_CLEANUP_SQL, (TICKET_TTL_MS,))
    _assert_sql_execution(
        fake_storage,
        TICKET_INSERT_SQL,
        (SECOND_TICKET_HASH, CREATOR, TICKET_TTL_MS, 2 * TICKET_TTL_MS),
        failed=True,
    )
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_ticket_consume_rolls_back_consumed_timestamp_on_injected_failure(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    issued = store.issue_ticket(CREATOR, 0)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.inject_sql_failure(TICKET_CONSUME_SQL)

    with pytest.raises(StorageFault):
        store.consume_ticket(issued.ticket, 1)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.rows("tickets")[0]["consumed_at"] is None
    _assert_sql_execution(
        fake_storage,
        TICKET_CONSUME_SQL,
        (1, FIRST_TICKET_HASH),
        failed=True,
    )
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_expiry_rolls_back_room_update_and_ticket_delete_on_injected_failure(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    store.issue_ticket(CREATOR, 0)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.inject_sql_failure(DELETE_TICKETS_SQL)

    with pytest.raises(StorageFault):
        store.expire(WAITING_ROOM_TTL_MS)

    assert fake_storage.database_snapshot() == before
    assert _room_row(fake_storage)["status"] == "waiting"
    assert _room_row(fake_storage)["version"] == 0
    assert len(fake_storage.rows("tickets")) == 1
    _assert_sql_execution(
        fake_storage,
        EXPIRY_ROOM_UPDATE_SQL,
        (1, WAITING_STATE_JSON, 0),
    )
    _assert_sql_execution(fake_storage, DELETE_TICKETS_SQL, (), failed=True)
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_action_insert_failure_rolls_back_participant_room_and_history(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.fail_next_action_insert = True

    with pytest.raises(StorageFault):
        store.execute_command(
            SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 10
        )

    assert fake_storage.database_snapshot() == before
    assert fake_storage.rows("participants")[0]["ready"] == 0
    assert _room_row(fake_storage)["version"] == 0
    assert fake_storage.rows("actions") == []
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_move_action_failure_rolls_back_state_rng_version_and_history(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _activate(store)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    fake_storage.fail_next_action_insert = True

    with pytest.raises(StorageFault):
        store.execute_command(
            SocketIdentity(CREATOR, 0),
            MoveCommand(direction="left", expected_board_version=0),
            400,
        )

    assert fake_storage.database_snapshot() == before
    _assert_transaction_encloses_sql(fake_storage, "ROLLBACK")


def test_mutation_reads_final_snapshot_after_action_and_before_commit(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.reset_observations()

    result = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 10
    )

    action_index = next(
        index
        for index, event in enumerate(fake_storage.events)
        if event.lower().startswith("sql:insert into actions")
    )
    final_room_read = max(
        index
        for index, event in enumerate(fake_storage.events)
        if event.lower().startswith("sql:select") and " from room " in event.lower()
    )
    commit_index = fake_storage.events.index("transaction:COMMIT")
    assert action_index < final_room_read < commit_index
    assert result.snapshot.version == 1


def test_all_lifecycle_snapshots_validate_and_private_active_json_retains_rng(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    created = _create(store)
    joined = store.join(GUEST, 100)
    ready = store.execute_command(
        SocketIdentity(CREATOR, 0), ReadyCommand(expected_version=0), 200
    ).snapshot
    active = store.execute_command(
        SocketIdentity(GUEST, 1), ReadyCommand(expected_version=1), 300
    ).snapshot
    moved = store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(direction="left", expected_board_version=0),
        400,
    ).snapshot

    for snapshot in (created, joined, ready, active, moved):
        _assert_public_snapshot(snapshot)
    assert "rng_state" in cast(str, _room_row(fake_storage)["state_json"])


def test_connected_users_are_snapshot_only_presence(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    store.join(GUEST, 1)
    before = fake_storage.database_snapshot()

    snapshot = store.get_snapshot(CREATOR, connected_user_ids={GUEST})

    assert [participant.connected for participant in snapshot.participants] == [False, True]
    assert fake_storage.database_snapshot() == before


def test_require_room_id_is_read_only_and_rejects_mismatch(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    store.require_room_id(ROOM_ID)

    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0
    assert fake_storage.transaction_events == []

    fake_storage.reset_observations()
    _assert_fault("room_not_found", lambda: store.require_room_id(OTHER_ROOM_ID))
    assert fake_storage.database_snapshot() == before
    assert fake_storage.writes == 0
    assert fake_storage.transaction_events == []

    fake_storage.execute("DELETE FROM participants WHERE user_id = ?", CREATOR)
    corrupted = fake_storage.database_snapshot()
    fake_storage.reset_observations()
    with pytest.raises(StorageFault):
        store.require_room_id(ROOM_ID)
    assert fake_storage.database_snapshot() == corrupted
    assert fake_storage.writes == 0
    assert fake_storage.transaction_events == []


def test_connection_timestamp_updates_are_transactional_identity_checked_and_version_neutral(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store, now_ms=100)
    fake_storage.execute(
        "UPDATE participants SET disconnected_at = 150 WHERE user_id = ?",
        CREATOR,
    )
    room_before = _room_row(fake_storage).copy()
    actions_before = fake_storage.rows("actions")
    fake_storage.reset_observations()

    store.mark_connected(SocketIdentity(CREATOR, 0))

    assert fake_storage.rows("participants")[0]["disconnected_at"] is None
    assert _room_row(fake_storage) == room_before
    assert fake_storage.rows("actions") == actions_before
    _assert_sql_execution(fake_storage, MARK_CONNECTED_SQL, (CREATOR, 0))
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")

    fake_storage.reset_observations()
    store.mark_disconnected(SocketIdentity(CREATOR, 0), 200)

    assert fake_storage.rows("participants")[0]["disconnected_at"] == 200
    assert _room_row(fake_storage) == room_before
    assert fake_storage.rows("actions") == actions_before
    _assert_sql_execution(
        fake_storage,
        MARK_DISCONNECTED_SQL,
        (200, CREATOR, 0),
    )
    _assert_transaction_encloses_sql(fake_storage, "COMMIT")


def test_connection_timestamp_updates_reject_invalid_identity_and_time_without_writes(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store, now_ms=100)
    before = fake_storage.database_snapshot()
    fake_storage.reset_observations()

    _assert_fault(
        "not_joined",
        lambda: store.mark_connected(SocketIdentity(CREATOR, 1)),
    )
    assert fake_storage.database_snapshot() == before
    assert fake_storage.transaction_events == ["BEGIN", "ROLLBACK"]

    fake_storage.reset_observations()
    _assert_fault(
        "invalid_request",
        lambda: store.mark_disconnected(SocketIdentity(CREATOR, 0), True),
    )
    assert fake_storage.database_snapshot() == before
    assert fake_storage.executions == []


@pytest.mark.parametrize("invalid_now", [True, -1, 1.5, MAX_TIMESTAMP_MS + 1])
def test_nonnegative_integer_timestamp_validation_rejects_invalid_values(
    store: RoomStore,
    invalid_now: object,
) -> None:
    _assert_fault(
        "invalid_request",
        lambda: store.create_room(
            room_id=ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=CREATOR,
            now_ms=invalid_now,
        ),
    )


def test_timestamp_ttl_boundary_accepts_exact_max_and_rejects_overflow(
    fake_storage: FakeRoomStorage,
) -> None:
    valid_store = RoomStore(
        fake_storage,
        entropy=EntropySequence(),
        random_uint32=Uint32Sequence(),
    )
    valid_store.initialize_schema()
    snapshot = valid_store.create_room(
        room_id=ROOM_ID,
        descriptor=DESCRIPTOR,
        created_by=CREATOR,
        now_ms=MAX_TIMESTAMP_MS - WAITING_ROOM_TTL_MS,
    )
    assert int(snapshot.expires_at.timestamp() * 1_000) == MAX_TIMESTAMP_MS

    overflow_storage = FakeRoomStorage()
    overflow_store = RoomStore(
        overflow_storage,
        entropy=EntropySequence(),
        random_uint32=Uint32Sequence(),
    )
    overflow_store.initialize_schema()
    _assert_fault(
        "invalid_request",
        lambda: overflow_store.create_room(
            room_id=OTHER_ROOM_ID,
            descriptor=DESCRIPTOR,
            created_by=CREATOR,
            now_ms=MAX_TIMESTAMP_MS - WAITING_ROOM_TTL_MS + 1,
        ),
    )


def test_global_version_overflow_fails_closed_and_rolls_back(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _create(store)
    fake_storage.execute(
        "UPDATE room SET version = ? WHERE singleton = 1", MAX_SAFE_INTEGER
    )
    before = fake_storage.database_snapshot()

    with pytest.raises(StorageFault):
        store.execute_command(
            SocketIdentity(CREATOR, 0),
            ReadyCommand(expected_version=MAX_SAFE_INTEGER),
            10,
        )

    assert fake_storage.database_snapshot() == before


@pytest.mark.parametrize(
    ("room_id", "created_by"),
    [("", CREATOR), (ROOM_ID, ""), (True, CREATOR), (ROOM_ID, False)],
)
def test_identifier_validation_rejects_empty_and_non_string_values(
    store: RoomStore,
    room_id: object,
    created_by: object,
) -> None:
    _assert_fault(
        "invalid_request",
        lambda: store.create_room(
            room_id=room_id,
            descriptor=DESCRIPTOR,
            created_by=created_by,
            now_ms=0,
        ),
    )


def test_every_production_cursor_is_exhausted_before_return(
    store: RoomStore,
    fake_storage: FakeRoomStorage,
) -> None:
    _activate(store)
    ticket = store.issue_ticket(CREATOR, 400)
    store.consume_ticket(ticket.ticket, 401)
    store.execute_command(
        SocketIdentity(CREATOR, 0),
        MoveCommand(direction="left", expected_board_version=0),
        500,
    )

    assert fake_storage.cursors
    assert fake_storage.all_cursors_consumed


def test_owned_files_exclude_out_of_scope_markers() -> None:
    root = Path(__file__).parents[1]
    owned = (
        root / "src/retro_backend/multiplayer/bindings.py",
        root / "src/retro_backend/multiplayer/storage.py",
        root / "tests/multiplayer_fakes.py",
        root / "tests/test_multiplayer_storage.py",
    )
    markers = tuple(
        "".join(parts)
        for parts in (("te", "am"), ("connect", "4"), ('"dr', 'op"'))
    )

    for path in owned:
        source = path.read_text().lower()
        assert all(marker not in source for marker in markers), path
