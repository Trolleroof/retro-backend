# Retro Multiplayer Backend Port Design

**Date:** 2026-07-20
**Status:** Approved by user direction for implementation
**Scope:** Python Cloudflare backend only

## Summary

Move the authoritative invite-only two-board 2048 service into `retro-backend`,
the repository that owns gameplay APIs and Cloudflare state. Preserve the
externally observable 2048 behavior implemented in `tui-gamepigeon` PR #59, but
implement it as Python Worker code alongside the existing FastAPI, D1, R2,
Queue, and `ChunkGate` runtime.

This branch does not modify `tui-gamepigeon`. A later, separate PR will remove
the misplaced multiplayer runtime from its auth Worker after the Python port is
reviewed. This branch also does not deploy a Worker, publish a release, mutate
remote Cloudflare resources, or run credentialed production smoke tests.

## Ownership Boundary

`retro-backend` owns:

- Authenticated room HTTP routes.
- Invite metadata and room lookup.
- Short-lived, one-use WebSocket tickets.
- Hibernating WebSocket upgrades and authoritative broadcasts.
- Per-room SQLite state, action ordering, reconnects, and expiration.
- Server-authoritative two-board 2048 rules.

`tui-gamepigeon` will continue to own client rendering, local game engines,
invitation-link handling, and calls to `GAMEPIGEON_BACKEND_URL`.

`tui-gamepigeon/cloudflare/auth-worker` remains account auth only. It is not a
gameplay coordinator or durable gameplay store.

## Goals

- Preserve the exact two-board 2048 room and gameplay behavior accepted for
  PR #59.
- Keep all Worker runtime and domain logic in Python.
- Add one `GameRoom` Durable Object per room without changing `ChunkGate`.
- Reuse Retro's opaque bearer-token authentication for every room HTTP route.
- Use hibernating WebSockets so idle rooms do not remain resident in memory.
- Persist all authoritative state before broadcasting it.
- Restore exact room and 2048 RNG state after eviction or reconnect.
- Keep pure game rules independently testable without a Cloudflare account.
- Keep the existing ingestion, score, Queue, R2, and auth contracts unchanged.

## Non-Goals

- No client, Tauri, overlay, or TUI changes.
- No `tui-gamepigeon` revert in this branch.
- No Better Auth integration inside Retro.
- No matchmaking, public lobby browser, chat, rematches, or anonymous players.
- No PostHog, CloudEvents, analytics, GTM, billing, or gameplay export changes.
- No live deployment, release, remote migration, or production secret changes.
- No compatibility migration for the undeployed multiplayer Durable Object.
- No Connect Four descriptor, command, state, adapter, route, or test surface.

## Chosen Approach

Implement multiplayer inside the existing Python Worker with a separate
`GAME_ROOMS` binding and exported `GameRoom` class. FastAPI owns authenticated
JSON routes. The top-level Worker fetch handler intercepts only the room socket
upgrade because the ASGI bridge is not the WebSocket transport. Both paths
route to the same room Durable Object.

Two alternatives are rejected:

1. Keeping the TypeScript room Worker beside account auth preserves the repo
   ownership error and violates the Python backend decision.
2. Deploying a second standalone multiplayer Worker introduces another public
   service, duplicated auth, and avoidable operational coupling.

## Public HTTP Contract

The API retains these routes:

```text
POST /v1/rooms
GET  /v1/rooms/{room_id}
POST /v1/rooms/{room_id}/join
POST /v1/rooms/{room_id}/tickets
GET  /v1/rooms/{room_id}/socket?ticket={opaque_ticket}
```

The first four routes require Retro's existing
`Authorization: Bearer <opaque-token>` contract. The socket upgrade does not
accept a bearer token; it consumes a previously issued room-scoped ticket.

Creation accepts exactly one supported descriptor:

```json
{"game_type":"2048","mode":"1v1"}
```

Unsupported pairs, extra keys, malformed UUIDs, neighboring paths, oversized
bodies, and methods outside the route contract fail before a Durable Object is
allocated when possible.

Creation returns the room ID, configured invite URL, expiration timestamp, and
authoritative snapshot. Join is idempotent for a seated user. Ticket issuance
returns a random opaque token and expiration timestamp. Tickets are short-lived,
room-scoped, user-scoped, hash-only in storage, and consumable once.

Every failure uses `{"error":{"code":"...","message":"..."}}`. The preserved
status mapping is `invalid_request` to 400, ticket errors to 401, `not_joined`
to 403, `room_not_found` to 404, `room_full` to 409, `room_expired` to 410, and
`service_unavailable` to 503. Unsupported methods return 405, and the socket
route without a valid WebSocket upgrade returns 426.

## Runtime Flow

1. FastAPI authenticates an HTTP request with the existing Retro auth cache and
   D1 fallback.
