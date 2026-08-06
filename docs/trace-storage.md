# Trace storage runbook

Where gameplay data lands in R2, how the `episodes` D1 index projects it, and the
read-only commands for inspecting both from a workstation.

This is the operator-facing companion to [episode-assembly.md](episode-assembly.md)
(which explains how an episode is decided) and [production-setup.md](production-setup.md)
(which explains how the account is configured). Nothing here mutates state; every
recipe below is a read.

## Status: partly live

| Piece | State |
| --- | --- |
| `raw/`, `validated/`, `rejected/`, `dlq/` prefixes | Live — written by the deployed Worker today. |
| `episodes/parquet/v1/…` prefix | Lands with the Parquet writer ([#30](https://github.com/adavyas/retro-backend/issues/30)). |
| `episodes` D1 table | Lands with the episode index ([#31](https://github.com/adavyas/retro-backend/issues/31)); no migration file exists yet. |
| `retro-episode-events` queue | **Created, but not yet bound.** Step 1 of the [activation checklist](episode-assembly.md#activation-checklist) has been run — the queue and `retro-episode-events-dlq` both exist in the production account. Neither has a registered producer or consumer until the Worker is redeployed. |

Read the sections about the Parquet prefix and the `episodes` table as the contract
those branches are being built against, not as a description of production.

## The storage map

Everything lives in one bucket: `retro-gameplay-raw`, bound as `RAW_BUCKET` in
[`wrangler.toml`](../wrangler.toml). The prefix is the only thing that separates
concerns.

| Prefix | Key shape | Written by |
| --- | --- | --- |
| `raw/` | `raw/user={user_id}/game={game_id}/chunk={chunk_id}.json` | The ingest route, before the pointer is published to `retro-gameplay-events`. Exact client bytes. |
| `validated/` | `validated/date={YYYY-MM-DD}/user={user_id}/game={game_id}/chunk={chunk_id}.json` | `GameplayConsumer`, after the chunk passes the v2 event contract. `date` is the UTC date of the envelope's `created_at_ms`. |
| `rejected/` | `rejected/date={YYYY-MM-DD}/user={user_id}/game={game_id}/chunk={chunk_id}.json` | `GameplayConsumer`, on a permanent rejection of an otherwise well-formed pointer. |
| `rejected/` (quarantine) | `rejected/date={YYYY-MM-DD}/quarantine={sha256}.json` | `GameplayConsumer`, when the queue message is not a valid pointer at all — there is no `user_id`/`game_id` to partition by, so the digest of the message stands in. |
| `dlq/` | `dlq/date={YYYY-MM-DD}/message={sha256}.json` | `GameplayDeadLetterConsumer`, archiving a pointer that exhausted its five processing retries. |
| `episodes/parquet/v1/` | `episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/{game_id}.parquet` | The Parquet writer, from one `episode.ready` message. `{YYYY}/{MM}/{DD}` are UTC and zero-padded, derived from `completed_at_ms`. |
| `episodes/parquet/v1/` (sidecar) | `…/{game_id}.manifest.json` | The same writer, alongside each Parquet object. |

Key construction lives in [`src/retro_backend/storage/r2.py`](../src/retro_backend/storage/r2.py)
(`raw_key`, `put_derived`) and [`src/retro_backend/consumers/gameplay.py`](../src/retro_backend/consumers/gameplay.py)
(`_validated_key`, `_rejected_key`, and the DLQ archive key).

Every key is deterministic, and every write goes through the conditional
`onlyIf: {etagDoesNotMatch: "*"}` path with a stored `payload_sha256`. A replayed
message therefore re-derives the same key, sees the same digest, and no-ops. Same
key with *different* bytes is an error, not an overwrite — see
[Duplicate and retried writes](#duplicate-and-retried-writes).

### Retention

[`cloudflare/r2-lifecycle.json`](../cloudflare/r2-lifecycle.json) is the source of
truth. It is applied with `make apply-r2-lifecycle`, and
[`scripts/deployment_preflight.py`](../scripts/deployment_preflight.py) fails the
deploy if the live bucket configuration drifts from the `EXPECTED_LIFECYCLE`
constant. Edit both together or the next deploy breaks.

| Rule | Prefix | Effect |
| --- | --- | --- |
| `raw-chunks-infrequent-access-then-expire` | `raw/` | 30 days → `InfrequentAccess`, deleted at 90 days. |
| `validated-artifacts-expire` | `validated/` | Deleted at 7 days. |
| `rejected-artifacts-expire` | `rejected/` | Deleted at 7 days. |
| `dlq-archives-expire` | `dlq/` | Deleted at 7 days. |

**There is no rule for `episodes/parquet/v1/`, so Parquet objects and their
manifests are retained indefinitely.** That is deliberate: they are the training
corpus, and they outlive the 7-day `validated/` window they were built from. The
practical consequence is a hard deadline — after 90 days the `raw/` chunks are
gone, so an episode that was never converted to Parquet can never be
reconstructed. After 7 days it can only be rebuilt from `raw/`, not `validated/`.

The live policy has never successfully applied — the previously committed file used
the S3-style `{"Rules": […]}` spelling, which `wrangler r2 bucket lifecycle set`
rejects. Confirm before trusting any of the above:

```bash
npx --yes wrangler@4.112.0 r2 bucket lifecycle list retro-gameplay-raw
```

## The `episodes` D1 table

One row per episode, in the `retro-backend` database. It is a queryable projection
of what is already in R2 — the Durable Object holds assembly state, R2 holds the
data, and this table exists so an operator can answer "which episodes exist" without
listing a bucket.

| Column | Meaning |
| --- | --- |
| `episode_id` | `sha256(f"{user_id}:{game_id}")` hex. Primary key, and the same address the `EpisodeAssembler` Durable Object uses. |
| `user_id` | Canonical Worker-side user id (not the upstream auth subject). |
| `game_id` | Client-supplied game id. Also carries a `UNIQUE` index. |
| `game_type` | Game family, e.g. `tetris`. First path segment under the Parquet prefix. |
| `game_version` | Game version string as reported by the client. |
| `status` | `complete`, `incomplete`, or `abandoned` — see [episode-assembly.md](episode-assembly.md#terminal-statuses). |
| `chunk_count` | Number of source chunks folded into the episode. |
| `row_count` | Parquet rows written — one per human control that survived the behavior-cloning join, not one per raw event. No-op controls (`moved == false`) are dropped, so this is normally well below the episode's event count. See [parquet-v1.md](parquet-v1.md). |
| `first_event_sequence` / `last_event_sequence` | Event range covered. For a `complete` episode these chain without gaps; for `incomplete` they bound a range with holes. |
| `completed_at_ms` | UTC ms the episode became terminal. Drives the `{YYYY}/{MM}/{DD}` path segments. |
| `parquet_key` | Full R2 key of the Parquet object. |
| `parquet_bytes` | Byte length of that object. |
| `parquet_sha256` | SHA-256 of the Parquet bytes, 64 hex chars. |
| `manifest_key` | Full R2 key of the manifest sidecar. |
| `written_at_ms` | UTC ms the writer finished. Compare against `completed_at_ms` to see conversion lag. |

Indexes:

| Index | Columns | Use |
| --- | --- | --- |
| implicit primary key | `episode_id` | Point lookup, and the upsert conflict target. |
| `episodes_game_id_idx` (UNIQUE) | `game_id` | Look up by the id a client or a support ticket knows. The uniqueness is a guard: two episodes for one `game_id` would mean the assembler emitted twice. |
| `episodes_type_completed_idx` | `game_type`, `completed_at_ms` | "Recent episodes for this game", and dataset-export range scans. |
| `episodes_user_completed_idx` | `user_id`, `completed_at_ms` | Per-user timelines and deletion requests. |

### Why R2 is written before D1

The writer puts the Parquet object and its manifest to R2 **first**, and only then
upserts the D1 row (`ON CONFLICT(episode_id) DO UPDATE`). There is no transaction
across the two, so a crash between them is expected, not exceptional. The ordering
picks which half-written state you can be left in:

- **R2 object, no D1 row.** The chosen failure. Data is durable; the index is stale.
  A Queue retry re-derives the identical key, the conditional write no-ops, and the
  upsert converges. Nothing is lost, and the window closes on its own.
- **D1 row, no R2 object.** The state the ordering exists to prevent. It would
  advertise a `parquet_key` that 404s, and a downstream export would fail on a
  dangling pointer rather than on a missing row.

So for an operator reading a partially-written state, the invariant is: **`episodes`
is a lagging index, never a leading one.** A row implies the object existed at write
time. A missing row implies nothing about the object. Both mismatch directions have
queries in [Reconciling R2 against D1](#reconciling-r2-against-d1); only one of them
is a bug.

## The manifest sidecar

Every `.parquet` object has a `.manifest.json` next to it, same base name. It is
small, plain JSON, and readable without a Parquet reader — which matters because
nothing in this repo can parse Parquet at runtime.

```json
{
  "version": 1,
  "episode_id": "…",
  "user_id": "usr_…",
  "game_id": "game_…",
  "game_type": "tetris",
  "game_version": "1",
  "status": "complete",
  "chunk_count": 3,
  "row_count": 412,
  "first_event_sequence": 1,
  "last_event_sequence": 412,
  "completed_at_ms": 1760000000000,
  "parquet_key": "episodes/parquet/v1/tetris/2026/07/29/game_….parquet",
  "parquet_bytes": 20481,
  "parquet_sha256": "…64 hex…",
  "source_chunk_ids": ["chk_…", "chk_…"],
  "written_at_ms": 1760000000123
}
```

The fields are the same values the D1 row carries, plus `source_chunk_ids` — the
chunks the episode was built from, which is what lets you walk back to the
`validated/` or `raw/` objects behind a suspect episode.

`parquet_sha256` is the integrity anchor. It is written in three places that must
agree: the manifest, the D1 row, and the R2 object's own `payload_sha256` custom
metadata (set by `put_derived`). Verifying is a `shasum` against the downloaded
bytes — see [Verify a checksum](#verify-a-checksum). Any disagreement is covered in
[Checksum mismatch](#checksum-mismatch).

`chunk_count` versus `row_count` is the other useful pair: chunks are transport
units, rows are gameplay steps. A `complete` episode with a plausible `chunk_count`
but a `row_count` of zero means the events carried no convertible steps, which is a
join-logic problem, not a storage one.

## CLI recipes

### Before you run any of them

The repo pins Wrangler in the [`Makefile`](../Makefile) (`WRANGLER_VERSION ?= 4.112.0`)
and every target invokes `npx --yes wrangler@$(WRANGLER_VERSION)`. The commands below
spell the version out for the same reason: a bare `npx wrangler` resolves to whatever
is newest that day, and R2/D1 flag spellings have moved between majors.

Two environment concerns:

- **`CLOUDFLARE_ACCOUNT_ID` must be set explicitly** for any command that does not
  read it from a config file — that is, the `r2 *` and `queues *` commands here. If
  your API token can reach more than one account, Wrangler cannot pick one
  non-interactively and the command fails. `npx --yes wrangler@4.112.0 whoami` lists
  the accounts and their ids.
- **`d1 execute --remote` needs a config that resolves the database id.** The
  committed `wrangler.toml` ships an all-zero placeholder on purpose (see
  [production-setup.md](production-setup.md#why-the-ids-are-not-written-down-here)),
  so run `make prepare-deployment-config` first and pass
  `--config wrangler.production.toml`, exactly as `make migrate-remote` does.

Read the id out of `whoami` and export it before anything else in this section:

```bash
export CLOUDFLARE_ACCOUNT_ID=REPLACE_WITH_ACCOUNT_ID
```

### List episode objects under a prefix

**Wrangler has no `r2 object list` command** — `wrangler r2 object` offers only `get`,
`put`, and `delete`. Listing is only available through R2's S3-compatible API, which
needs a separate R2 access key id and secret (Cloudflare dashboard → R2 → Manage API
tokens), not the Workers API token used everywhere else:

```bash
aws s3api list-objects-v2 --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" --bucket retro-gameplay-raw --prefix "episodes/parquet/v1/tetris/2026/07/29/"
```

If you do not have S3 credentials, do not go hunting for them — the `episodes` table
is the intended listing surface, and the D1 queries below are the supported path.
Falling back to a bucket listing is for reconciliation only.

### Download a Parquet object

```bash
npx --yes wrangler@4.112.0 r2 object get "retro-gameplay-raw/episodes/parquet/v1/tetris/2026/07/29/game_abc123.parquet" --remote --file ./game_abc123.parquet
```

### Download its manifest

```bash
npx --yes wrangler@4.112.0 r2 object get "retro-gameplay-raw/episodes/parquet/v1/tetris/2026/07/29/game_abc123.manifest.json" --remote --pipe
```

`--pipe` writes to stdout instead of a file, which is what you want for a small JSON
sidecar. The object path is `{bucket}/{key}` in one positional argument — there is no
separate `--bucket` flag. `--remote` is spelled out so the command never silently
reads Miniflare's local simulation.

### Verify a checksum

```bash
shasum -a 256 ./game_abc123.parquet
```

Compare that digest against three sources, which must all agree: `parquet_sha256` in
the manifest, `parquet_sha256` in the D1 row, and the `payload_sha256` custom metadata
on the R2 object itself.

### Recent episodes by game type

```bash
npx --yes wrangler@4.112.0 d1 execute retro-backend --remote --config wrangler.production.toml --json --command "SELECT game_id, status, row_count, chunk_count, completed_at_ms, parquet_key FROM episodes WHERE game_type = 'tetris' ORDER BY completed_at_ms DESC LIMIT 20;"
```

### Recent episodes by user

```bash
npx --yes wrangler@4.112.0 d1 execute retro-backend --remote --config wrangler.production.toml --json --command "SELECT game_id, game_type, status, row_count, completed_at_ms FROM episodes WHERE user_id = 'usr_REPLACE' ORDER BY completed_at_ms DESC LIMIT 20;"
```

### Episodes by status

```bash
npx --yes wrangler@4.112.0 d1 execute retro-backend --remote --config wrangler.production.toml --json --command "SELECT status, COUNT(*) AS episodes, SUM(row_count) AS rows_total, MAX(completed_at_ms) AS newest FROM episodes GROUP BY status;"
```

A healthy corpus is mostly `complete` with a steady trickle of `abandoned` (players
quit games). A rising `incomplete` share means chunks are going missing between
ingest and assembly, which is an ingestion problem, not a writer problem.

### Reconciling R2 against D1

D1 and R2 cannot be joined in one query. Reconciliation is two halves, run against a
single bucket listing.

**Missing D1 row despite an R2 object** — the benign direction (see
[Why R2 is written before D1](#why-r2-is-written-before-d1)). Take the `game_id`
values from the bucket listing and ask D1 which of them it does not know:

```bash
npx --yes wrangler@4.112.0 d1 execute retro-backend --remote --config wrangler.production.toml --json --command "SELECT game_id FROM episodes WHERE game_id IN ('game_abc123','game_def456');"
```

Any id you passed in that does not come back has an object and no row.

**Missing R2 object despite a D1 row** — the direction that should be impossible.
List the keys D1 claims exist, then `head` each one by attempting a `get`:

```bash
npx --yes wrangler@4.112.0 d1 execute retro-backend --remote --config wrangler.production.toml --json --command "SELECT parquet_key, manifest_key, parquet_bytes, parquet_sha256 FROM episodes WHERE completed_at_ms > 1760000000000 ORDER BY completed_at_ms DESC LIMIT 50;"
```

Then, per key, the `r2 object get … --pipe` above. A non-zero exit is a dangling
pointer — escalate, do not delete the row until you understand how it got there.

### Backfilling chunks that never became an episode

Assembly only sees chunks that arrive live on the queue, so anything uploaded before
it was deployed sits in `raw/` with no episode. `scripts/backfill-episodes.py` walks
`raw/`, replays assembly for each stranded game in-process, and writes the same
Parquet, manifest and D1 row the consumer writes.

It reads `raw/`, not `validated/`. `validated/` is a seven-day cache that
[`load_episode_rows`](../src/retro_backend/episode_rows.py) treats an absent object as
a silent gap in, so re-deriving history from it would produce short files that look
whole. `raw/` is the copy of record.

`wrangler` has no object-listing command, so the inventory comes from the REST API and
both `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` must be exported. Always look
before you write:

```bash
uv run --frozen python scripts/backfill-episodes.py --game-type all --dry-run
```

Then convert, optionally narrowing by type, upload date, or explicit ids:

```bash
uv run --frozen python scripts/backfill-episodes.py --game-type all
uv run --frozen python scripts/backfill-episodes.py --game-type 2048 --until 2026-08-03
```

The D1 upsert needs `migrations/0003_episodes_index.sql`. If it is not applied yet,
`make migrate-remote` first, or pass `--skip-index` and index later. Re-running is
safe: a game that already has Parquet is skipped, and `--state-file` keeps an
interrupted run from re-reading what it finished.

`--force` is the separate repair mode, for episodes that were converted but recorded
wrongly. It re-derives them and compares each artifact before writing: Parquet whose
bytes have not changed keeps its `parquet_sha256`, and only a manifest whose contents
actually differ is rewritten. The corrected values come from replaying the chunks, so
whatever fixed the classification must be merged before the run means anything.

### Queue backlog

`QUEUE_CHECKS` in
[`src/retro_backend/queue_health.py`](../src/retro_backend/queue_health.py) lists
exactly three queues, and the scheduled handler checks their backlog and oldest
message every five minutes:

| Queue | In `QUEUE_CHECKS`? |
| --- | --- |
| `retro-gameplay-events` | Yes — alerts on a message older than ten minutes. |
| `retro-gameplay-events-dlq` | Yes — same age rule. |
| `retro-gameplay-events-archive-dlq` | Yes — alerts on *any* message. |
| `retro-episode-events` | **No.** |
| `retro-episode-events-dlq` | **No.** |

**Nothing alerts on either episode queue.** They have no entry in `QUEUE_CHECKS`, no
Cloudflare notification, and no consumer, so a backlog there is silent — it will not
appear in `queue_health_failed` logs and will not page anyone. Until that changes,
checking them is a manual operator step:

```bash
npx --yes wrangler@4.112.0 queues info retro-episode-events
```

```bash
npx --yes wrangler@4.112.0 queues info retro-episode-events-dlq
```

Both queues now exist in the production account, so these commands succeed. Expect
zeroes across the board: the Worker has not been redeployed since they were created,
so neither queue has a registered producer or consumer, backlog reads 0, and the
binding-registration columns read 0 as well. That is the correct current state, not a
fault. `wrangler.toml` declares an `EPISODE_QUEUE` producer binding for
`retro-episode-events`; `retro-episode-events-dlq` has no binding in the config at
all yet.

To see every queue in the account:

```bash
npx --yes wrangler@4.112.0 queues list
```

## Troubleshooting

### Episode stuck without a Parquet object

An `episode.ready` message was emitted but no object appeared. Work down the chain,
stopping at the first thing that is not true:

1. **Is anything bound to the queue?** `queues info retro-episode-events`. The queue
   exists, but until the Worker is redeployed it has no registered producer, so
   nothing has ever been *published* to it — the pipeline is dormant end to end.
   That is the current state of production, not a fault.
2. **Does a consumer exist?** Until [#30](https://github.com/adavyas/retro-backend/issues/30)
   is deployed, the queue accumulates without a reader. `queues info` shows the
   consumer list. Backlog with no consumer is expected, not stuck — and because
   neither episode queue is in `QUEUE_CHECKS`, you will only ever learn about that
   backlog by looking.
3. **Is the episode terminal at all?** An episode only emits when it completes or its
   idle TTL expires — `EPISODE_IDLE_TTL_MS` defaults to six hours. An episode whose
   last chunk arrived an hour ago is not stuck, it is waiting.
4. **Did the episode reach a terminal status but fail conversion?** Look for the
   `validated/` artifacts of its chunks. If they exist and no Parquet object does, the
   writer is failing; check Worker logs for the consumer.
5. **Is it actually a D1 gap?** An object with no row looks identical to a missing
   object if you only query D1. Confirm against R2 before concluding anything.

### DLQ growth

`retro-gameplay-events-archive-dlq` is intentionally unconsumed — it is the operator
intervention queue. A message there means a gameplay payload failed processing five
times and then failed archival ten more.

```bash
npx --yes wrangler@4.112.0 queues info retro-gameplay-events-archive-dlq
```

Any non-zero backlog is a failure by definition, and the scheduled check already
reports it. The forensic trail is in R2 under `dlq/date=…/message={sha256}.json` —
allowlisted, token-free archive records with `message_body_sha256` and the reason.
Those expire after 7 days, so triage inside that window or the evidence is gone.

A `retro-episode-events` backlog is a different thing. `retro-episode-events-dlq`
exists in the account but is not referenced by `wrangler.toml` at all — the config
declares only the `EPISODE_QUEUE` producer binding, no consumer and no
`dead_letter_queue` pointing at it. So growth on `retro-episode-events` means "no
consumer yet", not "messages are failing", and a message can never reach the episode
DLQ until a consumer is deployed with that binding.

**Check both by hand.** Unlike the three gameplay queues, neither episode queue is in
`QUEUE_CHECKS`, so no scheduled check and no notification covers them. Add
`queues info retro-episode-events` and `queues info retro-episode-events-dlq` to
whatever routine you already run against the archive DLQ, and treat wiring them into
`QUEUE_CHECKS` as the real fix.

### Checksum mismatch

Three copies of `parquet_sha256` exist. Which two disagree tells you what happened:

| Symptom | Reading |
| --- | --- |
| Downloaded bytes ≠ manifest, manifest = D1 = R2 metadata | Transfer corruption. Re-download before doing anything else. |
| Downloaded bytes = R2 metadata, but ≠ manifest and ≠ D1 | The object was overwritten by different bytes. This should be impossible — `put_derived` refuses a same-key write with a different digest and raises `DerivedObjectConflict`. Escalate. |
| Manifest = R2, D1 disagrees | The D1 row is stale — an older episode revision's digest survived an upsert. The R2 side is authoritative; the row is the projection. |
| Manifest missing entirely, Parquet present | The writer crashed between the two puts. The Parquet object is still valid; the manifest is regenerable from the D1 row plus a re-read. |

The rule of precedence: **R2 object bytes are truth, the manifest describes them, and
D1 indexes them.** Never repair in the other direction.

### Duplicate and retried writes

Retries are the normal case, not an incident. Queue delivery is at-least-once and
`episode.ready` publishing is explicitly at-least-once (a send that succeeds but fails
to record its emission republishes — see
[episode-assembly.md](episode-assembly.md#idempotency)). Three mechanisms make that
safe:

- **Deterministic keys.** The same episode always derives the same Parquet and
  manifest keys, so a retry targets the same object rather than creating a second one.
- **Conditional writes.** `put_derived` heads the key first and compares
  `payload_sha256`; identical bytes are a no-op, different bytes raise
  `DerivedObjectConflict` instead of overwriting.
- **Upsert on `episode_id`.** `ON CONFLICT(episode_id) DO UPDATE` makes the D1 write
  converge rather than error on a replay.

So what you should never see is two objects for one episode, or two rows for one
`game_id` — the `UNIQUE` index on `game_id` enforces the latter. If a unique-constraint
violation shows up on `episodes_game_id_idx`, that is not a retry artifact: it means
two different `episode_id` values (so two different `user_id`s) claimed the same
`game_id`, and the client is reusing game ids across users.

What you *may* legitimately see is a Parquet object whose `written_at_ms` is well
after `completed_at_ms`. That is retry lag, and it is fine.
