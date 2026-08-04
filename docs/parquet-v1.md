# Episode Parquet dataset — `parquet/v1`

Tracking [retro-backend#30](https://github.com/adavyas/retro-backend/issues/30) (Parquet generator) and
[#31](https://github.com/adavyas/retro-backend/issues/31) (D1 episode index).

One Parquet file per episode, written by the `retro-episode-events` consumer when
[episode assembly](episode-assembly.md) publishes `episode.ready`. Each row is one behavior-cloning
sample: the observation the player saw, the control they gave, and the outcome it produced.

## Where the rows come from

The observation → control → outcome join is **not** implemented in the writer. It is
[`extract_behavior_cloning_samples()`](../src/retro_backend/services/bc_export.py), the port of
`extractBehaviorCloningSamples()` from tui-gamepigeon's `packages/trace-export/src/bc-export.ts`
(see [bc-join-logic-plan.md](bc-join-logic-plan.md)). That port is pinned to the TypeScript
implementation's real output by [`tests/fixtures/bc_export_golden.json`](../tests/fixtures/bc_export_golden.json).

[`episode_rows.py`](../src/retro_backend/episode_rows.py) only reads the validated chunk artifacts
out of R2, hands the envelopes to that function, and flattens each returned sample into one row —
so **the Parquet row count is the golden fixture's row count exactly**, asserted in
`tests/test_episode_rows.py::test_rows_equal_the_bc_export_golden_output_exactly`.

Consequences inherited from the join, not decided here:

- Only `actor == "human"` controls become rows (`actors` defaults to `{"human"}`).
- A control whose outcome reports `progress.moved == false` is dropped as a no-op.
- Events are deduplicated across chunks by `event_id`, and ordered by ICU collation on `game_id`
  then `event_sequence`.
- A control with no resolvable `based_on_observation_id` produces no row.

## Columns

Column order is normative. `optional` columns are true Parquet OPTIONAL leaves — a null is written
as definition level 0 with no value byte, never as `0.0` / `false`.

| # | column | parquet type | null? | source |
|---|---|---|---|---|
| 1 | `schema_version` | INT32 | required | BC sample schema version (currently `1`) |
| 2 | `user_id` | BYTE_ARRAY / UTF8 | required | control event |
| 3 | `game_id` | BYTE_ARRAY / UTF8 | required | control event |
| 4 | `game_type` | BYTE_ARRAY / UTF8 | required | chunk envelope (last chunk wins) |
| 5 | `game_version` | BYTE_ARRAY / UTF8 | required | chunk envelope (last chunk wins) |
| 6 | `ruleset_id` | BYTE_ARRAY / UTF8 | **nullable** | `episode_started.payload.ruleset_id`; null when that event is not in the episode's chunks |
| 7 | `event_sequence` | INT64 | required | control event |
| 8 | `control_event_id` | BYTE_ARRAY / UTF8 | required | control event `event_id` |
| 9 | `observation_id` | BYTE_ARRAY / UTF8 | required | resolved observation |
| 10 | `observation_json` | BYTE_ARRAY / UTF8 | required | canonical JSON of the observation payload minus `observation_id` / `caused_by_event_id` |
| 11 | `action` | BYTE_ARRAY / UTF8 | required | `control_changed.payload.action` |
| 12 | `actor` | BYTE_ARRAY / UTF8 | required | `control_changed.payload.actor` |
| 13 | `reward` | DOUBLE | **nullable** | `outcome_changed.payload.engine_reward`; null when the control has no outcome |
| 14 | `score_delta` | INT64 | **nullable** | `outcome_changed.payload.score_delta` |
| 15 | `terminal` | BOOLEAN | **nullable** | `outcome_changed.payload.terminal` |
| 16 | `moved` | BOOLEAN | **nullable** | `outcome_changed.payload.progress.moved` |

`observation_json` is a nested object flattened to text because Parquet groups are out of scope for
the writer. Canonical JSON is `json.dumps(..., sort_keys=True, separators=(",", ":"))`, so it
round-trips to the same structure the client export produces.

### Not emitted: `client_time_ms`

Issue #30 lists a `client_time_ms` column. `extract_behavior_cloning_samples()` does not return it,
and it is **not** in the `control_changed` *payload* — it is a field of the v2 event envelope
(`event["client_time_ms"]`, see `ChunkEnvelope` in [contracts.py](../src/retro_backend/contracts.py)).
Emitting it would mean either re-deriving the control-event ordering outside the ported join, or
changing `bc_export.py`, whose output is frozen by the golden fixture. It is deliberately omitted
rather than faked; adding it belongs upstream in `bc-export.ts` and then in the port.

## File format

The writer is [`parquet.py`](../src/retro_backend/parquet.py), dependency-free because Pyodide has no
pyarrow and a Python Worker cannot import npm packages.

- PLAIN encoding, `UNCOMPRESSED`, a single row group, one v1 `DATA_PAGE` per column.
- `created_by = "retro-backend"`.
- OPTIONAL columns carry RLE-encoded definition levels (max level 1), length-prefixed as v1 requires.
- An episode that yields no samples still produces a valid schema-only file with zero row groups.

## R2 layout

Partitioned by the UTC date of `EpisodeReady.completed_at_ms`, zero-padded:

```
episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/user={user_id}/{game_id}.parquet
episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/user={user_id}/{game_id}.manifest.json
```

**The `user=` segment is a deliberate departure from the path sketched in #30.**
`game_id` is chosen by the client — `GAME_ID_PATTERN` accepts any `game_<suffix>` — and is
assigned by the server nowhere, so it is unique only within a user. That is the same reason
`raw/` and `validated/` are scoped by `user=`. Without it, two players who both pick `game_1`
and finish on the same UTC day resolve to one key, and the second episode silently overwrites
the first while the first player's `episodes` row still points at it.

For the same reason `episodes_game_id_idx` is **not** a unique index: `episode_id`
(`sha256(user_id:game_id)`) is the primary key and already enforces one row per
`(user, game)`. A unique index on `game_id` alone would turn a second user's identical
`game_id` into a constraint violation that `ON CONFLICT(episode_id)` cannot absorb, failing
the write and retrying the message into a dead-letter queue that has no consumer.

Bucket binding `RAW_BUCKET` (`retro-gameplay-raw`), the same bucket the raw and validated artifacts
use. Note that `cloudflare/r2-lifecycle.json` has **no** expiry rule for `episodes/` — unlike
`validated/`, `rejected/` and `dlq/`, this is the retained dataset.

### Manifest sidecar

```json
{
  "version": 1,
  "episode_id": "…64 hex…",
  "user_id": "usr_…", "game_id": "game_…",
  "game_type": "2048", "game_version": "1", "status": "complete",
  "chunk_count": 3, "row_count": 5,
  "first_event_sequence": 1, "last_event_sequence": 17,
  "completed_at_ms": 1784289600000,
  "parquet_key": "episodes/parquet/v1/…/user=usr_…/game_….parquet",
  "parquet_bytes": 2048,
  "parquet_sha256": "…64 hex…",
  "source_chunk_ids": ["chk_…", "chk_…", "chk_…"],
  "written_at_ms": 1784289600123
}
```

`chunk_count`, `first_event_sequence` and `last_event_sequence` come from the `episode.ready`
message and describe the episode as assembled. `source_chunk_ids` lists only the chunks whose
validated artifact was actually readable, so `len(source_chunk_ids) < chunk_count` is the signal
that rows are missing.

`episode_id` is `sha256(f"{user_id}:{game_id}")` hex — the same value the EpisodeAssembler Durable
Object uses as its object name.

## D1 index

[`migrations/0003_episodes_index.sql`](../migrations/0003_episodes_index.sql) creates `episodes`,
one row per episode, upserted on `episode_id`.

**Write order is R2 first, D1 second.** The index must never name an object that does not exist. A
Queue retry that fails at the D1 step simply rewrites the identical R2 objects and retries the
upsert.

### Idempotency

Assembly publishes `episode.ready` at least once, so the consumer must converge rather than
conflict. Both R2 writes are unconditional overwrites at deterministic keys (`R2Store.put_object`,
not `put_derived`): a republished episode carries a fresh `written_at_ms`, and a chunk artifact may
have expired between attempts, so a conditional exact-bytes write would wedge the message into a
permanent conflict loop. The D1 statement is `ON CONFLICT(episode_id) DO UPDATE`.

A message that can never succeed — one that fails `EpisodeReady` validation, names an unroutable
`game_type`, or whose validated artifacts do not satisfy the join — is logged and acknowledged
rather than retried five times into the dead-letter queue.

## Queue configuration

```toml
[[queues.consumers]]
queue = "retro-episode-events"
max_batch_size = 10
max_batch_timeout = 5
max_retries = 5
dead_letter_queue = "retro-episode-events-dlq"
```

Both `retro-episode-events` and `retro-episode-events-dlq` exist in the account; this consumer block
binds them on the next deploy. `scripts/deployment_preflight.py` fails the deploy if the consumer,
its dead-letter target, or the `EPISODE_DLQ` producer binding goes missing.

`retro-episode-events-dlq` has **no** archival consumer of its own (unlike
`retro-gameplay-events-dlq`), and is not yet in `queue_health.QUEUE_CHECKS` — watch its backlog
manually until one of those lands.
