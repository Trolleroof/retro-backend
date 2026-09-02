"""Role projections (#57) must match the four examples in
docs/restaurant-arena-tools.md field-for-field.
"""

import json
from pathlib import Path

from retro_backend.esim.contracts import RestaurantState
from retro_backend.esim.observations import observe

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "contracts/fixtures/restaurant-arena-v1.json").read_text()
)


def _state() -> RestaurantState:
    return RestaurantState.model_validate(FIXTURE)


def test_chef_sees_only_active_orders_with_allergy_and_priority():
    obs = observe(_state(), "chef")
    assert obs.role == "chef"
    ids = {o.id for o in obs.orders}
    assert ids == {"order-1", "order-2"}
    order_2 = next(o for o in obs.orders if o.id == "order-2")
    assert order_2.allergy == "peanut"
    assert order_2.priority == "high"
    assert obs.inventory == {"pasta": 3, "fish": 2, "safe-sauce": 1}


def test_server_sees_occupied_tables_and_guest_constraints():
    obs = observe(_state(), "server")
    assert obs.role == "server"
    table_ids = {t.id for t in obs.tables}
    assert table_ids == {"table-1", "table-2"}
    assert any(c.order_id == "order-2" and c.kind == "allergy" for c in obs.guest_constraints)


def test_host_sees_empty_tables_only():
    obs = observe(_state(), "host")
    assert obs.role == "host"
    assert [t.id for t in obs.tables] == ["table-3"]


def test_expediter_sees_all_orders_workers_and_incidents():
    obs = observe(_state(), "expediter")
    assert obs.role == "expediter"
    assert {o.id for o in obs.orders} == {"order-1", "order-2"}
    assert {w.id for w in obs.workers} == {
        "worker-chef",
        "worker-server",
        "worker-host",
        "worker-expediter",
    }
    # The fixture's emergency starts present-but-inactive (not omitted), so
    # the expediter sees its true (inactive) status rather than nothing.
    assert len(obs.incidents) == 1
    assert obs.incidents[0].kind == "oven_failure"
    assert obs.incidents[0].active is False


def test_observation_load_matches_the_role_own_worker():
    state = _state()
    obs = observe(state, "chef")
    chef = next(w for w in state.workers if w.role == "chef")
    assert obs.load == chef.load
