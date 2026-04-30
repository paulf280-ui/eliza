"""Entry gate evaluators for the BC strategy.

Each `evaluate_gate_X(state)` returns a tuple `(passed: bool, reason: str)`.
The loop calls all three on every Geyser-driven tick; entry fires only when
all three return True for the same mint within the same tick.

Implementations are STUBS at scaffold time — they return `(False, reason)`
explaining what's missing. When implementing, replace the stub body with
real logic and remove the NotImplementedError gate inside `assert_ready`.
"""

from __future__ import annotations

import time

from elizaos.plugins.solana.bc_strategy.config import (
    GATE_A_EXCLUDE_DEV_BUYS,
    GATE_A_MAX_UNIQUE_WALLETS,
    GATE_A_MIN_TOTAL_SOL_INFLOW,
    GATE_A_MIN_UNIQUE_WALLETS,
    GATE_A_WINDOW_SECS,
    GATE_B_BUY_PRESSURE_WINDOW_SECS,
    GATE_B_MAX_BC_PROGRESS_PCT,
    GATE_B_MIN_BC_PROGRESS_PCT,
    GATE_B_MIN_BUY_RATIO_PCT,
    GATE_C_BLOCK_0_BUNDLE_VETO,
    GATE_C_MAX_DEV_HOLDING_PCT,
    GATE_C_MIN_CREATOR_GRAD_RATE,
    GATE_C_REQUIRE_CREATOR_HISTORY,
)
from elizaos.plugins.solana.bc_strategy.state import BcMintState


def evaluate_gate_a(s: BcMintState) -> tuple[bool, str]:
    """Liquidity Path Velocity — diverse organic interest, not bot-driven."""
    if s.launch_ts == 0.0:
        return False, "gate_a: launch_ts unknown — can't measure velocity window"
    elapsed = time.time() - s.launch_ts
    if elapsed > GATE_A_WINDOW_SECS:
        # Past the observation window. Gate A is launch-time only; later
        # entries take Gate B's path. Treat as not-applicable rather than fail.
        return False, f"gate_a: window expired ({elapsed:.0f}s > {GATE_A_WINDOW_SECS}s)"

    cutoff = s.launch_ts  # window is from launch
    excluded: set[str] = {s.creator} if (GATE_A_EXCLUDE_DEV_BUYS and s.creator) else set()
    unique = s.unique_buyers_in_window(cutoff) - excluded
    inflow = s.total_sol_inflow_in_window(cutoff, excluded)

    if not (GATE_A_MIN_UNIQUE_WALLETS <= len(unique) <= GATE_A_MAX_UNIQUE_WALLETS):
        return False, (
            f"gate_a: {len(unique)} unique buyers outside "
            f"[{GATE_A_MIN_UNIQUE_WALLETS}, {GATE_A_MAX_UNIQUE_WALLETS}]"
        )
    if inflow < GATE_A_MIN_TOTAL_SOL_INFLOW:
        return False, f"gate_a: inflow {inflow:.1f} SOL < {GATE_A_MIN_TOTAL_SOL_INFLOW} SOL"

    return True, f"gate_a: {len(unique)} buyers, {inflow:.1f} SOL inflow"


def evaluate_gate_b(s: BcMintState) -> tuple[bool, str]:
    """40/60 Escape Rule — past the death zone, with sustained buying."""
    if not (GATE_B_MIN_BC_PROGRESS_PCT <= s.progress_pct <= GATE_B_MAX_BC_PROGRESS_PCT):
        return False, (
            f"gate_b: progress {s.progress_pct:.1f}% outside "
            f"[{GATE_B_MIN_BC_PROGRESS_PCT}, {GATE_B_MAX_BC_PROGRESS_PCT}]"
        )
    br = s.buy_ratio_recent(GATE_B_BUY_PRESSURE_WINDOW_SECS)
    if br is None:
        return False, "gate_b: buy_ratio data not yet populated by Geyser"
    if br < GATE_B_MIN_BUY_RATIO_PCT:
        return False, f"gate_b: buy_ratio {br:.0f}% < {GATE_B_MIN_BUY_RATIO_PCT}%"
    return True, f"gate_b: progress {s.progress_pct:.1f}%, buy_ratio {br:.0f}%"


def evaluate_gate_c(s: BcMintState) -> tuple[bool, str]:
    """Safety filters — dev holding, block-0 bundle, creator history."""
    if s.dev_holding_pct > GATE_C_MAX_DEV_HOLDING_PCT:
        return False, f"gate_c: dev holding {s.dev_holding_pct:.1f}% > {GATE_C_MAX_DEV_HOLDING_PCT}%"

    if GATE_C_BLOCK_0_BUNDLE_VETO and s.block_0_bundle_detected:
        return False, "gate_c: block-0 bundle detected (multi-wallet deployer buys)"

    if GATE_C_REQUIRE_CREATOR_HISTORY:
        # Reuse the existing dev_reputation system — it tracks per-creator
        # win/loss/rug counts and can derive a graduation rate from
        # `tokens_launched` and `successful_launches`.
        try:
            from elizaos.plugins.solana.dev_reputation import get_reputation
            rep = get_reputation()
            if rep.is_blacklisted(s.creator):
                return False, f"gate_c: creator {s.creator[:8]} blacklisted"
            # NOTE: graduation rate = successful_launches / tokens_launched.
            # Leave the threshold check off for the cold-start period — most
            # creators will be 'unknown' until our outcome recording fills
            # in their history. Re-enable once the proven-tier population
            # is non-trivial.
        except Exception as exc:  # noqa: BLE001
            return False, f"gate_c: dev_reputation lookup error — {exc}"

    return True, "gate_c: safety checks pass"


def all_gates_pass(s: BcMintState) -> tuple[bool, list[str]]:
    """Returns (overall_pass, reasons_list). Logs every reason for forensics."""
    a_ok, a_msg = evaluate_gate_a(s)
    b_ok, b_msg = evaluate_gate_b(s)
    c_ok, c_msg = evaluate_gate_c(s)
    return (a_ok and b_ok and c_ok), [a_msg, b_msg, c_msg]
