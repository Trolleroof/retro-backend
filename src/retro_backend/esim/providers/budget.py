"""A rolling-window request budget shared by every adapter in this process (#84).

The account this benchmark runs against is metered per minute, and the
per-adapter token bucket that came before this could not protect it for two
independent reasons.

**A token bucket does not bound a rolling window.** A bucket refilling at
``N/60`` per second with a burst capacity of ``N`` will admit ``N`` requests
instantly, then admit each refilled permit as it arrives -- up to ``2N`` starts
inside a single rolling 60 seconds. The account's limit is a rolling-window
limit, so the thing enforcing it has to be a rolling window too:
:class:`RollingWindowBudget` keeps the timestamp of every start still inside
the window and refuses to admit an ``N+1``-th one until the oldest leaves it.

**A limiter per adapter is a limiter per actor, not per account.** A managed
run builds two adapters -- one manager, one worker -- so two independent
buckets each admitted a full budget against one shared key, and every extra
concurrent run multiplied it again. The budget here defaults to a process-wide
singleton (:func:`default_budget`) precisely so that the manager, the four
workers, and their retries draw from one allowance.

This is a *process*-wide budget, not an account-wide one. A second benchmark
process, or the desktop app playing a live shift, holds its own instance and
knows nothing about this one. Splitting the account's allowance between those
callers is a deployment decision, made through
``ESIM_NIM_MAX_STARTS_PER_WINDOW``; see ``docs/esim-benchmark-manifest.md``.
"""

import asyncio
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MAX_STARTS_PER_WINDOW",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "AdmissionTimeout",
    "BudgetSnapshot",
    "RollingWindowBudget",
    "default_budget",
    "reset_default_budget",
]


class AdmissionTimeout(Exception):
    """Raised when a caller could not be admitted inside its queue deadline."""

# This process' share of the account, not the account's own ceiling. The demo
# account is metered at 40 requests per minute and two callers draw on it: the
# desktop app playing a live shift and this benchmark. The shares have to sum
# to less than 40, and the benchmark yields the larger half to the live
# session, which a person is waiting on. Raise it only by lowering the app's
# share to match -- see docs/esim-benchmark-manifest.md.
DEFAULT_MAX_STARTS_PER_WINDOW = 10
DEFAULT_WINDOW_SECONDS = 60.0

# A provider's Retry-After is honored, but not unconditionally: a header
# naming an hour would strand a run that is holding a simulation open. Longer
# waits are clamped to this and the call fails on its own timeout instead,
# which is a bounded, visible outcome rather than a silent stall.
MAX_RETRY_AFTER_SECONDS = 120.0

# The longest a caller will queue before giving up, derived rather than picked:
# a full window plus the largest penalty a provider can impose is the worst
# *legitimate* wait, so this never rejects a caller that was going to be
# admitted anyway. Beyond it something is wrong -- a budget configured far
# below what the run needs, or a provider throttling hard -- and a bounded,
# named failure beats a run stalling with nothing to read. Without it,
# admission sits outside the adapter's per-request timeout, so a throttled
# call could hold a turn open for minutes past the timeout that was supposed
# to bound it.
DEFAULT_MAX_QUEUE_SECONDS = DEFAULT_WINDOW_SECONDS + MAX_RETRY_AFTER_SECONDS

_ENV_MAX_STARTS = "ESIM_NIM_MAX_STARTS_PER_WINDOW"
_ENV_WINDOW_SECONDS = "ESIM_NIM_WINDOW_SECONDS"


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """What the budget looks like right now, for status display and records.

    Attributes:
        starts_in_window: Request starts still inside the rolling window.
        max_starts: The most starts the window will admit.
        waiting: Callers currently blocked waiting for admission.
        penalty_seconds: Seconds remaining on a provider-imposed backoff, 0.0
            when none is in force.
    """

    starts_in_window: int
    max_starts: int
    waiting: int
    penalty_seconds: float

    @property
    def admitting(self) -> bool:
        """True when the next caller would be admitted without waiting."""
        return self.penalty_seconds <= 0.0 and self.starts_in_window < self.max_starts


