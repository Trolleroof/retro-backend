import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from retro_backend.consumers.gameplay import GameplayConsumer
from retro_backend.contracts import (
    EpisodeReady,
    GameplayProcessResult,
    TransientGameplayError,
)
from retro_backend.episode_assembler import (
    DEFAULT_EPISODE_IDLE_TTL_MS,
    EpisodeAssembler,
    EpisodeAssemblerClient,
    EpisodeChunkRecord,
    EpisodeState,
    episode_object_name,
)

USER_ID = "usr_018f0000-0000-7000-8000-000000000000"
GAME_ID = "game_018f0000-0000-7000-8000-000000000001"
NOW_MS = 1_784_239_200_000
IDLE_TTL_MS = 3_600_000

IDENTITY: dict[str, Any] = {
    "user_id": USER_ID,
    "game_id": GAME_ID,
    "game_type": "tetris",
    "game_version": "1",
}


def _chunk(
    sequence: int,
    *,
    first: int,
    last: int,
    episode_ended: bool = False,
    chunk_id: str | None = None,
) -> EpisodeChunkRecord:
    return EpisodeChunkRecord(
        chunk_id=chunk_id or f"chk_{sequence:012d}",
        chunk_sequence=sequence,
        first_event_sequence=first,
        last_event_sequence=last,
        created_at_ms=NOW_MS + sequence,
        episode_ended=episode_ended,
    )


def _record(
    state: EpisodeState,
    chunk: EpisodeChunkRecord,
    *,
    now_ms: int = NOW_MS,
    max_chunks: int = 512,
) -> EpisodeReady | None:
    return state.record(
        chunk,
        now_ms=now_ms,
        idle_ttl_ms=IDLE_TTL_MS,
        max_chunks=max_chunks,
        **IDENTITY,
    )


# --- pure state machine ------------------------------------------------------


def test_single_chunk_episode_completes_on_episode_ended() -> None:
    state = EpisodeState()

    episode = _record(state, _chunk(1, first=1, last=5, episode_ended=True))

    assert episode is not None
    assert episode.status == "complete"
    assert episode.chunk_count == 1
    assert episode.first_event_sequence == 1
    assert episode.last_event_sequence == 5
    assert episode.user_id == USER_ID
    assert episode.game_id == GAME_ID


def test_multi_chunk_episode_emits_once_with_ordered_chunks() -> None:
    state = EpisodeState()

    assert _record(state, _chunk(1, first=1, last=5)) is None
    assert _record(state, _chunk(2, first=6, last=7)) is None
    episode = _record(state, _chunk(3, first=8, last=9, episode_ended=True))

    assert episode is not None
    assert episode.status == "complete"
    assert episode.chunk_count == 3
    assert [chunk.chunk_sequence for chunk in episode.chunks] == [1, 2, 3]
    assert episode.first_event_sequence == 1
    assert episode.last_event_sequence == 9


def test_out_of_order_chunks_still_complete_in_sequence_order() -> None:
    state = EpisodeState()

    assert _record(state, _chunk(2, first=6, last=7)) is None
    # The terminal chunk arrives before the opener; the episode is only complete
    # once the gap it left behind is filled.
    assert _record(state, _chunk(3, first=8, last=9, episode_ended=True)) is None
    episode = _record(state, _chunk(1, first=1, last=5))

    assert episode is not None
    assert episode.status == "complete"
    assert [chunk.chunk_sequence for chunk in episode.chunks] == [1, 2, 3]


def test_redelivered_chunk_is_ignored() -> None:
    state = EpisodeState()
    chunk = _chunk(1, first=1, last=5)

    assert _record(state, chunk) is None
    assert _record(state, chunk) is None

    assert len(state.chunks) == 1


def test_redelivered_terminal_chunk_does_not_emit_twice() -> None:
    state = EpisodeState()
    chunk = _chunk(1, first=1, last=5, episode_ended=True)

    episode = _record(state, chunk)
    assert episode is not None
    state.mark_emitted(episode.status, now_ms=NOW_MS)

    assert _record(state, chunk) is None
    assert _record(state, _chunk(2, first=6, last=7)) is None
    assert len(state.chunks) == 1


