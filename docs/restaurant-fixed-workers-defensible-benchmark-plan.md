# Defensible fixed-worker restaurant benchmark plan

Status: proposed for review. This document does not authorize implementation.

Branch: `codex/restaurant-fixed-workers-benchmark`

Deadline: one working benchmark and an initial 30-model comparison within 24 hours.

## Why the current pilot is not a useful benchmark

The current pilot proves that one model manager can control deterministic workers, but its score does not measure much decision ability:

- A shift has 15 gameplay actions, and 11 can be `pass_round`.
- The observation exposes option labels such as `safe` and `risky`.
- Choosing the safe-labeled option and otherwise passing earns $216 against a $190 target.
- That trivial policy meets the current goal in 6 of 9 seed and variation combinations.
- Several intended measurements are not causal yet: order wait time is not advanced, allergy verification can apply too broadly, oven recovery has an obvious answer, and some staffing changes do not measurably affect outcomes.

We should keep the current `RestaurantGame` and fixed local workers, but change the three levels, measurements, and scored case set before comparing models.

## Repository ownership

Keep the product simulator and the frozen evaluation harness separate.

| Change | Repository | Contents |
| --- | --- | --- |
| Environment behavior | `tui-gamepigeon` | `RestaurantGame`, level configuration, order/incident rules, legal manager actions, observations, and native event traces |
| Environment tests | `tui-gamepigeon` | Deterministic replay and causal checks proving that interventions change the intended outcomes |
| Frozen runner and grading | `retro-backend` | Provider adapter, bounded retries and timeouts, episode execution, event-log grading, composite calculation, and validity accounting |
| Case registry and reports | `retro-backend` | Benchmark version, nine held-out configurations, scripted-policy calibration artifacts, per-model results, and provider audit artifacts |

The `retro-backend` runner must pin the `tui-gamepigeon` engine commit or an exported versioned environment contract. Do not duplicate restaurant rules in the backend. If a semantic environment change is needed, update `tui-gamepigeon`, version the contract, and then update the corresponding backend case registry and grader.

## Suggested model assignments

Use one stronger model for decisions that define the benchmark and cheaper models for bounded implementation or repetitive checks. These are assignments for parallel workers, not models being evaluated by the benchmark.

| Step | Task | Repository | Suggested model | Status |
| --- | --- | --- | --- | --- |
| 1 | Correct measurements and causal state transitions | `tui-gamepigeon` | `gpt-5.6-terra` | **DONE**; finish line verified |
| 2 | Remove answer leakage and validate observations/actions | `tui-gamepigeon` | `gpt-5.6-luna` | **DONE**; finish line verified |
| 3 | Prove actions are causal with development replays | `tui-gamepigeon` + `retro-backend` | `gpt-5.6-terra` | **DONE** in both repositories; finish line verified |
| 4 | Calibrate passive, cautious, and adaptive policies | `retro-backend` | `gpt-5.6-terra` | **DONE**; finish line verified |
| 5 | Freeze held-out cases and benchmark version | Both | `gpt-6-astra` | **DONE**; benchmark frozen as `restaurant-fixed-workers/v1` |
| Cross-cutting | Freeze score contract, legal actions, and level purposes | Both | `gpt-6-astra` | **DONE**; contracts and schemas frozen |
| Cross-cutting | Implement grading, composite score, and validity accounting | `retro-backend` | `gpt-6-astra` | Not started |
| Cross-cutting | Build bounded provider runner and retry policy | `retro-backend` | `gpt-5.6-luna` | Not started |
| Cross-cutting | Run representative models and audit artifacts | `retro-backend` | `gpt-5.6-sol` | Runs after Step 5 |

Steps 1 and 2 have merged into `main` in `tui-gamepigeon` through the measurement, expiry, neutral-option, and action-schema changes. Their finish-line checks passed: scores recompute from atomic trace events, and the leakage scan found zero decision labels. The table status is authoritative: a step is not complete until its detailed finish line below passes. Every remaining worker should return a small diff, a focused check, and the exact files changed. The `gpt-6-astra` owner reviews the score contract, grader, and final case freeze before any 30-model run.

## What the benchmark will test

Every model gets the same observable state, legal manager actions, decision windows, fixed-worker policy, and held-out cases. The benchmark measures four criteria.

| Criterion | Capability being tested | Episode score |
| --- | --- | --- |
| Service | Prioritize work and protect throughput under changing demand | Percentage of eligible orders served by their published deadline, scaled to 0–100 |
| Resources | Judge whether an intervention is worth its cost | Net operating return, including purchases and waste, normalized between a published floor and ceiling |
| Recovery | Diagnose and recover from a disruption | Time from equipment failure to restoration and the next affected order served, normalized between a target and hard deadline |
| Safety | Follow hard constraints under pressure | 100 for no safety violation; 0 after any unverified allergy order or other defined safety violation |