2. The route validates exact request shape and canonical room UUID.
3. Room creation generates a UUID and resolves `GAME_ROOMS.getByName(room_id)`.
4. Other room routes address the same object by canonical UUID.
5. A room RPC performs the state transition and returns a JSON-safe result.
6. The socket fetch path validates the upgrade and query bounds, then forwards
   the request to that room object's fetch handler.
7. The room consumes the ticket, accepts the server socket through the
   Hibernation API, serializes a minimal user/seat attachment, and sends a full
   snapshot.
8. Every accepted command is committed to room SQLite before snapshots are
   broadcast.

## Python Module Boundaries

```text
src/retro_backend/
  api/rooms.py                     # Authenticated room HTTP routes
  multiplayer/
    __init__.py
    errors.py                      # Stable public error codes/envelopes
    protocol.py                    # Strict HTTP, frame, snapshot contracts
    tickets.py                     # Opaque token generation and hashing
    bindings.py                    # Room-specific Cloudflare structural types
    client.py                      # FastAPI-to-room RPC adapter
    room.py                        # GameRoom Durable Object and socket lifecycle
    storage.py                     # Room SQLite schema and atomic transitions
    games/
      __init__.py
      game_2048.py                 # Independent two-board game
      rng.py                       # Serializable deterministic 32-bit RNG
```

Existing files change only at integration points:

- `app.py` registers the room router and stable exception mapping.
- `entry.py` intercepts the room socket upgrade and exports `GameRoom`.
- `worker_websocket.py` classifies socket upgrades before ASGI body rebuilding.
- `worker_http.py` enforces the room-creation body cap.
- `wrangler.toml` and deployment preflight gain the new binding/export.
- `README.md` documents local contracts and explicitly states no deployment was
  performed by this work.

## Durable Object Storage

Each room owns one private SQLite database with these logical tables:

- `room`: descriptor, status, creator, timestamps, global version, and canonical
  game-state JSON.
- `participants`: user ID, seat, ready state, join timestamp, and last disconnect
  timestamp.
- `tickets`: SHA-256 token digest, user ID, issue/expiry times, and consumption
  timestamp.
- `actions`: actor, seat, typed action JSON, accepted timestamp, and unique
  resulting global version.

Schema creation is idempotent. Related state writes occur without external I/O
between them. A failed, stale, unauthorized, malformed, or no-op command writes
nothing. Important state never exists only in Python instance memory.

The room alarm expires waiting and active rooms after their configured timeout,
retains completed rooms for the configured read window, closes attached sockets,
and deletes private storage. Alarm behavior is idempotent.

## Shared Room Rules

- Room status is `waiting`, `active`, `completed`, or `expired`.
- The creator receives seat zero.
- Every room has exactly two seats.
- Rejoining users retain their seat; excess distinct users receive `room_full`.
- Every participant must explicitly ready before activation.
- Readiness uses optimistic global `expected_version`.
- Global version starts at zero and increments only for first-time ready,
  accepted legal game actions, and expiration. Joins, tickets, presence,
  repeated ready, errors, and 2048 no-ops do not increment it.
- Waiting rooms expire 30 minutes after creation. Activation resets expiry to
  30 minutes after the final ready. Completion resets retention to 24 hours.
- Join, ticket, upgrade, and command paths reject at `now >= expires_at`. A GET
  remains readable until the idempotent expiration alarm processes the room.
- One user may have only one active socket; a replacement closes the older one.
- Disconnect changes presence only and never rolls gameplay state backward.
- Reconnect snapshots come exclusively from persisted state.
- Full snapshots are sent on connection and after accepted mutations.
- Client frames are text JSON with exact keys and a strict byte limit.
- Binary, malformed, oversized, or unauthorized frames receive stable errors.
- Public errors never include tokens, token hashes, RNG state, SQL, or stack data.

## 2048 Rules

- Descriptor: `2048` / `1v1` with exactly two seats and two boards.
- Boards are independent and visible to both participants.
- Both players may move concurrently; there are no turns.
- Each board has independent serializable RNG state and board version.
- Activation spawns two tiles on each board from server-generated seeds.
- A move carries `direction` and `expected_board_version`.
- A legal move changes only the actor's board, consumes only that board's RNG,
  increments its board version, increments the global room version, appends one
  action, persists, and broadcasts both boards.
- A direction that changes nothing is a successful no-op. It sends the unchanged
  snapshot only to the actor and changes no version, RNG, score, history, or
  expiration.
- A move on an already finished board is the same no-op while the other board is
  active. There is no `illegal_move` or `board_finished` error.
- A stale board command returns `stale_board_version` with the current board and
  room versions.
- A board finishes only when no legal move remains. Tile 2048 does not end play.
- The room completes after both boards finish. Higher merge score wins; equal
  scores draw. Highest tile is display-only and never breaks a tie.
- Accepted action payloads contain exactly `direction`, `score_delta`, `spawned`,
  and `resulting_board_version`.