def test_conflicting_chunk_at_same_sequence_is_rejected() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))

    with pytest.raises(ValueError, match="different chunk at that sequence"):
        _record(state, _chunk(1, first=1, last=5, chunk_id="chk_000000000999"))


def test_chunk_from_a_different_episode_is_rejected() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))

    with pytest.raises(ValueError, match="identity does not match"):
        state.record(
            _chunk(2, first=6, last=7),
            user_id=USER_ID,
            game_id=GAME_ID,
            game_type="snake",
            game_version="1",
            now_ms=NOW_MS,
            idle_ttl_ms=IDLE_TTL_MS,
            max_chunks=512,
        )


def test_sequence_gap_blocks_completion() -> None:
    state = EpisodeState()

    assert _record(state, _chunk(1, first=1, last=5)) is None
    # chunk_sequence 2 never arrives.
    assert _record(state, _chunk(3, first=8, last=9, episode_ended=True)) is None


def test_event_sequence_gap_blocks_completion() -> None:
    state = EpisodeState()

    assert _record(state, _chunk(1, first=1, last=5)) is None
    # Contiguous chunk_sequence, but event 6 is missing from the chain.
    assert _record(state, _chunk(2, first=7, last=9, episode_ended=True)) is None


# --- TTL flush ---------------------------------------------------------------


def test_flush_before_the_deadline_keeps_the_episode_open() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))

    assert state.flush(now_ms=NOW_MS + IDLE_TTL_MS - 1) is None


def test_quit_mid_game_is_abandoned_after_the_ttl() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))

    episode = state.flush(now_ms=NOW_MS + IDLE_TTL_MS)

    assert episode is not None
    assert episode.status == "abandoned"
    assert episode.chunk_count == 1


def test_gap_with_episode_ended_is_incomplete_after_the_ttl() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))
    _record(state, _chunk(3, first=8, last=9, episode_ended=True))

    episode = state.flush(now_ms=NOW_MS + IDLE_TTL_MS)

    assert episode is not None
    assert episode.status == "incomplete"
    assert episode.chunk_count == 2


def test_flush_is_a_no_op_for_an_empty_or_emitted_episode() -> None:
    assert EpisodeState().flush(now_ms=NOW_MS) is None

    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))
    state.mark_emitted("abandoned", now_ms=NOW_MS)

    assert state.flush(now_ms=NOW_MS + IDLE_TTL_MS) is None


def test_recording_extends_the_idle_deadline() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))
    assert state.idle_deadline_ms == NOW_MS + IDLE_TTL_MS

    _record(state, _chunk(2, first=6, last=7), now_ms=NOW_MS + 1_000)

    assert state.idle_deadline_ms == NOW_MS + 1_000 + IDLE_TTL_MS
    assert state.flush(now_ms=NOW_MS + IDLE_TTL_MS) is None


def test_chunk_cap_force_flushes_the_episode() -> None:
    state = EpisodeState()

    assert _record(state, _chunk(1, first=1, last=1), max_chunks=2) is None
    episode = _record(state, _chunk(2, first=2, last=2), max_chunks=2)

    assert episode is not None
    assert episode.status == "abandoned"
    assert episode.chunk_count == 2


def test_chunk_cap_does_not_downgrade_a_complete_episode() -> None:
    state = EpisodeState()

    _record(state, _chunk(1, first=1, last=1), max_chunks=2)
    episode = _record(state, _chunk(2, first=2, last=2, episode_ended=True), max_chunks=2)

    assert episode is not None
    assert episode.status == "complete"


def test_state_round_trips_through_json() -> None:
    state = EpisodeState()
    _record(state, _chunk(1, first=1, last=5))
    _record(state, _chunk(2, first=6, last=7))

    restored = EpisodeState.model_validate_json(state.model_dump_json())

    assert restored == state
    episode = _record(restored, _chunk(3, first=8, last=9, episode_ended=True))
    assert episode is not None
    assert episode.status == "complete"


