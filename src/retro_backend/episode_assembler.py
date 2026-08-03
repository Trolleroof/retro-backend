"""Pure EpisodeAssembler state transitions and a Python Workers RPC client.

One Durable Object per logical ``(user_id, game_id)`` accumulates the chunk
records the gameplay consumer has already validated, decides when that episode
is terminal, and publishes exactly one ``episode.ready`` message.

A Durable Object rather than a D1 row because two chunks of the same episode can
land in different Queue batches concurrently: deciding "am I the chunk that
completed this episode?" needs serialized state, and the abandoned/incomplete
flush needs an alarm. The D1 episode index (retro-backend#31) is the queryable
projection of this state, not a replacement for it.
"""

import hashlib
import sys
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bindings import (
    DurableObjectContext,
    EpisodeAssemblerNamespace,
    is_binding_null,
    value_field,
)
from .contracts import (
    CHUNK_ID_PATTERN,
    GAME_ID_PATTERN,
    ID_SUFFIX_MAX_LENGTH,
    USER_ID_PATTERN,
    EpisodeChunkRef,
    EpisodeReady,
    EpisodeStatus,
)
from .storage.queues import EpisodeQueuePublisher

if sys.platform == "emscripten":
    # Pyright does not narrow this platform-exclusive declaration.
    from workers import DurableObject  # pyright: ignore[reportAssignmentType]
else:

    class DurableObject:
        """Minimal CPython-only base for exercising runtime methods in tests."""

        def __init__(self, ctx: DurableObjectContext, env: object) -> None:
            self.ctx = ctx
            self.env = env


_EPISODE_KEY = "episode"

DEFAULT_EPISODE_IDLE_TTL_MS = 21_600_000
DEFAULT_EPISODE_MAX_CHUNKS = 512

_USER_ID_MAX_LENGTH = len("usr_") + ID_SUFFIX_MAX_LENGTH
_GAME_ID_MAX_LENGTH = len("game_") + ID_SUFFIX_MAX_LENGTH
_CHUNK_ID_MAX_LENGTH = len("chk_") + ID_SUFFIX_MAX_LENGTH


def _current_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _validate_now(now_ms: object) -> None:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")


