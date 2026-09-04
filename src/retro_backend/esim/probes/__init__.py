"""Diagnostic probes: the behavioral half of the grade (#71).

An outcome score says how the shift went. A probe says whether the model
behaved the way the scenario was built to test, and reports it as a readable
record rather than a number: what was expected, what actually happened, and
whether that passed. Every probe here is a pure function of a
:class:`~retro_backend.esim.trajectory.Trajectory` -- no model calls, no
clock, no RNG -- so a grade is reproducible from a recorded file alone.
"""

from retro_backend.esim.probes.correction_uptake import (
    CORRECTION_UPTAKE_WINDOW,
    grade_correction_uptake,
)
from retro_backend.esim.probes.models import ProbeDimension, ProbeResult
from retro_backend.esim.probes.safety import grade_safety
from retro_backend.esim.probes.scope_control import (
    WARMING_SHELF_PROBE_TAG,
    ProbeVerdict,
    ScopeDecision,
    ScopeReport,
    classify_scope_action,
    grade_scope_control,
    summarize_scope,
)

__all__ = [
    "CORRECTION_UPTAKE_WINDOW",
    "WARMING_SHELF_PROBE_TAG",
    "ProbeDimension",
    "ProbeResult",
    "ProbeVerdict",
    "ScopeDecision",
    "ScopeReport",
    "classify_scope_action",
    "grade_correction_uptake",
    "grade_safety",
    "grade_scope_control",
    "summarize_scope",
]
