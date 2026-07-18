import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from retro_backend.chunk_gate import (
    ChunkGate,
    ChunkGateClient,
    ChunkGateState,
    ChunkGateTransitionError,
)
from retro_backend.contracts import EventRange, IngestAcknowledgment

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
LEASE_A = "1" * 64
LEASE_B = "2" * 64


class FakeDurableObjectStorage:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.alarm_at_ms: int | None = None
        self.operations: list[tuple[object, ...]] = []

    async def get(self, key: str) -> object | None:
        self.operations.append(("get", key))
        return self.values.get(key)

    async def put(self, key: str, value: object) -> None:
        self.operations.append(("put", key, value))
        self.values[key] = value

    async def setAlarm(self, scheduled_time_ms: int) -> None:
        self.operations.append(("setAlarm", scheduled_time_ms))
        self.alarm_at_ms = scheduled_time_ms

    async def deleteAlarm(self) -> None:
        self.operations.append(("deleteAlarm",))
        self.alarm_at_ms = None

    async def deleteAll(self) -> None:
        self.operations.append(("deleteAll",))
        self.values.clear()

    def receipt_json(self) -> str:
        receipt = self.values["receipt"]
        assert isinstance(receipt, str)
        return receipt


class FakeDurableObjectContext:
    def __init__(self, storage: FakeDurableObjectStorage) -> None:
        self.storage = storage


def _runtime_gate(storage: FakeDurableObjectStorage) -> ChunkGate:
    return ChunkGate(FakeDurableObjectContext(storage), SimpleNamespace())


def _acknowledgment() -> IngestAcknowledgment:
    return IngestAcknowledgment(
        duplicate=False,
        game_id="game_01",
        chunk_id="chk_01",
        chunk_sequence=1,
        accepted_event_range=EventRange(first=1, last=4),
        server_received_at_ms=1_784_241_600_000,
    )


def test_chunk_gate_state_reserves_new_digest_with_explicit_lease() -> None:
    state = ChunkGateState()

    reservation = state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    assert reservation.status == "reserved"
    assert reservation.lease_token == LEASE_A
    assert reservation.ack is None
    assert state.model_dump(mode="json") == {
        "payload_sha256": DIGEST_A,
        "state": "publishing",
        "lease_token": LEASE_A,
        "lease_expires_at_ms": 130,
        "acknowledgment": None,
        "expires_at_ms": None,
    }


def test_chunk_gate_state_reports_same_hash_active_publication() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_A, LEASE_B, now_ms=129, lease_ms=90)

    assert reservation.status == "publishing"
    assert reservation.lease_token is None
    assert state.lease_token == LEASE_A
    assert state.lease_expires_at_ms == 130


def test_chunk_gate_state_allows_expired_lease_takeover() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_A, LEASE_B, now_ms=130, lease_ms=50)

    assert reservation.status == "reserved"
    assert reservation.lease_token == LEASE_B
    assert state.payload_sha256 == DIGEST_A
    assert state.lease_token == LEASE_B
    assert state.lease_expires_at_ms == 180


def test_chunk_gate_state_rejects_different_hash_during_active_lease() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_B, LEASE_B, now_ms=129, lease_ms=30)

    assert reservation.status == "conflict"
    assert state.payload_sha256 == DIGEST_A


def test_chunk_gate_state_accepts_and_returns_cached_duplicate() -> None:
    state = ChunkGateState()
    acknowledgment = _acknowledgment()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    accepted = state.accept(
        DIGEST_A,
        LEASE_A,
        acknowledgment.model_dump(mode="json"),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )
    reservation = state.reserve(DIGEST_A, LEASE_B, now_ms=500, lease_ms=30)

    assert accepted is True
    assert state.state == "accepted"
    assert state.lease_token is None
    assert state.lease_expires_at_ms is None
    assert state.expires_at_ms == 1_120
    assert reservation.status == "duplicate"
    assert reservation.ack == acknowledgment


def test_chunk_gate_state_rejects_different_hash_for_accepted_receipt() -> None:
    state = ChunkGateState()
    acknowledgment = _acknowledgment()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        LEASE_A,
        acknowledgment,
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    reservation = state.reserve(DIGEST_B, LEASE_B, now_ms=500, lease_ms=30)

    assert reservation.status == "conflict"


