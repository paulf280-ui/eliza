"""Per-mint bonding curve state tracker.

Holds the rolling state Geyser events update, and the gates read. One
BcMintState per mint we're observing; lifecycle is launch → graduation
or rug. Cleared from memory on either terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class WalletBuy:
    """A single buy seen on a mint, used by Gate A's velocity calc."""
    wallet: str
    sol_amount: float
    ts: float


@dataclass
class BcMintState:
    """Live state for a single bonding-curve mint we're tracking."""

    mint: str
    creator: str = ""

    # Launch context (set on first sighting)
    launch_ts: float = 0.0
    launch_slot: int = 0

    # BC progress — driven by Geyser virtual_sol_reserves / virtual_token_reserves
    # progress_pct goes 0 → 100; graduation triggers around 96-100% per pump.fun
    progress_pct: float = 0.0
    last_progress_update_ts: float = 0.0

    # Aggregate buy volume — populated by Geyser buy-event subscriber
    buys: list[WalletBuy] = field(default_factory=list)
    sells_count_60s: int = 0
    buys_count_60s: int = 0

    # Holder-side signals
    dev_holding_pct: float = 0.0
    block_0_bundle_detected: bool = False
    holder_count: int = 0

    # Position state (set when we enter)
    entry_price: float | None = None
    entry_ts: float | None = None
    entry_size_sol: float | None = None
    peak_pnl_pct: float = 0.0
    peak_pnl_ts: float = 0.0
    trailing_armed: bool = False

    # ── Gate A helpers ────────────────────────────────────────────────────
    def unique_buyers_in_window(self, since_ts: float) -> set[str]:
        return {b.wallet for b in self.buys if b.ts >= since_ts}

    def total_sol_inflow_in_window(self, since_ts: float, exclude: set[str]) -> float:
        return sum(b.sol_amount for b in self.buys if b.ts >= since_ts and b.wallet not in exclude)

    # ── Gate B helpers ────────────────────────────────────────────────────
    def buy_ratio_recent(self, window_secs: float) -> float | None:
        """Buy/(buy+sell) ratio over the last `window_secs`. None if too few txns."""
        # Stub: real impl requires sell counts populated from Geyser.
        # Returning None forces Gate B to skip when data is missing rather
        # than firing on an empty window.
        return None

    # ── Position-side helpers ─────────────────────────────────────────────
    def update_peak(self, current_pnl_pct: float) -> None:
        if self.entry_pnl_to_peak() is None or current_pnl_pct > self.peak_pnl_pct:
            self.peak_pnl_pct = current_pnl_pct
            self.peak_pnl_ts = time.time()

    def entry_pnl_to_peak(self) -> float | None:
        if self.peak_pnl_ts == 0.0:
            return None
        return self.peak_pnl_pct


# In-memory state pool. Geyser subscriber populates / mutates entries here;
# loop.py reads them every tick. Cleared on graduation, rug, or position close.
_bc_states: dict[str, BcMintState] = {}


def get_or_create(mint: str, creator: str = "") -> BcMintState:
    s = _bc_states.get(mint)
    if s is None:
        s = BcMintState(mint=mint, creator=creator, launch_ts=time.time())
        _bc_states[mint] = s
    return s


def drop(mint: str) -> None:
    _bc_states.pop(mint, None)


def all_states() -> dict[str, BcMintState]:
    return _bc_states
