import hashlib
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from episode_fixtures import (  # pyright: ignore[reportImplicitRelativeImport]
    COMPLETED_AT_MS,
    GAME_ID,
    GAME_TYPE,
    MANIFEST_KEY,
    PARQUET_KEY,
    USER_ID,
    WRITTEN_AT_MS,
    seed_chunks,
    three_chunk_episode,
    validated_key,
)

from retro_backend.consumers.dispatch import dispatch_queue
from retro_backend.consumers.episodes import (
    EpisodeConsumer,
    episode_manifest_key,
    episode_parquet_key,
)
from retro_backend.contracts import TransientGameplayError
from retro_backend.episode_rows import EPISODE_COLUMNS
from retro_backend.parquet import PARQUET_MAGIC
from retro_backend.services.bc_export import extract_behavior_cloning_samples

EPISODE_ID = hashlib.sha256(f"{USER_ID}:{GAME_ID}".encode()).hexdigest()


def _consumer(fake_env: Any) -> EpisodeConsumer:
    return EpisodeConsumer(fake_env, clock_ms=lambda: WRITTEN_AT_MS)


def _put(fake_env: Any, key: str) -> Any:
    return next(call for call in fake_env.RAW_BUCKET.put_calls if call.key == key)


def test_keys_are_utc_partitioned_and_zero_padded() -> None:
    assert episode_parquet_key(
        game_type=GAME_TYPE,
        user_id=USER_ID,
        game_id=GAME_ID,
        completed_at_ms=COMPLETED_AT_MS,
    ) == PARQUET_KEY
    assert episode_manifest_key(PARQUET_KEY) == MANIFEST_KEY


def test_unroutable_game_type_never_escapes_the_key_prefix() -> None:
    with pytest.raises(ValueError, match="invalid format"):
        episode_parquet_key(
            game_type="../../etc",
            user_id=USER_ID,
            game_id=GAME_ID,
            completed_at_ms=COMPLETED_AT_MS,
        )


@pytest.mark.parametrize(
    "environment",
    [SimpleNamespace(DB=object()), SimpleNamespace(RAW_BUCKET=object())],
    ids=["missing-bucket", "missing-database"],
)
def test_missing_bindings_are_transient(environment: object) -> None:
    with pytest.raises(TransientGameplayError):
        EpisodeConsumer(environment)


