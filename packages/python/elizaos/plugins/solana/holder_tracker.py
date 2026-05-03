"""
holder_tracker.py — Track holder count velocity per token.

Records a snapshot of holder_count each time a token is evaluated.
Computes growth rate (holders/min) over a configurable window.

Usage:
    from elizaos.plugins.solana.holder_tracker import record_snapshot, get_growth_rate

    record_snapshot(mint, holder_count)
    rate = get_growth_rate(mint)  # holders/min; None if insufficient data
"""
from __future__ import annotations

import time
from collections import defaultdict

# mint → list of (timestamp, holder_count) sorted ascending
_snapshots: dict[str, list[tuple[float, int]]] = defaultdict(list)

# Max snapshots to retain per mint (prevents unbounded growth)
_MAX_SNAPSHOTS = 50
# Max age of a snapshot before it's pruned (seconds)
_MAX_AGE_SECS = 3600


def record_snapshot(mint: str, count: int) -> None:
    """Record a holder count snapshot for a mint."""
    now = time.time()
    snaps = _snapshots[mint]
    snaps.append((now, count))

    # Prune old snapshots
    cutoff = now - _MAX_AGE_SECS
    _snapshots[mint] = [s for s in snaps if s[0] >= cutoff][-_MAX_SNAPSHOTS:]


def get_growth_rate(mint: str, window_secs: float = 300.0) -> float | None:
    """Return holders/min growth rate over the last window_secs.

    Returns None if there are fewer than 2 snapshots within the window
    (not enough data to compute a rate).

    A negative rate means holders are leaving.
    """
    now = time.time()
    cutoff = now - window_secs
    snaps = [s for s in _snapshots.get(mint, []) if s[0] >= cutoff]

    if len(snaps) < 2:
        return None

    oldest_ts, oldest_count = snaps[0]
    newest_ts, newest_count = snaps[-1]
    elapsed_secs = newest_ts - oldest_ts
    if elapsed_secs < 1.0:
        return None

    return (newest_count - oldest_count) / (elapsed_secs / 60.0)


def get_snapshots(mint: str) -> list[tuple[float, int]]:
    """Return all stored snapshots for a mint (oldest first)."""
    return list(_snapshots.get(mint, []))


def get_holder_change(mint: str, window_secs: float = 300.0) -> tuple[int, int] | None:
    """Return (oldest_count, newest_count) within window, or None."""
    now = time.time()
    cutoff = now - window_secs
    snaps = [s for s in _snapshots.get(mint, []) if s[0] >= cutoff]
    if len(snaps) < 2:
        return None
    return snaps[0][1], snaps[-1][1]


def purge(mint: str) -> None:
    """Remove all snapshots for a mint (call when token is rejected/bought)."""
    _snapshots.pop(mint, None)
