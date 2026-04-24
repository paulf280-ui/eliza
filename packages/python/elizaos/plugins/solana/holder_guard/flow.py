"""holder_guard.flow — rolling holder-distribution deltas for open positions.

Sits alongside price-based exit logic. Produces two kinds of signals:

  - Hold override: price wobbles but holder count is still growing and top-10
    isn't concentrating → don't exit. This is the AINI fix (exited at +3%
    after +25% wobble; holder flow was still accumulating).

  - Hard exit: top-10 jumps +3% absolute in 5 min, OR holder count drops >5%
    in 10 min. Distribution-phase signatures that precede 40-70% dumps.

Stores top-10 snapshots in memory per-mint (complementing holder_tracker which
stores holder counts). Purge called when position closes.
"""
from __future__ import annotations

import time
from collections import defaultdict

from elizaos.plugins.solana import holder_tracker

from . import config as cfg
from .models import GuardDecision, HolderDelta, HolderSnapshot

# mint → list[(ts, top10_pct)], sorted ascending. Bounded per _MAX_SNAPSHOTS.
_top10_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
_MAX_SNAPSHOTS = 60
_MAX_AGE_SECS = 3600


def record_snapshot(snapshot: HolderSnapshot) -> None:
    """Store the snapshot's top-10 and holder count for later delta computation."""
    if snapshot.top10_pct is not None:
        series = _top10_history[snapshot.mint]
        series.append((snapshot.ts, snapshot.top10_pct))
        cutoff = snapshot.ts - _MAX_AGE_SECS
        _top10_history[snapshot.mint] = [s for s in series if s[0] >= cutoff][-_MAX_SNAPSHOTS:]
    if snapshot.unique_holders is not None:
        holder_tracker.record_snapshot(snapshot.mint, int(snapshot.unique_holders))


def _top10_delta(mint: str, window_secs: float) -> float | None:
    now = time.time()
    cutoff = now - window_secs
    series = [s for s in _top10_history.get(mint, []) if s[0] >= cutoff]
    if len(series) < 2:
        return None
    return round(series[-1][1] - series[0][1], 2)


def compute_deltas(mint: str) -> list[HolderDelta]:
    """Return HolderDelta for each configured window."""
    out: list[HolderDelta] = []
    for w_min in cfg.FLOW_DELTA_WINDOWS_MINS:
        w_secs = w_min * 60
        d = HolderDelta(mint=mint, window_mins=w_min)
        d.top10_delta_abs_pct = _top10_delta(mint, w_secs)
        chg = holder_tracker.get_holder_change(mint, window_secs=w_secs)
        if chg:
            d.holder_count_delta = chg[1] - chg[0]
            d.holder_count_delta_pct = (
                round((chg[1] - chg[0]) / chg[0] * 100, 2) if chg[0] > 0 else None
            )
        out.append(d)
    return out


def evaluate_exit(snapshot: HolderSnapshot) -> GuardDecision:
    """Produce a GuardDecision for an open position based on holder flow."""
    record_snapshot(snapshot)
    d = GuardDecision(mint=snapshot.mint, snapshot=snapshot)
    deltas = {dd.window_mins: dd for dd in compute_deltas(snapshot.mint)}

    # ── Hard exit: top-10 jumps +3% over 5min ───────────────────────────
    d5 = deltas.get(5)
    if d5 and d5.top10_delta_abs_pct is not None and d5.top10_delta_abs_pct >= cfg.TOP_10_JUMP_ABS_PCT_OVER_5MIN:
        d.should_exit = True
        d.exit_reason = f"top10_jump_+{d5.top10_delta_abs_pct:.1f}pp_5min"
        return d

    # ── Hard exit: holder count drops >5% over 10min ────────────────────
    d10 = None
    # Find closest window to 10min (we configured 1/5/15)
    for cand in (deltas.get(15), deltas.get(5)):
        if cand and cand.holder_count_delta_pct is not None:
            d10 = cand
            break
    if d10 and d10.holder_count_delta_pct is not None and d10.holder_count_delta_pct <= -cfg.HOLDER_COUNT_DROP_PCT_10MIN:
        d.should_exit = True
        d.exit_reason = f"holder_drop_{d10.holder_count_delta_pct:.1f}pct_{d10.window_mins}min"
        return d

    # ── Hold override: growth still positive, top-10 not concentrating ──
    growth_pct = snapshot.holder_growth_per_min_pct
    t10_d5 = d5.top10_delta_abs_pct if d5 else None
    if growth_pct is not None and growth_pct >= cfg.HOLDER_GROWTH_HOLD_OVERRIDE_PER_MIN_PCT:
        if t10_d5 is None or t10_d5 <= cfg.TOP_10_DECREASING_HOLD_DELTA_PCT:
            d.hold_override = True
            d.hold_reason = (
                f"accumulating: growth=+{growth_pct:.2f}%/min, "
                f"top10Δ5min={t10_d5 if t10_d5 is not None else 'n/a'}pp"
            )

    return d


def purge(mint: str) -> None:
    """Clear snapshots for a mint (call on position close or rejection)."""
    _top10_history.pop(mint, None)
    holder_tracker.purge(mint)
