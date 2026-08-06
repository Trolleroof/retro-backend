import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import pytest

import retro_backend.consumers.dispatch as dispatch_module
import retro_backend.consumers.gameplay as gameplay_module
from retro_backend.consumers.dispatch import dispatch_queue


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pointer(payload: dict[str, Any], raw_body: bytes) -> dict[str, object]:
    return {
        "version": 1,
        "r2_key": (
            f"raw/user={payload['user_id']}/game={payload['game_id']}"
            f"/chunk={payload['chunk_id']}.json"
        ),
        "payload_sha256": hashlib.sha256(raw_body).hexdigest(),
        "user_id": payload["user_id"],
        "game_id": payload["game_id"],
        "chunk_id": payload["chunk_id"],
        "received_at_ms": int(
            datetime(2026, 7, 17, 12, tzinfo=UTC).timestamp() * 1000
        ),
    }


def _variant(payload: dict[str, Any], suffix: str) -> dict[str, Any]:
    payload["game_id"] = f"game_{suffix}"
    payload["chunk_id"] = f"chk_{suffix}"
    for event in payload["events"]:
        event["game_id"] = payload["game_id"]
        event["chunk_id"] = payload["chunk_id"]
        event["event_id"] = f"{event['event_id']}_{suffix}"
    return payload


def _seed_later_message(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_message_factory: Any,
    operation_order: list[str],
    suffix: str,
) -> Any:
    payload = _variant(json.loads(json.dumps(fixture_payload)), suffix)
    raw_body = _canonical(payload)
    pointer = _pointer(payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )
    return queue_message_factory(
        pointer,
        message_id=f"later-{suffix}",
        operation_order=operation_order,
    )


class _BodyGetterFailureMessage:
    def __init__(self, operation_order: list[str]) -> None:
        self.id = "getter-failure"
        self.timestamp = datetime(2026, 7, 17, tzinfo=UTC)
        self.operation_order = operation_order
        self.ack_calls = 0
        self.retry_calls = 0

    @property
    def body(self) -> object:
        raise RuntimeError("private getter detail")

    def ack(self) -> None:
        self.ack_calls += 1

    def retry(self) -> None:
        self.operation_order.append("retry:getter-failure")
        self.retry_calls += 1


class _GetTimeFailure:
    def getTime(self) -> float:
        raise RuntimeError("private timestamp detail")


async def test_dispatch_processes_sequentially_with_per_message_ack_and_retry(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    first_payload = _variant(json.loads(json.dumps(fixture_payload)), "first")
    missing_payload = _variant(json.loads(json.dumps(fixture_payload)), "missing")
    later_payload = _variant(json.loads(json.dumps(fixture_payload)), "later")
    first_raw = _canonical(first_payload)
    missing_raw = _canonical(missing_payload)
    later_raw = _canonical(later_payload)
    first_pointer = _pointer(first_payload, first_raw)
    missing_pointer = _pointer(missing_payload, missing_raw)
    later_pointer = _pointer(later_payload, later_raw)
    fake_env.RAW_BUCKET.seed_raw(
        first_pointer["r2_key"], first_raw, first_pointer["payload_sha256"]
    )
    fake_env.RAW_BUCKET.seed_raw(
        later_pointer["r2_key"], later_raw, later_pointer["payload_sha256"]
    )
    message_order: list[str] = []
    messages = [
        queue_message_factory(
            first_pointer,
            message_id="valid-first",
            operation_order=message_order,
        ),
        queue_message_factory(
            {"invalid": "pointer"},
            message_id="permanent",
            operation_order=message_order,
        ),
        queue_message_factory(
            missing_pointer,
            message_id="transient",
            operation_order=message_order,
        ),
        queue_message_factory(
            later_pointer,
            message_id="valid-later",
            operation_order=message_order,
        ),
    ]

    await dispatch_queue(queue_batch_factory("retro-gameplay-events", messages), fake_env)

    assert [(message.acked, message.retried) for message in messages] == [
        (True, False),
        (True, False),
        (False, True),
        (True, False),
    ]
    assert message_order == [
        "ack:valid-first",
        "ack:permanent",
        "retry:transient",
        "ack:valid-later",
    ]
    assert any(call.key.startswith("rejected/") for call in fake_env.RAW_BUCKET.put_calls)
    assert any(
        call.key.endswith(f"chunk={later_payload['chunk_id']}.json")
        for call in fake_env.RAW_BUCKET.put_calls
    )


@pytest.mark.parametrize(
    "environment",
    [SimpleNamespace(), SimpleNamespace(RAW_BUCKET=None)],
    ids=["missing-raw-bucket", "null-raw-bucket"],
)
async def test_dispatch_retries_every_message_when_consumer_initialization_is_transient(
    environment: object,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    operation_order: list[str] = []
    messages = [
        queue_message_factory(
            {"message": index},
            message_id=f"message-{index}",
            operation_order=operation_order,
        )
        for index in range(3)
    ]

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", messages),
        environment,
    )

    assert [(message.acked, message.retried) for message in messages] == [
        (False, True),
        (False, True),
        (False, True),
    ]
    assert operation_order == [
        "retry:message-0",
        "retry:message-1",
        "retry:message-2",
    ]


async def test_dispatch_retries_all_messages_on_unexpected_consumer_init_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    class UnexpectedConsumer:
        def __init__(self, _environment: object) -> None:
            raise RuntimeError("private constructor detail")

    monkeypatch.setattr(dispatch_module, "GameplayConsumer", UnexpectedConsumer)
    operation_order: list[str] = []
    messages = [
        queue_message_factory(
            {"message": index},
            message_id=f"unexpected-init-{index}",
            operation_order=operation_order,
        )
        for index in range(3)
    ]

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", messages),
        fake_env,
    )

    assert [(message.acked, message.retried) for message in messages] == [
        (False, True),
        (False, True),
        (False, True),
    ]


