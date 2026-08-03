# Episode assembly

Tracking [retro-backend#29](https://github.com/adavyas/retro-backend/issues/29): group validated gameplay chunks by
`game_id` until the episode is terminal, then emit one `episode.ready` message for the Parquet writer
([#30](https://github.com/adavyas/retro-backend/issues/30)) to consume.

## Status: implemented, dormant until bound

The code is merged and inert. `GameplayConsumer` only assembles episodes when the `EPISODE_ASSEMBLER` Durable Object
binding exists; on a deployment without it, chunk validation behaves exactly as it did before. Activating the feature is
the infrastructure checklist at the bottom of this file.

## Why a Durable Object

Two chunks of the same episode can land in different Queue batches at the same time, so "am I the chunk that completed
this episode?" needs serialized state — D1 gives no cheap compare-and-set for that, and a lost race would emit
`episode.ready` twice or not at all. Durable Object alarms also give the abandoned/incomplete flush for free, the same
way [`ChunkGate`](../src/retro_backend/chunk_gate.py) already expires its dedup receipts.

The D1 episode index ([#31](https://github.com/adavyas/retro-backend/issues/31)) is the queryable projection of this
state for operators, not a replacement for it.

One object per logical `(user_id, game_id)`, addressed by `sha256(f"{user_id}:{game_id}")`.

## Flow

1. `GameplayConsumer` validates a chunk and writes its `validated/...` artifact to R2, exactly as before.
2. Only after that artifact is durable, it calls `EpisodeAssembler.record()` with the chunk's identity, its
   `chunk_sequence`, its event range, and whether it carried an `episode_ended` event.
3. The Durable Object folds the chunk into its episode and decides whether the episode is terminal.
4. On a terminal episode the object publishes one `episode.ready` message to `retro-episode-events`, then records the
   emission. Publishing before recording means a failed send is retried rather than silently swallowed.

Assembly runs last in the consumer so a Queue retry always replays a chunk whose artifact already exists; the Durable
Object drops the duplicate record by `chunk_id`.

## Terminal statuses

Every episode emits exactly one message. `status` tells the Parquet writer what it is looking at:

| Status | Meaning |
| --- | --- |
| `complete` | Chunk sequences form an unbroken `1..N` chain, event sequences chain across them, and the last chunk carries `episode_ended`. |
| `incomplete` | The idle TTL expired and some chunk carried `episode_ended`, but the chain has gaps — chunks are missing. |
| `abandoned` | The idle TTL expired and `episode_ended` never arrived — the player quit mid-game. |

An episode that reaches `EPISODE_MAX_CHUNKS` without completing is flushed immediately under the same classification,
which bounds both Durable Object storage and the emitted message size.

### Idempotency

`record()` is safe to replay: a redelivered `chunk_id` is a no-op, and any chunk arriving after emission is dropped.
Publishing is at-least-once — a send that succeeds but fails to record its emission republishes on retry — so the
`episode.ready` consumer must be idempotent. Deterministic derived R2 keys already give #30 that property.

## Message shape

```json
{
  "version": 1,
  "status": "complete",
  "user_id": "usr_...",
  "game_id": "game_...",
  "game_type": "tetris",
  "game_version": "1",
  "chunk_count": 2,
  "first_event_sequence": 1,
  "last_event_sequence": 7,
  "chunks": [
    {"chunk_id": "chk_...", "chunk_sequence": 1, "created_at_ms": 1784239200000},
    {"chunk_id": "chk_...", "chunk_sequence": 2, "created_at_ms": 1784239201000}
  ],
  "completed_at_ms": 1784239202000
}
```

`chunks` carries identity and ordering only. Both R2 keys are derivable from those fields, which keeps a 512-chunk
episode comfortably inside the 120 KiB Queue size guard:

- raw: `R2Store.raw_key(user_id=..., game_id=..., chunk_id=...)`
- validated: `validated/date={UTC date of created_at_ms}/user={user_id}/game={game_id}/chunk={chunk_id}.json`

## Activation checklist

None of this is applied yet. Deploying the current code without it is safe — assembly simply stays off.

1. Create the queue and its dead-letter queue:

```bash
npx wrangler queues create retro-episode-events
```

```bash
npx wrangler queues create retro-episode-events-dlq
```

2. Add to `wrangler.toml`:

```toml
[[queues.producers]]
binding = "EPISODE_QUEUE"
queue = "retro-episode-events"

[[durable_objects.bindings]]
name = "EPISODE_ASSEMBLER"
class_name = "EpisodeAssembler"

[exports.EpisodeAssembler]
type = "durable-object"
storage = "sqlite"
```

3. Optionally override the defaults under `[vars]`:

| Var | Default | Meaning |
| --- | --- | --- |
| `EPISODE_IDLE_TTL_MS` | `21600000` (6h) | Idle time after the last chunk before an episode is flushed. |
| `EPISODE_MAX_CHUNKS` | `512` | Hard cap on chunks per episode. |

4. Deploy per [production-setup.md](production-setup.md), then add the `retro-episode-events` consumer with
   [#30](https://github.com/adavyas/retro-backend/issues/30). Until that consumer exists the queue accumulates
   messages — treat its backlog the same way as `retro-gameplay-events-archive-dlq` and watch it.
