# BC join logic port — status

Tracking [retro-backend#33](https://github.com/adavyas/retro-backend/issues/33): port the observation→control→outcome
join logic from [tui-gamepigeon's `bc-export.ts`](https://github.com/Trolleroof/tui-gamepigeon/blob/main/src/data/bc-export.ts)
so the Parquet generator here has the same semantics as `extractBehaviorCloningSamples()`.

## Blocked on

[tui-gamepigeon#167](https://github.com/Trolleroof/tui-gamepigeon/issues/167) — extracting `@tui-games/trace-export`
out of `bc-export.ts` on the tui-gamepigeon side. Until that lands, there's no stable source to port/copy from, and
tui-gamepigeon#167 itself is waiting on a decision from this repo's issue.

retro-backend's Worker is Python (see
[docs/superpowers/specs/2026-07-17-cloudflare-python-backend-design.md](superpowers/specs/2026-07-17-cloudflare-python-backend-design.md)),
so the "shared npm package" option isn't directly consumable here — most likely we copy the join logic in Python with a
golden/contract test against `bc-export.test.ts` fixtures (issue #33's option 2), rather than importing
`@tui-games/trace-export` at runtime. Final call TBD once #167 lands and the extracted module's shape is known.

## Next steps (once #167 is done)

- [ ] Pull the golden fixtures from `bc-export.test.ts` / the extracted `packages/trace-export`
- [ ] Port `extractBehaviorCloningSamples()` join logic into the Python Parquet generator
- [ ] Add a contract test asserting identical sample rows against the same fixtures
- [ ] Preserve `include_noops` and `actors` flag parity
- [ ] Document the chosen option (copy vs. call-as-library) on retro-backend#33
