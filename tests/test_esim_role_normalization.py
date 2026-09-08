import pytest

from retro_backend.esim.role_normalization import normalize_role_for_v1


@pytest.mark.parametrize(
    ("canonical", "v1"),
    [("chef", "chef"), ("server", "server"), ("host", "host"), ("supply_lead", "expediter")],
)
def test_normalize_role_for_v1(canonical: str, v1: str) -> None:
    assert normalize_role_for_v1(canonical) == v1


def test_normalize_role_for_v1_preserves_legacy_expediter() -> None:
    assert normalize_role_for_v1("expediter") == "expediter"


def test_normalize_role_for_v1_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown canonical role"):
        normalize_role_for_v1("manager")
