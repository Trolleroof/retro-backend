"""Opaque one-use multiplayer ticket primitives."""

import base64
import hashlib
import re
import secrets
from collections.abc import Callable

from .errors import RoomFault

TICKET_BYTES = 32
TICKET_TTL_MS = 60_000
TICKET_PATTERN = r"^[A-Za-z0-9_-]{43}$"

_TICKET_RE = re.compile(TICKET_PATTERN)
EntropySource = Callable[[int], object]


def generate_ticket(entropy: EntropySource = secrets.token_bytes) -> str:
    """Generate one 32-byte unpadded base64url ticket."""
    random_value: object = entropy(TICKET_BYTES)
    if not isinstance(random_value, bytes) or len(random_value) != TICKET_BYTES:
        raise ValueError("Ticket entropy must return exactly 32 bytes")
    return base64.urlsafe_b64encode(random_value).rstrip(b"=").decode("ascii")


def validate_ticket(raw_ticket: object) -> str:
    """Validate and return the exact public ticket representation."""
    if not isinstance(raw_ticket, str) or _TICKET_RE.fullmatch(raw_ticket) is None:
        raise RoomFault.for_code("invalid_ticket")
    return raw_ticket


def hash_ticket(raw_ticket: str) -> str:
    """Return the lowercase SHA-256 digest used for private storage."""
    valid_ticket = validate_ticket(raw_ticket)
    return hashlib.sha256(valid_ticket.encode("ascii")).hexdigest()


def ticket_expires_at(issued_at_ms: int) -> int:
    return issued_at_ms + TICKET_TTL_MS


def is_ticket_expired(*, now_ms: int, expires_at_ms: int) -> bool:
    return now_ms >= expires_at_ms
