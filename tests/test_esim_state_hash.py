"""Canonical JSON is the definition of "the same state" for replay and grading."""

import pytest

from retro_backend.esim.state_hash import (
    STATE_HASH_PREFIX,
    canonical_json,
    state_hash,
)


def test_key_order_does_not_affect_the_hash() -> None:
    assert state_hash({"b": 1, "a": 2}) == state_hash({"a": 2, "b": 1})


def test_nested_key_order_does_not_affect_the_hash() -> None:
    left = {"outer": {"z": [1, {"b": 2, "a": 3}], "y": 4}}
    right = {"outer": {"y": 4, "z": [1, {"a": 3, "b": 2}]}}

    assert state_hash(left) == state_hash(right)


def test_hash_is_stable_for_a_known_state() -> None:
    # Pinned against `printf '%s' '{"seed":218220,"tick":0}' | shasum -a 256`,
    # so an accidental change to the canonical form fails loudly.
    assert state_hash({"seed": 218220, "tick": 0}) == (
        "sha256:950af57a97af3810a84f0c57e9ac23ad7fa39844cc7f49c802317b3dac0c51d2"
    )


def test_integer_and_float_spellings_hash_differently() -> None:
    # A field that drifts from int to float is a real contract change here,
    # even though JavaScript's JSON.stringify cannot express the difference.
    assert state_hash({"load": 0}) != state_hash({"load": 0.0})


def test_list_and_tuple_share_one_spelling() -> None:
    # esim.contracts models hold sequences as tuples; fixtures decode to lists.
    assert state_hash({"items": ["pasta"]}) == state_hash({"items": ("pasta",)})


def test_canonical_json_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_canonical_json_keeps_non_ascii_unescaped() -> None:
    assert canonical_json({"note": "café"}) == '{"note":"café"}'.encode()


def test_hash_is_prefixed_and_hex() -> None:
    digest = state_hash({"tick": 0})

    assert digest.startswith(STATE_HASH_PREFIX)
    assert len(digest) == len(STATE_HASH_PREFIX) + 64
    assert set(digest[len(STATE_HASH_PREFIX) :]) <= set("0123456789abcdef")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        state_hash({"reading": value})


def test_non_finite_numbers_are_rejected_when_nested() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        state_hash({"equipment": [{"reading": float("nan")}]})


@pytest.mark.parametrize("key", [1, None, (1, 2)])
def test_non_string_object_keys_are_rejected(key: object) -> None:
    with pytest.raises(TypeError, match="string object keys"):
        state_hash({"inventory": {key: 1}})