class RollingWindowBudget:
    """Admit at most ``max_starts`` request starts per rolling window.

    The guarantee is on *starts*, not completions: a request that is admitted
    and then runs long still occupies its slot for the whole window, because
    what the account meters is the moment the request was made.

    ``clock`` and ``sleep`` are injectable so a test can drive the window on a
    fake clock rather than by sleeping through real minutes.
    """

    def __init__(
        self,
        max_starts: int = DEFAULT_MAX_STARTS_PER_WINDOW,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        *,
        max_queue_seconds: float | None = DEFAULT_MAX_QUEUE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_starts < 1:
            raise ValueError(f"max_starts must be strictly positive, got: {max_starts}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be strictly positive, got: {window_seconds}")
        if max_queue_seconds is not None and max_queue_seconds <= 0:
            raise ValueError(
                f"max_queue_seconds must be strictly positive when set, got: {max_queue_seconds}"
            )
        self.max_starts = int(max_starts)
        self.window_seconds = float(window_seconds)
        self.max_queue_seconds = max_queue_seconds
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._waiting = 0
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        horizon = now - self.window_seconds
        while self._starts and self._starts[0] <= horizon:
            self._starts.popleft()

    async def acquire(self) -> float:
        """Wait until a start is admissible, record it, and return the wait.

        The lock is deliberately held across the wait. Releasing it to sleep
        would let every blocked caller wake against the same expired slot and
        admit itself, which is precisely the over-admission this class exists
        to prevent; holding it makes waiters queue and re-check one at a time.
        The returned delay is the queue time this call paid, recorded per
        request so a slow run can be told apart from a throttled one.

        Raises:
            AdmissionTimeout: If admission would take longer than
                ``max_queue_seconds``. Waiting happens outside any per-request
                timeout the caller holds, so without this bound a throttled
                call could hold a turn open long past it.
        """
        started_waiting = self._clock()
        deadline = (
            None if self.max_queue_seconds is None else started_waiting + self.max_queue_seconds
        )
        self._waiting += 1
        try:
            async with self._lock:
                while True:
                    now = self._clock()
                    self._prune(now)
                    if now < self._blocked_until:
                        self._wait_until(self._blocked_until, deadline, now)
                        await self._sleep(self._blocked_until - now)
                        continue
                    if len(self._starts) < self.max_starts:
                        self._starts.append(now)
                        return max(0.0, now - started_waiting)
                    # The oldest start leaves the window at this moment, and
                    # its slot is free from then on.
                    ready_at = self._starts[0] + self.window_seconds
                    self._wait_until(ready_at, deadline, now)
                    await self._sleep(ready_at - now)
        finally:
            self._waiting -= 1

    def _wait_until(self, ready_at: float, deadline: float | None, now: float) -> None:
        """Refuse a wait that would run past the caller's queue deadline.

        Checked before sleeping rather than after, so a caller that cannot be
        admitted in time fails immediately instead of sleeping first and
        discovering it later.
        """
        if deadline is not None and ready_at > deadline:
            waited = self.max_queue_seconds
            away = ready_at - now
            raise AdmissionTimeout(
                f"could not be admitted within {waited:.0f}s; next slot is {away:.0f}s away"
            )

    def penalize(self, retry_after_seconds: float) -> float:
        """Back every caller off after the provider said to, and report for how long.

        Applied to the whole budget rather than to the one caller that saw the
        429: the limit belongs to the account, so a refusal is evidence about
        what every caller in this process may do next, not just that one.
        """
        if retry_after_seconds <= 0:
            return 0.0
        capped = min(float(retry_after_seconds), MAX_RETRY_AFTER_SECONDS)
        deadline = self._clock() + capped
        self._blocked_until = max(self._blocked_until, deadline)
        return capped

    def snapshot(self) -> BudgetSnapshot:
        """Current admission state, safe to call without awaiting."""
        now = self._clock()
        self._prune(now)
        return BudgetSnapshot(
            starts_in_window=len(self._starts),
            max_starts=self.max_starts,
            waiting=self._waiting,
            penalty_seconds=max(0.0, self._blocked_until - now),
        )


_shared_budget: RollingWindowBudget | None = None


def default_budget() -> RollingWindowBudget:
    """The process-wide budget every adapter shares unless handed its own.

    Built on first use from ``ESIM_NIM_MAX_STARTS_PER_WINDOW`` and
    ``ESIM_NIM_WINDOW_SECONDS`` so a deployment can re-split the account's
    allowance without a code change.
    """
    global _shared_budget
    if _shared_budget is None:
        _shared_budget = RollingWindowBudget(
            max_starts=_env_int(_ENV_MAX_STARTS, DEFAULT_MAX_STARTS_PER_WINDOW),
            window_seconds=_env_float(_ENV_WINDOW_SECONDS, DEFAULT_WINDOW_SECONDS),
        )
    return _shared_budget


def reset_default_budget() -> None:
    """Drop the process-wide budget so the next call rebuilds it from the environment."""
    global _shared_budget
    _shared_budget = None


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be strictly positive, got: {value}")
    return value


def _env_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be strictly positive, got: {value}")
    return value
