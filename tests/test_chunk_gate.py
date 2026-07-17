import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from retro_backend.chunk_gate import (
    ChunkGateClient,
    ChunkGateState,
    ChunkGateTransitionError,
)
from retro_backend.contracts import EventRange, IngestAcknowledgment

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


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

    reservation = state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    assert reservation.status == "reserved"
    assert reservation.ack is None
    assert state.model_dump(mode="json") == {
        "payload_sha256": DIGEST_A,
        "state": "publishing",
        "lease_expires_at_ms": 130,
        "acknowledgment": None,
        "expires_at_ms": None,
    }


def test_chunk_gate_state_reports_same_hash_active_publication() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_A, now_ms=129, lease_ms=90)

    assert reservation.status == "publishing"
    assert state.lease_expires_at_ms == 130


def test_chunk_gate_state_allows_expired_lease_takeover() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_B, now_ms=130, lease_ms=50)

    assert reservation.status == "reserved"
    assert state.payload_sha256 == DIGEST_B
    assert state.lease_expires_at_ms == 180


def test_chunk_gate_state_rejects_different_hash_during_active_lease() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    reservation = state.reserve(DIGEST_B, now_ms=129, lease_ms=30)

    assert reservation.status == "conflict"
    assert state.payload_sha256 == DIGEST_A


def test_chunk_gate_state_accepts_and_returns_cached_duplicate() -> None:
    state = ChunkGateState()
    acknowledgment = _acknowledgment()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    accepted = state.accept(
        DIGEST_A,
        acknowledgment.model_dump(mode="json"),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )
    reservation = state.reserve(DIGEST_A, now_ms=500, lease_ms=30)

    assert accepted is True
    assert state.state == "accepted"
    assert state.lease_expires_at_ms is None
    assert state.expires_at_ms == 1_120
    assert reservation.status == "duplicate"
    assert reservation.ack == acknowledgment


def test_chunk_gate_state_rejects_different_hash_for_accepted_receipt() -> None:
    state = ChunkGateState()
    acknowledgment = _acknowledgment()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        acknowledgment,
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    reservation = state.reserve(DIGEST_B, now_ms=500, lease_ms=30)

    assert reservation.status == "conflict"


def test_chunk_gate_state_release_only_clears_matching_digest_lease() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    assert state.release(DIGEST_B) is False
    assert state.state == "publishing"
    assert state.release(DIGEST_A) is True
    assert state == ChunkGateState()


def test_chunk_gate_state_rejects_accept_for_non_owner_digest() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)

    with pytest.raises(ChunkGateTransitionError, match="matching active lease"):
        state.accept(
            DIGEST_B,
            _acknowledgment(),
            now_ms=120,
            dedupe_ttl_ms=1_000,
        )


def test_chunk_gate_state_expires_receipt_at_dedupe_ttl() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        _acknowledgment(),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    assert state.expire(now_ms=1_119) is False
    assert state.expire(now_ms=1_120) is True
    assert state == ChunkGateState()
    assert state.reserve(DIGEST_B, now_ms=1_120, lease_ms=30).status == "reserved"


def test_chunk_gate_state_round_trips_through_serializable_data() -> None:
    state = ChunkGateState()
    state.reserve(DIGEST_A, now_ms=100, lease_ms=30)
    state.accept(
        DIGEST_A,
        _acknowledgment(),
        now_ms=120,
        dedupe_ttl_ms=1_000,
    )

    restored = ChunkGateState.model_validate(state.model_dump(mode="json"))

    assert restored == state


async def test_chunk_gate_client_hashes_identity_and_orders_explicit_rpc_calls(
    fake_env: Any,
) -> None:
    client = ChunkGateClient(
        fake_env.CHUNK_GATE,
        lease_ms=321,
        dedupe_ttl_ms=654,
    )
    acknowledgment = _acknowledgment()

    reservation = await client.reserve("usr_01", "chk_01", DIGEST_A, 100)
    await client.accept(DIGEST_A, acknowledgment, 120)
    await client.release(DIGEST_A)

    expected_name = hashlib.sha256(b"usr_01:chk_01").hexdigest()
    assert reservation.status == "reserved"
    assert fake_env.CHUNK_GATE.id_from_name_calls == [expected_name]
    assert fake_env.CHUNK_GATE.rpc_order == [
        "idFromName",
        "get",
        "reserve",
        "accept",
        "release",
    ]
    assert fake_env.CHUNK_GATE.stub.reserve_calls == [(DIGEST_A, 100, 321)]
    assert fake_env.CHUNK_GATE.stub.accept_calls == [
        (
            DIGEST_A,
            acknowledgment.model_dump(mode="json"),
            120,
            654,
        )
    ]
    assert fake_env.CHUNK_GATE.stub.release_calls == [DIGEST_A]


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
        dedupe_ttl_ms=86_400_000,
    )

    reservation = await client.reserve("usr_01", "chk_01", DIGEST_A, 100)

    assert reservation.status == "duplicate"
    assert reservation.ack == acknowledgment
