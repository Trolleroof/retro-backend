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

Configure in the Cloudflare dashboard for the account that owns the Worker,
under Notifications. Wrangler has no command for this. Create three:

- **Worker errors** — on `retro-backend`, alerting on elevated error rate or
  invocation failures.
- **Queue backlog** — on `retro-gameplay-events`, alerting when the backlog or
  message age grows beyond normal steady state.
- **Archive DLQ depth** — alert on *any* message reaching
  `retro-gameplay-events-archive-dlq`.

The third is the important one. `retro-gameplay-events-archive-dlq` is
intentionally left unconsumed: it is the operator intervention queue, and a
message arriving there means a gameplay payload failed processing five times
and then failed archival ten more. Nothing drains it automatically, so without
an alert those messages sit unnoticed until someone happens to look.

## What is already done

- Both D1 migrations (`0001_core`, `0002_auth_sessions`) are applied to the
  production database.
- The Worker is deployed to `api.runretroarcade.com` with its D1, R2, Queue,
  Durable Object, and rate-limiter bindings, plus the queue and DLQ consumers.
- The R2 lifecycle rule is applied.
- `make e2e-remote DEPLOY_CONFIG=wrangler.production.toml` passes against the
  deployment and is now a gate inside the deploy workflow.