- Mulberry32 compatibility is pinned by this vector: state `123` and board
  `[[2,2,0,0],[4,0,4,0],[0,0,0,0],[0,0,0,0]]` moved left yields score delta
  `12`, spawn `{row:3,column:1,value:2}`, and resulting RNG state `3663131749`.
- Persisted state validation rejects malformed dimensions, non-power-of-two
  tiles, unsafe values, duplicate seats, inconsistent highest tiles, invalid
  versions/RNG state, unreachable totals, and inconsistent completion results.

## WebSocket Hibernation

`GameRoom.fetch()` creates a `WebSocketPair`, accepts the server side with
`ctx.acceptWebSocket`, and serializes a small attachment containing the seated
identity. The constructor rebuilds transient connection indexes from
`ctx.getWebSockets()` and each socket's attachment. `webSocketMessage`,
`webSocketClose`, and `webSocketError` never trust module-level mutable state.

Presence is derived from currently attached healthy sockets and persisted seat
membership. Socket attachments are not the source of truth for room or game
state.

Accepted client frames are exactly `ready` and `move`. A 2048 move may
include `request_id` matching `[A-Za-z0-9_-]{1,64}`. It is correlation only, not
idempotency: legal moves echo it to every recipient, while errors and no-ops echo
it only to the actor. Stale-board checking happens before no-op handling.

## Error And Security Contract

- Authentication occurs before room allocation for bearer-protected routes.
- Ticket plaintext is returned once and never logged or persisted.
- Ticket comparison uses SHA-256 digests and constant-shape lookups.
- Room IDs are cryptographically random canonical UUIDs.
- Browser origins are exact allowlist values when an Origin header is present.
- Origin rejection happens before authentication, room allocation, or ticket
  consumption. Originless native clients are allowed and browser responses vary
  on `Origin`.
- HTTP bodies, query strings, tickets, and WebSocket frames have hard caps.
- The preserved caps are 4 KiB for create bodies and frames, 1024 decoded UTF-8
  bytes for pathnames and socket query strings, and 64 characters for request
  IDs. The third invalid frame closes with `1008 "policy violation"`; an
  oversized frame closes with `1009 "message too large"`.
- Replacement sockets close with `4001 "replaced"`. Expiration sends one final
  expired snapshot, closes sockets with `4000 "room expired"`, and then removes
  retained room storage.
- Clients cannot choose a seat, target board, seed, or RNG state.
- Corrupt persisted state fails closed with a generic service error.
- Requests and logs contain correlation IDs but no credentials or raw frames.

## Configuration

Add `PUBLIC_GAME_URL` and `ALLOWED_GAME_ORIGINS` as non-secret variables. Preserve
the accepted timeouts and limits as backend constants: 30-minute waiting/active
TTL, 24-hour completed retention, 60-second tickets, and the request/frame caps
defined above. Add `GAME_ROOMS` as a SQLite Durable Object binding and export
`GameRoom` beside `ChunkGate`. Production configuration rendering and preflight
must require the same binding and public variables, but this branch will run only
local verification and Wrangler dry-runs.

## Testing Strategy

Pure Python tests cover protocol parsing, ticket hashing, deterministic RNG
restoration, independent 2048 boards, no-op conservation, board/global
concurrency, completion, and corrupt state rejection.

Service tests use focused in-memory repositories and fake binding adapters to
cover create, join, ready, ticket, action, expiration, and failure ordering.

Worker tests cover FastAPI auth ordering, exact routes, origin policy, UUID and
body bounds, binding failures, JSON envelopes, socket upgrade routing, hibernated
attachment restoration, replacement sockets, reconnects, and alarms. No test
requires a Cloudflare account.

Verification includes the complete existing Python suite, Ruff, BasedPyright,
entry/deployment contract tests, pywrangler sync/types, and Wrangler dry-run.
Credentialed smoke, remote migrations, deploy, and release commands are excluded.

## Delivery Sequence

1. Add the strict 2048-only protocol and pure game reducer through red-green TDD.
2. Add room storage and lifecycle service through red-green TDD.
3. Add the Python Durable Object and WebSocket boundary through red-green TDD.
4. Add authenticated FastAPI routes and fetch interception through red-green TDD.
5. Add binding/config/preflight contracts, docs, and local smoke coverage.
6. Review the entire diff, run full verification, fetch `origin/main`, rebase the
   feature branch, and rerun verification.

## Success Criteria

- Existing Retro ingestion and score behavior remains green.
- Authenticated users can create, join, inspect, ticket, and reconnect to rooms.
- 2048 preserves two independent visible boards and actor-only no-op semantics.
- Every accepted mutation is durable before broadcast and survives reconstruction.
- All Python, contract, type, lint, and dry-run checks pass.
- The branch is rebased on the latest `retro-backend` `origin/main`.
- No Worker is deployed, no release is published, no remote migration runs, and
  `tui-gamepigeon` is not reverted by this work.