def _validate_positive(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


class EpisodeChunkRecord(BaseModel):
    """One validated chunk's contribution to its episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(pattern=CHUNK_ID_PATTERN, max_length=_CHUNK_ID_MAX_LENGTH)
    chunk_sequence: int = Field(ge=1, strict=True)
    first_event_sequence: int = Field(ge=1, strict=True)
    last_event_sequence: int = Field(ge=1, strict=True)
    created_at_ms: int = Field(ge=0, strict=True)
    episode_ended: bool

    @model_validator(mode="after")
    def validate_event_range(self) -> "EpisodeChunkRecord":
        if self.last_event_sequence < self.first_event_sequence:
            raise ValueError("last_event_sequence must not precede first_event_sequence")
        return self

    @property
    def ref(self) -> EpisodeChunkRef:
        return EpisodeChunkRef(
            chunk_id=self.chunk_id,
            chunk_sequence=self.chunk_sequence,
            created_at_ms=self.created_at_ms,
        )


class EpisodeState(BaseModel):
    """Serializable assembly state for one logical ``(user_id, game_id)``."""

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(
        default=None,
        pattern=USER_ID_PATTERN,
        max_length=_USER_ID_MAX_LENGTH,
    )
    game_id: str | None = Field(
        default=None,
        pattern=GAME_ID_PATTERN,
        max_length=_GAME_ID_MAX_LENGTH,
    )
    game_type: str | None = Field(default=None, min_length=1)
    game_version: str | None = Field(default=None, min_length=1)
    chunks: list[EpisodeChunkRecord] = Field(default_factory=list)
    idle_deadline_ms: int | None = Field(default=None, ge=0)
    emitted_at_ms: int | None = Field(default=None, ge=0)
    emitted_status: EpisodeStatus | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "EpisodeState":
        if (self.emitted_at_ms is None) != (self.emitted_status is None):
            raise ValueError("emission requires both a timestamp and a status")
        sequences = [chunk.chunk_sequence for chunk in self.chunks]
        if len(set(sequences)) != len(sequences):
            raise ValueError("episode chunks must have unique sequences")
        if sequences != sorted(sequences):
            raise ValueError("episode chunks must be stored in sequence order")
        return self

    @property
    def emitted(self) -> bool:
        return self.emitted_at_ms is not None

    def _identity_matches(
        self,
        *,
        user_id: str,
        game_id: str,
        game_type: str,
        game_version: str,
    ) -> bool:
        return all(
            stored is None or stored == incoming
            for stored, incoming in (
                (self.user_id, user_id),
                (self.game_id, game_id),
                (self.game_type, game_type),
                (self.game_version, game_version),
            )
        )

    def _is_contiguous(self) -> bool:
        """Whether the stored chunks form an unbroken 1..N event-sequence chain."""
        for index, chunk in enumerate(self.chunks):
            if chunk.chunk_sequence != index + 1:
                return False
            if index and chunk.first_event_sequence != (
                self.chunks[index - 1].last_event_sequence + 1
            ):
                return False
        return True

    def _terminal_status(self) -> EpisodeStatus | None:
        """Classify the episode, or ``None`` while it may still be completed."""
        if not self.chunks:
            return None
        if self.chunks[-1].episode_ended and self._is_contiguous():
            return "complete"
        return None

    def _flush_status(self) -> EpisodeStatus:
        """Classify an episode that ran out of time or space to complete."""
        if self._terminal_status() == "complete":
            return "complete"
        # Seeing episode_ended without a clean chain means chunks are missing;
        # never seeing it at all means the player walked away mid-game.
        if any(chunk.episode_ended for chunk in self.chunks):
            return "incomplete"
        return "abandoned"

    def _build(self, status: EpisodeStatus, now_ms: int) -> EpisodeReady:
        if (
            self.user_id is None
            or self.game_id is None
            or self.game_type is None
            or self.game_version is None
        ):
            raise ValueError("episode identity is incomplete")
        return EpisodeReady(
            status=status,
            user_id=self.user_id,
            game_id=self.game_id,
            game_type=self.game_type,
            game_version=self.game_version,
            chunk_count=len(self.chunks),
            first_event_sequence=self.chunks[0].first_event_sequence,
            last_event_sequence=self.chunks[-1].last_event_sequence,
            chunks=[chunk.ref for chunk in self.chunks],
            completed_at_ms=now_ms,
        )

    def record(
        self,
        chunk: EpisodeChunkRecord,
        *,
        user_id: str,
        game_id: str,
        game_type: str,
        game_version: str,
        now_ms: int,
        idle_ttl_ms: int,
        max_chunks: int,
    ) -> EpisodeReady | None:
        """Fold one validated chunk in, returning a message when it is terminal.

        Redelivering an already-recorded chunk is a no-op, and a chunk arriving
        after emission is dropped, so the caller may retry freely.
        """
        _validate_now(now_ms)
        _validate_positive(idle_ttl_ms, "idle_ttl_ms")
        _validate_positive(max_chunks, "max_chunks")
        if not self._identity_matches(
            user_id=user_id,
            game_id=game_id,
            game_type=game_type,
            game_version=game_version,
        ):
            raise ValueError("chunk identity does not match the episode")
        if self.emitted:
            return None
        if any(existing.chunk_id == chunk.chunk_id for existing in self.chunks):
            return None
        if any(
            existing.chunk_sequence == chunk.chunk_sequence for existing in self.chunks
        ):
            raise ValueError("episode already holds a different chunk at that sequence")

        self.user_id = user_id
        self.game_id = game_id
        self.game_type = game_type
        self.game_version = game_version
        self.chunks.append(chunk)
        self.chunks.sort(key=lambda item: item.chunk_sequence)
        self.idle_deadline_ms = now_ms + idle_ttl_ms

        status = self._terminal_status()
        if status is None and len(self.chunks) >= max_chunks:
            # Bound both Durable Object storage and the emitted message; an
            # episode this long is not going to close cleanly.
            status = self._flush_status()
        if status is None:
            return None
        return self._build(status, now_ms)

    def flush(self, *, now_ms: int) -> EpisodeReady | None:
        """Classify an idle episode once its deadline has passed."""
        _validate_now(now_ms)
        if self.emitted or not self.chunks:
            return None
        if self.idle_deadline_ms is not None and now_ms < self.idle_deadline_ms:
            return None
        return self._build(self._flush_status(), now_ms)

    def mark_emitted(self, status: EpisodeStatus, *, now_ms: int) -> None:
        _validate_now(now_ms)
        self.emitted_at_ms = now_ms
        self.emitted_status = status
        self.idle_deadline_ms = None


def episode_object_name(user_id: str, game_id: str) -> str:
    return hashlib.sha256(f"{user_id}:{game_id}".encode()).hexdigest()


class EpisodeAssemblerClient:
    """Route one validated chunk to its episode's Durable Object."""

    def __init__(
        self,
        namespace: EpisodeAssemblerNamespace,
        *,
        idle_ttl_ms: int = DEFAULT_EPISODE_IDLE_TTL_MS,
        max_chunks: int = DEFAULT_EPISODE_MAX_CHUNKS,
        object_name: Callable[[str, str], str] = episode_object_name,
    ) -> None:
        _validate_positive(idle_ttl_ms, "idle_ttl_ms")
        _validate_positive(max_chunks, "max_chunks")
        self.namespace = namespace
        self.idle_ttl_ms = idle_ttl_ms
        self.max_chunks = max_chunks
        self._object_name = object_name

    async def record(
        self,
        record: EpisodeChunkRecord,
        *,
        user_id: str,
        game_id: str,
        game_type: str,
        game_version: str,
        now_ms: int,
    ) -> str | None:
        """Return the emitted status, or ``None`` when the episode is still open."""
        _validate_now(now_ms)
        object_id = self.namespace.idFromName(self._object_name(user_id, game_id))
        stub = self.namespace.get(object_id)
        result = await stub.record(
            {
                "chunk": record.model_dump(mode="json"),
                "user_id": user_id,
                "game_id": game_id,
                "game_type": game_type,
                "game_version": game_version,
            },
            now_ms,
            self.idle_ttl_ms,
            self.max_chunks,
        )
        status = value_field(result, "emitted_status")
        return status if isinstance(status, str) else None


class EpisodeAssembler(DurableObject):
    """Runtime Durable Object assembling one logical episode."""

    async def _load_state(self) -> EpisodeState:
        stored = await self.ctx.storage.get(_EPISODE_KEY)
        if is_binding_null(stored):
            return EpisodeState()
        if not isinstance(stored, (str, bytes, bytearray)):
            raise TypeError("episode state must be JSON text")
        return EpisodeState.model_validate_json(stored)

    async def _persist(self, state: EpisodeState) -> None:
        await self.ctx.storage.put(_EPISODE_KEY, state.model_dump_json())
        if state.idle_deadline_ms is None:
            await self.ctx.storage.deleteAlarm()
        else:
            await self.ctx.storage.setAlarm(state.idle_deadline_ms)

    async def _publish(self, episode: EpisodeReady) -> None:
        await EpisodeQueuePublisher(self.env).publish(episode)

    async def _emit(self, state: EpisodeState, episode: EpisodeReady) -> str:
        """Publish before recording emission so a failed send is retried."""
        await self._publish(episode)
        state.mark_emitted(episode.status, now_ms=episode.completed_at_ms)
        await self._persist(state)
        return episode.status

    async def record(
        self,
        request: object,
        now_ms: int,
        idle_ttl_ms: int,
        max_chunks: int,
    ) -> dict[str, object]:
        state = await self._load_state()
        chunk = EpisodeChunkRecord.model_validate(value_field(request, "chunk"))
        episode = state.record(
            chunk,
            user_id=str(value_field(request, "user_id")),
            game_id=str(value_field(request, "game_id")),
            game_type=str(value_field(request, "game_type")),
            game_version=str(value_field(request, "game_version")),
            now_ms=now_ms,
            idle_ttl_ms=idle_ttl_ms,
            max_chunks=max_chunks,
        )
        if episode is None:
            await self._persist(state)
            return {"emitted_status": None}
        return {"emitted_status": await self._emit(state, episode)}

    async def alarm(self) -> None:
        state = await self._load_state()
        episode = state.flush(now_ms=_current_time_ms())
        if episode is None:
            return
        await self._emit(state, episode)


__all__ = [
    "DEFAULT_EPISODE_IDLE_TTL_MS",
    "DEFAULT_EPISODE_MAX_CHUNKS",
    "EpisodeAssembler",
    "EpisodeAssemblerClient",
    "EpisodeChunkRecord",
    "EpisodeState",
    "episode_object_name",
]
