# quiet-tuesday v2.1 seed screen — pre-registration

**Status: pre-registered. No seed has been screened at the time this document is
committed.** The commit that adds this file contains no results, and that ordering is the
evidence that the predicate below was fixed before any outcome was seen. A later commit
adds `tests/fixtures/fixed-workers-v21/seed-screen.json`.

Engine revision this screen is defined against:
`c49b01cfad194ec5de169fbeccd07c28aa04690d` (`/Users/nikhi/tui-gamepigeon`).
The screen script records the sha and worktree path it actually ran in. That repository
carries several worktrees, so a screen that did not record its own revision would not be
reproducible.

## Why this screen exists

On the frozen v2 case set, all three scripted policies score identically on every
quiet-tuesday seed: composites 100 / 100 / 96.15, requirements met on all nine episodes.
Three of the nine scored episodes therefore carry no signal about which model is better —
they add the same constant to every model.

The existing rejection gates did not catch this because they are computed suite-wide.
`passivePassesEveryCase` is false because passive fails dinner-rush; `allPoliciesTie`
compares each policy's whole nine-composite vector. Both are honestly false. A suite-wide
gate cannot see a single level on which everything ties.

## Why the level is flat, precisely

Quiet-tuesday raises three decisions per episode: two `allergy_ticket:<orderId>` and one
`shelf_hot`. Of the components:

- **Recovery** is `null` — the level's only incident is `warming_shelf_overheat`, so
  `recoveryApplicable` is false.
- **Resources** cannot move. `resourceCeiling` equals `cashTarget` at 140 and the shift
  earns roughly 247, so the component saturates.
- **`shelf_hot`** scores identically whether answered `serve` or `dump`.
- **`rush_wave`** is gated on `waiting.length >= 3` and is unreachable at 10-tick arrival
  gaps against one-party-per-tick seating.

That leaves **Service**, moved only by the allergy tickets. And the three scripted arms
all answer `verify`, which is correct, so the level's one real decision is never failed by
any arm. The level was never shown to be flat — it was never probed.

## The decision this screen selects on

`DECISIONS` in `src/games/restaurant-arena/arena-view.ts` gives `allergy_ticket` three
options, with three distinct score signatures:

| option | mechanism | effect |
|---|---|---|
| `verify` | one tick of kitchen time; components checked against the declared allergen | correct; costs prep time |
| `substitute` | no kitchen time; the swapped component is never checked | serves unverified → `safety_violation` → `safety = 0` → composite 0 |
| `pull` | ticket voided, `−$22` and a satisfaction reduction | the order stays in `eligibleOrders` but never reaches `order_served` → Service loses 1/eligible |

So the level already contains a three-way judgment call: careless, over-cautious, and
correct are all separable. A seed is being selected for whether that call is *dense enough
to bite*, not for being harder in any other sense. Arrival cadence, cash target and
duration are untouched, so quiet-tuesday stays the low-load end of the cross-level
gradient.

**Why three tickets and not two.** With 11 parties, pulling k tickets leaves Service at
`(11 − k)/11`: k = 2 gives 81.8, which clears the 80 threshold, and k = 3 gives 72.7,
which does not. Over-caution is only punishable from three tickets up. The threshold comes
from that arithmetic, not from a wish to make the shift harder.

## Arms

Four, all deterministic and observation-free apart from the decision id:

| arm | policy |
|---|---|
| `verify-all` | every `allergy_ticket:*` → `verify`; `shelf_hot` → `dump`; no purchases |
| `careless` | every `allergy_ticket:*` → `substitute`; otherwise as `verify-all` |
| `over-cautious` | every `allergy_ticket:*` → `pull`; otherwise as `verify-all` |
| `passive` | answers no decision; steps `tick` only |

`careless` is the proof obligation. If it does not score 0, this screen has falsified its
own premise and the level does not punish carelessness.

`passive` is reported separately because it may fail by the same mechanism as `careless`
(an unanswered allergy order serving unverified). If the two coincide, the record must say
which mechanism fired rather than implying two independent findings.

## Accept predicate

A seed is accepted if and only if all five hold.

- **S1 — judgment present.** The episode raises at least three distinct
  `allergy_ticket:<orderId>` decisions.
- **S2 — carelessness punished.** `careless` records `safety == 0`, `composite == 0`, and
  does not meet requirements.
- **S3 — over-caution punished.** `over-cautious` does not meet requirements, *while*
  keeping `safety == 100`. The safety clause matters: without it, S3 could be satisfied by
  a second copy of S2 rather than by a judgment failure on Service.
- **S4 — the correct answer is rewarded.** `verify-all` meets requirements, keeps
  `safety == 100`, reaches the cash target, and scores `composite >= 80`. This is the
  screen most likely to reject a seed: `verify` adds a prep tick to every open order, so
  tickets clustered near a deadline can make even correct play miss Service.
- **S5 — uncontaminated.** The eligible-order arrival signature differs from every
  already-accepted v2.1 seed, and the seed does not appear anywhere in the v2 case set:
  `{1, 2, 5, 9, 17, 19, 23, 29, 31, 37, 43, 47, 53}`.

S5's exclusion covers all three levels, not just quiet-tuesday. The disjointness assertion
in `tests/test_esim_fixed_workers_v2.py` compares development and held-out seeds globally,
so a quiet-tuesday seed of 31 would collide with `dinner-rush-v2-held-out-31`.

## Selection rule

Scan seeds **11 through 74** ascending. The first three accepted become the development
cases; the next three become the held-out cases. Disjointness holds by construction.

> A seed that fails any screen is skipped and the scan continues. No seed was kept,
> dropped or replaced on the basis of any score a model produced.

Every rejection is recorded with the screen it failed and a non-empty reason.

## Fallback, pre-registered here so that using it is not a post-hoc move

1. If fewer than six seeds are accepted in 11–74, extend the scan to **11–199** under the
   identical rule and record `scannedRange` accordingly.
2. If fewer than six are accepted in 11–199, that is positive evidence that
   `allergyOdds: 8` cannot produce a judgment-dense quiet shift. The full screen output is
   kept as `tests/fixtures/fixed-workers-v21/seed-screen-exhausted.json` and the work
   stops. The remaining lever would be a new engine preset, which changes the level id and
   therefore the benchmark version — out of scope here.

At `allergyOdds: 8` over 11 orders, P(at least three tickets) is roughly 17%, so a 64-seed
scan is expected to yield about 11 candidates before S2–S5 are applied.

## What v2.1 will and will not claim

It will claim that quiet-tuesday separates careless, over-cautious and correct play, and
that the separation was selected by this screen rather than found after the fact.

It will not claim to defeat a memorized policy. Option *order* is shuffled per
`(episodeSeed, decisionId)` by `shuffledOptions`, so a positional constant such as "always
take the first option" fails. A *semantic* constant — "always `verify`" — still clears
every quiet-tuesday seed, and no choice of seed changes that, because seeds vary how many
allergy tickets appear and never whether verifying is right. Earning that gate needs a
decision whose correct answer depends on the situation, which is engine game logic rather
than seed selection, and is recorded as the next benchmark version's work.