These scores test outcomes rather than whether a model chose an expected action. Different valid strategies can receive the same score.

Handover accuracy remains a required diagnostic. It checks whether the final report matches the recorded state, but it does not add points for copying numbers.

## Exact score

Each level publishes its deadlines, resource floor and ceiling, recovery target, recovery hard deadline, and safety rules before the model runs.

```text
Service = 100 × on-time eligible orders / eligible orders

Resources = 100 × clamp(
  (net operating return - floor) / (ceiling - floor),
  0,
  1
)

Recovery = 100                              when recovery time <= target
Recovery = 100 × (hard deadline - time)
                 / (hard deadline - target) when target < time < hard deadline
Recovery = 0                                when recovery is incomplete or late

Safety = 100 when there are zero violations; otherwise 0
```

Recovery is `N/A` in a level with no recovery incident. The episode's operational score is the mean of its applicable Service, Resources, and Recovery scores.

```text
Composite = operational score × (Safety / 100)
```

A safety violation therefore produces a composite of 0. Component scores remain visible so the result still explains the model's behavior.

`All Requirements Met` is true for an episode only when:

- the episode reaches a valid terminal state;
- Safety is 100;
- every applicable operational component is at least 80; and
- the final handover is factually accurate.

For each model, report the mean component scores, mean Composite, and percentage of scored episodes where `All Requirements Met` is true. Show scores to one decimal place and retain the raw per-case results.

## The three revised environments

The environments stay inside the current restaurant simulator. Levels change through configuration and the smallest engine corrections needed to make outcomes causal.

### 1. Quiet Tuesday: restraint and efficiency

Purpose: test whether the manager recognizes that intervention can be wasteful.

- Modest demand can be handled by the starting crew.
- Unnecessary stock, upgrades, or staffing moves reduce Resources.
- Ignoring an actual allergy constraint fails Safety.
- Passing is often reasonable, but blindly passing cannot satisfy every held-out instance.
- Applicable scores: Service, Resources, Safety. Recovery is `N/A`.

### 2. Dinner Rush: disruption recovery

Purpose: test whether the manager protects service while responding to an equipment failure.

- An oven failure occurs while heat-dependent orders remain active.
- Repair, workload changes, purchases, and delay each have observable costs and consequences.
- No option is labeled safe, risky, or correct.
- Recovery requires restored equipment and the first subsequent affected order served.
- Applicable scores: Service, Resources, Recovery, Safety.

### 3. Saturday Night: sustained allocation

Purpose: test whether the manager identifies the current bottleneck during prolonged pressure.

- Demand, inventory, station capacity, and worker placement create competing needs.
- Early overbuying hurts Resources; waiting too long hurts Service.
- At least one disruption forces reprioritization after the opening plan.
- Applicable scores: Service, Resources, Recovery, Safety.

The level names and simulator remain familiar. The held-out arrival schedules, incident timing, order mix, and starting constraints create variation without changing the task definition between models.

## Case set and model calls

Create two development instances and three held-out scored instances for each level:

- 6 development episodes calibrate causal effects and thresholds.
- 9 held-out episodes score each model.
- 30 models require 270 scored episodes for one complete sweep.

Development instances and scripted-policy results may be inspected while tuning. Freeze the 9 scored instances before any scored model run. Never tune a case after seeing a scored model's result; semantic changes require a new benchmark version.

Do not call the model on routine rounds that have no useful choice. Open a decision window when the state materially changes, an incident occurs, a published checkpoint is reached, or the model requested a legal intervention. Target at most eight gameplay calls plus one handover call per episode. The limit controls cost; it is not itself a capability score.

## Make the environments valuable before model evaluation

Complete these checks in order. Do not begin the 30-model sweep until every gate passes.

### Step 1: correct the measurements

Status: **DONE** — implementation merged and finish-line verification passed.

- Track order creation, deadline, service, expiration, and eligibility explicitly.
- Make the cash ledger include the real cost of purchases, staffing changes, upgrades, waste, and revenue exactly once.
- Record equipment failure, restoration, and the first subsequent affected order served.
- Scope allergy verification to the relevant order or constraint; one correction cannot certify later orders.
- Resolve the shelf-temperature contradiction so an action has one consistent safety meaning.
- Ensure every legal action either changes state, advances time, or returns a clear rejection.

Finish line: the four formulas can be recomputed from the saved event log without trusting a model-written summary.

### Step 2: remove answer leakage

Status: **DONE** — implementation merged and finish-line verification passed.

- Remove `safe`, `risky`, preferred, and equivalent labels from scored observations.
- Describe options using neutral facts: cost, duration, capacity, known risk, and affected station.
- Give human and model managers the same useful state and action information.

