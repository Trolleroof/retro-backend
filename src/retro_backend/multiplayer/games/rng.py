"""Serializable Mulberry32 random-number generation with JavaScript parity."""

UINT32_MASK = 0xFFFF_FFFF
MULBERRY32_INCREMENT = 0x6D2B79F5
_FLOAT_DENOMINATOR = 4_294_967_296
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _require_uint32(value: object, message: str) -> int:
    if type(value) is not int or not 0 <= value <= UINT32_MASK:
        raise ValueError(message)
    return value


class Mulberry32:
    """The stateful uint32 generator used by the canonical 2048 engine."""

    __slots__ = ("_state",)

    def __init__(self, state: int) -> None:
        self._state = _require_uint32(
            state,
            "RNG state must be an unsigned 32-bit integer",
        )

    @classmethod
    def from_state(cls, state: int) -> "Mulberry32":
        return cls(state)

    @property
    def state(self) -> int:
        return self._state

    def export_state(self) -> int:
        return self._state

    def next(self) -> float:
        """Return the next float in the half-open interval [0, 1)."""
        self._state = (self._state + MULBERRY32_INCREMENT) & UINT32_MASK
        value = self._state
        value = ((value ^ (value >> 15)) * (value | 1)) & UINT32_MASK
        mixed = ((value ^ (value >> 7)) * (value | 61)) & UINT32_MASK
        value = (value ^ ((value + mixed) & UINT32_MASK)) & UINT32_MASK
        value = (value ^ (value >> 14)) & UINT32_MASK
        return value / _FLOAT_DENOMINATOR

    def int(self, upper_bound: int) -> int:
        """Return an integer in [0, upper_bound)."""
        if (
            type(upper_bound) is not int
            or upper_bound <= 0
            or upper_bound > _MAX_SAFE_INTEGER
        ):
            raise ValueError("RNG upper bound must be a positive safe integer")
        return int(self.next() * upper_bound)


__all__ = ["Mulberry32"]
