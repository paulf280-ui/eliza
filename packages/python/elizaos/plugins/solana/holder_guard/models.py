"""holder_guard.models — dataclasses carrying guard state between layers.

Optional fields (snipers_pct, bundlers_pct, audit_score, fresh_pct) are typed
as None when data is not available. Filters that depend on them skip gracefully
instead of rejecting, so missing data never auto-blocks.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class HolderSnapshot:
    """Point-in-time holder/authority picture for a single mint."""
    mint: str
    ts: float = field(default_factory=time.time)

    # Distribution — from top_wallet_distribution()
    top1_pct: float | None = None
    top10_pct: float | None = None
    wallets_scanned: int = 0

    # Supply + authority — from fetcher
    total_supply: float | None = None
    mint_authority_renounced: bool | None = None
    freeze_authority_renounced: bool | None = None

    # Dev wallet — from Metaplex metadata + balance
    dev_wallet: str | None = None
    dev_holding_pct: float | None = None

    # LP — from pool inspection
    lp_burned_pct: float | None = None   # 0.0 – 100.0

    # Holder count / growth — from DexScreener + holder_tracker
    unique_holders: int | None = None
    holder_growth_per_min: float | None = None   # holders/min raw number
    holder_growth_per_min_pct: float | None = None  # pct of current count

    # Buy/sell pressure — from DexScreener txns window
    buy_count_h1: int | None = None
    sell_count_h1: int | None = None
    buy_volume_h1: float | None = None
    sell_volume_h1: float | None = None

    # Paid-data slots — filled if/when we integrate Bubblemaps / Nansen / audit feeds
    snipers_pct: float | None = None
    bundlers_pct: float | None = None
    audit_score: int | None = None   # out of 8
    fresh_pct: float | None = None    # % of top-20 wallets created recently


@dataclass
class HolderDelta:
    """Rolling window deltas for a mint's holder distribution."""
    mint: str
    window_mins: int

    top10_delta_abs_pct: float | None = None     # change in top-10 percentage points
    holder_count_delta: int | None = None         # raw delta in unique holder count
    holder_count_delta_pct: float | None = None   # delta as % of starting count


@dataclass
class GuardDecision:
    """Orchestrator output for an entry or exit evaluation."""
    mint: str

    # Entry-side fields
    hard_block: bool = False
    block_reasons: list[str] = field(default_factory=list)
    sweet_spot_pass: bool = False
    failing_gates: list[str] = field(default_factory=list)   # which sweet-spot rules missed

    # Exit-side fields
    should_exit: bool = False
    exit_reason: str | None = None

    # Accumulation override — if True, suppress price-based exits
    hold_override: bool = False
    hold_reason: str | None = None

    # Raw snapshot for logging / downstream brains
    snapshot: HolderSnapshot | None = None
