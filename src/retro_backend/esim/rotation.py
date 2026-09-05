"""Deterministic role order and retry policy for an E-Sim rollout (#70)."""

from collections.abc import Iterator

from retro_backend.esim.contracts import ActionErrorCode, RestaurantState, Role
from retro_backend.esim.providers.base import (
    ERR_HTTP,
    ERR_MALFORMED,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    ERR_TRUNCATED,
)

ROLE_ORDER: tuple[Role, ...] = ("chef", "server", "host", "expediter")

# A bad request can be corrected without changing the state. Safety failures do
# not get a second chance: they are evidence the model proposed unsafe work.
RETRYABLE_ACTION_ERRORS: frozenset[ActionErrorCode] = frozenset(
    {"role_not_permitted", "invalid_arguments", "unknown_entity", "invalid_state"}
)


def role_rotation(state: RestaurantState) -> Iterator[tuple[Role, str]]:
    """Yield the one worker for each role in the benchmark's fixed order."""
    workers = {worker.role: worker.id for worker in state.workers}
    for role in ROLE_ORDER:
        worker_id = workers.get(role)
        if worker_id is None:
            raise ValueError(f"frozen case has no worker for role {role!r}")
        yield role, worker_id


def retryable_model_error(error: str | None) -> bool:
    """Whether retrying the same model call can reasonably help.

    ``ERR_TRUNCATED`` is retryable on the assumption that the retry is not
    actually identical: :func:`~retro_backend.esim.runner._messages` folds the
    error text into ``previousError`` on the next attempt, so the model is
    told its last answer ran out of room and to answer shorter. A retry under
    an *unchanged* prompt would be pointless -- same budget, same cutoff -- so
    this only holds while that feedback wiring stays in place.
    """
    if error is None:
        return False
    if error.startswith((ERR_TIMEOUT, ERR_NETWORK, ERR_RATE_LIMIT, ERR_MALFORMED, ERR_TRUNCATED)):
        return True
    if not error.startswith(ERR_HTTP):
        return False
    status = error.split(":", 1)[-1].lstrip().split(" ", 1)[0]
    return status in {"500", "502", "503", "504"}
