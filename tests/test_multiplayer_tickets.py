import re
from hashlib import sha256

import pytest

from retro_backend.multiplayer.errors import RoomFault
from retro_backend.multiplayer.tickets import (
    TICKET_BYTES,
    TICKET_PATTERN,
    TICKET_TTL_MS,
    EntropySource,
    generate_ticket,
    hash_ticket,
    is_ticket_expired,
    ticket_expires_at,
    validate_ticket,
)


def _short_entropy(_size: int) -> bytes:
    return b"x" * 31


def _long_entropy(_size: int) -> bytes:
    return b"x" * 33


def test_ticket_is_unpadded_base64url_and_only_digest_is_storable() -> None:
    raw = generate_ticket(lambda size: bytes(range(size)))

    assert raw == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert len(raw) == 43
    assert re.fullmatch(TICKET_PATTERN, raw) is not None
    assert hash_ticket(raw) == sha256(raw.encode("ascii")).hexdigest()


def test_ticket_entropy_requests_exactly_32_bytes() -> None:
    requested_sizes: list[int] = []

    def entropy(size: int) -> bytes:
        requested_sizes.append(size)
        return b"x" * size

    generate_ticket(entropy)

    assert TICKET_BYTES == 32
    assert requested_sizes == [32]


@pytest.mark.parametrize("entropy", [_short_entropy, _long_entropy])
def test_ticket_rejects_entropy_with_wrong_byte_count(entropy: EntropySource) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        generate_ticket(entropy)


@pytest.mark.parametrize(
    "raw_ticket",
    [
        "a" * 42,
        "a" * 44,
        "a" * 42 + "=",
        "a" * 42 + "+",
        "a" * 42 + "/",
        "a" * 42 + ".",
        "a" * 42 + "\u00e9",
        b"a" * 43,
    ],
)
def test_malformed_ticket_rejection_is_stable(raw_ticket: object) -> None:
    with pytest.raises(RoomFault) as raised:
        validate_ticket(raw_ticket)

    assert raised.value.code == "invalid_ticket"
    assert raised.value.message == "Invalid ticket"


def test_hash_ticket_validates_before_hashing() -> None:
    with pytest.raises(RoomFault) as raised:
        hash_ticket("not-a-ticket")

    assert raised.value.code == "invalid_ticket"


def test_ticket_ttl_and_expiry_boundary_are_exact() -> None:
    assert TICKET_TTL_MS == 60_000
    assert ticket_expires_at(1_000) == 61_000
    assert is_ticket_expired(now_ms=60_999, expires_at_ms=61_000) is False
    assert is_ticket_expired(now_ms=61_000, expires_at_ms=61_000) is True
    assert is_ticket_expired(now_ms=61_001, expires_at_ms=61_000) is True