Finish line: no observation field directly tells a policy which legal option to choose.

### Step 3: prove actions are causal

Status: **DONE** in both repositories; finish line verified. `src/games/restaurant-arena/causal-replay.test.ts` replays each intervention on two distinct seeded development instances, each seed twice (treatment vs. no-op), and asserts the score delta from `scoreBenchmarkEvents`:

- Dinner Rush, seeds 2 and 5: answering the oven-failure decision with `repair` versus leaving it unanswered moves Recovery from 0 to 100 and Service by at least 10 points on both seeds, with `netOperatingReturn` diverging between runs.
- Quiet Tuesday, seeds 1 and 5: buying the unneeded second stove versus not moves Resources down by at least 10 points on both seeds (27–43 points across seeds 1–8), and the ledger reflects the full $60 cost every time.

Finding (resolved): `raiseDecision` used to hold only one decision open at a time and never re-raise a decision id once used (`decisionsRaised`), so an incident's decision could silently never surface at all if an earlier decision (e.g. `allergy_ticket`, which opens on tick 1 of every level) was still unanswered when the incident fired. A slow-to-answer manager — human or model — could lose the ability to earn Recovery points through no fault of choice quality, which was a benchmark fairness bug, not a scoring signal. Fix: a decision raised while another is already open is now queued (`decisionQueue`) instead of dropped, and is promoted the instant the current decision is answered (`promoteQueuedDecision`), so every decision a level triggers eventually surfaces regardless of manager pacing. See `decision-fairness.test.ts` for the regression coverage: an oven-failure decision that arrives while the allergy ticket is still open is queued rather than lost, and the manager can still answer it and reach Recovery = 100 once the allergy ticket clears.

`retro-backend`'s held-out-case replay work is done: `scripts/export-benchmark-causal-fixtures.ts` replays the same eight seed/level/intervention runs `causal-replay.test.ts` covers and exports each run's raw measurement events plus this engine's `scoreBenchmarkEvents` result to `retro-backend`'s `tests/fixtures/traces/benchmark-causal/*.json` — the held-out case registry for this gate (development instances, not the frozen Step 5 suite). `retro-backend/tests/test_esim_benchmark_causal_parity.py` replays every fixture's events through the Python grader (`grade_benchmark_events`) and asserts, per case, that its score matches this engine's recorded score field for field, and separately re-proves the same treatment-vs-no-op deltas above using only the Python-computed scores.

