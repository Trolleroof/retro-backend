"""Canonical role names at the frozen v1 E-Sim boundary."""

from retro_backend.esim.contracts import Role

_V1_ROLE_BY_CANONICAL: dict[str, Role] = {
    "chef": "chef",
    "server": "server",
    "host": "host",
    "supply_lead": "expediter",
    # Keep already-v1 names readable while callers migrate to canonical names.
    "expediter": "expediter",
}


def normalize_role_for_v1(role: str) -> Role:
    """Map a canonical cross-engine role to the frozen v1 E-Sim role set."""

    try:
        return _V1_ROLE_BY_CANONICAL[role]
    except KeyError as exc:
        raise ValueError(f"unknown canonical role {role!r}") from exc
