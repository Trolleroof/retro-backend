"""Semantic consumer health check for Docker and orchestration probes."""

from __future__ import annotations

import argparse
import time
import urllib.request


def _unlabelled_metric(payload: bytes, name: str) -> float | None:
    prefix = f"{name} ".encode()
    for line in payload.splitlines():
        if line.startswith(prefix):
            try:
                return float(line[len(prefix) :])
            except ValueError:
                return None
    return None


def metrics_are_healthy(payload: bytes, *, now: float, stale_seconds: float) -> bool:
    if stale_seconds <= 0:
        return False
    running = _unlabelled_metric(payload, "retro_consumer_running")
    heartbeat = _unlabelled_metric(
        payload,
        "retro_consumer_last_heartbeat_timestamp_seconds",
    )
    return (
        running == 1.0
        and heartbeat is not None
        and 0 <= now - heartbeat <= stale_seconds
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Check semantic health of the event consumer")
    parser.add_argument("--url", default="http://127.0.0.1:9100/metrics")
    parser.add_argument("--stale-seconds", type=float, default=30.0)
    arguments = parser.parse_args()
    try:
        with urllib.request.urlopen(arguments.url, timeout=3) as response:  # noqa: S310
            payload = response.read()
    except OSError as error:
        raise SystemExit(f"consumer metrics unavailable: {error}") from error
    if not metrics_are_healthy(
        payload,
        now=time.time(),
        stale_seconds=arguments.stale_seconds,
    ):
        raise SystemExit("consumer is stopped or heartbeat is stale")


if __name__ == "__main__":
    main()