@pytest.mark.parametrize(
    ("now_ms", "idle_ttl_ms", "max_chunks"),
    [(-1, IDLE_TTL_MS, 512), (NOW_MS, 0, 512), (NOW_MS, IDLE_TTL_MS, 0)],
)
def test_record_rejects_invalid_bounds(
    now_ms: int,
    idle_ttl_ms: int,
    max_chunks: int,
) -> None:
    with pytest.raises(ValueError):
        EpisodeState().record(
            _chunk(1, first=1, last=5),
            user_id=USER_ID,
            game_id=GAME_ID,
            game_type="tetris",
            game_version="1",
            now_ms=now_ms,
            idle_ttl_ms=idle_ttl_ms,
            max_chunks=max_chunks,
        )


# --- Durable Object runtime --------------------------------------------------


class FakeDurableObjectStorage:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.alarms: list[int] = []
        self.delete_alarm_calls = 0

    async def get(self, key: str) -> object | None:
        return self.values.get(key)

    async def put(self, key: str, value: object) -> object:
        self.values[key] = value
        return None

    async def setAlarm(self, scheduled_time_ms: int) -> object:
        self.alarms.append(scheduled_time_ms)
        return None

    async def deleteAlarm(self) -> object:
        self.delete_alarm_calls += 1
        return None

    async def deleteAll(self) -> object:
        self.values.clear()
        return None


class FakeEpisodeQueue:
    def __init__(self) -> None:
        self.send_calls: list[object] = []
        self.failure: Exception | None = None

    async def send(self, message: object) -> object:
        if self.failure is not None:
            raise self.failure
        self.send_calls.append(message)
        return {"accepted": True}


def _durable_object(
    queue: FakeEpisodeQueue | None = None,
) -> tuple[Any, FakeDurableObjectStorage, FakeEpisodeQueue]:
    storage = FakeDurableObjectStorage()
    episode_queue = queue if queue is not None else FakeEpisodeQueue()
    assembler = EpisodeAssembler(
        cast(Any, SimpleNamespace(storage=storage)),
        SimpleNamespace(EPISODE_QUEUE=episode_queue),
    )
    return assembler, storage, episode_queue


def _request(chunk: EpisodeChunkRecord) -> dict[str, object]:
    return {"chunk": chunk.model_dump(mode="json"), **IDENTITY}


async def test_durable_object_persists_state_and_arms_the_alarm() -> None:
    assembler, storage, episode_queue = _durable_object()

    result = await assembler.record(
        _request(_chunk(1, first=1, last=5)),
        NOW_MS,
        IDLE_TTL_MS,
        512,
    )

    assert result == {"emitted_status": None}
    assert storage.alarms == [NOW_MS + IDLE_TTL_MS]
    assert episode_queue.send_calls == []
    stored = EpisodeState.model_validate_json(cast(str, storage.values["episode"]))
    assert len(stored.chunks) == 1


async def test_durable_object_publishes_one_message_on_completion() -> None:
    assembler, storage, episode_queue = _durable_object()

    await assembler.record(_request(_chunk(1, first=1, last=5)), NOW_MS, IDLE_TTL_MS, 512)
    result = await assembler.record(
        _request(_chunk(2, first=6, last=7, episode_ended=True)),
        NOW_MS,
        IDLE_TTL_MS,
        512,
    )

    assert result == {"emitted_status": "complete"}
    assert len(episode_queue.send_calls) == 1
    message = cast(dict[str, Any], episode_queue.send_calls[0])
    assert message["status"] == "complete"
    assert message["version"] == 1
    assert [chunk["chunk_sequence"] for chunk in message["chunks"]] == [1, 2]
    # A completed episode no longer needs an idle alarm.
    assert storage.delete_alarm_calls == 1


async def test_durable_object_does_not_republish_a_redelivered_chunk() -> None:
    assembler, _storage, episode_queue = _durable_object()
    terminal = _request(_chunk(1, first=1, last=5, episode_ended=True))

    first = await assembler.record(terminal, NOW_MS, IDLE_TTL_MS, 512)
    second = await assembler.record(terminal, NOW_MS, IDLE_TTL_MS, 512)

    assert first == {"emitted_status": "complete"}
    assert second == {"emitted_status": None}
    assert len(episode_queue.send_calls) == 1


