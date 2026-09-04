"""Drive a reference policy through the frozen case and record it (#71).

The grader's tests need real trajectories, not fakes: a hand-written snapshot
list would let the grader agree with a fiction. This drives the actual
``RestaurantSim`` over the actual frozen case and records what happens, so
every state the grader reads is one the simulator really produced.

Lives in ``tests/`` because ``esim.policies`` is infrastructure-only and test
code is its one permitted importer (see that module's docstring).
"""

from retro_backend.esim.case import ENV_VERSION, load_case
from retro_backend.esim.contracts import ActionError, Role
from retro_backend.esim.policies import GreedyPolicy, IdlePolicy, Policy
from retro_backend.esim.rotation import ROLE_ORDER
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.trajectory import Trajectory, TrajectoryRecorder

CASE_ID = "dinner-rush-001"
ACTOR_IDS: dict[Role, str] = {
    "chef": "worker-chef",
    "server": "worker-server",
    "host": "worker-host",
    "expediter": "worker-expediter",
}


def record_rollout(policies: dict[Role, Policy], seed: int) -> Trajectory:
    """Run one episode to completion and return the recorded trajectory."""
    case = load_case(CASE_ID)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    state = sim.reset(seed)
    for policy in policies.values():
        policy.reset()

    recorder = TrajectoryRecorder(
        case_id=case.case_id,
        seed=seed,
        env_version=ENV_VERSION,
        contract_version=case.contract_version,
        event_schedule=case.event_schedule,
    )
    recorder.record_state(state)

    while state.episode.phase == "active":
        for role in ROLE_ORDER:
            policy = policies.get(role)
            if policy is None:
                continue
            envelope = policy.decide(sim.observe(role))
            if envelope is None:
                continue
            result = sim.act(envelope)
            recorder.record_action(envelope, result)
            if not isinstance(result, ActionError):
                state = result
        state = sim.advance_time()
        recorder.record_state(state)

    return recorder.finish("episode_complete")


def greedy_rollout(seed: int) -> Trajectory:
    return record_rollout(
        {
            role: GreedyPolicy(
                episode_id=CASE_ID, actor_id=ACTOR_IDS[role], role=role
            )
            for role in ROLE_ORDER
        },
        seed,
    )


def idle_rollout(seed: int) -> Trajectory:
    return record_rollout(
        {
            role: IdlePolicy(episode_id=CASE_ID, actor_id=ACTOR_IDS[role], role=role)
            for role in ROLE_ORDER
        },
        seed,
    )
