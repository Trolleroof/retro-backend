# Fixed-Workers v2 benchmark: code-level audit

Scope: the eval/benchmark code only, not the write-up prose. Every claim below was
verified by reading the code and running it on this checkout (`main` @ `d7d921a`).

Two groups:

- **Part A — scoring and harness defects.** Things that make the published numbers
  wrong, unverifiable, or less meaningful than they read.
- **Part B — trace observability.** The traces we publish do not show how the model
  thought or acted. This is what a HuggingFace-style trace dataset would need.

---

## Part A — scoring and harness defects

### A1. `main` is red; the cross-engine parity suite has never run (blocking)

> **FIXED** on branch `fix/parity-suite-and-effort-labels`. `make verify` is green:
> 2172 passed, ruff clean, basedpyright clean. The 23 parity tests now bind each
> fixture to its level's frozen development case via `_case_for()`; the entry
> contract carries a named, self-checking exemption for the bridge. A third,
> pre-existing failure was also blocking `make verify` and is fixed: an unsorted
> import block in `sweep.py` that `ruff` rejected (CI never reached it, because
> `test` runs before `lint`).

`make verify` runs `pytest -q`, and CI runs `make verify` on every push to `main`
(`.github/workflows/ci.yml`). Current state on this checkout:

```
24 failed, 2146 passed, 12 skipped
```

