# Step 5: the held-out case registry

`contracts/esim/held-out-case-registry.json` is the scored test set for the
fixed-worker restaurant benchmark: nine held-out configurations, three per
level, each pinning everything a scored run consumes. It replaces
`held-out-case-registry.draft.json`, whose starting states were hand-authored
and did not match anything the engine produces.

Every check below is executable in `tests/test_esim_held_out_registry.py`, so
the registry cannot drift away from its own review.

## What each case pins

| Field | Source |
| --- | --- |
| `seed` | selected by the screen below |
| `startingState` (+ `startingStateHash`) | `RestaurantGame.getInitialState()` at tick 0, at the frozen v1 role boundary |
| `orderSchedule` | the `order_created` measurements the seed produces on the passive reference run |
| `eventSchedule`, `expectedIncident`, `incidentTiming` | tui-gamepigeon's `SHIFT_PRESETS` incidents for the level |
| `thresholds.resourceFloor` / `resourceCeiling` | the engine's own `benchmark_config` measurement |
| `thresholds.recoveryTarget` / `recoveryHardDeadline` | same, `null` where Recovery does not apply |
| `applicableMetrics` | Service, Resources, Safety, plus Recovery on the two incident levels |
| `benchmarkVersion`, `graderVersion` | `restaurant-fixed-workers/v1`, `restaurant-fixed-workers-grader/v1` |

Each case also has a loadable twin at `contracts/esim/cases/<caseId>.json`, so
`load_case` — which `sweep.validate_sweep_inputs` calls on every case before a
scored sweep — resolves. A test asserts the two are the same bytes.

Two canonicalizations are applied to the engine's tick-0 state and recorded
rather than done silently: the canonical `supply_lead` role becomes the frozen
v1 `expediter` (`role_normalization.py`), and `episode.id` is set to the case
id. The engine only rewrites `episode.id` for levels other than its default
shift, so all three dinner-rush seeds otherwise ship as `dinner-rush-001` —
which is also an existing development case id. The engine's own value is kept
in each entry as `engineEpisodeId`.

## How the seeds were chosen

Candidates are the nine seeds pre-registered in the v1 draft, written before
any model was run. Each is screened on three structural properties — never on
a score — and a seed that fails is replaced by the next ascending seed that
passes:

- **R1 recovery earnable.** On a level with an oven failure, a reference run
  that answers the incident with `repair` must record both
  `equipment_restored` and `recovery_order_served`. Otherwise Recovery scores
  0 for a manager who did exactly the right thing.
- **R2 allergy answerable.** No `safety_violation` may occur in a reference
  run that verifies every allergy decision it is offered, on both a
  cautious-pace and a fast-pace arm. Safety is a hard gate on the composite,
  so an unanswerable allergy order is a zero nobody earned.
- **R3 distinct.** The eligible-order arrival schedule must differ from every
  already-accepted seed on the same level.

Four replacements, all logged in the registry:

| Level | Pre-registered | Rejected | Why | Accepted |
| --- | --- | --- | --- | --- |
| quiet-tuesday | 307 | 307 (R3) | identical arrival schedule to seed 101 | 308 |
| dinner-rush | 631 | 631, 632 (R1) | oven restored but no affected heat ticket left to serve, so Recovery is unearnable | 633 |
| saturday-night | 313 | 313 (R2), 314 (R3) | `order-x7` served unverified on tick 13, the tick its own decision first surfaces; then 314 duplicates 107's schedule | 315 |

Final set: quiet-tuesday 101/308/619, dinner-rush 103/311/633, saturday-night
107/315/641. None reuse a development seed (quiet-tuesday 1/5, dinner-rush
2/5, saturday-night 1/2).

## Review results

