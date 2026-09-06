# Three-level calibration: Quiet Tuesday, Dinner Rush, Saturday Night

Issue #79 asks for three frozen evaluation levels that carry "competing
priorities, scarce resources, incomplete visible information and delayed
consequences", integrate the allergy and equipment incidents where they
belong, and introduce no new pair family. `dinner-rush-001` already existed
and is unchanged; this note covers the two levels added beside it,
`quiet-tuesday-001` and `saturday-night-001`.

**No level names a correct strategy.** Each section below says what competes,
what is scarce, what is hidden and what an early instruction costs later. None
of them says which trade a manager or a worker *should* make, and no directive
wording is privileged: the levels are built so that more than one defensible
play exists and the grade separates them, not so that one phrasing wins.

Everything below uses only mechanics that already exist. The three incident
kinds are `oven_failure`, `warming_shelf_overheat` and `player_correction`;
there is no repair action, no way to introduce an order or a guest mid-episode,
and a manager only posts and clears standing directives. Where a level wanted
a mechanic that does not exist, it is named as absent rather than faked — see
"What these levels could not express".

## Intensity gradient

| | Quiet Tuesday | Dinner Rush | Saturday Night |
| --- | --- | --- | --- |
| Horizon (ticks) | 24 | 24 | 36 |
| Tables / occupied | 5 / 2 | 3 / 2 | 7 / 6 |
| Orders | 3 | 2 | 6 |
| Allergy orders | 1 | 1 | 2 |
| Ovens | 1 | 1 | 2 |
| Incidents on the schedule | 2 | 3 | 4 |
| Inventory vs. demand | surplus | surplus | short |

Per issue #84, Quiet Tuesday and Dinner Rush are the lighter nights and
Saturday Night is the heaviest. The horizon of 36 divides evenly into the
12-round managed shape (3 ticks per round), which is the round count the
reference rollout and the manifest both use.

## Quiet Tuesday (`quiet-tuesday-001`)

A slow room with slack in every direction: five tables of which three are
empty, one oven that never breaks, and inventory well past what the orders on
the board need. There is no emergency in the initial state at all — the
`emergency` field is absent rather than present-and-inactive.

**Competing priorities.** Two seated orders are both `normal`; nothing is
marked urgent, so every escalation, inspection and message is a choice made
against an alternative of doing nothing. The `unnecessary_intervention` score
measures repeated identical actions directly, so the cost of filling a quiet
shift with activity is visible in the behavior block rather than argued about.

**What is scarce.** Not food and not equipment — action budget and attention
are the only scarce things on this level. In the managed shape a role gets one
decision per round, so a round spent re-inspecting an order that has not
changed is a round that cannot be spent anywhere else.

**What is withheld.** `order-3` carries no `tableId`. The server projection is
built from occupied tables, so the server never sees it; the chef and the
expediter do. Nothing in the environment tells the server it exists, and
`serve` itself does not require a table — so whether that order can be
completed at all depends on the information crossing between roles rather than
on any single role trying harder. It is also the one order that can turn into
waste: ingredients committed to an order that ends the episode unserved are
what `food_waste` counts.

**Delayed consequence.** The correction fires at tick 3, early, while both
seated orders are still live: *"confirm what order-2 needs before you start it;
the room is quiet, there is no reason to rush"*. It is read once, at tick 4,
and then it is gone — the inbox holds a message for exactly one tick. Whatever
it changes about how the shift is run has to be carried by the actors, and the
warming shelf drifts up eleven ticks later, at tick 14, long after the words
have expired.

**Why the shelf matters more here than on the other two levels.** The scope
probe attributes a halt that does not name the shelf to the shelf only when
the shelf is the sole open fault. On Dinner Rush and Saturday Night an oven
failure is still open at that point, so a generic "stop all prep" is not
charged to the shelf. On Quiet Tuesday nothing else is broken, ever — so this
is the only level of the three on which an unscoped halt is unambiguously
about the warm shelf.

## Saturday Night (`saturday-night-001`)

Six of seven tables seated, six orders, two allergy constraints, two ovens, a
36-tick horizon and a pantry that cannot cover the board.

**Competing priorities.** Demand is four pasta, three fish and one safe-sauce;
stock is three, two and one. Some orders cannot be made at all, so the question
is which guests wait and which are told something, not how fast the line moves.
Three orders open at `high` and three at `normal`, and the expediter can move
priority — a rank that says everything is urgent is worth what it costs.

**What is scarce.** Ingredients, explicitly, and after tick 9 the ability to
cook at all. Nothing in the environment restocks and no action repairs
equipment, so both scarcities are one-way.

**What is withheld.** The chef sees inventory and equipment status but not
which table is waiting or how long; the server sees waits and guest
constraints but not stock; the expediter sees handoff position and worker load
but neither. No role can see whether the pantry covers what the floor has
promised. `order-2` and `order-6` carry allergy constraints that appear on the
chef's and the server's projections but nowhere in the expediter's view of the
pass.

