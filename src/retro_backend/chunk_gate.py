"""Pure ChunkGate state transitions and a Python Workers RPC client."""

import hashlib
import re
import secrets
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bindings import ChunkGateStub, DurableObjectNamespace
from .contracts import ChunkReservation, IngestAcknowledgment

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LEASE_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ChunkGateTransitionError(RuntimeError):
    """Raised when an accept operation does not own an active lease."""


def _validate_now(now_ms: object) -> None:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")


def _validate_time_and_duration(now_ms: object, duration_ms: object) -> None:
    _validate_now(now_ms)
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
    ):
        raise ValueError("duration must be a positive integer")


def _validate_digest(payload_sha256: str) -> None:
    if _SHA256_PATTERN.fullmatch(payload_sha256) is None:
        raise ValueError("payload_sha256 must be lowercase hexadecimal SHA-256")


def _validate_lease_token(lease_token: str) -> None:
    if _LEASE_TOKEN_PATTERN.fullmatch(lease_token) is None:
        raise ValueError("lease_token must be 256-bit lowercase hexadecimal")


def _new_lease_token() -> str:
    return secrets.token_hex(32)


class ChunkGateState(BaseModel):
    """Serializable state machine for one logical ``(user_id, chunk_id)``."""

    model_config = ConfigDict(extra="forbid")

    payload_sha256: str | None = None
    state: str | None = None
    lease_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    lease_expires_at_ms: int | None = Field(default=None, ge=0)
    acknowledgment: IngestAcknowledgment | None = None
    expires_at_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "ChunkGateState":
        if self.payload_sha256 is not None:
            _validate_digest(self.payload_sha256)
        if self.state is None:
            if any(
                value is not None
                for value in (
                    self.payload_sha256,
                    self.lease_token,
                    self.lease_expires_at_ms,
                    self.acknowledgment,
                    self.expires_at_ms,
                )
            ):
                raise ValueError("empty gate state must not contain receipt data")
        elif self.state == "publishing":
            if (
                self.payload_sha256 is None
                or self.lease_token is None
                or self.lease_expires_at_ms is None
                or self.acknowledgment is not None
                or self.expires_at_ms is not None
            ):
                raise ValueError("publishing gate state is incomplete")
        elif self.state == "accepted":
            if (
                self.payload_sha256 is None
                or self.lease_token is not None
                or self.lease_expires_at_ms is not None
                or self.acknowledgment is None
                or self.expires_at_ms is None
            ):
                raise ValueError("accepted gate state is incomplete")
        else:
            raise ValueError("gate state is invalid")
        return self

    def _clear(self) -> None:
        self.payload_sha256 = None
        self.state = None
        self.lease_token = None
        self.lease_expires_at_ms = None
        self.acknowledgment = None
        self.expires_at_ms = None

    def _begin_publication(
        self,
        payload_sha256: str,
        lease_token: str,
        *,
        now_ms: int,
        lease_ms: int,
    ) -> ChunkReservation:
        self.payload_sha256 = payload_sha256
        self.state = "publishing"
        self.lease_token = lease_token
        self.lease_expires_at_ms = now_ms + lease_ms
        self.acknowledgment = None
        self.expires_at_ms = None
        return ChunkReservation(status="reserved", lease_token=lease_token)

    def reserve(
        self,
        payload_sha256: str,
        lease_token: str,
        *,
        now_ms: int,
        lease_ms: int,
    ) -> ChunkReservation:
        _validate_digest(payload_sha256)
        _validate_lease_token(lease_token)
        _validate_time_and_duration(now_ms, lease_ms)

        if (
            self.state == "publishing"
            and self.lease_expires_at_ms is not None
            and now_ms >= self.lease_expires_at_ms
        ):
            return self._begin_publication(
                payload_sha256,
                lease_token,
                now_ms=now_ms,
                lease_ms=lease_ms,
            )
        if (
            self.state == "accepted"
            and self.expires_at_ms is not None
            and now_ms >= self.expires_at_ms
        ):
            self._clear()

        if self.state is None:
            return self._begin_publication(
                payload_sha256,
                lease_token,
                now_ms=now_ms,
                lease_ms=lease_ms,
            )
        if self.payload_sha256 != payload_sha256:
            return ChunkReservation(status="conflict")
        if self.state == "publishing":
            return ChunkReservation(status="publishing")
        return ChunkReservation(status="duplicate", ack=self.acknowledgment)

    def accept(
        self,
        payload_sha256: str,
        lease_token: str,
        acknowledgment: IngestAcknowledgment | dict[str, object],
        *,
        now_ms: int,
        dedupe_ttl_ms: int,
    ) -> bool:
        _validate_digest(payload_sha256)
        _validate_lease_token(lease_token)
        _validate_time_and_duration(now_ms, dedupe_ttl_ms)
        if (
            self.state != "publishing"
            or self.payload_sha256 != payload_sha256
            or self.lease_token != lease_token
            or self.lease_expires_at_ms is None
            or now_ms >= self.lease_expires_at_ms
        ):
            raise ChunkGateTransitionError(
                "accept requires the matching active lease"
            )

        receipt = IngestAcknowledgment.model_validate(acknowledgment)
        self.state = "accepted"
        self.lease_token = None
        self.lease_expires_at_ms = None
        self.acknowledgment = receipt
        self.expires_at_ms = now_ms + dedupe_ttl_ms
        return True

    def release(self, payload_sha256: str, lease_token: str) -> bool:
        _validate_digest(payload_sha256)
        _validate_lease_token(lease_token)
        if (
            self.state != "publishing"
            or self.payload_sha256 != payload_sha256
            or self.lease_token != lease_token
        ):
            return False
        self._clear()
        return True

    def expire(self, *, now_ms: int) -> bool:
        _validate_now(now_ms)
        deadline = (
            self.lease_expires_at_ms
            if self.state == "publishing"
            else self.expires_at_ms
        )
        if deadline is None or now_ms < deadline:
            return False
        self._clear()
        return True


