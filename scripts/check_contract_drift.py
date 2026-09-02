"""Fail loudly when this repo's contract files diverge from the client's copies.

``contracts/*.schema.json`` in this repo and in ``Trolleroof/tui-gamepigeon``
must be byte-identical: both sides parse the same wire format, and a schema
that's stricter on only one side lets invalid payloads slip past client-side
validation and 400 once they reach the backend (see issue #15).

The ``restaurant-arena/v1`` schemas are the exception: they are authored here
for tui-gamepigeon to back-port (issue #54), so until that lands they are
reported as pending rather than failing the run.

There is no CI checkout of the client repo here, so this only runs when a
local checkout is available. Point it at one explicitly:

    uv run python scripts/check_contract_drift.py --peer-repo ../tui-gamepigeon

or set ``TUI_GAMEPIGEON_REPO`` in the environment. Run it after changing any
file in ``contracts/`` and whenever pulling upstream client changes.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FILES = (
    "contracts/ingest-response-v1.schema.json",
    "contracts/game-event-chunk-v1.schema.json",
    "contracts/interaction-trace-chunk-v2.schema.json",
)
# Authored in this repo first; tui-gamepigeon has no copy yet. A file moves into
# CONTRACT_FILES once the back-port lands, at which point drift becomes an error.
PENDING_BACKPORT_FILES = (
    "contracts/restaurant-arena-state-v1.schema.json",
    "contracts/restaurant-arena-observation-v1.schema.json",
    "contracts/restaurant-arena-observation-v2.schema.json",
    "contracts/restaurant-arena-action-v1.schema.json",
    "contracts/restaurant-arena-trace-v1.schema.json",
)


def find_pending_backports(peer_repo: Path) -> list[str]:
    """Return a line per schema this repo owns that the client has not adopted."""
    pending = []
    for relative_path in PENDING_BACKPORT_FILES:
        ours = ROOT / relative_path
        theirs = peer_repo / relative_path
        if not theirs.is_file():
            pending.append(f"{relative_path}: not yet back-ported to {peer_repo}")
        elif ours.read_bytes() != theirs.read_bytes():
            pending.append(f"{relative_path}: back-ported copy differs from ours")
    return pending


def find_drift(peer_repo: Path) -> list[str]:
    """Return a human-readable line per contract file that differs or is missing."""
    problems = []
    for relative_path in CONTRACT_FILES:
        ours = ROOT / relative_path
        theirs = peer_repo / relative_path
        if not theirs.is_file():
            problems.append(f"{relative_path}: missing in peer repo ({theirs})")
            continue
        if ours.read_bytes() != theirs.read_bytes():
            problems.append(f"{relative_path}: differs from {theirs}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--peer-repo",
        type=Path,
        default=Path(os.environ.get("TUI_GAMEPIGEON_REPO", "")) or None,
        help="Path to a local tui-gamepigeon checkout (or set TUI_GAMEPIGEON_REPO).",
    )
    args = parser.parse_args()

    if args.peer_repo is None:
        print(
            "No peer repo configured; pass --peer-repo or set TUI_GAMEPIGEON_REPO.",
            file=sys.stderr,
        )
        return 2

    peer_repo = args.peer_repo.resolve()

    pending = find_pending_backports(peer_repo)
    if pending:
        print("Schemas awaiting back-port to tui-gamepigeon (not a failure):")
        for item in pending:
            print(f"  - {item}")

    problems = find_drift(peer_repo)
    if problems:
        print("Contract drift detected between this repo and tui-gamepigeon:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("Contract files match tui-gamepigeon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
