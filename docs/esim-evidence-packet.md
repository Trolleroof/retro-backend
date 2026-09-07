# The portable evidence packet (#80)

Status: **skeleton**. The packaging, the report shell, the limitation text and the
sanitized/private split are in place and tested; the rows are not. The matrix
itself is retro-backend#81's, and this packet closes only once that report
lands.

A run bundle is evidence for one run. A *packet* is the thing the demo is shown
from: one directory, cut at a known revision, that an unfamiliar reader can open
with no provider key, no network, and no history of this repository.

---

## 1. The plan comes first

`contracts/esim/packets/m5-demo.json` is the frozen matrix — levels, manager
roster, the fixed worker model, rounds, and the models excluded from the roster
with the reason each was excluded. It is authored by hand and validated as
`esim-packet-plan/v1`.

Its `bundles` map is the only field that changes as the matrix runs: a row id
maps to the directory that row's run landed in, relative to `--runs-root`. A row
absent from `bundles` has not been run, and the packet prints it as `planned`
with the exact command that produces it rather than omitting it.

Row ids are derived, not authored: `<caseId>--<slugified manager spec>`, e.g.
`dinner-rush-001--nim-openai-gpt-oss-20b`. They are stable, so the paths and
commands in a skeleton report are the paths and commands in the final one.

## 2. Building the packet

```bash
uv run python -m retro_backend.esim.cli package \
  --plan contracts/esim/packets/m5-demo.json \
  --runs-root . \
  --output-dir dist/m5-demo
```

No model is called and no run is made. The command walks the bundles that exist,
copies the shareable half of each, re-grades every complete row from the copy it
just wrote, and prints the counts:

```
planned=6 completed=0 invalid=0 mismatched=0 redacted=0 incomplete=0 missingBundle=0 notYetRun=6
```

It refuses to land on an existing packet — including one that is only half
built, since a run that died during collection leaves a `sanitized/runs/` tree
and none of the generated files. `--overwrite` replaces a packet, and clears the
run tree with it, so a dropped row cannot outlive the report that referenced it
and a retry never merges into the remains of a failed build.

## 3. What lands

```
dist/m5-demo/
  sanitized/            <- this is what is shared
    TASK-CARD.md          what is frozen, what is compared, exclusions, counts
    REPORT.md             one row per planned run, each linked to its evidence
    LIMITATIONS.md        what these numbers do and do not establish
    bundle-manifest.json  configuration, counts, per-file digests, re-grade commands
    runs/<row-id>/        manifest, states, trajectory, grade, summary, directives
  private/
    source-index.json     where each row came from, and the digests of what stayed
```

`attempts.jsonl` — every raw model reply, including the malformed ones, with the
provider metadata attached to each — is **not** copied into `sanitized/`. It
stays in the source bundle and is indexed by absolute path and SHA-256 in
`private/source-index.json`.

Every shared artifact carries a digest on both sides of the copy. `sourceSha256`
is the run's own file; `sanitizedSha256` is the shared one. They differ only
where a redaction fired, and no redaction is expected — the credential patterns
are defence in depth, and a hit means a secret reached a run record and that
bundle needs looking at, not that the packet did its job. A row where one fired
is held at `redacted` and publishes no numbers until someone has looked at the
source bundle.

## 4. Re-grading offline

Every row carries the command that reproduces its grade with no provider access:

```bash
uv run python -m retro_backend.esim.cli regrade \
  --trajectory runs/<row-id>/trajectory.json
```

Run from the `sanitized/` directory, against files in the packet only.
`regrade` calls no model and steps no simulation — the grader is a pure function
of the recorded trajectory — so what it prints is what `grade.json` says, or the
packet is wrong.

The packet does not take that on trust. Building it re-grades every complete row
**from the sanitized copy** and compares outcome, behavior, termination, ticks,
safety failure and grader version against the recorded grade. A row that no
longer reproduces is reported `invalid` with the reason on the row; it is not
quietly dropped and its numbers are not published as results.

## 5. Row statuses

| Status | Meaning |
| --- | --- |
| `planned` | no bundle recorded; the run has not been made |
| `missing` | a bundle path is recorded but nothing is there |
| `incomplete` | the bundle exists but a required artifact is absent or unreadable |
| `mismatched` | the bundle is intact but is not this row's run |
| `invalid` | the run recorded itself degraded, or its evidence no longer reproduces |
| `redacted` | credential-shaped content was found; the source needs a human before this ships |
| `complete` | every artifact present, identity matches, `runValidity: valid`, re-grade reproduces |

**Only a `complete` row publishes numbers.** Every other row still appears in
both tables with its status, its reason and its artifact links — what is
withheld is the figures, not the failure. A degraded run, a half-copied bundle
and a bundle belonging to another cell all still carry readable scores, and
printing those would publish exactly what the status exists to hold back.

`mismatched` is checked against the run's own manifest: case id, manager model,
worker model, harness shape and round count. A grade reproducing proves the
trajectory and the grade agree; it says nothing about which cell the run belongs
to, so a stale path left in `bundles` would otherwise re-grade cleanly and be
published under the wrong manager.

`invalid`, `incomplete` and `mismatched` are infrastructure, evidence and
bookkeeping states, not statements about a manager model. The counts block keeps
them apart from `completed` for exactly that reason.

## 6. Founder check

Disable provider access (unset the NIM key, or pull the network), then:

1. `cd dist/m5-demo/sanitized`
2. Open `REPORT.md`, pick a row, follow its `grade.json` link.
3. Run that row's re-grade command and confirm the scores match the row.
4. Open `TASK-CARD.md` for the frozen configuration and the roster exclusions,
   and `LIMITATIONS.md` for what the row does not establish.

Nothing in steps 1–4 touches a provider. If it does, that is a defect in this
packet, not a step to work around.

## 7. What still belongs to #81

- Running the matrix and filling `bundles`.
- Flipping the plan's `status` to `frozen` with a `frozenAt` date.
- The one trace-backed comparison and the real usage figures for the Tui/Mercor
  packet (submission status is logged separately, not from this issue).

Cost stays `unknown` per row until an authoritative per-model rate is confirmed;
these runs sit inside the free developer tier, which reports no billed figure,
and a `0` would read as measured-and-free.