class ChunkGateClient:
    def __init__(
        self,
        namespace: DurableObjectNamespace,
        *,
        lease_ms: int,
        dedupe_ttl_ms: int,
        token_factory: Callable[[], str] = _new_lease_token,
    ) -> None:
        _validate_time_and_duration(0, lease_ms)
        _validate_time_and_duration(0, dedupe_ttl_ms)
        self.namespace = namespace
        self.lease_ms = lease_ms
        self.dedupe_ttl_ms = dedupe_ttl_ms
        self.token_factory = token_factory
        self._stubs_by_lease_token: dict[str, ChunkGateStub] = {}

    async def reserve(
        self,
        user_id: str,
        chunk_id: str,
        payload_sha256: str,
        now_ms: int,
    ) -> ChunkReservation:
        _validate_digest(payload_sha256)
        _validate_now(now_ms)
        lease_token = self.token_factory()
        _validate_lease_token(lease_token)
        if lease_token in self._stubs_by_lease_token:
            raise RuntimeError("lease token collision")
        name = hashlib.sha256(f"{user_id}:{chunk_id}".encode()).hexdigest()
        object_id = self.namespace.idFromName(name)
        stub = self.namespace.get(object_id)
        result = await stub.reserve(
            payload_sha256,
            lease_token,
            now_ms,
            self.lease_ms,
        )
        reservation = ChunkReservation.from_binding(result)
        if reservation.status == "reserved":
            if reservation.lease_token != lease_token:
                raise RuntimeError("chunk gate returned unexpected lease token")
            self._stubs_by_lease_token[lease_token] = stub
        return reservation

    def _stub(self, lease_token: str) -> ChunkGateStub:
        _validate_lease_token(lease_token)
        try:
            return self._stubs_by_lease_token[lease_token]
        except KeyError as error:
            raise RuntimeError("chunk gate has no matching reservation") from error

    async def accept(
        self,
        payload_sha256: str,
        lease_token: str,
        acknowledgment: IngestAcknowledgment | dict[str, object],
        now_ms: int,
    ) -> None:
        _validate_digest(payload_sha256)
        _validate_now(now_ms)
        receipt = IngestAcknowledgment.model_validate(acknowledgment)
        await self._stub(lease_token).accept(
            payload_sha256,
            lease_token,
            receipt.model_dump(mode="json"),
            now_ms,
            self.dedupe_ttl_ms,
        )
        del self._stubs_by_lease_token[lease_token]

    async def release(self, payload_sha256: str, lease_token: str) -> None:
        _validate_digest(payload_sha256)
        await self._stub(lease_token).release(payload_sha256, lease_token)
        del self._stubs_by_lease_token[lease_token]
