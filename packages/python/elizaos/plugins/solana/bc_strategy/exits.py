"""BC strategy exit rules.

Returns `(reason: str | None, sell_fraction: float)` per evaluation. The
loop calls `evaluate_exit(state, current_price)` after every Geyser tick
that updates BC progress or price.

Priority order matches the PDF §4:
  1. -40% hard SL (rug)
  2. 98% bonding curve completion (avoid migration dump)
  3. 15% TP + 5% trailing stop (winner take)
"""

from __future__ import annotations

import time

from elizaos.plugins.solana.bc_strategy.config import (
    BC_EXIT_NEAR_GRADUATION_PCT,
    BC_HARD_STOP_LOSS_PCT,
    BC_PRIMARY_TP_PCT,
    BC_TRAILING_ARM_AT_PCT,
    BC_TRAILING_STOP_GIVEBACK_PCT,
)
from elizaos.plugins.solana.bc_strategy.state import BcMintState


def evaluate_exit(s: BcMintState, current_price: float) -> tuple[str | None, float]:
    if s.entry_price is None or s.entry_price <= 0 or current_price <= 0:
        return None, 0.0

    pnl_pct = ((current_price / s.entry_price) - 1.0) * 100.0

    # ── 1. Hard SL ────────────────────────────────────────────────────────
    if pnl_pct <= BC_HARD_STOP_LOSS_PCT:
        return f"bc_hard_sl_{pnl_pct:.0f}pct", 1.0

    # ── 2. Near-graduation exit ──────────────────────────────────────────
    if s.progress_pct >= BC_EXIT_NEAR_GRADUATION_PCT:
        return f"bc_pre_migration_{s.progress_pct:.0f}pct", 1.0

    # ── 3. TP + trailing stop ────────────────────────────────────────────
    # Update peak first so trailing always tracks the highest seen pnl.
    if pnl_pct > s.peak_pnl_pct:
        s.peak_pnl_pct = pnl_pct
        s.peak_pnl_ts = time.time()

    # Arm trailing once pnl crosses BC_TRAILING_ARM_AT_PCT — before that we
    # want the position to ride freely; tight trail too early ejects winners.
    if pnl_pct >= BC_TRAILING_ARM_AT_PCT:
        s.trailing_armed = True

    # If trailing armed AND pnl has given back BC_TRAILING_STOP_GIVEBACK_PCT
    # from peak, exit. Fires both as 'tp hit' (>15% peak) and as 'profit
    # protection' (any peak below 15% but armed).
    if s.trailing_armed:
        giveback = s.peak_pnl_pct - pnl_pct
        if giveback >= BC_TRAILING_STOP_GIVEBACK_PCT:
            tag = (
                "bc_tp_trailed"
                if s.peak_pnl_pct >= BC_PRIMARY_TP_PCT
                else "bc_be_trail"
            )
            return f"{tag}_peak{s.peak_pnl_pct:.0f}_pnl{pnl_pct:.0f}", 1.0

    return None, 0.0
