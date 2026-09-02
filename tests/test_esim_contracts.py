"""restaurant-arena/v1 models and published schemas must agree.

The Tui-side contract files are untracked in that repo, so the fixture and the
schemas here pin their content. Every example in
``docs/restaurant-arena-tools.md`` is reproduced below verbatim; if the models
drift from the contract, these fail.
"""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    ActionErrorCode,
    ChefObservation,
    ExpediterObservation,
    HostObservation,
    Observation,
    RestaurantState,
    Role,
    ServerObservation,
    Tool,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "contracts/fixtures/restaurant-arena-v1.json"
STATE_SCHEMA = json.loads(
    (ROOT / "contracts/restaurant-arena-state-v1.schema.json").read_text()
)
OBSERVATION_SCHEMA_V1 = json.loads(
    (ROOT / "contracts/restaurant-arena-observation-v1.schema.json").read_text()
)
OBSERVATION_SCHEMA = json.loads(
    (ROOT / "contracts/restaurant-arena-observation-v2.schema.json").read_text()
)
ACTION_SCHEMA = json.loads(
    (ROOT / "contracts/restaurant-arena-action-v1.schema.json").read_text()
)
TRACE_SCHEMA = json.loads(
    (ROOT / "contracts/restaurant-arena-trace-v1.schema.json").read_text()
)
FIXTURE = json.loads(FIXTURE_PATH.read_text())

OBSERVATION_ADAPTER: TypeAdapter[Any] = TypeAdapter(Observation)

# docs/restaurant-arena-tools.md, verbatim. Still spelled v1: the doc
# predates the inbox, and pinning it here is what proves v2 did not
# silently change any field v1 already had.
OBSERVATION_EXAMPLES_V1 = {
    "chef": {
        "version": "restaurant-arena-observation/v1",
        "role": "chef",
        "tick": 6,
        "orders": [
            {"id": "order-2", "items": ["fish"], "allergy": "peanut", "priority": "high"}
        ],
        "inventory": {"fish": 2, "safe-sauce": 1},
        "equipment": [{"id": "oven-1", "status": "failed"}],
        "load": 0.4,
    },
    "server": {
        "version": "restaurant-arena-observation/v1",
        "role": "server",
        "tick": 6,
        "tables": [
            {
                "id": "table-2",
                "orderId": "order-2",
                "status": "preparing",
                "waitMinutes": 4,
            }
        ],
        "guestConstraints": [
            {"orderId": "order-2", "kind": "allergy", "value": "peanut"}
        ],
        "load": 0.6,
    },
    "host": {
        "version": "restaurant-arena-observation/v1",
        "role": "host",
        "tick": 6,
        "queue": [{"partyId": "party-3", "size": 2, "waitMinutes": 3}],
        "tables": [{"id": "table-3", "seats": 2, "status": "empty"}],
        "waitEstimateMinutes": 7,
        "load": 0.2,
    },
    "expediter": {
        "version": "restaurant-arena-observation/v1",
        "role": "expediter",
        "tick": 6,
        "orders": [
            {"id": "order-2", "status": "preparing", "priority": "high", "handoff": "kitchen"}
        ],
        "workers": [
            {"id": "worker-chef", "role": "chef", "load": 0.4},
            {"id": "worker-server", "role": "server", "load": 0.6},
        ],
        "incidents": [{"kind": "oven_failure", "active": True}],
        "load": 0.3,
    },
}

# The same four observations under v2: one version string, one empty inbox,
# every other field byte-identical. Deriving them rather than retyping them is
# the point -- if v2 ever alters a v1 field, these stop matching.
OBSERVATION_EXAMPLES = {
    role: {
        **example,
        "version": "restaurant-arena-observation/v2",
        "messages": [],
    }
    for role, example in OBSERVATION_EXAMPLES_V1.items()
}

# A delivered message, as the chef sees it on the tick after it was sent.
OBSERVATION_WITH_MESSAGE = {
    **OBSERVATION_EXAMPLES["chef"],
    "messages": [
        {
            "from": "worker-expediter",
            "text": "86 the fish",
            "priority": "high",
            "sentAtTick": 5,
        }
    ],
}