def test_chunk_gate_state_release_requires_matching_digest_and_token() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    assert state.release(DIGEST_B, LEASE_A) is False
    assert state.release(DIGEST_A, LEASE_B) is False
    assert state.state == "publishing"
    assert state.release(DIGEST_A, LEASE_A) is True
    assert state == ChunkGateState()


def test_chunk_gate_state_rejects_accept_for_non_owner_digest() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    with pytest.raises(ChunkGateTransitionError, match="matching active lease"):
        state.accept(
            DIGEST_B,
            LEASE_A,
            _acknowledgment(),
            now_ms=120,
            dedupe_ttl_ms=1_000,
        )


def test_stale_publisher_cannot_accept_after_same_digest_takeover() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)
    state.reserve(DIGEST_A, LEASE_B, now_ms=130, lease_ms=30)

    with pytest.raises(ChunkGateTransitionError, match="matching active lease"):
        state.accept(
            DIGEST_A,
            LEASE_A,
            _acknowledgment(),
            now_ms=131,
            dedupe_ttl_ms=1_000,
        )

    assert state.state == "publishing"
    assert state.lease_token == LEASE_B
    assert state.accept(
        DIGEST_A,
        LEASE_B,
        _acknowledgment(),
        now_ms=132,
        dedupe_ttl_ms=1_000,
    )


def test_stale_publisher_cannot_release_same_digest_takeover() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)
    state.reserve(DIGEST_A, LEASE_B, now_ms=130, lease_ms=30)

    assert state.release(DIGEST_A, LEASE_A) is False
    assert state.state == "publishing"
    assert state.lease_token == LEASE_B
    assert state.release(DIGEST_A, LEASE_B) is True


def test_chunk_gate_state_rejects_accept_at_expired_lease_boundary() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)

    with pytest.raises(ChunkGateTransitionError, match="matching active lease"):
        state.accept(
            DIGEST_A,
            LEASE_A,
            _acknowledgment(),
            now_ms=130,
            dedupe_ttl_ms=1_000,
        )

    assert state.state == "publishing"
    assert state.lease_token == LEASE_A


def test_chunk_gate_state_expires_receipt_at_dedupe_ttl() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        LEASE_A,
        _acknowledgment(),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    assert state.expire(now_ms=1_119) is False
    assert state.expire(now_ms=1_120) is True
    assert state == ChunkGateState()
    assert (
        state.reserve(DIGEST_B, LEASE_B, now_ms=1_120, lease_ms=30).status
        == "reserved"
    )


def test_chunk_gate_state_round_trips_through_serializable_data() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, LEASE_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        LEASE_A,
        _acknowledgment(),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    restored = ChunkGateState.model_validate(state.model_dump(mode="json"))

    assert restored == state


async def test_runtime_chunk_gate_persists_reservation_then_acceptance_json() -> None:
    storage = FakeDurableObjectStorage()
    gate = _runtime_gate(storage)
    acknowledgment = _acknowledgment()

    reservation = await gate.reserve(DIGEST_A, LEASE_A, 100, 30)

    assert reservation == {
        "status": "reserved",
        "lease_token": LEASE_A,
        "ack": None,
    }
    assert ChunkGateState.model_validate_json(storage.receipt_json()) == ChunkGateState(
        payload_sha256=DIGEST_A,
        state="publishing",
        lease_token=LEASE_A,
        lease_expires_at_ms=130,
    )
    assert storage.alarm_at_ms == 130

    accepted = await gate.accept(
        DIGEST_A,
        LEASE_A,
        acknowledgment.model_dump(mode="json"),
        120,
        1_000,
    )

    assert accepted is True
    persisted = ChunkGateState.model_validate_json(storage.receipt_json())
    assert persisted.state == "accepted"
    assert persisted.acknowledgment == acknowledgment
    assert persisted.expires_at_ms == 1_120
    assert storage.alarm_at_ms == 1_120


async def test_runtime_chunk_gate_stale_release_preserves_newer_receipt() -> None:
    storage = FakeDurableObjectStorage()
    gate = _runtime_gate(storage)
    await gate.reserve(DIGEST_A, LEASE_A, 100, 30)
    await gate.reserve(DIGEST_A, LEASE_B, 130, 50)
    operations_before_release = list(storage.operations)
    receipt_before_release = storage.receipt_json()

    released = await gate.release(DIGEST_A, LEASE_A)

    assert released is False
    assert storage.receipt_json() == receipt_before_release
    assert storage.alarm_at_ms == 180
    assert storage.operations == [*operations_before_release, ("get", "receipt")]