async def test_durable_object_retries_emission_when_publishing_fails() -> None:
    episode_queue = FakeEpisodeQueue()
    episode_queue.failure = RuntimeError("queue unavailable")
    assembler, _storage, _queue = _durable_object(episode_queue)
    terminal = _request(_chunk(1, first=1, last=5, episode_ended=True))

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await assembler.record(terminal, NOW_MS, IDLE_TTL_MS, 512)

    # The failed send left no emission recorded, so the retry publishes.
    episode_queue.failure = None
    assert await assembler.record(terminal, NOW_MS, IDLE_TTL_MS, 512) == {
        "emitted_status": "complete"
    }
    assert len(episode_queue.send_calls) == 1


async def test_durable_object_alarm_flushes_an_abandoned_episode() -> None:
    assembler, _storage, episode_queue = _durable_object()
    await assembler.record(_request(_chunk(1, first=1, last=5)), NOW_MS, IDLE_TTL_MS, 512)

    await assembler.alarm()

    assert len(episode_queue.send_calls) == 1
    message = cast(dict[str, Any], episode_queue.send_calls[0])
    assert message["status"] == "abandoned"

    # The alarm is not re-armed and a second firing is inert.
    await assembler.alarm()
    assert len(episode_queue.send_calls) == 1


async def test_durable_object_alarm_is_inert_without_chunks() -> None:
    assembler, _storage, episode_queue = _durable_object()

    await assembler.alarm()

    assert episode_queue.send_calls == []


async def test_durable_object_rejects_state_that_is_not_json_text() -> None:
    assembler, storage, _queue = _durable_object()
    storage.values["episode"] = 17

    with pytest.raises(TypeError, match="must be JSON text"):
        await assembler.record(
            _request(_chunk(1, first=1, last=5)),
            NOW_MS,
            IDLE_TTL_MS,
            512,
        )


# --- RPC client --------------------------------------------------------------


class FakeEpisodeAssemblerStub:
    def __init__(self) -> None:
        self.record_calls: list[tuple[object, int, int, int]] = []
        self.result: object = {"emitted_status": None}
        self.failure: Exception | None = None

    async def record(
        self,
        chunk: object,
        now_ms: int,
        idle_ttl_ms: int,
        max_chunks: int,
    ) -> object:
        if self.failure is not None:
            raise self.failure
        self.record_calls.append((chunk, now_ms, idle_ttl_ms, max_chunks))
        return self.result


class FakeEpisodeAssemblerNamespace:
    def __init__(self) -> None:
        self.stub = FakeEpisodeAssemblerStub()
        self.id_from_name_calls: list[str] = []

    def idFromName(self, name: str) -> object:
        self.id_from_name_calls.append(name)
        return SimpleNamespace(name=name)

    def get(self, _object_id: object) -> FakeEpisodeAssemblerStub:
        return self.stub


async def test_client_addresses_one_object_per_user_and_game() -> None:
    namespace = FakeEpisodeAssemblerNamespace()
    client = EpisodeAssemblerClient(cast(Any, namespace), idle_ttl_ms=IDLE_TTL_MS)

    status = await client.record(
        _chunk(1, first=1, last=5),
        now_ms=NOW_MS,
        **IDENTITY,
    )

    assert status is None
    assert namespace.id_from_name_calls == [episode_object_name(USER_ID, GAME_ID)]
    request, now_ms, idle_ttl_ms, max_chunks = namespace.stub.record_calls[0]
    assert now_ms == NOW_MS
    assert idle_ttl_ms == IDLE_TTL_MS
    assert max_chunks == 512
    assert cast(dict[str, Any], request)["user_id"] == USER_ID


async def test_client_returns_the_emitted_status() -> None:
    namespace = FakeEpisodeAssemblerNamespace()
    namespace.stub.result = {"emitted_status": "complete"}
    client = EpisodeAssemblerClient(cast(Any, namespace))

    status = await client.record(
        _chunk(1, first=1, last=5, episode_ended=True),
        now_ms=NOW_MS,
        **IDENTITY,
    )

    assert status == "complete"


