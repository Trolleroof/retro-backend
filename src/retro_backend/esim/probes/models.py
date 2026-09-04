"""The one record shape every probe reports in (#71)."""

from typing import Literal

from retro_backend.esim.contracts import ArenaModel

# Named rather than free-form so a report can be grouped and compared across
# runs without string matching on probe names.
ProbeDimension = Literal[
    "safety",
    "scope_control",
    "correction_uptake",
    "coordination",
    "intervention",
]


class ProbeResult(ArenaModel):
    """One diagnostic check: what was asked, what happened, did it pass.

    ``expected`` and ``actual`` are prose on purpose. The score carries the
    machine-readable verdict; these two exist so a human reading a failing
    benchmark row can see what the probe was actually looking for without
    opening the grader.
    """

    probe: str
    dimension: ProbeDimension
    expected: str
    actual: str
    passed: bool
