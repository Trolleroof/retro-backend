# Production Setup

Two one-time setup tasks stand between this repository and hands-off releases.
Neither can be done from a workstation checkout, and neither is a code change:

1. The GitHub `production` environment does not exist, so `Deploy production`
   has no secrets to read. **Needs admin on `adavyas/retro-backend`.**
2. Cloudflare notifications are not configured. **Needs access to the
   Cloudflare account that owns `api.runretroarcade.com`.**

Until both are done, releases must be cut by hand with the commands in the
README's Production Deployment section.

## 1. The GitHub `production` environment

`adavyas/retro-backend` is a user-owned private repository, so only `adavyas`
can do this. Collaborator roles top out at Write, and creating an environment
requires Admin. Check who has it under Settings → Collaborators.

Settings → Environments → New environment → name it exactly `production`.

Then configure:

- **Required reviewers** — add at least one. This is the approval gate that
  makes a dispatched release pause for a human.
- **Deployment branches** — restrict to `main`. The workflow already refuses to
  run off any other ref, but this enforces it on GitHub's side too.

### Secrets (Settings → Environments → production → Environment secrets)

| Name | Value |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | The account that owns `api.runretroarcade.com`. `npx wrangler@4.112.0 whoami` lists it. |
| `CLOUDFLARE_API_TOKEN` | Create at Cloudflare → My Profile → API Tokens. Permissions below. |
| `CLOUDFLARE_D1_DATABASE_ID` | The `retro-backend` database UUID. `npx wrangler@4.112.0 d1 list` shows it. |

The API token needs permission to:

- deploy Workers and attach custom domains
- read and write D1 (apply migrations, and let the post-deploy smoke assert and
  clean up its own rows)
- manage Queue consumers
- read and write R2, including the bucket lifecycle configuration

### Variables (Settings → Environments → production → Environment variables)

| Name | Value |
| --- | --- |
| `PUBLIC_GAME_URL` | The canonical public client base, e.g. `https://runretroarcade.com` |
| `ALLOWED_GAME_ORIGINS` | Comma-separated canonical origins, each exactly `scheme://host[:port]` with no path |

> These two must be **Variables, not Secrets.** The workflow reads them as
> `vars.PUBLIC_GAME_URL` / `vars.ALLOWED_GAME_ORIGINS`. Entered as secrets they
> resolve to empty strings and the run fails at `make prepare-deployment-config`
> with `PUBLIC_GAME_URL is required`. That failure is loud and harmless, but the
> cause is not obvious from the message.

Values must be real canonical URLs on domains the operator controls. The
preflight rejects the committed `gamepigeon.example` placeholders, the
`<angle-bracket>` replacement tokens, and every hostname under `.example`,
`example.com`, `example.net`, and `example.org`.

### Why the ids are not written down here

The repository deliberately keeps the account id and D1 UUID out of committed
configuration — `wrangler.toml` ships an all-zero D1 id specifically so nobody
can deploy into an implied account. Filling those values into a committed
document would undo that. Read them from the account with the commands above.

### Confirming it worked

```bash
gh api repos/adavyas/retro-backend/environments --jq '.total_count'
```

`0` means the environment still does not exist. Once it returns `1`, dispatch
`Deploy production` from `main` and approve it when it pauses for review.

There is no separate "is it configured" check to run, because the workflow is
already its own check: a missing secret or variable fails at the render step,
before anything touches Cloudflare.

## 2. Cloudflare notifications

The Worker now checks all three queue metrics every five minutes through its
scheduled handler. Configure one Workers Observability notification for
`retro-backend` scheduled-invocation failures, then deploy the Worker. Wrangler
has no command for creating the notification policy.

The health check treats these as failures:

- any message in `retro-gameplay-events-archive-dlq`;
- a message older than ten minutes in the main queue or first DLQ.

The archive queue is intentionally left unconsumed: it is the operator
intervention queue, and a message arriving there means a gameplay payload
failed processing five times and then failed archival ten more. The scheduled
check makes that state visible without a separate API-token poller.

For the existing account, configure the notification in the Cloudflare
dashboard under Notifications. Create one policy:

- **Worker errors** — on `retro-backend`, alerting on elevated error rate or
  invocation failures.

## 3. Raw chunk staging in R2

Every accepted `/v1/game-event-chunks` chunk is written to the `RAW_BUCKET`
bucket (`retro-gameplay-raw`) as the exact bytes the client sent, before the
pointer is published to `retro-gameplay-events`. The key is deterministic:

```
raw/user={user_id}/game={game_id}/chunk={chunk_id}.json
```

`chunk_sequence` is deliberately not in the key. `chunk_id` alone identifies the
chunk, so a re-upload lands on the same key and the conditional write
(`onlyIf: {etagDoesNotMatch: "*"}`) plus the stored `payload_sha256` make the
retry a no-op instead of a second object. A same-key upload with *different*
bytes is rejected as a 409 rather than overwriting the audit trail.

Each object carries `Content-Type: application/json` and this custom metadata:

| key | value |
| --- | --- |
| `payload_sha256` | SHA-256 of the exact stored bytes |
| `schema_version` | `2` |
| `user_id`, `game_id`, `chunk_id` | envelope identity |
| `game_type` | envelope `game_type` |
| `event_count` | envelope `event_count`, as a decimal string |

Custom metadata travels as HTTP header values, so `game_type` is restricted to
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` at envelope validation — tighter than the
published v2 schema's `minLength: 1`. A chunk that violates it gets a 400, not
a failed write.

### Lifecycle

`cloudflare/r2-lifecycle.json` is the source of truth and is applied with
`make apply-r2-lifecycle`; `scripts/deployment_preflight.py` fails the deploy if
the live bucket configuration drifts from it. The current policy is:

- **30 days → `STANDARD_IA`.** Raw chunks are read constantly for the first few
  days (episode assembly, re-export after a BC join change) and rarely after
  that, so infrequent-access storage is the right tier for the long tail.

There is **no expiration rule**: raw chunks are retained indefinitely today.
Retention is a product/compliance decision, not a technical one — adding a
delete rule (the 90-day option floated in #28) means losing the ability to
re-export older gameplay if the join logic changes, so it stays open until
someone decides. When it is decided, edit `cloudflare/r2-lifecycle.json` and the
`EXPECTED_LIFECYCLE` constant in `scripts/deployment_preflight.py` together, or
the preflight will fail the next deploy.

## What is already done

- Both D1 migrations (`0001_core`, `0002_auth_sessions`) are applied to the
  production database.
- The Worker is deployed to `api.runretroarcade.com` with its D1, R2, Queue,
  Durable Object, and rate-limiter bindings, plus the queue and DLQ consumers.
- The R2 lifecycle rule is applied.
- `make e2e-remote DEPLOY_CONFIG=wrangler.production.toml` passes against the
  deployment and is now a gate inside the deploy workflow.