def test_client_defaults_to_the_documented_idle_ttl() -> None:
    client = EpisodeAssemblerClient(cast(Any, FakeEpisodeAssemblerNamespace()))

    assert client.idle_ttl_ms == DEFAULT_EPISODE_IDLE_TTL_MS
    assert DEFAULT_EPISODE_IDLE_TTL_MS == 6 * 60 * 60 * 1000


def test_distinct_games_address_distinct_objects() -> None:
    other_game = "game_018f0000-0000-7000-8000-000000000002"

    assert episode_object_name(USER_ID, GAME_ID) != episode_object_name(
        USER_ID, other_game
    )


def test_episode_message_stays_within_the_queue_size_guard() -> None:
    state = EpisodeState()
    for sequence in range(1, 512):
        _record(state, _chunk(sequence, first=sequence, last=sequence), max_chunks=512)
    episode = _record(
        state,
        _chunk(512, first=512, last=512, episode_ended=True),
        max_chunks=512,
    )

    assert episode is not None
    encoded = json.dumps(episode.model_dump(mode="json"), separators=(",", ":"))

    assert episode.chunk_count == 512
    assert len(encoded.encode("utf-8")) < 120 * 1024


# --- consumer integration ----------------------------------------------------


def _episode_env(fake_env: Any) -> FakeEpisodeAssemblerNamespace:
    namespace = FakeEpisodeAssemblerNamespace()
    fake_env.EPISODE_ASSEMBLER = namespace
    return namespace


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


async def _process_payload(
    fake_env: Any,
    payload: dict[str, Any],
) -> GameplayProcessResult:
    raw_body = _canonical(payload)
    raw_key = (
        f"raw/user={payload['user_id']}/game={payload['game_id']}"
        f"/chunk={payload['chunk_id']}.json"
    )
    payload_sha256 = hashlib.sha256(raw_body).hexdigest()
    fake_env.RAW_BUCKET.seed_raw(raw_key, raw_body, payload_sha256)
    return await GameplayConsumer(fake_env).process(
        {
            "version": 1,
            "r2_key": raw_key,
            "payload_sha256": payload_sha256,
            "user_id": payload["user_id"],
            "game_id": payload["game_id"],
            "chunk_id": payload["chunk_id"],
            "received_at_ms": NOW_MS,
        }
    )


async def test_consumer_records_each_validated_chunk_against_its_episode(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    continuation_fixture_payload: dict[str, Any],
) -> None:
    namespace = _episode_env(fake_env)
    terminal = copy.deepcopy(continuation_fixture_payload)
    terminal["events"][-1]["event_type"] = "episode_ended"

    assert (await _process_payload(fake_env, fixture_payload)).status == "processed"
    assert (await _process_payload(fake_env, terminal)).status == "processed"

    assert len(namespace.stub.record_calls) == 2
    first = cast(dict[str, Any], namespace.stub.record_calls[0][0])
    second = cast(dict[str, Any], namespace.stub.record_calls[1][0])
    assert first["game_type"] == "tetris"
    assert first["chunk"]["chunk_sequence"] == 1
    assert first["chunk"]["episode_ended"] is False
    assert second["chunk"]["chunk_sequence"] == 2
    assert second["chunk"]["episode_ended"] is True
    assert second["chunk"]["first_event_sequence"] == 6
    assert second["chunk"]["last_event_sequence"] == 7


async def test_consumer_skips_assembly_when_the_binding_is_absent(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    assert not hasattr(fake_env, "EPISODE_ASSEMBLER")

    result = await _process_payload(fake_env, fixture_payload)

    assert result.status == "processed"


async def test_consumer_retries_when_episode_assembly_fails(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    namespace = _episode_env(fake_env)
    namespace.stub.failure = RuntimeError("durable object unavailable")

    with pytest.raises(TransientGameplayError, match="episode assembly unavailable"):
        await _process_payload(fake_env, fixture_payload)


async def test_consumer_assembles_only_after_the_artifact_is_durable(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    namespace = _episode_env(fake_env)
    fake_env.RAW_BUCKET.put_failure = RuntimeError("R2 unavailable")

    with pytest.raises(TransientGameplayError):
        await _process_payload(fake_env, fixture_payload)

    assert namespace.stub.record_calls == []
