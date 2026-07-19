"""Fail fast when pywrangler would run with an unsupported Node.js major."""

import re
import subprocess
import sys

PINNED_NODE_VERSION = "22.23.1"
SUPPORTED_NODE_MAJOR = 22
NODE_VERSION_PATTERN = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


class NodeVersionError(RuntimeError):
    """Raised when the Node.js executable cannot support this Worker toolchain."""


def validate_node_version(output: str) -> str:
    """Return a normalized Node.js version after enforcing major version 22."""
    version = output.strip()
    match = NODE_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise NodeVersionError(
            f"Could not parse `node --version` output {version!r}; "
            + "expected vMAJOR.MINOR.PATCH."
        )

    major = int(match.group("major"))
    if major != SUPPORTED_NODE_MAJOR:
        raise NodeVersionError(
            f"Node.js 22.x is required (pinned to {PINNED_NODE_VERSION}); "
            + f"found {version}. Only major 22 is supported by pywrangler 1.15; "
            + "Node.js 23+ can fail its Pyodide interpreter probe."
        )
    return version


def detect_node_version() -> str:
    """Read and validate the Node.js executable selected by PATH."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise NodeVersionError(
            "Node.js 22.x is required, but `node` was not found on PATH. "
            + f"Install Node.js {PINNED_NODE_VERSION} (see .node-version)."
        ) from None

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise NodeVersionError(
            f"`node --version` failed with exit code {result.returncode}{suffix}"
        )

    return validate_node_version(result.stdout)


def main() -> int:
    try:
        version = detect_node_version()
    except NodeVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Node.js {version} is supported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
