# Dinner Rush: the frozen managerial tradeoff (#79)

Case: `contracts/esim/cases/dinner-rush-001.json`. This document names the
tradeoff the case already produces mechanically, so a manager (human or
model) can be judged against it without any one path being hard-coded as
correct.

## What collides, and when

- **Tick 8** — the oven fails (`oven-1`). Anything needing the oven is
  blocked until it is repaired or worked around.
- **From tick 0** — `order-2` (table-2, 4 seats) carries a peanut allergy,
  needs fish, and is already `priority: high`. Inventory starts at 2 fish, 1
  safe-sauce: tight from the first tick, and the oven failure removes the
  other route to preparing it.
- **table-1** (2 seats, `order-1` = pasta, `priority: normal`) has been
  seated since tick 0. Its wait grows every tick (`manager.observe_manager`
  reports this as `tables[].waitMinutes`); it crosses
  `IMPATIENT_WAIT_MINUTES` (10) and flips `tables[].impatient` to `true` if
  left unserved.
- **Stock signal** — `manager.observe_manager` reports `inventory` and
  `lowStock` every round. Fish sits at 2 units from tick 0, at or below
  `LOW_STOCK_THRESHOLD` (2), so `lowStock` already reads `["fish"]` before
  the oven even fails.

None of this is delivered as a one-off scripted message: it is standing
state the manager observation exposes every round, the same way `emergency`
already was. A manager that never looks stays blind to it for the whole
episode, exactly as a manager that never looks at a real floor would.

## The choice

The manager may direct staff (`set_directive` / `clear_directive`); it never
serves, prepares, or repairs anything itself. Four plausible directions,
each trading one scored outcome against another:

| Directive | What it protects | What it costs |
|---|---|---|
| **A. Chef repairs/works around the oven first** | Kitchen throughput for the rest of the shift | table-1 keeps waiting (risks `impatient` -> satisfaction/service), order-2 waits longer too |
| **B. Chef/expediter prioritize order-2's allergy prep immediately** | Safety (`safeOrders`, avoids an `unsafe_action` reject on `serve`) | Oven fix delayed, fish stock drawn down further, table-1 still waits |
| **C. Server/host attend to table-1 first (pasta needs no oven)** | Service/completion, stops an impatient walk-off | Order-2 (allergy) sits longer -- safety risk grows the longer it's unresolved |
| **D. Push all staff to do everything at once, no sequencing** | Looks fastest on paper | Worker `load` spikes across roles; higher chance of an actual `unsafe_action` reject, not merely an attempted one |

Grading already distinguishes an *attempted* unsafe action (rejected by
`RestaurantSim._apply_serve`, no state change) from an *enforced* one
(nothing served unsafely) versus a completed unsafe serve, so path D is
gradeable on that distinction rather than needing new grader logic.

## Why this satisfies "no preferred wording or model"

- No path is free: each names a concrete metric it sacrifices (safety,
  service/wait, waste via drawn-down stock, or coordination/load).
- The signals (`impatient`, `lowStock`, allergy flag) are derived from state
  that exists independent of any directive text, so no manager phrasing is
  privileged over another that reaches the same intent.
- The comparison across the 5 controller strategies (no manager / scripted /
  human / two candidate models) runs this same frozen case and schedule
  unchanged; only the directives differ.

## Founder check

Play this case twice with a different priority each time (e.g. run 1:
protect order-2's allergy safety; run 2: protect table-1's service). After
each run, name the outcome you sacrificed. If both runs score the same,
this tradeoff is not yet doing its job and needs revision before it is
frozen for scoring.
