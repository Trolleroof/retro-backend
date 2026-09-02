"""Seedable PRNG shared with the Tui client.

Ported from ``tui-gamepigeon/src/core/rng.ts`` (mulberry32) so both repos draw
the identical stream from the same seed: a trajectory recorded by the consumer
game has to replay here bit-for-bit.

JavaScript bitwise operators work on 32-bit two's-complement integers, and both
``Math.imul`` and ``+`` truncate modulo 2**32 once the result is coerced back.
Arithmetic modulo 2**32 is identical whether the operands are read as signed or
unsigned, so this port keeps every intermediate as an unsigned 32-bit integer
and still matches the TypeScript output exactly.
"""

UINT32_MASK = 0xFFFFFFFF
UINT32_RANGE = 0x100000000

_MULBERRY32_INCREMENT = 0x6D2B79F5


def _imul(left: int, right: int) -> int:
    """Low 32 bits of a product, mirroring JavaScript's ``Math.imul``."""
    return (left * right) & UINT32_MASK


def _require_int(value: object, label: str) -> int:
    """Reject bools and non-integers, which would silently skew the stream."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"RNG {label} must be an int")
    return value


def _to_uint32(value: object) -> int:
    """Coerce an integer the way JavaScript's ``>>> 0`` does."""
    return _require_int(value, "seed") & UINT32_MASK


class Rng:
    """Deterministic mulberry32 generator over an unsigned 32-bit state."""

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = _to_uint32(seed)

    @classmethod
    def from_state(cls, state: int) -> "Rng":
        """Resume a generator from a previously exported state."""
        state = _require_int(state, "state")
        if not 0 <= state <= UINT32_MASK:
            raise ValueError("RNG state must be an unsigned 32-bit integer")
        return cls(state)

    def export_state(self) -> int:
        """Return the state needed to resume this exact stream."""
        return self._state

    def next(self) -> float:
        """Return the next float in [0, 1)."""
        self._state = (self._state + _MULBERRY32_INCREMENT) & UINT32_MASK
        value = self._state
        value = _imul(value ^ (value >> 15), value | 1)
        value ^= (value + _imul(value ^ (value >> 7), value | 61)) & UINT32_MASK
        return ((value ^ (value >> 14)) & UINT32_MASK) / UINT32_RANGE

    def int(self, bound: int) -> int:
        """Return the next integer in [0, ``bound``)."""
        bound = _require_int(bound, "bound")
        if bound < 1:
            raise ValueError("RNG bound must be positive")
        return int(self.next() * bound)
