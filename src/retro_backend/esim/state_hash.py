"""Canonical serialization and hashing of E-Sim state.

``reset(case_id, seed)`` has to be repeatable and replay has to reproduce
identical hashes (#53), so "the same state" needs exactly one spelling. The
canonical form is UTF-8 JSON with object keys sorted, no insignificant
whitespace, non-finite numbers rejected, and non-string object keys rejected
rather than silently coerced.

Numbers keep Python's own JSON spelling: ``1`` serializes as ``1`` and ``1.0``
as ``1.0``. That deliberately diverges from JavaScript's ``JSON.stringify``,
which has one number type and writes ``1`` for both. It is safe here because
every state hashed by this module has first passed through the typed models in
``esim.contracts``, which pin each field to int or float, so the spelling is
stable across runs. It does mean a hash computed here is not comparable with
one computed by a JavaScript implementation of the same contract -- the Tui
client hashes its own live episodes, and the frozen E-Sim hashes ours.
"""

import hashlib
import json
import math
from collections.abc import Mapping

STATE_HASH_PREFIX = "sha256:"
STATE_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _reject_uncanonical(value: object) -> None:
    """Reject values whose JSON spelling would be ambiguous or unstable."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("unsupported non-finite JSON number")
        if isinstance(item, Mapping):
            for key in item:
                if not isinstance(key, str):
                    raise TypeError("canonical JSON requires string object keys")
            pending.extend(item.values())
        elif isinstance(item, list | tuple):
            pending.extend(item)


def canonical_json(value: object) -> bytes:
    """Serialize ``value`` to the one byte sequence that represents it."""
    _reject_uncanonical(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def state_hash(value: object) -> str:
    """Return the ``sha256:<hex>`` digest of ``value``'s canonical JSON."""
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    return f"{STATE_HASH_PREFIX}{digest}"