- **23 failures: the entire `tests/test_esim_benchmark_v2_parity.py`.** Every test
  calls `grade_benchmark_events_v2(events)`, but the function signature is
  `grade_benchmark_events_v2(events, case)` (`src/retro_backend/esim/grader.py:476`).
  Both the test file and the two-argument signature landed in the *same* commit
  (`abe0ea9`, PR #99), so this suite has never passed since v2 existed.
- **1 failure: `tests/test_entry_contract.py::test_backend_has_no_typescript_or_javascript_source`.**
  The contract forbids `.ts`/`.js` in the backend; `scripts/fixed_workers_v2_bridge.ts`
  was added deliberately in `4a47de3` and the contract test was never amended.

**Why it matters.** That parity suite is the only executable evidence that the Python
grader reproduces the TypeScript engine's score. While it does not run, the
"replay-verified, deterministic, cross-engine" claim is asserted, not tested.

**Good news — parity actually holds.** Binding each fixture to its level's frozen case
and grading manually, all 8 fixtures reproduce the recorded `tsScore` exactly, on every
field:

```
dinner-rush-v2-recovery-incomplete.json   OK
dinner-rush-v2-recovery-repaired.json     OK
dinner-rush-v2-timer-expiry.json          OK
quiet-tuesday-v2-target-missed.json       OK
quiet-tuesday-v2-target-reached.json      OK
saturday-night-v2-allergy-repeated.json   OK
saturday-night-v2-capacity-bought.json    OK
saturday-night-v2-capacity-skipped.json   OK
```

So this is a harness fix, not a scoring fix.

**Fix.** Pass the frozen case into each call in the parity test (each fixture carries a
`variationId` that maps to its level's case), and add `scripts/*.ts` to the entry
contract's allowed list with a comment naming why the bridge is exempt.
Estimated ~20 minutes.

---

### A2. Resources and the cash target are the same measurement, published as two

In all 18 v2 case files, `thresholds.resourceFloor == 0` and
`thresholds.resourceCeiling == cashTarget`:

| level | cashTarget | resourceCeiling |
|---|---|---|
| quiet-tuesday-v2 | 140 | 140 |
| dinner-rush-v2 | 190 | 190 |
| saturday-night-v2 | 260 | 260 |

The scorer computes (`src/retro_backend/esim/grader.py:414`):

```python
resources = 100.0 * _clamp((ledger - floor) / (ceiling - floor))
```

With `floor = 0` and `ceiling = cashTarget` that is `100 * clamp(cash / target)`.
Therefore `resources == 100` **if and only if** `target_reached` is true. Confirmed
against every row of `tests/fixtures/fixed-workers-v2/calibration.json` — no exceptions.

**Two consequences.**

1. Composite already contains the cash-target signal, and the leaderboard reports the
   same quantity twice under two names.
2. In `BenchmarkEpisodeV2.all_requirements_met` (`grader.py:253`) the gate reads:

   ```python
   self.scores.valid_terminal_state
   and self.scores.safety == 100.0
   and all(score >= 80.0 for score in operational)   # includes resources >= 80
   and self.scores.target_reached
   and self.handover_accurate
   ```

   `target_reached` implies `resources == 100`, so the `resources >= 80` clause can
   never fail independently. It is dead code.

**Fix (pick one).** Either set `resourceCeiling` above `cashTarget` so Resources
measures margin beyond the target rather than progress toward it, or drop Resources
from Composite and keep `target_reached` as the cash signal. Either way, say in the
methodology which one Composite uses.

---

### A3. A third of the suite cannot separate a do-nothing policy from a good one

From `tests/fixtures/fixed-workers-v2/calibration.json` (27 scripted episodes,
3 policies x 3 levels x 3 seeds), composite and `requirementsMet`:

**quiet-tuesday — all three policies identical on every seed:**

| seed | passive | cautious | adaptive |
|---|---|---|---|
| 1 | 100 / pass | 100 / pass | 100 / pass |
| 5 | 100 / pass | 100 / pass | 100 / pass |
| 9 | 96.2 / pass | 96.2 / pass | 96.2 / pass |

**dinner-rush — nobody meets requirements, and the "good" policy loses one case:**

| seed | passive | cautious | adaptive |
|---|---|---|---|
| 2 | 49.7 / fail | 40.0 / fail | 0.0 / fail (safety violation) |
| 5 | 55.0 / fail | 40.5 / fail | 63.3 / fail |
| 9 | 46.3 / fail | 42.1 / fail | 60.0 / fail |

**saturday-night — this is the level that actually discriminates:**

| seed | passive | cautious | adaptive |
|---|---|---|---|
| 1 | 38.2 / fail | 38.6 / fail | 94.4 / pass |
| 2 | 41.3 / fail | 38.2 / fail | 95.8 / pass |
| 9 | 39.0 / fail | 39.7 / fail | 56.9 / fail |

**What this means for the published leaderboard.**

- 3 of 9 episodes (quiet-tuesday) carry **zero** discriminative signal. A passive
  policy that answers nothing scores exactly what the adaptive one scores.
- No scripted policy meets requirements on **any** dinner-rush case, so the
  all-requirements-met / Pass@1 metric is capped at **6/9 = 66.7%** by construction.
  The "66.7%, two of three shifts" figure in the write-up is at least partly a
  property of the case set, not of model skill.
- This is a large part of why the leaderboard is compressed into half a composite
  point. The compression is the benchmark's, not the models'.

**The gate is too coarse to see it** — *corrected from an earlier draft of this
document, which claimed the registry contradicted its own calibration data. It does
not.* The rejection gates are computed **suite-wide** by
`/Users/nikhi/tui-gamepigeon/scripts/run-fixed-workers-v2-calibration.ts`:
`passivePassesEveryCase` means "passive met requirements on all nine development cases"
and is honestly false, because passive fails dinner-rush; `allPoliciesTie` compares the
concatenation of each policy's nine composites and is honestly false for the same
reason. Both booleans are accurate as defined. The defect is that a suite-wide gate
cannot detect a single level on which every policy ties — which is exactly what
quiet-tuesday is. The registry records:

```json
"calibration": {
  "accepted": true,
  "rejectionGates": {
    "passivePassesEveryCase": false,
    "allPoliciesTie": false,
    ...
  }
}
```

Read per case, on quiet-tuesday, passive passes every case and all three policies tie
exactly — but no gate is evaluated per case, so nothing fires.

**Why the level is flat, precisely.** Quiet-tuesday raises three decisions per episode:
two `allergy_ticket:<order>` and one `shelf_hot`. It has no oven failure, so Recovery is
`None`; `shelf_hot`'s `serve` and `dump` score identically; `rush_wave` is gated on
`waiting.length >= 3` and is unreachable at 10-tick arrival gaps; and the cash target of
140 is met at roughly 247, so Resources saturates. The allergy verify is the only
decision that can move a score — and all three scripted arms always verify, so the
level's one real trap is never sprung by any arm. For a real model the level may well
discriminate sharply, since declining a verify takes a safety violation and zeroes the
composite. The calibration never probes it.

**Fix.** Add a per-case discrimination gate, and add a scripted arm that fails the
allergy verify so the gate has something to detect. Then either re-select the
quiet-tuesday seeds under a pre-registered screen or drop the level from the scored set.
Per the plan doc's own rule — "never tune a case after seeing a scored model's result;
semantic changes require a new benchmark version" — a re-selection ships as a new case
set version, not as an edit to v2.

---

### A4. An unparseable model response is a silent free pass

Three places combine:

1. `scripts/fixed_workers_v2_bridge.ts:46` — a null action skips `game.step` entirely:

   ```ts
   const result = command.action === null ? null : game.step(command.action);
   ```

2. The decision id was already added to `decisionsOffered` before the ask, so the
   decision is **never re-offered**. The model simply never faces it again.
3. `src/retro_backend/esim/fixed_workers_v2_runner.py:159` — on invalid JSON or an
   out-of-set action, `_generate` returns `(None, None)`. `provider_error` stays
   `None`, so `run_validity` stays `"valid"` and the episode is published.

**Why it matters.** On a decision where both options have a cost, emitting garbage can
score *better* than answering. A model that fails to produce valid JSON is scored as if
it made a deliberate, cost-free choice to abstain.

**And it is invisible.** `totals["invalid_responses"]` is counted and written into
`summary.json`, but `_model_report` (`src/retro_backend/esim/sweep.py`) never reads it,
so it appears in no table and gates nothing.

**Fix.** Minimum: surface `invalid_responses` per model in the sweep report and the
published table. Better: treat a non-answer as an explicit "declined" outcome with a
defined cost, or re-offer the decision and count the retry, so abstention is a scored
behaviour rather than an escape hatch.

---

### A5. The engine revision is not pinned, so no score is reproducible

The environment and the reference scorer both live in the tui-gamepigeon repo. What we
record about it:

- `src/retro_backend/esim/fixed_workers_v2_runner.py:327` — the v2 manifest stores
  `"engineRepo": str(config.engine_repo.resolve())`, a filesystem path on the machine
  that ran the sweep. No commit sha, no version.
- `src/retro_backend/esim/packet.py:553` — the packet manifest pins
  `repositoryRevision`, but that is *this* repo, not the engine.
- The exported parity fixtures carry an `engineVersion` field, so the engine can
  report one. We just never store it.

**Why it matters.** This is the single claim the whole page rests on. A third party
cannot re-run an episode against the same engine state, because nothing records what
that state was. A silent retune in the other repo moves every published score with no
trace.

**It is worse than one unpinned path.** `/Users/nikhi/tui-gamepigeon` carries git
worktrees `tui-gamepigeon-worker-fast`, `tui-gamepigeon-luna-sweep` and
`tui-gamepigeon-step5-freeze`. Each is a separate checkout that may hold a different
`FIXED_WORKERS_V2_SHIFT_PRESETS`, and a manifest recording only a filesystem path cannot
say which one a score came from. Before trusting any published v2 number, confirm which
tree the sweep actually ran against.

**Fix.** Have the bridge emit the engine's git sha (and `engineVersion`) in its
`complete` record; store both in `manifest.json`; assert they match the registry's
pinned value before a scored sweep runs.

---

### A6. Smaller defects

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | `grader.py:476` + case files | Case binding is level-wide, not case-wide: the grader checks arrivals, cash target, ceiling and failure schedule, all identical across the 6 cases in a level. Any dinner-rush trace grades clean against `dinner-rush-v2-dev-2`. Seeds *do* produce different episodes (calibration shows differing cash and order ids), but the frozen case contract cannot demonstrate dev/held-out separation and the grader cannot enforce it. | Emit the seed as a measurement; check it against the frozen case. |
| 2 | `grader.py:626` vs `sweep.py:317` | Two disagreeing publish gates: `summarize_benchmark` defaults to 8-of-9, `_model_report` requires 9-of-9 for v2. Only the second runs, but the first is exported and tested. | Make one authoritative; have the other call it. |
| 3 | `sweep.py:270` | `_metric`'s Safety fallback reads `outcome.safety`, which is 0..1, into a 0..100 column. Latent scale bug if a legacy grade reaches it. | Scale or drop the fallback. |
| 4 | `grader.py:413` | `service = 100.0` when there are no eligible orders — a free 100 for a shift that served nobody. | Return `None` (not-applicable), consistent with the module's own "not-applicable is not a pass" rule. |
| 5 | `fixed_workers_v2_runner.py` summary | v2 summaries emit snake_case totals (`total_billed_tokens`, `invalid_responses`) beside camelCase keys in the same JSON; `packet.py:788` looks for `invalidResponses`. | Normalise to camelCase at the write boundary. |
| 6 | `sweep.py:417` | Token-efficiency ties break on roster order, while `packet.py:899` states "ties are published as ties, and no stable ranking is claimed from one run." | Assign equal ranks on equal scores. |

---

## Part B — trace observability (the HuggingFace-style trace question)

Goal: a reader should be able to open a published episode and see **what the model
saw, what it thought, what it called, and what happened** — the thing that makes a
trace dataset worth reading. Right now none of those four are fully recoverable.

### B1. Chain-of-thought is never stored

`ModelReply` (`src/retro_backend/esim/providers/base.py:93`) has fields for `text`,
token counts, latency, `served_model`, `retry_after_seconds`, `queue_delay_ms` — and
**no reasoning field**.

The NIM adapter reads reasoning only to classify an error, then throws it away
(`src/retro_backend/esim/providers/nim.py:311`):

```python
message.get("reasoning_content") or message.get("reasoning")
```

It is used to decide whether a truncation message should say
`" (spent on reasoning tokens)"`. It is never persisted.

The OpenRouter adapter sends `reasoning_effort`
(`src/retro_backend/esim/providers/openrouter.py:37`) but does not read back
`reasoning` or `reasoning_details`.

**Consequence.** For reasoning models we bill and report reasoning tokens in the
token-efficiency score while discarding the reasoning itself. The one artifact that
would explain *why* a model chose an option does not exist in any bundle.

**Fix.** Add `reasoning: str | None` and `reasoning_tokens: int` to `ModelReply`;
populate from `message.reasoning_content` / `message.reasoning` /
`reasoning_details` (NIM and OpenRouter both return one of these); write both into
each `attempts.jsonl` record.

### B2. The prompt the model saw is not recorded

Per turn, the bridge prints `{observation, decision, availableActions}` to stdout, the
runner wraps it in `_messages()` and sends it — and then records only the *response*
(`fixed_workers_v2_runner.py:136-146`): tick, kind, attempt, servedModel, latency,
tokens, response text, action.

`trajectory.json` holds `initialState`, `finalState`, `terminalObservation`, `events`,
and `actions` (tick / kind / action / result). Nowhere is the per-turn observation, the
option set, or the rendered system+user message stored.

**Consequence.** You cannot reconstruct the decision the model was actually facing,
which means you cannot audit a bad choice, cannot re-prompt another model on the same
turn, and cannot build an SFT/eval dataset out of the traces.

**Fix.** Record the full `turn` payload (observation, decision, availableActions) and
the exact rendered messages in each `attempts.jsonl` record, or in a sibling
`turns.jsonl` keyed by tick so the file stays diffable.

### B3. The raw model replies are deliberately excluded from the published packet

`src/retro_backend/esim/packet.py:78`:

```python
PRIVATE_FILES = ("attempts.jsonl",)
```

and the generated report says so in as many words (`packet.py:708`):

> `attempts.jsonl` — every raw model reply — is **not** in this tree

So the published packet contains scores, states and decisions, but not a single word
the model wrote. The page's pitch is that everything is inspectable; the most
interesting artifact is the one held back.

**Fix.** The stated reason is that raw replies carry provider metadata and credentials.
The `redact()` pass (`packet.py:240`) already exists and already runs over every other
file. Run `attempts.jsonl` through the same pass, drop provider metadata to an
allowlist (servedModel, tokens, latency), and ship it. If something must stay private,
name the specific field, not the whole file.

### B4. There is no tool-call trace, because there are no tool calls

The v2 loop is not a tool-calling agent loop. The model gets a JSON observation and
must reply with exactly `{"action": "<one of availableActions>"}`
(`fixed_workers_v2_runner.py:_messages`, `_parse_action`). Actions per episode are
only the decision points the bridge raises plus at most one purchase — a handful of
turns, not a shift's worth of operating.

This is a legitimate design, but it should be stated plainly, because "agents running
inside enterprises" implies tool use and this measures single-shot option selection at
a few branch points. The `esim` v1 path (`tools.py`, `runner.py`) does have a real
tool surface; v2 does not use it.

**Fix (choose and state).** Either document v2 as constrained-choice evaluation and
stop calling it agentic, or extend the bridge to expose the v1 tool surface per tick
and record the tool call sequence.

### B5. The v2 bundles have no packaging, sanitisation or re-grade path at all

`grep -n "v2" src/retro_backend/esim/packet.py` returns **nothing**. The packet builder
is entirely v1:

- `_regrade` (`packet.py:263`) validates `trajectory.json` against the v1 `Trajectory`
  model and calls the v1 `grade()`. A v2 `trajectory.json` is the engine's `completed`
  payload and will not validate, so every v2 row would be reported as
  "does not match this build".
- `SANITIZED_FILES` expects `initial-state.json`, `final-state.json`,
  `directives.json`; v2 bundles write none of those.

**Consequence.** The evidence-packaging path — redaction, source/sanitised digests,
executed re-grade, manifest — covers the v1 runs and not the v2 runs that produced the
published leaderboard.

**Fix.** Add a v2 branch to `packet.py` that re-grades via
`grade_benchmark_events_v2(events, case)` and `verify_benchmark_replay_v2`, and set
`SANITIZED_FILES` per benchmark version.

---

## Suggested fix order

1. **A1** — fix the parity suite signature and the entry-contract exemption. Gets CI
   green, turns the determinism claim back into a tested claim. (~20 min, mechanical.)
2. **A5 + B1** — record the engine sha, and capture reasoning text and reasoning
   tokens. Both are additive; neither changes a published number. (~half a day.)
3. **B2 + B3** — record per-turn observations, and ship a redacted `attempts.jsonl`.
   This is what makes the traces actually readable. (~half a day.)
4. **A4** — make invalid responses visible, then decide how to score them.
5. **A2 + A3** — these change what the published numbers mean. Decide the intent
   first, then rescore and restate the methodology.

**A6** items are cheap and can ride along with whichever change touches that file.
**B5** only matters once v2 evidence has to be published as a packet.

---

## Part C — what a published trajectory has to show (APEX + HF Hub)

Two live references for the bar we are being measured against.

### C1. Mercor APEX-Agents 1.1 — the leaderboard we are submitting to

`https://www.mercor.com/apex/apex-agents-leaderboard/corporate-lawyer-agent/`
(paper: [arXiv:2509.25721](https://arxiv.org/abs/2509.25721))

Structure of a per-model result on that page:

- Leaderboard rows: model name, **effort label** (`Max`, `xHigh`, `High`, `Max • Pro`),
  and a single `Pass@1` percentage. Nothing else on the row.
- A **"Show sample task for &lt;model&gt;"** control on every row — the trace is reached
  per model, not as one shared example.
- The sample panel is `Sample task (1 out of 80)` with three tabs:
  **Task / Environment / Trajectory**, plus a rubric panel that renders per-criterion
  results for that model (it reads "No rubric data available for this model" when a
  model has none).
- A disclaimer on the panel: *"This sample task is provided for illustration only. The
  scores may not reflect an agent's overall performance on the leaderboard."*

Mapping their four panes onto what we can produce today:

| APEX pane | What it shows | Our artifact | State |
|---|---|---|---|
| **Task** | The exact instruction given to the agent | not stored anywhere | **missing** (see B2) |
| **Environment** | The world the agent worked in | `trajectory.json` has `initialState` / `finalState` only | **partial** — no per-turn state |
| **Trajectory** | Turn-by-turn: model messages, reasoning, tool calls, results | `attempts.jsonl`, which is withheld and has no reasoning | **missing** (B1, B3, B4) |
| **Rubric** | Per-criterion pass/fail for that model on that task | component scores exist; no per-criterion rows bound to the trace | **partial** |

Three things to copy directly, cheapest first:

1. **Put the effort label on the leaderboard row.** — **DONE.** `_model_report`
   now publishes a `decode` block (`reasoning_effort`, `max_output_tokens`,
   `requested_provider_route`, `uniform_across_episodes`) and promotes
   `reasoning_effort` to the top level of each model's row. A setting is named
   only when every scored episode agrees on it; on disagreement the value is
   `null` and `uniform_across_episodes` is false, so the report cannot invent a
   setting the score was not earned under. Original finding: APEX prints `Max` / `xHigh` /
   `High` beside every model. We already record `openrouterReasoningEffort` and
   `requestedProviderRoute` in each bundle's `manifest.json` and `summary.json` — and
   then drop them: `_model_report` (`src/retro_backend/esim/sweep.py`) never reads them
   back, so `sweep-report.json` carries no effort, no provider route and no
   `maxOutputTokens` per model. Two models run at different efforts are presented as
   directly comparable. This is a ~10-line fix and it closes the write-up reviewer's
   "was temperature fixed?" question at the same time.
2. **One sample episode per model, reachable from its row**, with the same disclaimer
   about a single sample not representing the aggregate.
3. **A rubric pane** — per-criterion outcomes for the episode, not just the four
   component numbers. Our grader already produces `ProbeResult`s and
   `safety_findings` in the v1 `Grade`; v2 throws that structure away and keeps only
   scalars. Surfacing the probe rows is what makes a score arguable rather than
   asserted.

### C2. Hugging Face Hub trace viewer — the zero-work distribution channel

[Agent Traces on the Hub](https://huggingface.co/changelog/agent-trace-viewer)
(Apr 7, 2026): upload agent session JSONL to a HF Dataset and the Hub **auto-detects
the trace format**, tags the dataset as
[Traces](https://huggingface.co/datasets?format=format:agent-traces&sort=trending), and
renders a viewer for **sessions, turns, tool calls and model responses**. No
preprocessing — the session JSONL from Claude Code, Codex, Factory/Droid, Pi or Hermes
uploads as-is.

Existing datasets in that shape, useful as schema references:

- [Exgentic/agent-llm-traces](https://huggingface.co/datasets/Exgentic/agent-llm-traces)
  — 1,781 execution traces over 6 benchmarks: full conversation history, token usage,
  timing, tool calls and results, and tool definitions.
- [Exgentic/agent-llm-traces-v2](https://huggingface.co/datasets/Exgentic/agent-llm-traces-v2)
  — 10,057 runs, OpenTelemetry-shaped.
- [lambda/hermes-agent-reasoning-traces](https://huggingface.co/datasets/lambda/hermes-agent-reasoning-traces)
  — multi-turn tool-calling trajectories with step-by-step `<think>` blocks alongside
  real tool results.
- [PatronusAI/TRAIL](https://huggingface.co/datasets/PatronusAI/TRAIL) — traces
  annotated with failure taxonomies.

**What this means for us.** The fields those datasets carry — conversation history,
reasoning blocks, tool calls, tool results, tool definitions, token usage, timing — are
exactly the fields B1 through B4 say we discard. Fix B1–B3 and publishing to the Hub is
a file upload; skip them and there is nothing to upload, because a bundle of scores
with no model text does not render in a trace viewer.

Order of operations: capture reasoning (B1) → capture the per-turn prompt (B2) → ship
the redacted `attempts.jsonl` (B3) → emit one JSONL per episode in a Hub-detected
session shape → upload.

### C3. Revised priority

The write-up's reviewer asked for a limitations section. Most of what belongs in it is
in Part A of this document. But for the submission itself, the ranking changes:

1. ~~**A1** — green CI.~~ **Done.**
2. ~~**C1.1** — effort and provider route on every leaderboard row.~~ **Done.**
3. **B1 + B2** — capture reasoning and the per-turn prompt. Without these there is no
   Trajectory pane to build and no HF dataset to publish.
4. **B3 + C2** — redact and ship `attempts.jsonl`, then upload as a Hub trace dataset.
5. **A5** — pin the engine sha, so a third party can reproduce what they are reading.
6. **A2 + A3** — the two findings that change what the published numbers mean. Decide
   intent, rescore, restate.