# docs/restaurant-arena-tools.md, verbatim.
ACTION_EXAMPLE = {
    "version": "restaurant-arena-action/v1",
    "episodeId": "dinner-rush-001",
    "actorId": "worker-expediter",
    "actorType": "worker",
    "role": "expediter",
    "tool": "coordinate",
    "arguments": {"workerId": "worker-chef", "orderId": "order-2", "priority": "high"},
    "tick": 6,
    "timestamp": "2026-09-02T00:00:06Z",
    "requestId": "req-006-01",
    "idempotencyKey": "dinner-rush-001:req-006-01",
}

OBSERVATION_MODELS = {
    "chef": ChefObservation,
    "server": ServerObservation,
    "host": HostObservation,
    "expediter": ExpediterObservation,
}


def numbers_equal(left: object, right: object) -> bool:
    """Compare decoded JSON, treating 0 and 0.0 as the same wire value.

    JavaScript has one number type, so the client can never emit ``0.0`` for a
    field the models type as a float. Round-tripping is checked on values, not
    on the spelling of whole numbers.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            numbers_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            numbers_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right)
    return left == right


@pytest.mark.parametrize(
    "schema",
    [
        STATE_SCHEMA,
        OBSERVATION_SCHEMA_V1,
        OBSERVATION_SCHEMA,
        ACTION_SCHEMA,
        TRACE_SCHEMA,
    ],
    ids=["state", "observation-v1", "observation-v2", "action", "trace"],
)
def test_published_schemas_are_valid_draft_2020_12(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)


def test_state_schema_accepts_the_fixture() -> None:
    assert list(Draft202012Validator(STATE_SCHEMA).iter_errors(FIXTURE)) == []


def test_state_model_accepts_the_fixture() -> None:
    state = RestaurantState.model_validate(FIXTURE)

    assert state.seed == 218220
    assert state.episode.id == "dinner-rush-001"
    assert state.episode.max_ticks == 24


def test_fixture_round_trips_through_the_model_in_camel_case() -> None:
    state = RestaurantState.model_validate(FIXTURE)

    dumped = state.model_dump(by_alias=True, mode="json", exclude_none=True)

    assert numbers_equal(dumped, FIXTURE)


def test_state_dump_is_accepted_by_the_schema() -> None:
    state = RestaurantState.model_validate(FIXTURE)

    dumped = state.model_dump(by_alias=True, mode="json", exclude_none=True)

    assert list(Draft202012Validator(STATE_SCHEMA).iter_errors(dumped)) == []


def test_state_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RestaurantState.model_validate({**FIXTURE, "cashDrawer": 100})


def test_state_model_rejects_a_foreign_contract_version() -> None:
    with pytest.raises(ValidationError):
        RestaurantState.model_validate({**FIXTURE, "version": "restaurant-arena/v2"})


def test_state_model_is_frozen() -> None:
    state = RestaurantState.model_validate(FIXTURE)

    with pytest.raises(ValidationError):
        state.reputation = 0.0


@pytest.mark.parametrize("role", sorted(OBSERVATION_EXAMPLES))
def test_documented_observation_examples_validate(role: str) -> None:
    example = OBSERVATION_EXAMPLES[role]

    assert list(Draft202012Validator(OBSERVATION_SCHEMA).iter_errors(example)) == []
    assert OBSERVATION_MODELS[role].model_validate(example).role == role


@pytest.mark.parametrize("role", sorted(OBSERVATION_EXAMPLES))
def test_observations_resolve_through_the_role_discriminator(role: str) -> None:
    observation = OBSERVATION_ADAPTER.validate_python(OBSERVATION_EXAMPLES[role])

    assert isinstance(observation, OBSERVATION_MODELS[role])


@pytest.mark.parametrize("role", sorted(OBSERVATION_EXAMPLES))
def test_observations_round_trip_in_camel_case(role: str) -> None:
    model = OBSERVATION_MODELS[role].model_validate(OBSERVATION_EXAMPLES[role])

    dumped = model.model_dump(by_alias=True, mode="json", exclude_none=True)

    assert numbers_equal(dumped, OBSERVATION_EXAMPLES[role])


@pytest.mark.parametrize("role", sorted(OBSERVATION_EXAMPLES_V1))
def test_v1_examples_still_validate_against_the_published_v1_schema(
    role: str,
) -> None:
    """v2 supersedes v1; it does not retract it.

    ``contracts/restaurant-arena-observation-v1.schema.json`` stays published
    and unchanged, so a consumer pinned to v1 -- the Tui client among them --
    keeps validating exactly what it validated before.
    """
    example = OBSERVATION_EXAMPLES_V1[role]

    errors = list(Draft202012Validator(OBSERVATION_SCHEMA_V1).iter_errors(example))

    assert errors == []


def test_a_v1_observation_is_a_v2_observation_but_for_its_version() -> None:
    """The only field v2 adds is the inbox, and it is optional."""
    v1_without_version = {
        key: value
        for key, value in OBSERVATION_EXAMPLES_V1["chef"].items()
        if key != "version"
    }
    candidate = {
        **v1_without_version,
        "version": "restaurant-arena-observation/v2",
    }

    errors = list(Draft202012Validator(OBSERVATION_SCHEMA).iter_errors(candidate))

    assert errors == []


def test_a_delivered_message_validates_and_round_trips() -> None:
    errors = list(
        Draft202012Validator(OBSERVATION_SCHEMA).iter_errors(OBSERVATION_WITH_MESSAGE)
    )
    assert errors == []

    model = ChefObservation.model_validate(OBSERVATION_WITH_MESSAGE)

    assert model.messages[0].from_worker_id == "worker-expediter"
    assert model.messages[0].sent_at_tick == 5
    dumped = model.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert dumped == OBSERVATION_WITH_MESSAGE


def test_a_message_is_rejected_when_it_names_no_sender() -> None:
    broken = {
        **OBSERVATION_EXAMPLES["chef"],
        "messages": [{"text": "hi", "priority": "high", "sentAtTick": 5}],
    }

    errors = list(Draft202012Validator(OBSERVATION_SCHEMA).iter_errors(broken))

    assert errors != []


def test_a_role_cannot_carry_another_roles_projection() -> None:
    # The host sees table capacity; it never sees order contents.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HostObservation.model_validate(
            {**OBSERVATION_EXAMPLES["host"], "orders": [], "inventory": {}}
        )


def test_documented_action_example_validates() -> None:
    assert list(Draft202012Validator(ACTION_SCHEMA).iter_errors(ACTION_EXAMPLE)) == []

    envelope = ActionEnvelope.model_validate(ACTION_EXAMPLE)

    assert envelope.tool == "coordinate"
    assert envelope.idempotency_key == "dinner-rush-001:req-006-01"
    assert envelope.timestamp.tzinfo is not None


def test_action_round_trips_in_camel_case() -> None:
    envelope = ActionEnvelope.model_validate(ACTION_EXAMPLE)

    dumped = envelope.model_dump(by_alias=True, mode="json")

    assert dumped["idempotencyKey"] == ACTION_EXAMPLE["idempotencyKey"]
    assert dumped["episodeId"] == ACTION_EXAMPLE["episodeId"]
    assert dumped["actorType"] == ACTION_EXAMPLE["actorType"]


def test_action_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        ActionEnvelope.model_validate(
            {**ACTION_EXAMPLE, "timestamp": "2026-09-02T00:00:06"}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tool", "cook_everything"),
        ("role", "dishwasher"),
        ("actorType", "system"),
        ("episodeId", "dinner rush 001"),
        ("idempotencyKey", "dinner-rush-001/req-006-01"),
    ],
)
def test_action_rejects_values_outside_the_contract(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ActionEnvelope.model_validate({**ACTION_EXAMPLE, field: value})


def test_action_error_carries_a_stable_code_and_retryability() -> None:
    error = ActionError.model_validate(
        {"code": "role_not_permitted", "message": "chef cannot seat guests", "retryable": False}
    )

    assert error.retryable is False


def test_action_error_rejects_an_unlisted_code() -> None:
    with pytest.raises(ValidationError):
        ActionError.model_validate(
            {"code": "kitchen_on_fire", "message": "nope", "retryable": False}
        )


def test_error_codes_match_the_published_action_schema() -> None:
    published = ACTION_SCHEMA["$defs"]["actionError"]["properties"]["code"]["enum"]

    assert sorted(get_args(ActionErrorCode)) == sorted(published)


def test_tools_match_the_published_action_schema() -> None:
    assert sorted(get_args(Tool)) == sorted(ACTION_SCHEMA["properties"]["tool"]["enum"])


def test_roles_match_the_published_action_schema() -> None:
    assert sorted(get_args(Role)) == sorted(ACTION_SCHEMA["properties"]["role"]["enum"])


def test_all_four_roles_are_active_in_every_episode() -> None:
    state = RestaurantState.model_validate(FIXTURE)

    assert sorted(worker.role for worker in state.workers) == sorted(get_args(Role))