That replay surfaced one real cross-engine mismatch, since fixed: `grade_benchmark_events` rejected a served order whose `order_created` measurement had `eligible: false` (an order created too close to the shift's end for its own deadline to fit, e.g. a walk-in seated near closing — ordinary fixed-worker behavior, since workers do not consult the scoring window). `scoreBenchmarkEvents` already excludes such orders from the count without erroring; the Python grader now does the same, tracking referential order-created integrity separately from score eligibility instead of conflating the two.

For each development instance, replay the same seed with one intended intervention and with no intervention. The targeted raw metric must change in the expected direction, and its tradeoff must appear in another raw metric when the action has a cost.

Minimum gate:

- the intended action changes its targeted component by at least 10 points in two development instances; and
- unsupported staffing, purchase, or recovery choices are removed rather than scored as meaningful.

Finish line: every decision presented as important has a measurable consequence in the simulator, and `retro-backend` reproduces the same causal result and the same score from the same events. Met in both repositories.

### Step 4: calibrate three non-model policies

Status: **DONE** in both repositories; finish line verified. `scripts/run-step4-calibration.ts` and `retro-backend/tests/test_step4_calibration.py` verify that the deterministic policies produce distinct component profiles with zero false safety violations across all episodes, Passive fails the hard levels, Adaptive materially outperforms (>=10 points mean composite and recovery), and Cautious trades resources for safety without tying.

Finish line: the policies produce distinct component profiles, and no single trivial policy meets all requirements across the suite. Met in both repositories.

### Step 5: freeze the scored benchmark

Status: **DONE**; benchmark version frozen.

- **Benchmark Version**: `restaurant-fixed-workers/v1`
- **Registry**: `contracts/esim/held-out-case-registry.json` (`restaurant-fixed-workers-held-out/v1`, status `frozen`)
- **9 Frozen Cases**: `contracts/esim/cases/{quiet-tuesday-101,quiet-tuesday-308,quiet-tuesday-619,dinner-rush-103,dinner-rush-311,dinner-rush-633,saturday-night-107,saturday-night-315,saturday-night-641}.json`
- **Score Formulas**:
  - Service: $100 \times \frac{\text{on\_time\_orders}}{\text{eligible\_orders}}$ (100 if eligible == 0)
  - Resources: $100 \times \text{clamp}_{[0, 1]}\left(\frac{\text{net\_operating\_return} - \text{floor}}{\text{ceiling} - \text{floor}}\right)$
  - Recovery: applicable only on incident levels (dinner-rush, saturday-night). Clamped between target and deadline: $100 \times \frac{\text{deadline} - \text{time}}{\text{deadline} - \text{target}}$. Null on quiet-tuesday.
  - Safety: hard gate: 100 if safetyViolations == 0 else 0
  - Composite: $\frac{\text{mean}(\text{operational})}{100} \times \text{safety}$
- **Action Schema**: `restaurant-arena-action/v1` (manager decisions `decide_<id>_<option>`, `buy_second_stove`, `pass`)
- **Observation Schema**: `restaurant-arena-observation/v2` (neutral facts, equipment readings and safety thresholds, non-safety incident flags, zero answer leakage)
- **Worker Policy**: `restaurant-arena-scripted-workers/v1` (deterministic local scripted crew: chef, server, host, expediter / supply lead; zero worker model calls)
- **Engine Commit**: `f94d94fd83f2db28c8763eefd654d7560de3990c` (base: `ca646cf988880653ff9d5926c483a9364ea72a42`)
- **Suggested Model**: `gpt-6-astra`

Finish line: saved artifacts reproduce every score, all three models complete through the same interface, and the suite is neither universally passed nor universally failed. Verified by `docs/esim-held-out-freeze.md` and `tests/test_esim_held_out_registry.py`.

## Provider and malformed-output policy

The main leaderboard will not contain a `Provider Failures` column, and transport failures will not affect decision-quality scores.

- Rate limits, connection failures, and provider service errors are retried outside the scored attempt with bounded backoff.
- A provider-invalid episode is excluded from the score denominator and recorded in a separate run-audit artifact.
- If the provider returns a response but the model emits an invalid or illegal action, that is model behavior. Apply the frozen rejection/advance rule and retain it in the scored episode.
- Publish a model score only when at least 8 of its 9 episodes are valid. Otherwise show its benchmark result as `N/A` until the missing episode is rerun.
- Record `scored episodes / planned episodes` beside the report metadata so an apparently strong score cannot be based on a hidden small sample.

This separates operational reliability from decision quality while preventing incomplete runs from becoming favorable scores.

## Required report

The main comparison table is:

| Model | Service | Resources | Recovery | Safety | Composite | All Requirements Met |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Example model | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0% |

Recovery averages only episodes where the criterion applies. Each report also includes:

- benchmark version, engine commit, worker policy, and prompt version;
- the 9 per-case component and composite scores;
- scored episode count and total model calls;
- handover accuracy results; and
- a separate operational audit for excluded provider episodes.

The first 30-model table is a preliminary comparison. Nine episodes per model can reveal saturation and meaningful score spread, but it is not enough to claim a stable universal ranking. If time and request capacity remain, repeat the frozen episodes and report the number of independent attempts rather than silently pooling them.

## 24-hour execution plan

| Time | Work | Gate |
| --- | --- | --- |
| Hours 0–2 | Freeze formulas, event fields, legal actions, and level purposes | Written score contract has no contradictions |
| Hours 2–6 | Correct measurement bugs, remove answer labels, and configure the three levels | Scores recompute from event logs |
| Hours 6–10 | Build development instances and run causal pairs plus three scripted policies | All causal and calibration gates pass |
| Hours 10–13 | Freeze 9 scored instances and run three representative models | End-to-end artifacts and non-saturated scores |
| Hours 13–21 | Run 30 models with bounded account-level concurrency | At least 8 valid episodes per published model |
| Hours 21–24 | Audit artifacts and generate the comparison report | Every reported number links to reproducible evidence |

If a value gate fails, spend the remaining time fixing or reducing the case set. Do not spend provider budget on 30 models until the simulator demonstrates separation among the scripted policies.

## Scope for the first version

Keep the existing simulator, fixed workers, three named levels, supported manager-action boundary, and artifact approach. Add only the measurements and level configuration needed for the four scores.

Do not add model workers, a new simulator, subjective action rubrics, cross-engine parity claims, training, or a public leaderboard in this 24-hour pass.

## Stretch goal: frozen PPO workers

After the scripted-worker benchmark is calibrated and frozen, optionally add a learned worker condition. Train one shared PPO policy in `retro-backend`, export a small deterministic inference artifact, and load that artifact in `tui-gamepigeon` so every worker runs locally without provider calls. Include `role_id` in the observation and apply a role-specific legal-action mask. Freeze exploration, record the policy version and SHA-256 in each trace, and select an average-worker checkpoint using fixed scripted managers before evaluating model managers.

This is a separate experimental condition. The scripted workers remain the official benchmark baseline. Do not start PPO work until Steps 3–5 pass; otherwise worker-policy changes can hide flaws in the manager benchmark.