async def test_happy_path_writes_parquet_then_manifest_then_the_d1_row(
    fake_env: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    result = await _consumer(fake_env).process(message)

    assert result.status == "written"
    assert result.episode_id == EPISODE_ID
    assert result.parquet_key == PARQUET_KEY
    assert result.manifest_key == MANIFEST_KEY
    assert result.row_count == 5

    # R2 strictly before D1, parquet strictly before its manifest.
    assert [call.key for call in fake_env.RAW_BUCKET.put_calls] == [
        PARQUET_KEY,
        MANIFEST_KEY,
    ]
    assert fake_env.operation_order[-2:] == ["r2.put", "r2.put"]

    parquet_body = _put(fake_env, PARQUET_KEY).body
    assert parquet_body.startswith(PARQUET_MAGIC)
    assert parquet_body.endswith(PARQUET_MAGIC)
    assert _put(fake_env, PARQUET_KEY).options["httpMetadata"] == {
        "contentType": "application/vnd.apache.parquet"
    }

    manifest = json.loads(_put(fake_env, MANIFEST_KEY).body)
    assert manifest == {
        "version": 1,
        "episode_id": EPISODE_ID,
        "user_id": USER_ID,
        "game_id": GAME_ID,
        "game_type": GAME_TYPE,
        "game_version": "1",
        "status": "complete",
        "chunk_count": 3,
        "row_count": 5,
        "first_event_sequence": 1,
        "last_event_sequence": 17,
        "completed_at_ms": COMPLETED_AT_MS,
        "parquet_key": PARQUET_KEY,
        "parquet_bytes": len(parquet_body),
        "parquet_sha256": hashlib.sha256(parquet_body).hexdigest(),
        "source_chunk_ids": [envelope["chunk_id"] for envelope in envelopes],
        "written_at_ms": WRITTEN_AT_MS,
    }

    assert len(fake_env.DB.run_calls) == 1
    assert "INSERT INTO episodes" in fake_env.DB.run_calls[0]
    assert "ON CONFLICT(episode_id) DO UPDATE" in fake_env.DB.run_calls[0]
    assert fake_env.DB.bind_calls[0] == (
        EPISODE_ID,
        USER_ID,
        GAME_ID,
        GAME_TYPE,
        "1",
        "complete",
        3,
        5,
        1,
        17,
        COMPLETED_AT_MS,
        PARQUET_KEY,
        len(parquet_body),
        hashlib.sha256(parquet_body).hexdigest(),
        MANIFEST_KEY,
        WRITTEN_AT_MS,
    )


@pytest.mark.parametrize("status", ["complete", "incomplete", "abandoned"])
async def test_every_terminal_status_produces_a_readable_file(
    fake_env: Any,
    status: str,
) -> None:
    envelopes, message = three_chunk_episode(
        status=status,
        ended=status == "complete",
    )
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    result = await _consumer(fake_env).process(message)

    assert result.status == "written"
    manifest = json.loads(_put(fake_env, MANIFEST_KEY).body)
    assert manifest["status"] == status
    assert manifest["row_count"] == 5


async def test_written_parquet_is_readable_and_matches_the_bc_join(
    fake_env: Any,
) -> None:
    """Issue #30 acceptance: a completed 2048 episode yields readable Parquet."""
    parquet = pytest.importorskip("pyarrow.parquet")
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    result = await _consumer(fake_env).process(message)

    table = parquet.read_table(io.BytesIO(_put(fake_env, PARQUET_KEY).body))
    samples = extract_behavior_cloning_samples(envelopes)
    assert table.num_rows == len(samples) == result.row_count
    assert table.schema.names == [column.name for column in EPISODE_COLUMNS]
    for row, sample in zip(table.to_pylist(), samples, strict=True):
        assert json.loads(row["observation_json"]) == sample["observation"]
        assert row["action"] == sample["action"]
        assert row["control_event_id"] == sample["control_event_id"]
        assert row["reward"] == sample["reward"]
        assert row["moved"] == sample["moved"]


async def test_unconvertible_validated_chunks_are_acknowledged(fake_env: Any) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    broken = json.dumps(
        {
            "artifact_version": 1,
            "chunk": {"schema_version": 2, "events": [{"no_event_id": True}]},
        }
    ).encode("utf-8")
    fake_env.RAW_BUCKET.seed_raw(validated_key(envelopes[0]), broken, "0" * 64)

    result = await _consumer(fake_env).process(message, message_id="broken")

    assert result.status == "invalid"
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.DB.run_calls == []


async def test_redelivery_converges_on_identical_artifacts_and_one_index_row(
    fake_env: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    consumer = _consumer(fake_env)

    first = await consumer.process(message, message_id="episode-1")
    second = await consumer.process(message, message_id="episode-1")

    assert first == second
    parquet_puts = [
        call for call in fake_env.RAW_BUCKET.put_calls if call.key == PARQUET_KEY
    ]
    assert len(parquet_puts) == 2
    assert parquet_puts[0].body == parquet_puts[1].body
    # Deterministic keys mean the retry overwrites rather than duplicating.
    assert len({call.key for call in fake_env.RAW_BUCKET.put_calls}) == 2
    assert fake_env.DB.bind_calls[0] == fake_env.DB.bind_calls[1]


async def test_d1_failure_after_a_successful_r2_write_is_retryable(
    fake_env: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    class FailingStatement:
        def bind(self, *_values: object) -> "FailingStatement":
            return self

        async def run(self) -> object:
            raise RuntimeError("D1 unavailable")

    def prepare(_sql: str) -> FailingStatement:
        return FailingStatement()

    fake_env.DB.prepare = prepare

    with pytest.raises(TransientGameplayError, match="episode index unavailable"):
        await _consumer(fake_env).process(message)

    # The artifacts are already durable; only the index write needs replaying.
    assert [call.key for call in fake_env.RAW_BUCKET.put_calls] == [
        PARQUET_KEY,
        MANIFEST_KEY,
    ]


async def test_r2_failure_never_writes_the_index(fake_env: Any) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    fake_env.RAW_BUCKET.put_failure = RuntimeError("R2 unavailable")

    with pytest.raises(TransientGameplayError, match="episode artifact unavailable"):
        await _consumer(fake_env).process(message)

    assert fake_env.DB.run_calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"not": "an episode"},
        b"not json",
        "not json",
        None,
    ],
    ids=["wrong-shape", "bytes", "text", "null"],
)
async def test_poison_messages_are_acknowledged_rather_than_retried(
    fake_env: Any,
    body: object,
) -> None:
    result = await _consumer(fake_env).process(body, message_id="poison")

    assert result.status == "invalid"
    assert result.parquet_key is None
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.DB.run_calls == []


async def test_json_encoded_message_bodies_are_accepted(fake_env: Any) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    result = await _consumer(fake_env).process(json.dumps(message).encode("utf-8"))

    assert result.status == "written"
    assert result.row_count == 5


async def test_dispatch_routes_the_episode_queue_and_acknowledges(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    queue_message = queue_message_factory(message, message_id="episode-ready")

    await dispatch_queue(
        queue_batch_factory("retro-episode-events", [queue_message]),
        fake_env,
    )

    assert queue_message.acked is True
    assert queue_message.retried is False
    assert {call.key for call in fake_env.RAW_BUCKET.put_calls} == {
        PARQUET_KEY,
        MANIFEST_KEY,
    }


async def test_dispatch_retries_the_episode_message_when_r2_is_unavailable(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    fake_env.RAW_BUCKET.put_failure = RuntimeError("R2 unavailable")
    queue_message = queue_message_factory(message, message_id="episode-ready")

    await dispatch_queue(
        queue_batch_factory("retro-episode-events", [queue_message]),
        fake_env,
    )

    assert queue_message.retried is True
    assert queue_message.acked is False


def test_two_users_sharing_a_game_id_do_not_collide() -> None:
    """game_id is client-chosen, so it namespaces only within a user.

    Both players can legitimately pick ``game_1``. Before the ``user=`` segment
    existed, the second episode overwrote the first at an identical key while the
    first player's index row kept pointing at it — one player's D1 row resolving
    to another player's gameplay.
    """
    other_user = "usr_018f0000-0000-7000-8000-0000000000ff"
    assert other_user != USER_ID

    mine = episode_parquet_key(
        game_type=GAME_TYPE,
        user_id=USER_ID,
        game_id=GAME_ID,
        completed_at_ms=COMPLETED_AT_MS,
    )
    theirs = episode_parquet_key(
        game_type=GAME_TYPE,
        user_id=other_user,
        game_id=GAME_ID,
        completed_at_ms=COMPLETED_AT_MS,
    )

    assert mine != theirs
    assert episode_manifest_key(mine) != episode_manifest_key(theirs)
    assert f"user={USER_ID}" in mine
    assert f"user={other_user}" in theirs