| Check | Result |
| --- | --- |
| Every case is valid | **Pass.** All nine load as `FrozenCase`, hash-match the registry copy, and pass `validate_sweep_inputs` in dry-run. |
| No case uses the old allergy id | **Pass.** Every allergy decision is `allergy_ticket:<orderId>` against an order that exists in the case; the episode-scoped `allergy_ticket` appears nowhere. |
| Every case uses the fixed scorer | **Pass.** All nine declare `restaurant-fixed-workers-grader/v1` and carry exactly the four constants `grade_benchmark_events` reads, matching the engine's `benchmark_config`. |
| No case tuned on model results | **Pass.** Seeds are pre-registered, replacements are structural and logged, and no model identifier appears anywhere in the registry. |
| Nine cases defeat one memorized sequence | **Fail.** See below. |

### The blocking finding: option order leaks the answer

A driver that never reads the observation and always picks the **first listed
option** meets All Requirements on **8 of 9** cases with a mean composite of
**96.8**, against **4 of 9** and **93.7** for the adaptive reference policy.

The cause is in tui-gamepigeon's `DECISIONS` catalogue
(`src/games/restaurant-arena/arena-view.ts`): the scoring-optimal option is
listed first in `allergy_ticket` (`verify`), `oven_down` (`repair`) and
`rush_wave` (`seat_all`). Step 2 removed the `safe` / `risky` labels but left
the positional signal, so the label leak became an ordering leak. The mirror
image confirms it — always taking the **last** option scores 33.9 mean and
0 of 9.

A 30-model table built on this suite would rank a model that always answers
"the first one" above one that reads the shift, so the registry ships as
`"status": "pending-freeze"`. `sweep.validate_sweep_inputs` refuses any
non-dry-run sweep of a registry that does not say `frozen`, and a test holds
that consequence in place.

**Fix belongs in tui-gamepigeon**, not here: randomize option order per case
seed (or otherwise break the positional signal), then re-run the screen. The
case set itself does not need to change — the seeds, states, schedules and
constants in this registry stay valid, and flipping the gate is a `status`
change plus a regenerated `tests/fixtures/held-out-freeze/separation.json`.

### Two engine fairness defects found while screening

Both were screened out of the final case set, but they affect any future seed
selection and belong upstream:

1. **Recovery can be unearnable.** On 142 of 300 screened dinner-rush seeds,
   the oven fails and can be repaired, but no affected heat ticket remains to
   serve, so `recovery_order_served` never fires and Recovery is 0 no matter
   what the manager does. Recovery is one of three operational components on
   that level.
2. **An allergy order can be served before its decision exists.** On
   saturday-night seed 313 `order-x7` is served unverified on tick 13, the
   same tick its `allergy_ticket:order-x7` decision first surfaces. The
   decision is raised at the end of the tick, after service has already run.
   This is the same class as the decision-serialization bug fixed in #94, in a
   different place, and it lands on the hard safety gate.

## Separation evidence

`tests/fixtures/held-out-freeze/separation.json` records every scripted arm on
all nine cases. No model was called to produce it.

| Arm | Mean composite | All Requirements Met |
| --- | ---: | ---: |
| adaptive reference | 93.7 | 4/9 |
| cautious | 60.8 | 3/9 |
| passive | 57.4 | 3/9 |
| passive + always buys the stove | 47.6 | 0/9 |
| always first option (memorized) | 96.8 | 8/9 |
| always last option (memorized) | 33.9 | 0/9 |
| never answers a decision | 0.0 | 0/9 |

Step 4's separation gate holds on the held-out nine: adaptive beats both
weaker policies by more than 10 mean composite points, the over-intervening
spender arm loses Resources on every Quiet Tuesday case (so restraint is
measured, which is that level's whole purpose), and no scripted policy sweeps
the suite.

## Reproducing

The registry is generated from the engine, not hand-written. Regeneration
needs a `tui-gamepigeon` checkout at the pinned commit; the exporter runs each
case under scripted drivers and reports the tick-0 state, arrival schedule,
incident timing and `benchmark_config` constants. Any semantic change to a
case — a different seed, level preset, threshold, or engine rule — is a new
benchmark version and invalidates every earlier scored run.