async def test_dispatch_retries_proxy_getter_failure_and_continues(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    operation_order: list[str] = []
    failed = _BodyGetterFailureMessage(operation_order)
    later = _seed_later_message(
        fake_env,
        fixture_payload,
        queue_message_factory,
        operation_order,
        "getter",
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failed, later]),
        fake_env,
    )

    assert failed.retry_calls == 1
    assert failed.ack_calls == 0
    assert later.acked is True
    assert later.retried is False
    assert operation_order == ["retry:getter-failure", "ack:later-getter"]


async def test_dispatch_retries_get_time_failure_and_continues(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    operation_order: list[str] = []
    failed = queue_message_factory(
        {"invalid": True},
        message_id="get-time-failure",
        timestamp=_GetTimeFailure(),
        operation_order=operation_order,
    )
    later = _seed_later_message(
        fake_env,
        fixture_payload,
        queue_message_factory,
        operation_order,
        "get-time",
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failed, later]),
        fake_env,
    )

    assert failed.retried is True
    assert failed.acked is False
    assert later.acked is True
    assert later.retried is False
    assert operation_order == ["retry:get-time-failure", "ack:later-get-time"]


async def test_dispatch_retries_unexpected_consumer_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    original_process = dispatch_module.GameplayConsumer.process
    failing_body = {"unexpected": True}

    async def fail_selected(
        self: Any,
        pointer_body: object,
        *,
        message_id: object | None = None,
        message_timestamp_ms: object | None = None,
    ) -> Any:
        if pointer_body is failing_body:
            raise RuntimeError("private consumer detail")
        return await original_process(
            self,
            pointer_body,
            message_id=message_id,
            message_timestamp_ms=message_timestamp_ms,
        )

    monkeypatch.setattr(dispatch_module.GameplayConsumer, "process", fail_selected)
    operation_order: list[str] = []
    failed = queue_message_factory(
        failing_body,
        message_id="consumer-failure",
        operation_order=operation_order,
    )
    later = _seed_later_message(
        fake_env,
        fixture_payload,
        queue_message_factory,
        operation_order,
        "consumer",
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failed, later]),
        fake_env,
    )

    assert failed.retried is True
    assert failed.acked is False
    assert later.acked is True
    assert later.retried is False
    assert operation_order == ["retry:consumer-failure", "ack:later-consumer"]


def _over_depth_pointer_body() -> dict[str, object]:
    nested: object = {"private_value": "trajectory-secret"}
    for _ in range(max(sys.getrecursionlimit() + 100, 2_100)):
        nested = {"next": nested}
    body: dict[str, object] = {
        "invalid": True,
        "nested": nested,
        "bearer_token": "bearer-secret",
    }
    body["cycle"] = body
    return body


