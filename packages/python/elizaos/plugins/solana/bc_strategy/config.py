"""BC strategy configuration — env flags + thresholds from the quant guide.

All knobs collected here so the build phase has a single tuning surface.
Numbers reflect the pump.fun quant strategy guide as published; tighten or
relax as outcome data accumulates.
"""

from __future__ import annotations

import os


def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


# ─── Master flags ─────────────────────────────────────────────────────────
BC_STRATEGY_ENABLED = _env_on("BC_STRATEGY_ENABLED", "false")
BC_PAPER_ONLY       = _env_on("BC_PAPER_ONLY", "true")  # safe default while building

# ─── Sizing & economics (from PDF §1) ─────────────────────────────────────
BC_TRADE_SIZE_SOL    = 0.5      # PDF profitability threshold — minimum viable trade
BC_MAX_CONCURRENT    = 1        # one slot, mirroring the monster lane discipline
BC_JITO_TIP_SOL      = 0.005    # standard tip; scale to 0.05 on contested blocks
BC_SENDER_TIP_SOL    = 0.0002   # Sender minimum (in addition to Jito tip)
BC_SLIPPAGE_PCT      = 20       # PDF range 15-30%; 20 is the safe middle

# ─── Gate A — Liquidity Path Velocity (PDF §2) ────────────────────────────
GATE_A_WINDOW_SECS              = 120     # observation window from token launch
GATE_A_MIN_UNIQUE_WALLETS       = 5       # 5-10 unique non-dev wallets...
GATE_A_MAX_UNIQUE_WALLETS       = 10      # (range; >10 starts looking like a bot rush)
GATE_A_MIN_TOTAL_SOL_INFLOW     = 15.0    # combined buy volume from those wallets
GATE_A_EXCLUDE_DEV_BUYS         = True    # don't count the deployer's own buys

# ─── Gate B — 40/60 Escape Rule (PDF §2) ──────────────────────────────────
GATE_B_MIN_BC_PROGRESS_PCT      = 40.0    # token has cleared the 20-30% death zone
GATE_B_MAX_BC_PROGRESS_PCT      = 60.0    # don't enter once it's near graduation
GATE_B_BUY_PRESSURE_WINDOW_SECS = 60      # buy ratio measured over last 60s
GATE_B_MIN_BUY_RATIO_PCT        = 55.0    # sustained buying, not just a single pump

# ─── Gate C — Safety filters (PDF §2) ─────────────────────────────────────
GATE_C_MAX_DEV_HOLDING_PCT      = 10.0    # PDF threshold
GATE_C_BLOCK_0_BUNDLE_VETO      = True    # immediate blacklist if multi-wallet block-0 buy
GATE_C_REQUIRE_CREATOR_HISTORY  = True    # creator must have ≥1 prior graduated token
GATE_C_MIN_CREATOR_GRAD_RATE    = 0.20    # ≥20% of their prior tokens graduated

# ─── Exit rules (PDF §4) ──────────────────────────────────────────────────
BC_HARD_STOP_LOSS_PCT           = -40.0   # PDF; rug protection
BC_PRIMARY_TP_PCT               = 15.0    # PDF; combine with trailing stop
BC_TRAILING_STOP_GIVEBACK_PCT   = 5.0     # peak − giveback% triggers exit
BC_TRAILING_ARM_AT_PCT          = 8.0     # trail engages once pnl crosses +8%
BC_EXIT_NEAR_GRADUATION_PCT     = 98.0    # exit before migration liquidity dump

# ─── Infrastructure (PDF §3) ──────────────────────────────────────────────
BC_GEYSER_GRPC_URL              = os.getenv(
    "BC_GEYSER_GRPC_URL", ""
)  # Helius Geyser endpoint; required when BC_STRATEGY_ENABLED=true

BC_PUMP_FUN_PROGRAM             = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# pump.fun program id — buy/sell instruction discriminators live in
# elizaos.plugins.solana.constants (PUMP_FUN_BUY_DISCRIMINATOR / _SELL_).

BC_SENDER_TIP_ACCOUNTS = [
    # Helius Sender tip accounts (11 total, screenshot from sender dashboard
    # 2026-04-30). Pick one at random per-tx so we don't spotlight a single
    # account during high-volume periods.
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3wv7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
]


def assert_ready() -> None:
    """Called by the loop on startup. Fails loudly if the lane is enabled
    while critical infrastructure is missing — prevents accidental live
    trading with stubbed gates."""
    if not BC_STRATEGY_ENABLED:
        return
    if not BC_GEYSER_GRPC_URL:
        raise RuntimeError(
            "BC_STRATEGY_ENABLED=true but BC_GEYSER_GRPC_URL is empty. "
            "The BC lane needs a Yellowstone Geyser gRPC endpoint for "
            "real-time bonding curve events; DexScreener polling is too "
            "slow for pre-graduation entries."
        )