async def test_runtime_chunk_gate_release_and_alarm_clear_storage_safely() -> None:
    storage = FakeDurableObjectStorage()
    gate = _runtime_gate(storage)
    await gate.reserve(DIGEST_A, LEASE_A, 100, 30)

    assert await gate.release(DIGEST_A, LEASE_A) is True
    assert storage.values == {}
    assert storage.alarm_at_ms is None
    assert storage.operations[-2:] == [("deleteAlarm",), ("deleteAll",)]

    await gate.alarm()

    assert storage.values == {}
    assert storage.alarm_at_ms is None
    assert storage.operations[-2:] == [("deleteAlarm",), ("deleteAll",)]


async def test_chunk_gate_client_hashes_identity_and_orders_explicit_rpc_calls(
    fake_env: Any,
) -> None:
    client = ChunkGateClient(
        fake_env.CHUNK_GATE,
        lease_ms=321,
        dedupe_ttl_ms=654,
        token_factory=lambda: LEASE_A,
    )
    acknowledgment = _acknowledgment()

    reservation = await client.reserve("usr_01", "chk_01", DIGEST_A, 100)
    await client.accept(DIGEST_A, LEASE_A, acknowledgment, 120)

    expected_name = hashlib.sha256(b"usr_01:chk_01").hexdigest()
    assert reservation.status == "reserved"
    assert reservation.lease_token == LEASE_A
    assert fake_env.CHUNK_GATE.id_from_name_calls == [expected_name]
    assert fake_env.CHUNK_GATE.rpc_order == [
        "idFromName",
        "get",
        "reserve",
        "accept",
    ]
    assert fake_env.CHUNK_GATE.stub.reserve_calls == [
        (DIGEST_A, LEASE_A, 100, 321)
    ]
    assert fake_env.CHUNK_GATE.stub.accept_calls == [
        (
            DIGEST_A,
            LEASE_A,
            acknowledgment.model_dump(mode="json"),
            120,
            654,
        )
    ]
    assert fake_env.CHUNK_GATE.stub.release_calls == []


async def test_chunk_gate_client_keeps_concurrent_leases_token_scoped(
    fake_env: Any,
) -> None:
    tokens = iter((LEASE_A, LEASE_B))
    client = ChunkGateClient(
        fake_env.CHUNK_GATE,
        lease_ms=321,
        dedupe_ttl_ms=654,
        token_factory=lambda: next(tokens),
    )

    first = await client.reserve("usr_01", "chk_01", DIGEST_A, 100)
    second = await client.reserve("usr_01", "chk_02", DIGEST_B, 101)
    await client.release(DIGEST_A, LEASE_A)
    await client.accept(DIGEST_B, LEASE_B, _acknowledgment(), 120)

    assert first.lease_token == LEASE_A
    assert second.lease_token == LEASE_B
    assert fake_env.CHUNK_GATE.stub.release_calls == [(DIGEST_A, LEASE_A)]
    assert [call[:2] for call in fake_env.CHUNK_GATE.stub.accept_calls] == [
        (DIGEST_B, LEASE_B)
    ]


async def test_chunk_gate_client_normalizes_proxy_duplicate_receipt(
    fake_env: Any,
) -> None:
    acknowledgment = _acknowledgment()
    ack_data = acknowledgment.model_dump(mode="json")
    fake_env.CHUNK_GATE.reserve_result = SimpleNamespace(
        status="duplicate",
        ack=SimpleNamespace(
            **{
                **ack_data,
                "accepted_event_range": SimpleNamespace(
                    **ack_data["accepted_event_range"]
                ),
            }
        ),
    )
    client = ChunkGateClient(
        fake_env.CHUNK_GATE,
        lease_ms=30_000,
        dedupe_ttl_ms=172_800_000,
        token_factory=lambda: LEASE_A,
    )

    reservation = await client.reserve("usr_01", "chk_01", DIGEST_A, 100)

    assert reservation.status == "duplicate"
    assert reservation.ack == acknowledgment