class _ExplodingItemsMapping(dict[str, object]):
    @override
    def items(self) -> Any:
        raise RuntimeError("node limit was not enforced")


class _CountingOrdinalLookup(dict[int, tuple[object, int]]):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    @override
    def get(self, key: int, default: Any = None, /) -> Any:
        self.get_calls += 1
        return super().get(key, default)


def test_container_ordinal_lookup_work_is_linear_and_retains_references() -> None:
    container_ordinal = gameplay_module._container_ordinal  # pyright: ignore[reportPrivateUsage]
    seen = _CountingOrdinalLookup()
    containers = [[] for _ in range(512)]

    for expected_ordinal, container in enumerate(containers):
        assert container_ordinal(container, seen) == (expected_ordinal, True)
    for expected_ordinal, container in enumerate(containers):
        assert container_ordinal(container, seen) == (expected_ordinal, False)

    assert seen.get_calls == 2 * len(containers)
    assert set(seen) == {id(container) for container in containers}
    assert all(
        retained is containers[ordinal] and retained_ordinal == ordinal
        for ordinal, (retained, retained_ordinal) in enumerate(seen.values())
    )


def _over_node_pointer_body() -> dict[str, object]:
    nodes: list[object] = [{"value": index} for index in range(20_000)]
    nodes[-1] = _ExplodingItemsMapping(private_value="trajectory-secret")
    body: dict[str, object] = {
        "invalid": True,
        "nodes": nodes,
        "bearer_token": "bearer-secret",
        "private_value": "trajectory-secret",
    }
    body["cycle"] = body
    return body


@pytest.mark.parametrize(
    "body_factory",
    [_over_depth_pointer_body, _over_node_pointer_body],
    ids=["depth-limit", "node-limit"],
)
async def test_bounded_pointer_digest_is_stable_and_safe_across_queue_retry(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
    body_factory: Any,
) -> None:
    timestamp = datetime(2026, 7, 19, tzinfo=UTC)
    messages = [
        queue_message_factory(
            body_factory(),
            message_id="stable-bounded-message",
            timestamp=timestamp,
        )
        for _ in range(2)
    ]

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", messages),
        fake_env,
    )

    assert [(message.acked, message.retried) for message in messages] == [
        (True, False),
        (True, False),
    ]
    assert len(fake_env.RAW_BUCKET.put_calls) == 1
    put = fake_env.RAW_BUCKET.put_calls[0]
    assert put.key.startswith("rejected/date=2026-07-19/quarantine=")
    rejection_text = put.body.decode("utf-8")
    assert "trajectory-secret" not in rejection_text
    assert "bearer-secret" not in rejection_text
    assert "private_value" not in rejection_text


