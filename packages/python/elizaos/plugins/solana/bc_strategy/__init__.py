"""BC (Bonding Curve) pre-graduation strategy lane — SCAFFOLD.

Phase 2 strategy targeting tokens DURING the pump.fun bonding curve, before
graduation to PumpSwap. Complementary to the post-graduation monster lane
in strategy_e_monster.py — different market phase, different mechanics.

DISABLED BY DEFAULT. Set BC_STRATEGY_ENABLED=true in .env to engage. The
loop refuses to start unless all stubbed pieces have been implemented and
their NotImplementedError sentinels removed.

Design source: pump.fun quant strategy guide (2026-04-30, three-page PDF).
Architecture summary:

  Entry gates (all must pass):
    Gate A — Liquidity Path Velocity: 5-10 unique non-dev wallets with
             >15 SOL total volume in the first 120s of launch
    Gate B — 40/60 Escape Rule: enter at 40% bonding curve progress with
             sustained buy pressure (filters tokens dying at 20-30% BC)
    Gate C — Safety filters: dev <10%, no Block-0 bundle, creator with
             prior graduation history

  Exits (in priority order):
    1. -40% hard stop loss (rug protection)
    2. 98% bonding curve completion (exit before Raydium migration dump)
    3. 15% TP with 5% trailing stop (primary winner take)

  Infrastructure:
    - Yellowstone Geyser gRPC stream for real-time BC account updates
      (replaces DexScreener REST polling — far below the latency floor
      this lane needs to compete in graduation rushes)
    - Custom pump.fun BC buy/sell instruction builder (no PumpPortal —
      we control the instruction list end-to-end so we can splice in
      Sender's tip-account transfer for full dual-routing priority)
    - Sender (sender.helius-rpc.com/fast) submission with mandatory
      Sender tip-account transfer (one of 11 accounts, min 0.0002 SOL)

Module layout:
  config.py    — all constants + env flags
  state.py     — per-mint BC progress tracker
  gates.py     — Gate A / B / C entry logic
  exits.py     — exit rules
  geyser.py    — Yellowstone gRPC subscriber
  tx_builder.py — pump.fun BC instruction builder + Sender submitter
  loop.py      — orchestrator entry point (called from run_traderbot)
"""

from elizaos.plugins.solana.bc_strategy.config import BC_STRATEGY_ENABLED


def is_enabled() -> bool:
    """Top-level gate read by run_traderbot to decide whether to start the loop."""
    return BC_STRATEGY_ENABLED


__all__ = ["is_enabled"]