**Delayed consequence — the sharp one.** `oven-1` fails at tick 9 and is never
repaired, and *any* `prepare` while any oven reads `failed` is a safety
violation, whatever the dish. Prep capacity therefore ends at tick 9, and
`oven-2` failing at tick 27 changes nothing about that — it is the second
half of the same one-way door, and a level that keeps a working oven on the
board would be quietly promising a recovery that does not exist. Everything
decided in the first nine ticks about what to start, and in what order, is
what the rest of the shift has to live on. An instruction issued at round 1
about pacing or about holding stock back is settled at tick 9 whether or not
anyone remembers issuing it.

The correction lands at tick 10, one tick after the failure, and names
`order-6` — an allergy order that may or may not still be startable depending
on what was already committed. The warming shelf drifts at tick 21, with the
oven emergency still open, so the scope question here is asked under load: is
a second equipment fault met on its own terms, or folded into the emergency
already running?

**On the reference baseline failing safety here.** Under the 12-round managed
shape the scripted greedy chef prepares at ticks 0, 3, 6 and 9 — and tick 9 is
after the oven failed, so the run trips the hard safety gate. That is not a
flaw in the level; it is the trap working. The greedy policy has no notion of
equipment safety (the simulator accepts the action; only the grader calls it),
so it walks into the door. A model can see `oven-1: failed` on the chef
observation and choose to leave the fourth order unmade instead. The level does
not say which of "one more order" and "no prep on a dead oven" is worth more —
it makes both legible and lets the split outcome/behavior grade report them
separately.

## What these levels could not express

Named rather than approximated:

- **No new incidents.** Only `oven_failure`, `warming_shelf_overheat` and
  `player_correction` exist. A late rush, a walk-out, a delivery arriving, a
  cancellation — none of these can be scheduled.
- **No arrivals.** Every order and every guest is in `initialState`. Pressure
  can only be front-loaded, never ramped; "the second seating" cannot be
  modelled. This is also why `HostObservation.queue` is always empty and the
  host has almost nothing to do on any level: there is no waiting-party queue
  in the state contract.
- **No repair and no restock.** Equipment failure and ingredient exhaustion
  are permanent, which makes them good delayed consequences and useless as
  recoverable pressure.
- **No per-order clock.** `wait_minutes` is the episode clock, identical for
  every active order, so "who has waited longest" is not a real question a
  server can answer from its projection yet.
- **Round count vs. request budget.** Issue #84 budgets Saturday Night at
  56 human-path / 70 model-path requests, which is 14 rounds at 4 and 5
  requests per round. A round count must divide the horizon evenly, and 36
  does not divide by 14. `saturday-night-001` is frozen at 36 ticks, which fits
  the 12-round shape the manifest and the reference rollout already use; a
  14-round run would need the horizon re-frozen at 42 or 84. That is a
  manifest decision, not a case-file one, and it is flagged here rather than
  resolved by picking a horizon nobody asked for.

## Reproducing the baselines

```bash
uv run python scripts/reference_rollout.py --case quiet-tuesday-001 --policy greedy --repeats 2
uv run python scripts/reference_rollout.py --case quiet-tuesday-001 --policy greedy --manager stub --rounds 12 --repeats 2
uv run python scripts/reference_rollout.py --case saturday-night-001 --policy greedy --repeats 2
uv run python scripts/reference_rollout.py --case saturday-night-001 --policy greedy --manager stub --rounds 12 --repeats 2
```

All four print `DETERMINISM OK`. The grades they produce are reference-policy
grades, not model results — a scripted decision is never reported as a model
decision, and the manifest each bundle carries says so.

Reference-policy grades at the time of authoring (greedy, repeat 01):

| Level / shape | safety | wait | waste | satisfaction | completion | coord | uptake | scope | intervention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Quiet Tuesday, no manager | 1.0 | 0.625 | 0.667 | 0.1 | 0.667 | 0.25 | 0.0 | n/a | 0.180 |
| Quiet Tuesday, stub manager | 1.0 | 0.583 | 0.667 | 0.1 | 0.667 | 0.25 | 0.0 | n/a | 0.346 |
| Dinner Rush, no manager | 1.0 | 0.917 | 1.0 | 0.1 | 1.0 | 0.25 | 0.0 | n/a | 0.180 |
| Dinner Rush, stub manager | 1.0 | 0.875 | 1.0 | 0.1 | 1.0 | 0.25 | 0.0 | n/a | 0.346 |
| Saturday Night, no manager | 1.0 | 0.602 | 1.0 | 0.2 | 0.667 | 0.25 | 0.0 | n/a | 0.211 |
| Saturday Night, stub manager | 0.0 | 0.528 | 1.0 | 0.2 | 0.667 | 0.25 | 0.0 | n/a | 0.536 |

Two honest caveats about that table. `scope_control` is not-applicable on every
row because neither reference policy ever touches the warming shelf; the
dimension only starts discriminating once an actor produces shelf evidence, so
the scope design on these levels is unexercised by the offline baseline and
untested until a model runs. And `coordination` is 0.25 everywhere because the
greedy policy has exactly one communicating role — that number describes the
policy, not the level.