async def test_dispatch_persists_nonfinite_rejection_and_continues_later_message(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    nonfinite_payload = _variant(
        json.loads(json.dumps(fixture_payload)), "nonfinite"
    )
    marker = "__nonfinite_json_number__"
    nonfinite_payload["events"][1]["payload"]["offending_number"] = marker
    nonfinite_payload["events"][1]["payload"]["private_value"] = "trajectory-secret"
    nonfinite_raw = _canonical(nonfinite_payload).replace(
        json.dumps(marker).encode("utf-8"),
        b"1e9999",
    )
    nonfinite_pointer = _pointer(nonfinite_payload, nonfinite_raw)
    later_payload = _variant(json.loads(json.dumps(fixture_payload)), "after-nonfinite")
    later_raw = _canonical(later_payload)
    later_pointer = _pointer(later_payload, later_raw)
    fake_env.RAW_BUCKET.seed_raw(
        nonfinite_pointer["r2_key"],
        nonfinite_raw,
        nonfinite_pointer["payload_sha256"],
    )
    fake_env.RAW_BUCKET.seed_raw(
        later_pointer["r2_key"], later_raw, later_pointer["payload_sha256"]
    )
    operation_order: list[str] = []
    nonfinite_message = queue_message_factory(
        nonfinite_pointer,
        message_id="nonfinite",
        operation_order=operation_order,
    )
    later_message = queue_message_factory(
        later_pointer,
        message_id="later",
        operation_order=operation_order,
    )

    await dispatch_queue(
        queue_batch_factory(
            "retro-gameplay-events", [nonfinite_message, later_message]
        ),
        fake_env,
    )

    assert (nonfinite_message.acked, nonfinite_message.retried) == (True, False)
    assert (later_message.acked, later_message.retried) == (True, False)
    assert operation_order == ["ack:nonfinite", "ack:later"]
    rejection_put = next(
        call for call in fake_env.RAW_BUCKET.put_calls if call.key.startswith("rejected/")
    )
    rejection_text = rejection_put.body.decode("utf-8")
    assert json.loads(rejection_text)["reason_code"] == "invalid_chunk_json"
    assert "trajectory-secret" not in rejection_text
    assert "offending_number" not in rejection_text


async def test_dispatch_normalizes_proxy_body_and_queue_timestamp(
    fake_env: Any,
    queue_message_factory: Any,
) -> None:
    timestamp = datetime(2026, 7, 20, 3, 4, 5, 678000, tzinfo=UTC)
    expected_ms = int(timestamp.timestamp() * 1000)
    message = queue_message_factory(
        SimpleNamespace(version=1, user_id="invalid"),
        message_id="proxy-invalid",
        timestamp=timestamp,
    )
    batch = SimpleNamespace(queue="retro-gameplay-events", messages=(message,))

    await dispatch_queue(batch, fake_env)

    assert message.acked is True
    assert message.retried is False
    put = fake_env.RAW_BUCKET.put_calls[-1]
    assert put.key.startswith("rejected/date=2026-07-20/quarantine=")
    rejection = json.loads(put.body)
    assert rejection["rejection_at_ms"] == expected_ms


async def test_dispatch_nonfinite_timestamp_uses_stable_epoch_fallback(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    message = queue_message_factory({"invalid": True}, timestamp=float("inf"))

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [message]),
        fake_env,
    )

    assert message.acked is True
    rejection = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert rejection["rejection_at_ms"] == 0


@pytest.mark.parametrize("body_encoding", ["string", "bytes"])
async def test_dispatch_accepts_json_encoded_pointer_bodies(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    body_encoding: str,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    raw_body = _canonical(fixture_payload)
    pointer = _pointer(fixture_payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )
    encoded = json.dumps(pointer)
    body: object = encoded if body_encoding == "string" else encoded.encode("utf-8")
    message = queue_message_factory(body)

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [message]),
        fake_env,
    )

    assert message.acked is True
    assert message.retried is False


async def test_rejection_write_failure_retries_message_and_continues(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    later_payload = _variant(json.loads(json.dumps(fixture_payload)), "after-failure")
    later_raw = _canonical(later_payload)
    later_pointer = _pointer(later_payload, later_raw)
    fake_env.RAW_BUCKET.seed_raw(
        later_pointer["r2_key"], later_raw, later_pointer["payload_sha256"]
    )
    original_put = fake_env.RAW_BUCKET.put
    put_attempts = 0

    async def fail_first_put(
        key: str,
        value: object,
        options: object | None = None,
    ) -> object | None:
        nonlocal put_attempts
        put_attempts += 1
        if put_attempts == 1:
            raise RuntimeError("R2 unavailable")
        return await original_put(key, value, options)

    fake_env.RAW_BUCKET.put = fail_first_put
    failed = queue_message_factory({"invalid": True}, message_id="failed-rejection")
    later = queue_message_factory(later_pointer, message_id="later")

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failed, later]),
        fake_env,
    )

    assert failed.retried is True
    assert failed.acked is False
    assert later.acked is True
    assert later.retried is False


