"""BC strategy orchestrator loop — entry point.

Started by run_traderbot when BC_STRATEGY_ENABLED=true. Owns the lifecycle:
  1. assert_ready() — fail-fast if infra is missing
  2. spawn the Geyser subscriber as a background task
  3. tick every N ms over state.all_states() — evaluate gates for non-held
     mints, evaluate exits for held mints
  4. on entry pass: tx_builder.build_and_submit_buy(); persist position
  5. on exit pass: tx_builder.build_and_submit_sell(); record outcome
"""

from __future__ import annotations

import asyncio
from typing import Any

from elizaos.plugins.solana.bc_strategy.config import (
    BC_MAX_CONCURRENT,
    BC_PAPER_ONLY,
    BC_STRATEGY_ENABLED,
    BC_TRADE_SIZE_SOL,
    assert_ready,
)
from elizaos.plugins.solana.bc_strategy.exits import evaluate_exit
from elizaos.plugins.solana.bc_strategy.gates import all_gates_pass
from elizaos.plugins.solana.bc_strategy.geyser import run_geyser_loop
from elizaos.plugins.solana.bc_strategy.state import BcMintState, all_states, drop


# In-memory open-position table. Keyed by mint. Single source of truth for
# the loop; the close path persists to bc_closed_trades.json (TBD) for
# session-survival and learning.
_open_positions: dict[str, BcMintState] = {}


# ─── Tick interval ────────────────────────────────────────────────────────
# Aggressive — Geyser pushes events into state in real time, so the loop
# tick is just for evaluating gates / exits against current state. 250 ms
# is the floor; below that we'd be wasting CPU evaluating unchanged state.
TICK_INTERVAL_SECS = 0.25


async def bc_strategy_loop(runtime: Any) -> None:
    """Main BC strategy loop. Runs forever until cancelled.

    Args:
        runtime: AgentRuntime — used to fetch the wallet service for tx
                 submission. Same pattern monster lane uses.
    """
    if not BC_STRATEGY_ENABLED:
        print("[bc-strategy] disabled (BC_STRATEGY_ENABLED=false) — loop not started")
        return

    assert_ready()  # raises if infra missing

    mode = "PAPER" if BC_PAPER_ONLY else "LIVE"
    print(f"[bc-strategy] loop started ({mode}) — tick={TICK_INTERVAL_SECS}s, "
          f"slots={BC_MAX_CONCURRENT}, size={BC_TRADE_SIZE_SOL} SOL")

    # Geyser as a background task — pushes events into state.all_states().
    geyser_task = asyncio.create_task(run_geyser_loop())

    try:
        while True:
            try:
                await _tick(runtime)
            except Exception as exc:  # noqa: BLE001
                print(f"[bc-strategy] tick error: {exc}")
            await asyncio.sleep(TICK_INTERVAL_SECS)
    finally:
        geyser_task.cancel()


async def _tick(runtime: Any) -> None:
    """One evaluation pass over all tracked mints."""

    # ── Exit evaluation for open positions ────────────────────────────────
    for mint, s in list(_open_positions.items()):
        # Geyser updates `s` in place; we read the latest snapshot here.
        # current_price derived from BC virtual reserves; tx_builder will
        # return that on entry, and Geyser updates progress_pct on every
        # buy/sell. Real impl: compute price from state's reserves.
        current_price = _current_price_from_state(s)
        reason, frac = evaluate_exit(s, current_price)
        if reason:
            await _close_position(s, reason, frac, runtime)

    # ── Gate evaluation for un-held mints ─────────────────────────────────
    if len(_open_positions) >= BC_MAX_CONCURRENT:
        return  # slot pool full

    for mint, s in all_states().items():
        if mint in _open_positions:
            continue
        passed, reasons = all_gates_pass(s)
        if not passed:
            continue
        await _open_position(s, reasons, runtime)
        if len(_open_positions) >= BC_MAX_CONCURRENT:
            break


def _current_price_from_state(s: BcMintState) -> float:
    """Stub — real impl derives price from virtual_sol_reserves /
    virtual_token_reserves which Geyser populates on s. Returns 0 until
    Geyser is wired."""
    return 0.0


async def _open_position(s: BcMintState, gate_reasons: list[str], runtime: Any) -> None:
    print(f"[bc-strategy] 🎯 OPENING {s.mint[:8]} — {' | '.join(gate_reasons)}")
    if BC_PAPER_ONLY:
        # Record a paper entry for outcome tracking without submitting on-chain.
        s.entry_price = _current_price_from_state(s) or 1.0
        s.entry_size_sol = BC_TRADE_SIZE_SOL
        _open_positions[s.mint] = s
        return
    # Real impl uses tx_builder.build_and_submit_buy(). Stubbed for safety.
    raise NotImplementedError("BC live entry path not yet built")


async def _close_position(s: BcMintState, reason: str, frac: float, runtime: Any) -> None:
    print(f"[bc-strategy] 📤 CLOSING {s.mint[:8]} reason={reason} frac={frac}")
    _open_positions.pop(s.mint, None)
    drop(s.mint)
    if BC_PAPER_ONLY:
        return
    raise NotImplementedError("BC live exit path not yet built")