async def test_unknown_queue_name_raises_visible_runtime_error(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    message = queue_message_factory({"invalid": True})

    with pytest.raises(RuntimeError, match="unsupported Queue: other-events"):
        await dispatch_queue(queue_batch_factory("other-events", [message]), fake_env)

    assert message.acked is False
    assert message.retried is False


async def test_dlq_message_is_archived_to_r2_before_acknowledgment(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    pointer = {
        "version": 1,
        "r2_key": "raw/user=usr_01/game=game_01/chunk=chk_01.json",
        "payload_sha256": "a" * 64,
        "user_id": "usr_01",
        "game_id": "game_01",
        "chunk_id": "chk_01",
        "received_at_ms": 1_768_564_800_000,
    }
    message = queue_message_factory(
        pointer,
        message_id="exhausted-message",
        timestamp=datetime(2026, 7, 17, 12, tzinfo=UTC),
        operation_order=fake_env.operation_order,
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events-dlq", [message]),
        fake_env,
    )

    assert message.acked is True
    assert message.retried is False
    assert fake_env.operation_order == ["r2.get", "r2.put", "ack:exhausted-message"]
    [put_call] = fake_env.RAW_BUCKET.put_calls
    assert put_call.key.startswith("dlq/date=2026-07-17/message=")
    artifact = json.loads(put_call.body)
    assert artifact == {
        "artifact_version": 1,
        "chunk_id": "chk_01",
        "error_type": "unrecorded",
        "game_id": "game_01",
        "message_body_sha256": hashlib.sha256(_canonical(pointer)).hexdigest(),
        "message_id_sha256": hashlib.sha256(b"exhausted-message").hexdigest(),
        "payload_sha256": "a" * 64,
        "pointer_received_at_ms": 1_768_564_800_000,
        "queue_message_timestamp_ms": 1_784_289_600_000,
        "reason": "gameplay processing retries exhausted",
        "source_queue": "retro-gameplay-events",
        "user_id": "usr_01",
    }


async def test_dlq_r2_failure_retries_without_acknowledging(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    fake_env.RAW_BUCKET.put_failure = RuntimeError("R2 unavailable")
    message = queue_message_factory(
        {"invalid": True},
        message_id="archive-failure",
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events-dlq", [message]),
        fake_env,
    )

    assert message.acked is False
    assert message.retried is True


async def test_dlq_archive_replay_is_idempotent(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    timestamp = datetime(2026, 7, 17, 12, tzinfo=UTC)
    messages = [
        queue_message_factory(
            {"invalid": True},
            message_id="same-exhausted-message",
            timestamp=timestamp,
        )
        for _ in range(2)
    ]

    for message in messages:
        await dispatch_queue(
            queue_batch_factory("retro-gameplay-events-dlq", [message]),
            fake_env,
        )

    assert all(message.acked and not message.retried for message in messages)
    assert len(fake_env.RAW_BUCKET.put_calls) == 1


DLQ_CHUNK_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures/dlq-gameplay-chunk-2026-08-05.json"
)


def _dlq_fixture_pointer() -> tuple[bytes, dict[str, object]]:
    """The exact bytes of a chunk production dead-lettered on 2026-08-05.

    ``raw/user=usr_47bf8fbb…/game=game_01482d1e…/chunk=chk_01482d1e…_2.json``,
    still in R2 and still perfectly valid: replaying it through
    ``GameplayConsumer`` validates and writes cleanly. Nothing about the payload
    caused the loss, which is why the regression below is about how a failed
    message is settled rather than about how a chunk is parsed.
    """
    raw_body = DLQ_CHUNK_FIXTURE.read_bytes()
    return raw_body, _pointer(json.loads(raw_body), raw_body)


async def test_dlq_fixture_chunk_still_validates_cleanly(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    raw_body, pointer = _dlq_fixture_pointer()
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )
    message = queue_message_factory(pointer, message_id="recovered")

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [message]),
        fake_env,
    )

    assert (message.acked, message.retried) == (True, False)
    assert any(
        call.key.startswith("validated/") for call in fake_env.RAW_BUCKET.put_calls
    )


async def test_diagnostic_failure_never_strands_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    fake_env: Any,
    fixture_payload: dict[str, Any],
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    """Reproduces the production data loss.

    A Python Worker that has spent its CPU budget re-raises on the *next*
    operation, whatever that is. When the failure diagnostics ran before the
    message was settled, that re-raise escaped ``dispatch_queue`` with the rest
    of the batch neither acked nor retried, Cloudflare redelivered the whole
    batch, and the same wall was hit on all five attempts.
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cpu budget exhausted while logging")

    monkeypatch.setattr(dispatch_module, "_log_retry", explode)
    operation_order: list[str] = []
    failed = queue_message_factory(
        {"not": "a pointer body proxy"},
        message_id="cpu-exhausted",
        operation_order=operation_order,
    )
    monkeypatch.setattr(
        dispatch_module.GameplayConsumer,
        "process",
        _failing_process(failed.body),
    )
    later = _seed_later_message(
        fake_env,
        fixture_payload,
        queue_message_factory,
        operation_order,
        "after-cpu",
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failed, later]),
        fake_env,
    )

    assert (failed.acked, failed.retried) == (False, True)
    assert (later.acked, later.retried) == (True, False)
    assert operation_order == ["retry:cpu-exhausted", "ack:later-after-cpu"]


def _failing_process(failing_body: object) -> Any:
    original_process = dispatch_module.GameplayConsumer.process

    async def process(
        self: Any,
        pointer_body: object,
        *,
        message_id: object | None = None,
        message_timestamp_ms: object | None = None,
    ) -> Any:
        if pointer_body is failing_body:
            raise RuntimeError("Python Worker exceeded CPU time limit")
        return await original_process(
            self,
            pointer_body,
            message_id=message_id,
            message_timestamp_ms=message_timestamp_ms,
        )

    return process


async def test_batch_abort_still_settles_every_remaining_message(
    monkeypatch: pytest.MonkeyPatch,
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    async def abort(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Python Worker exceeded CPU time limit")

    monkeypatch.setattr(dispatch_module.GameplayConsumer, "process", abort)
    monkeypatch.setattr(
        dispatch_module,
        "_report_failure",
        _raising_report_failure(),
    )
    messages = [
        queue_message_factory({"invalid": index}, message_id=f"aborted-{index}")
        for index in range(3)
    ]

    with pytest.raises(RuntimeError):
        await dispatch_queue(
            queue_batch_factory("retro-gameplay-events", messages),
            fake_env,
        )

    assert [(message.acked, message.retried) for message in messages] == [
        (False, True),
        (False, True),
        (False, True),
    ]


def _raising_report_failure() -> Any:
    async def report(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cpu budget exhausted while reporting")

    return report


async def test_final_attempt_records_the_error_into_the_dlq_archive(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    _raw_body, pointer = _dlq_fixture_pointer()
    body_sha256 = hashlib.sha256(_canonical(pointer)).hexdigest()
    # The raw object is deliberately absent, so processing raises the same
    # TransientGameplayError the consumer raises when R2 is unreachable.
    failing = queue_message_factory(
        pointer,
        message_id="final-attempt",
        attempts=dispatch_module.GAMEPLAY_MAX_DELIVERY_ATTEMPTS,
    )

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [failing]),
        fake_env,
    )

    assert (failing.acked, failing.retried) == (False, True)
    note_call = next(
        call
        for call in fake_env.RAW_BUCKET.put_calls
        if call.key == f"dlq/error={body_sha256}.json"
    )
    note = json.loads(note_call.body)
    assert note["error_type"] == "TransientGameplayError"
    assert note["error_message"] == "raw object unavailable"
    assert note["attempts"] == dispatch_module.GAMEPLAY_MAX_DELIVERY_ATTEMPTS

    exhausted = queue_message_factory(
        pointer,
        message_id="final-attempt",
        timestamp=datetime(2026, 8, 5, 15, 50, tzinfo=UTC),
    )
    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events-dlq", [exhausted]),
        fake_env,
    )

    archive_call = next(
        call
        for call in fake_env.RAW_BUCKET.put_calls
        if call.key.startswith("dlq/date=")
    )
    archive = json.loads(archive_call.body)
    assert archive["error_type"] == "TransientGameplayError"
    assert archive["error_message"] == "raw object unavailable"
    assert archive["failed_attempts"] == dispatch_module.GAMEPLAY_MAX_DELIVERY_ATTEMPTS
    assert archive["reason"] == "gameplay processing retries exhausted"
    assert archive["chunk_id"] == pointer["chunk_id"]


async def test_early_attempt_does_not_write_a_failure_note(
    fake_env: Any,
    queue_batch_factory: Any,
    queue_message_factory: Any,
) -> None:
    _raw_body, pointer = _dlq_fixture_pointer()
    message = queue_message_factory(pointer, message_id="first-attempt", attempts=1)

    await dispatch_queue(
        queue_batch_factory("retro-gameplay-events", [message]),
        fake_env,
    )

    assert (message.acked, message.retried) == (False, True)
    assert not [
        call
        for call in fake_env.RAW_BUCKET.put_calls
        if call.key.startswith("dlq/error=")
    ]
