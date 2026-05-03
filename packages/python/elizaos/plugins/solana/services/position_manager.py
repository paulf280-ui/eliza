"""PositionManagerService — quantitative risk management for the Solana trading bot.

Implements:
  - Per-trade position sizing (max 2% of wallet)
  - Stop loss (-15% hard, -10% early in first 3 min)
  - 25/25 Staircase exits: TP1 +25% sell 50%, TP2 +56% sell 75% of remaining
  - Trailing stop -15% from peak on moonbag (after TP2)
  - Hard time limit (2 hours pump.fun, 6 hours PumpSwap graduation, 72 hours Raydium)
  - Token safety filter (mint authority, freeze authority)
  - Circuit breakers (daily -15% loss limit, 3 consecutive SL hits)
  - Background price monitor (15s interval)

Exit strategy based on research (March 2026):
  - 70-80% loss rate is NORMAL for this strategy
  - Edge comes from winners being 3-5x larger than losers
  - TP1 at +25% (sell 50%) ensures you can't lose capital even if moonbag goes to zero
  - Three strategies: A (bonding curve snipe), B (graduation snipe to PumpSwap), C (Raydium momentum)
  - After March 2025: graduated tokens route to PumpSwap (not Raydium)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")

from elizaos.types import Service, ServiceTypeRegistry

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime

# ─────────────────────────── risk constants ──────────────────────────────────
# All values aligned with the Moonshot DNA & Raydium Strategy playbook.

# Position sizing
MAX_POSITION_PCT: float = 0.50       # paper trial: 50% allows 0.05 SOL on 0.10 SOL wallet (reset for live)
MIN_POSITION_SOL: float = 0.05       # floor — below 0.05 SOL, fixed Jito tips (~0.002 SOL/tx × 2) eat >8% of trade — never profitable
MAX_CONCURRENT_POSITIONS: int = 3    # default — overridden at runtime by live_config.get("max_concurrent_positions")

# Stop loss (hard, applied before any TP is hit)
# Research (March 2026): -10% for quick scalp trades. We use -15% as a compromise
# for our hybrid lottery/scalp approach.
STOP_LOSS_PCT: float = 0.09          # -9% trigger → actual exit ~-12-16% after DexScreener lag (data: 67% of SL exits overshoot -15%)

# 25/25 Staircase exit (recommended by research for initial paper trading baseline)
#   TP1: +25%  → sell 50% of position  (capital protection — can't lose original even if moonbag dies)
#   TP2: +56%  → sell 75% of remaining  (= 37.5% of original sold, 12.5% moonbag held)
#   Moonbag (12.5%): progressive trailing stop after TP2; TP3 jackpot at +200%
#   Progressive trailing stop tiers (tightens as profit grows):
#     Entry → +100%: -12% from peak  (tight — catch reversals before they erase TP1 gains)
#     +100% → +200%: -8% from peak   (tightening further)
#     +200%+: -6% from peak           (maximum lock-in on big runners)
# Per-DEX full-exit TP targets (conservative scalping strategy — bank profits fast, 9% SL)
# Break-even WR: pumpswap 23%, pump_fun 13%, raydium 31%, meteora 37.5%
TP1_MULT: float = 1.30               # +30%  → FULL EXIT — PumpSwap (graduated tokens) — updated 2026-03-16
PF_TP1_MULT: float = 1.45            # +45%  → FULL EXIT — pump.fun BC — updated 2026-03-16
GROK_TP_MULT: float = 1.80           # +80%  → FULL EXIT — Grok-confirmed X social signal (extra conviction)
METEORA_TP1_MULT: float = 10.0       # +900% safety ceiling — AI cascade is primary exit for Meteora (liq/holder driven)
PUMPSWAP_TP1_MULT: float = 10.0      # +900% safety ceiling — AI cascade is primary exit for PumpSwap (same as Meteora)
RAYDIUM_TP1_MULT: float = 10.0       # +900% safety ceiling — AI cascade is primary exit for Raydium scouts (monsters need 6-20h holds)
TP15_MULT: float = 1.80              # legacy staircase step — unused in current conservative mode
TP2_MULT: float = 2.80               # legacy staircase step — unused in current conservative mode
TP3_MULT: float = 5.0                # jackpot moonbag target — unused in current conservative mode
TRAILING_STOP_PCT: float = 0.15      # 15% trailing stop — legacy moonbag protection
MOONBAG_STALL_SECS: int = 30 * 60    # 30min stall exit — legacy moonbag

# Grok Premium mode — triggered when Grok score ≥8/10 on a token.
# Uses a higher position size (set via bot_config.json) and a tighter TP/stall strategy:
#   Full exit at +50% TP, OR exit at +25% if price stalls (±2% for 60s).
GROK_PREMIUM_TP_MULT: float = 1.50   # +50% full exit for premium Grok-confirmed trades
GROK_PREMIUM_STALL_SECS: int = 60    # 60s stall window — if price doesn't move ±2%, take profit
GROK_PREMIUM_STALL_BAND: float = 0.02  # ±2% band — movement larger than this resets the stall timer
GROK_PREMIUM_STALL_MIN_GAIN: float = 0.25  # minimum P&L (+25%) to qualify for stall exit

# pump.fun bonding-curve specific (legacy — now simplified to single full-exit at PF_TP1_MULT)
PF_TP2_MULT: float = 2.50            # legacy — unused now that pump.fun uses single full exit
PF_STAGNANT_SECS: int = 180         # 3 min — if price flat for 3min on BC → dead money, exit
PF_STAGNANT_BAND: float = 0.04      # ±4%  — price must move outside this band to reset stagnant timer

# Consecutive zero-price cycles before declaring a rug and force-closing.
# When a pool is drained, DexScreener returns pairs=null → price fetches return None.
# 3 consecutive Nones after the first 60s = pool gone = emergency close.
RUG_ZERO_PRICE_STREAK: int = 3

# Time / stall limits (seconds)
MAX_HOLD_PUMP_FUN:      int = 2 * 3600  # 2h for bonding-curve tokens (most peak fast or die)
MAX_HOLD_PUMPSWAP:      int = 48 * 3600  # 48h — monster DNA: BURNIE peaked 14-24h, milkers 20-36h, ZEN 24-48h
MAX_HOLD_METEORA:       int = 6 * 3600  # 6h for Meteora DLMM launches (similar to PumpSwap)
MAX_HOLD_RAYDIUM:       int = 72 * 3600 # 72h for Raydium / LaunchLab tokens
# Social Momentum (Strategy E) hold times — research: Tier3 pumps last 15-120min
MAX_HOLD_SOCIAL_TIER3:  int = 90 * 60   # 90 min — generic meme, fast pump-and-dump
MAX_HOLD_SOCIAL_TIER12: int = 6 * 3600  # 6h — Tier1 (political/AI) or Tier2 (animal/gaming)
STALL_TIME_SECS:        int = 900       # exit if no +7% in 15 min — winners move fast, losers stay flat
STALL_TIME_SOCIAL:      int = 300       # 5 min stall for social plays — social pumps exhaust faster than pumpswap
STALL_TIME_RAYDIUM:     int = 480       # 8 min for native Raydium — research shows peaks at 3-8 min, be decisive
STALL_TIME_PUMP_FUN:    int = 180       # 3 min for BC tokens — Ghost Rider fast-exit: if no +7% in 3 min, stale entry
STALL_THRESHOLD:        float = 0.07    # +7% minimum gain before stall timer expires
# Social fade exit — exit when well into profit but momentum reversing (research: vol/holder drops = pump exhaust)
SOCIAL_FADE_GAIN_MIN:   float = 0.20   # must be >+20% before considering fade exit
SOCIAL_FADE_H1_THRESH:  float = -0.05  # h1 must be <-5% (price reversing) to trigger
SOCIAL_FADE_VOL_DROP:   float = 0.45   # exit if current h1_vol < 55% of entry h1_vol (volume fading)

# On-chain health monitor exit — runs every 60s via _liquidity_monitor_loop.
# When a position gains ≥ HEALTH_TRIGGER_PCT, the fixed TP ceiling is raised to ×20
# and the bot holds as long as fundamentals are positive.  Exit fires when
# HEALTH_EXIT_BAD_READINGS consecutive 60-s readings each show ≥2/3 bad signals.
HEALTH_TRIGGER_PCT:       float = 0.60   # activate health monitor at ≥+60% gain
HEALTH_EXIT_BAD_READINGS: int   = 3      # exit after N consecutive bad readings (3×60s = 3 min of bad signals)
HEALTH_LIQ_DROP_THRESH:   float = 0.20   # -20% from health-monitor peak liq = bad
HEALTH_MIN_BUY_RATIO:     float = 0.45   # buy ratio (m5) < 45% = selling pressure dominant
HEALTH_M5_DECLINE_THRESH: float = -0.05  # m5 price change < -5% = momentum breaking down

# Circuit breakers
MAX_DAILY_LOSS_PCT: float = 0.15     # pause if down 15% on the day (playbook §2.3)
MAX_CONSECUTIVE_LOSSES: int = 10     # pause after 10 consecutive losses; at 24% win rate P(10 in a row)=6.5% (acceptable false-positive)

# Pump.fun token filter thresholds
MAX_DEV_WALLET_PCT: float = 0.07     # reject if dev holds >7% of supply (playbook: 0-7%)
MAX_TOP10_HOLDER_PCT: float = 0.30   # reject if top-10 wallets own >30% (playbook §1.2 P6)

# Monitor loop interval — 5s catches fast dumps before early-SL overshoots
# Data showed MONITOR=15s caused early_stop_loss to fire at -20% to -43%
# (token crashes in single 15s window before monitor wakes up).
MONITOR_INTERVAL_SECS: int = 2

# Persistent trade log — survives bot restarts so overnight data is never lost
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "trade_history.json")

# Permanent mint blacklist — records every mint ever bought.
# NEVER wiped by a fresh start. Prevents re-buying rugged or manually-sold tokens
# across sessions even after trade_history.json is cleared.
TRADED_MINTS_FILE = os.path.join(os.path.dirname(__file__), "..", "traded_mints.json")

# Early stop-loss: tighter SL for the first few minutes to catch fast rug dumps.
# If the price drops more than EARLY_STOP_LOSS_PCT within EARLY_STOP_LOSS_SECS of
# entry we exit immediately — this caps the gap loss on sniped tokens.
EARLY_STOP_LOSS_PCT: float = 0.05    # -5% trigger → actual exit ~-8-10% with DexScreener lag (tightened 2026-03-13)
EARLY_STOP_LOSS_SECS: int = 180      # 3-minute early-exit window
EARLY_STOP_LOSS_GRACE_SECS: int = 60 # don't fire early SL in first 60s — avoids whipsaw on momentum tokens


# ─────────────────────────── helpers ─────────────────────────────────────────

def position_key(mint: str, label: str = "") -> str:
    """Return the dict key for a position.

    Normal positions:        key == mint
    Split-buy positions:     key == "mint:A"  or  "mint:B"
    """
    return f"{mint}:{label}" if label else mint


# ─────────────────────────── data classes ────────────────────────────────────

@dataclass
class Position:
    """Tracks a single open trade."""
    mint: str
    dex: str                  # "pump_fun" or "raydium"
    entry_price_sol: float    # SOL per token at entry
    entry_sol_spent: float    # SOL committed
    token_amount: int         # raw token units (after decimals)
    token_decimals: int = 9

    # Derived levels (set post-construction)
    stop_loss_price: float = 0.0      # entry * (1 - STOP_LOSS_PCT)
    tp1_price: float = 0.0            # entry * TP1_MULT  (+40%)
    tp15_price: float = 0.0           # entry * TP15_MULT (+80%) — second step
    tp2_price: float = 0.0            # entry * TP2_MULT  (+180%)
    tp3_price: float = 0.0            # entry * TP3_MULT  (+400% jackpot)
    peak_price: float = 0.0
    trailing_stop_price: float = 0.0

    creator_wallet: str = ""    # pump.fun deployer wallet (accounts[0] at Create tx)
    score: int = 0              # scout score at entry (0 = unknown)
    fill_pct: float = 0.0       # bonding curve fill % at entry (0 = unknown)

    tp1_hit: bool = False
    tp15_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    tp2_hit_time: float = 0.0   # timestamp when TP2 fired — starts moonbag stall timer
    zero_price_streak: int = 0  # consecutive monitor cycles with no price — ≥3 = rug detection

    is_reconciled: bool = False  # True = imported from wallet on restart, don't count toward active trading limit
    grok_confirmed: bool = False  # True = Grok/X social signal confirmed this token — use GROK_TP_MULT
    grok_premium: bool = False    # True = Grok score ≥8/10 — 0.3 SOL position, TP +50%, stall exit at +25%

    # Moon mode — activated for ultra-fast graduations (< 6 min BC→PumpSwap) or high DexScreener boost (≥200).
    # These tokens have demonstrated coordinated capital and large move potential (CHIBI +6,936%, fine999.9 +11,312%).
    # Instead of a fixed TP (+30%), moon mode uses a TRAILING STOP that activates once up > +100%.
    # Trail: -12% when +100–200%, -8% when +200–500%, -6% above +500% (locks in big runner profits).
    moon_mode: bool = False         # True = skip fixed TP, use trailing stop as primary exit
    moon_trail_active: bool = False # True once gain > +100% — trailing stop fires if price reverses

    # Social momentum (Strategy E) — narrative-aware TP and fade tracking
    narrative_tp_mult:   float  = 1.60  # 1.60=Tier3(+60%), 2.00=Tier2(+100%), 3.00=Tier1/KOL(+200%)
    social_entry_vol_h1: float  = 0.0   # h1 volume at entry (for fade detection)
    social_fade_check_at: float = 0.0   # timestamp of last DexScreener fade check
    social_peak_gain:    float  = 0.0   # highest P&L seen during the hold (for Jarvis reporting)

    # Premium stall tracking — updated every monitor cycle
    premium_stall_ref_price: float = 0.0   # price when stall window last reset
    premium_stall_since: float = 0.0       # timestamp when current stall window started

    # Grace period: TP cannot fire until this timestamp (prevents immediate fire on stale DexScreener prices)
    monitoring_starts_at: float = 0.0

    # Sell retry tracking — if PumpPortal returns 400 we keep position open and retry
    sell_fail_count: int = 0        # consecutive sell failures (reset on success)
    last_sell_attempt: float = 0.0  # timestamp of last sell attempt (throttle retries to 30s)

    entry_time: float = field(default_factory=time.time)
    stall_price: float = 0.0
    stall_since: float = 0.0
    last_known_price: float = 0.0  # updated every monitor tick — use as exit price in paper mode

    # PumpSwap pool address — stored at open time so _fetch_price_for_position
    # can use Helius RPC for real-time prices instead of DexScreener (5-30s lag).
    pool_address: str = ""

    # Liquidity at entry — used to detect sudden rug (>30% liq drain while holding)
    entry_liquidity_usd: float = 0.0
    # Track last liq check timestamp (avoid hammering DexScreener every 2s)
    _last_liq_check_ts: float = 0.0
    _last_known_liq_usd: float = 0.0
    # Liq tracking state: 0=normal, 1=growth positive logged, 2=warning sent, 3=emergency fired
    _liq_state: int = 0

    # On-chain health monitor — activated when gain hits HEALTH_TRIGGER_PCT (+60%)
    # Instead of fixed TP exit, bot holds while fundamentals are positive.
    # _liquidity_monitor_loop checks every 60s and sets health_exit_requested when
    # HEALTH_EXIT_BAD_READINGS consecutive readings each show ≥2/3 bad signals.
    health_monitor_mode:   bool  = False  # True = health monitor active; fixed TP ceiling raised to ×20
    health_exit_requested: bool  = False  # set by health monitor → close on next monitor tick
    health_bad_readings:   int   = 0      # consecutive unhealthy DexScreener readings
    health_peak_liq_usd:   float = 0.0   # highest liq seen since health monitor activated
    health_activated_at:   float = 0.0   # timestamp when health monitor first activated
    _last_buy_ratio_m5:    float = 0.5   # latest m5 buy ratio read by liq monitor (0–1)
    _last_m5_pct:          float = 0.0   # latest m5 price change as decimal (e.g. -0.03 = -3%)

    # Smart money: owner wallets of top holders at entry time (set from meta["holder_wallets"])
    holder_wallets: list = field(default_factory=list)

    # Split-buy system — "" = normal single position, "A" = HARVESTER, "B" = MONSTER HUNTER
    label: str = ""            # position label; determines which TP/SL config to use
    split_pair_key: str = ""   # pos_key of the paired position (informational, for Jarvis)

    def __post_init__(self) -> None:
        # ── Split-buy label overrides (MUST run before generic per-DEX logic) ──
        # HARVESTER (A) uses split_buy_a_tp_mult and split_buy_sl_mult.
        # MONSTER HUNTER (B) uses split_buy_b_tp_mult and split_buy_sl_mult.
        # Since the checks below are `if self.X == 0`, pre-setting them here is sufficient.
        if self.label in ("A", "B"):
            try:
                from elizaos.plugins.solana import live_config as _lc_split
                _split_sl_mult = float(_lc_split.get("split_buy_sl_mult", 0.80))
                _tp_key = "split_buy_a_tp_mult" if self.label == "A" else "split_buy_b_tp_mult"
                _split_tp = float(_lc_split.get(_tp_key, 1.60 if self.label == "A" else 2.50))
            except Exception:
                _split_sl_mult = 0.80
                _split_tp = 1.60 if self.label == "A" else 2.50
            if self.stop_loss_price == 0:
                self.stop_loss_price = self.entry_price_sol * _split_sl_mult
            if self.tp1_price == 0:
                self.tp1_price = self.entry_price_sol * _split_tp
            # fall through to set peak_price, trailing_stop, tp2, tp3, stall below
        if self.stop_loss_price == 0:
            # Age-based SL for PumpSwap: young tokens are volatile post-graduation, give room.
            # 60-120 min: -25% | 2-6 hr: -20% | 6hr+: config (default -9%)
            # Liq-collapse emergency exit (#12) provides real rug protection; hard SL is last resort.
            try:
                from elizaos.plugins.solana import live_config as _lc
                if self.dex == "pumpswap":
                    _token_age_mins = float(getattr(self, "meta", {}).get("time_since_launch_secs") or 0) / 60
                    if _token_age_mins < 120:
                        _sl_pct = 0.25   # 60-120 min: volatile graduation zone
                    elif _token_age_mins < 360:
                        _sl_pct = 0.20   # 2-6 hr: settling phase
                    else:
                        _sl_pct = float(_lc.get("pumpswap_stop_loss_pct", STOP_LOSS_PCT))
                else:
                    _sl_pct = float(_lc.get("stop_loss_pct", STOP_LOSS_PCT))
            except Exception:
                _sl_pct = STOP_LOSS_PCT
            self.stop_loss_price = self.entry_price_sol * (1 - _sl_pct)
        if self.tp1_price == 0:
            # Per-DEX full-exit TP targets — read from live_config so Jarvis can adjust at runtime.
            # Falls back to module-level constants if live_config not available.
            try:
                from elizaos.plugins.solana import live_config as _lc
                _pf_tp  = _lc.get("pf_tp1_mult", PF_TP1_MULT)
                _gk_tp  = _lc.get("grok_tp_mult", GROK_TP_MULT)
                _ps_tp  = _lc.get("tp1_mult", TP1_MULT)
            except Exception:
                _pf_tp, _gk_tp, _ps_tp = PF_TP1_MULT, GROK_TP_MULT, TP1_MULT
            # Grok Premium:    +50%  (score ≥8/10, 0.3 SOL stake, stall exit at +25%)
            # Grok-confirmed:  +80%  (score ≥7/10 but <8, high social conviction)
            # pump.fun BC:     +45%  (social-approved launch energy)
            # PumpSwap:        +30%  (graduated tokens, quick scalp)
            # Meteora DLMM:    +15%  (stable LP launches, fast scalp)
            # Raydium native:  +20%  (established pairs, conservative)
            if self.moon_mode:
                # Moon mode: trailing stop is the exit, not a fixed TP.
                # Set tp1_price to an astronomically high ceiling (×20 = +1900%) so the fixed-TP
                # branch never fires. The monitor loop uses trailing stop once gain > +100%.
                _tp1 = 20.0
            elif self.grok_premium:
                _tp1 = _lc.get("grok_premium_tp_mult", GROK_PREMIUM_TP_MULT)
            elif self.dex == "social_momentum":
                # Narrative-aware TP — set by Strategy E based on Grok research:
                # Tier1/KOL = 3.00 (+200%), Tier2 = 2.00 (+100%), Tier3 = 1.60 (+60%)
                # narrative_tp_mult set from meta by open_position() before __post_init__ runs
                _tp1 = self.narrative_tp_mult if self.narrative_tp_mult > 1.0 else 1.60
            elif self.grok_confirmed:
                _tp1 = _gk_tp
            elif self.dex == "pump_fun":
                _tp1 = _pf_tp
            elif self.dex == "meteora":
                _tp1 = METEORA_TP1_MULT
            elif self.dex in ("raydium", "native_raydium"):
                _tp1 = RAYDIUM_TP1_MULT
            else:  # pumpswap — AI cascade drives exit, ceiling is safety net
                _tp1 = PUMPSWAP_TP1_MULT
            self.tp1_price = self.entry_price_sol * _tp1
        if self.tp15_price == 0:
            self.tp15_price = self.entry_price_sol * TP15_MULT
        if self.tp2_price == 0:
            # Bonding curve tokens: +150% full close
            # Others: +180% partial sell (moonbag remains)
            _tp2 = PF_TP2_MULT if self.dex == "pump_fun" else TP2_MULT
            self.tp2_price = self.entry_price_sol * _tp2
        if self.tp3_price == 0:
            self.tp3_price = self.entry_price_sol * TP3_MULT
        if self.peak_price == 0:
            self.peak_price = self.entry_price_sol
        if self.trailing_stop_price == 0:
            self.trailing_stop_price = self.peak_price * (1 - TRAILING_STOP_PCT)
        self.stall_price = self.entry_price_sol
        self.stall_since = self.entry_time

    def update_trailing(self, current_price: float) -> None:
        if current_price > self.peak_price:
            self.peak_price = current_price
            # Progressive trailing stop — tightens as gains grow so big runners lock in more
            gain = (current_price - self.entry_price_sol) / self.entry_price_sol if self.entry_price_sol > 0 else 0
            if gain < 1.0:
                trail_pct = 0.12   # below +100%: catch reversals before they erase TP1 profit
            elif gain < 2.0:
                trail_pct = 0.08   # +100–200%: tightening, protect meaningful gains
            else:
                trail_pct = 0.06   # above +200%: very tight, lock in big runner profits
            self.trailing_stop_price = current_price * (1 - trail_pct)

    def age_seconds(self) -> float:
        return time.time() - self.entry_time

    def max_hold_seconds(self) -> int:
        if self.dex == "pump_fun":
            return MAX_HOLD_PUMP_FUN
        elif self.dex == "pumpswap":
            return MAX_HOLD_PUMPSWAP
        elif self.dex == "meteora":
            return MAX_HOLD_METEORA
        elif self.dex == "social_momentum":
            # Tier1/KOL (narrative_tp_mult >= 2.0) → 6h; Tier3 (1.6x) → 90 min
            return MAX_HOLD_SOCIAL_TIER12 if self.narrative_tp_mult >= 2.0 else MAX_HOLD_SOCIAL_TIER3
        else:
            return MAX_HOLD_RAYDIUM

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price_sol <= 0:
            return 0.0
        return (current_price - self.entry_price_sol) / self.entry_price_sol

    def summary(self, current_price: float | None = None) -> str:
        age_min = self.age_seconds() / 60
        lines = [
            f"Mint: {self.mint[:8]}...{self.mint[-4:]}",
            f"DEX: {self.dex}",
            f"Entry: {self.entry_price_sol:.8f} SOL | Spent: {self.entry_sol_spent:.4f} SOL",
            f"SL: {self.stop_loss_price:.8f} | TP: {self.tp1_price:.8f} (+40% full exit)",
            f"Age: {age_min:.1f} min",
        ]
        if current_price is not None:
            pnl = self.pnl_pct(current_price) * 100
            lines.append(f"Current: {current_price:.8f} SOL ({pnl:+.1f}%)")
        return "\n".join(lines)


# ─────────────────────────── service ─────────────────────────────────────────

class PositionManagerService(Service):
    """Quantitative risk manager — runs as a background service."""

    service_type: ClassVar[str] = "position_manager"

    @property
    def capability_description(self) -> str:
        return "Quantitative risk management: position tracking, SL/TP, circuit breakers."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self.positions: dict[str, Position] = {}
        self.daily_pnl_sol: float = 0.0
        self.day_start: float = time.time()
        self.consecutive_losses: int = 0
        self.circuit_broken: bool = False
        self.circuit_break_at: float = 0.0   # timestamp when circuit broke (for cooldown)
        self._monitor_task: asyncio.Task | None = None

        # Tiered circuit breaker (enhanced — 3 levels)
        # Level 1: 3 consec losses same platform → tighten gates, keep trading
        # Level 2: 5 consec losses OR -0.15 SOL → pause PumpSwap 2h, alert
        # Level 3: 7 consec losses OR -0.20 SOL OR wallet<0.15 SOL → full stop
        self._platform_consecutive_losses: dict[str, int] = {}
        self._pumpswap_paused_until: float = 0.0  # unix ts; 0 = not paused
        self._circuit_level: int = 0  # 0=ok, 1=caution, 2=warning, 3=emergency

        # Dashboard state
        self._trade_history: list[dict[str, Any]] = []
        self._equity_snapshots: list[dict[str, Any]] = []
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._activity_log: deque[dict[str, Any]] = deque(maxlen=200)
        self._last_equity_snapshot: float = 0.0
        self._start_equity_sol: float | None = None
        # All mints ever traded in this session — prevents re-entry regardless of cooldown dicts
        self._session_traded_mints: set[str] = set()
        self._liq_monitor_task: asyncio.Task | None = None

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> PositionManagerService:
        svc = cls()
        svc._runtime = runtime
        svc._load_trade_history()
        restored = svc._restore_open_positions()
        # Seed session traded mints from history so we never re-buy after restart
        svc._session_traded_mints = {t["mint"] for t in svc._trade_history}
        # Load permanent mint blacklist — survives trade_history wipes and fresh starts
        svc._load_traded_mints()
        svc._monitor_task = asyncio.create_task(svc._monitor_loop())
        svc._liq_monitor_task = asyncio.create_task(svc._liquidity_monitor_loop())
        runtime.logger.info(
            f"PositionManagerService started (loaded {len(svc._trade_history)} historical trades, "
            f"restored {restored} open positions)",
            src="service:position_manager",
        )
        return svc

    async def stop(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._liq_monitor_task and not self._liq_monitor_task.done():
            self._liq_monitor_task.cancel()
            try:
                await self._liq_monitor_task
            except asyncio.CancelledError:
                pass

    # ─────────────────────── persistence ─────────────────────────────────────

    def _load_traded_mints(self) -> None:
        """Load permanent mint blacklist — never wiped by fresh starts."""
        try:
            path = os.path.realpath(TRADED_MINTS_FILE)
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                self._session_traded_mints.update(data)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def _persist_traded_mint(self, mint: str) -> None:
        """Append mint to the permanent blacklist (non-fatal)."""
        try:
            path = os.path.realpath(TRADED_MINTS_FILE)
            existing: list = []
            try:
                with open(path) as f:
                    existing = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if mint not in existing:
                existing.append(mint)
                with open(path, "w") as f:
                    json.dump(existing, f, indent=2)
        except Exception:
            pass

    def _load_trade_history(self) -> None:
        """Load saved trade history from disk — called at startup."""
        try:
            path = os.path.realpath(TRADE_HISTORY_FILE)
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list):
                self._trade_history = data
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass  # Fresh start

    def _persist_trade(self, trade: dict[str, Any]) -> None:
        """Append the trade dict to the on-disk trade history file (non-fatal)."""
        try:
            path = os.path.realpath(TRADE_HISTORY_FILE)
            existing: list = []
            try:
                with open(path) as f:
                    existing = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            existing.append(trade)
            with open(path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass  # Non-fatal — never crash the bot over logging

    def _restore_open_positions(self) -> int:
        """Reconstruct open Position objects from persisted buy records that have no matching sell.

        Called at startup after _load_trade_history(). Returns number of positions restored.
        Handles both normal positions (key == mint) and split positions (key == mint:A / mint:B).
        """
        # Build set of pos_keys that were sold/closed.
        # Uses stored pos_key field for split positions; falls back to mint for old records.
        closed_pos_keys: set[str] = set()
        for t in self._trade_history:
            if t.get("side") == "sell":
                pk = t.get("pos_key") or t["mint"]
                closed_pos_keys.add(pk)

        # Find the most-recent buy for each open pos_key
        open_buys: dict[str, dict] = {}
        for t in self._trade_history:
            if t.get("side") == "buy":
                pk = t.get("pos_key") or t["mint"]
                if pk not in closed_pos_keys:
                    open_buys[pk] = t  # last buy per pos_key wins

        if not open_buys:
            return 0

        restored = 0
        for pos_key, buy in open_buys.items():
            if pos_key in self.positions:
                continue
            mint = buy["mint"]
            label = buy.get("label", "")
            entry_price = buy.get("entry_price_sol") or buy.get("price_sol") or 0.0
            sol_spent = buy.get("sol_amount", 0.0)
            token_amount = buy.get("token_amount", 0)
            dex = buy.get("dex", "pump_fun")
            decimals = buy.get("token_decimals", 6)
            entry_time = buy.get("timestamp", time.time())

            if entry_price <= 0 or sol_spent <= 0:
                continue  # can't restore without price/size

            # Reconstruct position — skip idempotency guard since we're in __init__ territory
            pos = Position(
                mint=mint,
                dex=dex,
                entry_price_sol=entry_price,
                entry_sol_spent=sol_spent,
                token_amount=token_amount,
                token_decimals=decimals,
                creator_wallet=buy.get("creator_wallet", ""),
                score=buy.get("score", 0),
                fill_pct=buy.get("fill_pct", 0.0),
                label=label,
            )
            pos.entry_time = entry_time
            # Re-derive TP/SL (they'll be correct since __post_init__ uses entry_price_sol)
            self.positions[pos_key] = pos
            restored += 1
            _label_tag = f" [{label}]" if label else ""
            self._log_activity(
                "info",
                f"Restored position{_label_tag} {mint[:8]}... {dex} entry={entry_price:.8f} SOL (from history)"
            )

        return restored

    # ─────────────────────── public interface ────────────────────────────────

    def check_trade_allowed(self, sol_amount: float, wallet_sol: float) -> tuple[bool, str]:
        """Gate check before executing any trade. Returns (allowed, reason)."""
        # Reset daily P&L if a new UTC day has started
        if time.time() - self.day_start > 86400:
            self.daily_pnl_sol = 0.0
            self.day_start = time.time()
            self.circuit_broken = False
            self.consecutive_losses = 0
            self._platform_consecutive_losses = {}
            self._pumpswap_paused_until = 0.0
            self._circuit_level = 0

        # Level 3: wallet below floor → emergency stop
        if wallet_sol > 0 and wallet_sol < 0.15 and self._circuit_level < 3:
            self._circuit_level = 3
            self.circuit_broken = True
            self.circuit_break_at = time.time()
            print(f"[circuit-breaker] 🚨🚨 LEVEL 3 EMERGENCY — wallet {wallet_sol:.4f} SOL < 0.15 SOL floor — ALL TRADING STOPPED")
            try:
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_fl
                import asyncio as _asyncio_fl
                _asyncio_fl.create_task(_tg_fl(
                    f"🚨🚨 <b>CIRCUIT BREAKER LEVEL 3 — WALLET FLOOR HIT</b>\n"
                    f"Wallet: {wallet_sol:.4f} SOL (floor: 0.15 SOL)\nALL strategies halted."
                ))
            except Exception:
                pass

        if self.circuit_broken:
            return False, (
                "Circuit breaker ACTIVE — trading paused. "
                f"Daily P&L: {self.daily_pnl_sol:+.4f} SOL, "
                f"consecutive losses: {self.consecutive_losses}. "
                "Auto-reset in ~5 min or reset manually via /api/control."
            )

        # Daily loss limit (15% per playbook)
        if wallet_sol > 0 and (self.daily_pnl_sol / wallet_sol) < -MAX_DAILY_LOSS_PCT:
            if not self.circuit_broken:
                self.circuit_broken = True
                self.circuit_break_at = time.time()
                self._emit_event("circuit_breaker_on", {"reason": f"daily loss limit hit ({self.daily_pnl_sol:+.4f} SOL)"})
            return False, (
                f"Daily loss limit hit ({self.daily_pnl_sol:+.4f} SOL = "
                f"{(self.daily_pnl_sol/wallet_sol)*100:.1f}%). Trading paused."
            )

        # Max concurrent positions — read live_config so Jarvis can change it at runtime
        from elizaos.plugins.solana import live_config as _lc_pos
        _max_pos = int(_lc_pos.get("max_concurrent_positions") or MAX_CONCURRENT_POSITIONS)
        active_count = sum(1 for p in self.positions.values() if not p.is_reconciled)
        if active_count >= _max_pos:
            return False, (
                f"Max concurrent positions reached ({active_count}/{_max_pos}). "
                "Close existing positions first."
            )

        # Per-trade sizing
        max_sol = wallet_sol * MAX_POSITION_PCT
        if sol_amount > max_sol:
            return False, (
                f"Position too large: {sol_amount:.4f} SOL exceeds {MAX_POSITION_PCT*100:.0f}% of wallet "
                f"({max_sol:.4f} SOL). Reduce to {max_sol:.4f} SOL or less."
            )

        if sol_amount < MIN_POSITION_SOL:
            return False, (
                f"Position too small: {sol_amount:.4f} SOL is below minimum "
                f"{MIN_POSITION_SOL:.4f} SOL (fees would dominate)."
            )

        return True, "OK"

    def is_pumpswap_paused(self) -> tuple[bool, float]:
        """Return (paused, seconds_remaining) for PumpSwap Level 2 pause."""
        if self._pumpswap_paused_until <= 0:
            return False, 0.0
        remaining = self._pumpswap_paused_until - time.time()
        if remaining <= 0:
            self._pumpswap_paused_until = 0.0
            if self._circuit_level == 2:
                self._circuit_level = 0
            return False, 0.0
        return True, remaining

    def open_position(
        self,
        mint: str,
        dex: str,
        entry_price_sol: float,
        entry_sol_spent: float,
        token_amount: int,
        token_decimals: int = 9,
        signature: str = "",
        creator_wallet: str = "",
        score: int = 0,
        fill_pct: float = 0.0,
        meta: dict | None = None,
        grok_confirmed: bool = False,
        grok_premium: bool = False,
        label: str = "",
        _is_split_b: bool = False,
    ) -> Position:
        pos_key = position_key(mint, label)

        # Idempotency guard: prevent async race from opening the same pos_key twice
        if pos_key in self.positions:
            return self.positions[pos_key]

        # Session guard: never re-buy a mint already traded this session.
        # Split-B skips this check — the mint was already claimed by position A.
        if not _is_split_b and mint in self._session_traded_mints:
            raise ValueError(f"Already traded {mint[:8]} this session — skipping re-entry")

        # Hard cap guard: reject if already at max active (non-reconciled) positions.
        # Split-B skips this check — it pairs with A which just claimed a slot.
        if not _is_split_b:
            from elizaos.plugins.solana import live_config as _lc_pos2
            _max_pos2 = int(_lc_pos2.get("max_concurrent_positions") or MAX_CONCURRENT_POSITIONS)
            active_count = sum(1 for p in self.positions.values() if not p.is_reconciled)
            if active_count >= _max_pos2:
                raise ValueError(
                    f"Position cap ({_max_pos2}) already reached; "
                    f"cannot open {mint[:8]}"
                )

        pos = Position(
            mint=mint,
            dex=dex,
            entry_price_sol=entry_price_sol,
            entry_sol_spent=entry_sol_spent,
            token_amount=token_amount,
            token_decimals=token_decimals,
            creator_wallet=creator_wallet,
            score=score,
            fill_pct=fill_pct,
            grok_confirmed=grok_confirmed,
            grok_premium=grok_premium,
            pool_address=(meta or {}).get("pool_address", ""),
            # Strategy E: narrative-aware TP (1.60/2.00/3.00 from Grok classification)
            narrative_tp_mult=float((meta or {}).get("narrative_tp_mult", 1.60)),
            # Moon mode: ultra-fast grad or high DexScreener boost — ride trailing stop
            moon_mode=bool((meta or {}).get("moon_mode", False)),
            entry_liquidity_usd=float((meta or {}).get("liq_usd", 0) or 0),
            holder_wallets=list((meta or {}).get("holder_wallets", [])),
            label=label,
        )
        # pumpswap: 30s grace — DexScreener can be 20-30s stale post-buy, causing immediate
        # false SL fires at -25% entry loss in paper mode (real price != quoted price).
        # All other dexes: 10s grace is sufficient.
        _grace = 30.0 if dex == "pumpswap" else 10.0
        pos.monitoring_starts_at = time.time() + _grace
        self.positions[pos_key] = pos
        if not _is_split_b:
            self._session_traded_mints.add(mint)
            self._persist_traded_mint(mint)  # permanent blacklist — survives fresh starts
        if self._runtime:
            self._runtime.logger.info(
                f"Position opened: {mint[:8]} entry={entry_price_sol:.8f} SOL "
                f"spent={entry_sol_spent:.4f} SOL SL={pos.stop_loss_price:.8f} "
                f"TP={pos.tp1_price:.8f} (+{int((pos.tp1_price/pos.entry_price_sol - 1)*100) if pos.entry_price_sol > 0 else '?'}%)",
                src="service:position_manager",
            )

        # Dashboard: record trade and emit event
        _now_utc = datetime.now(timezone.utc)
        _pair_created_ms = (meta or {}).get("pair_created_at", 0) or 0
        _pair_created_secs = _pair_created_ms / 1000.0 if _pair_created_ms > 0 else 0
        trade = {
            "id": str(uuid.uuid4()),
            "mint": mint,
            "dex": dex,
            "side": "buy",
            "entry_price_sol": entry_price_sol,
            "exit_price_sol": None,
            "sol_amount": entry_sol_spent,
            "token_amount": token_amount,
            "token_decimals": token_decimals,
            "pnl_pct": None,
            "pnl_sol": None,
            "reason": "entry",
            "timestamp": time.time(),
            "signature": signature,
            "creator_wallet": creator_wallet,
            "score": score,
            "fill_pct": fill_pct,
            "meta": meta or {},
            # ── Buy timing analytics ──────────────────────────────────────────
            "entry_utc": _now_utc.isoformat(),
            "day_of_week": _now_utc.strftime("%A"),
            "hour_of_day_utc": _now_utc.hour,
            "pair_created_utc": datetime.fromtimestamp(_pair_created_secs, tz=timezone.utc).isoformat() if _pair_created_secs > 0 else None,
            "time_since_launch_secs": round(time.time() - _pair_created_secs) if _pair_created_secs > 0 else None,
            "token_name": (meta or {}).get("name", ""),
            "token_symbol": (meta or {}).get("symbol", ""),
            "liq_usd_at_entry": float((meta or {}).get("liq_usd", 0) or 0),
            "vol_liq_ratio_at_entry": float((meta or {}).get("vol_liq_ratio", 0) or 0),
            # Split-buy: label and composite pos_key so _restore_open_positions can reconstruct
            "label": label,
            "pos_key": pos_key,
        }
        self._trade_history.append(trade)
        self._persist_trade(trade)  # persist buys so positions survive restarts
        pos_data = self._serialize_position(pos)
        self._emit_event("position_opened", pos_data)
        _label_tag = f" [{label}]" if label else ""
        self._log_activity("success", f"Opened {dex} position{_label_tag}: {mint[:8]}... for {entry_sol_spent:.4f} SOL")
        return pos

    def extend_position(
        self,
        mint: str,
        extra_sol: float,
        extra_tokens: int,
        current_price: float,
        signature: str = "",
    ) -> None:
        """Add to an existing open position (second tranche of staged entry).

        Recalculates average entry price, SL, and TP levels from the new totals.
        Records the add-on as a 'buy' entry in trade history.
        Non-fatal if position no longer exists (already closed by SL/TP).
        """
        pos = self.positions.get(mint)
        if pos is None:
            return  # position closed before second tranche fired — no-op

        total_tokens = pos.token_amount + extra_tokens
        total_sol = pos.entry_sol_spent + extra_sol
        pos.token_amount = total_tokens
        pos.entry_sol_spent = total_sol

        # Weighted average entry price
        if total_tokens > 0:
            pos.entry_price_sol = total_sol / (total_tokens / 10 ** pos.token_decimals)

        # Recalculate SL/TP from new average entry
        pos.stop_loss_price = pos.entry_price_sol * (1 - STOP_LOSS_PCT)
        if pos.grok_confirmed:
            _tp1 = GROK_TP_MULT
        elif pos.dex == "pump_fun":
            _tp1 = PF_TP1_MULT
        elif pos.dex == "meteora":
            _tp1 = METEORA_TP1_MULT
        elif pos.dex in ("raydium", "native_raydium"):
            _tp1 = RAYDIUM_TP1_MULT
        else:  # pumpswap — AI cascade drives exit
            _tp1 = PUMPSWAP_TP1_MULT
        pos.tp1_price = pos.entry_price_sol * _tp1
        pos.tp2_price = pos.entry_price_sol * TP2_MULT  # legacy — rarely reached with conservative TPs
        pos.trailing_stop_price = pos.peak_price * (1 - TRAILING_STOP_PCT)

        # Record the add-on trade in history
        trade = {
            "id": str(uuid.uuid4()),
            "mint": mint,
            "dex": pos.dex,
            "side": "buy",
            "entry_price_sol": current_price,
            "exit_price_sol": None,
            "sol_amount": extra_sol,
            "token_amount": extra_tokens,
            "token_decimals": pos.token_decimals,
            "pnl_pct": None,
            "pnl_sol": None,
            "reason": "add_to_position",
            "timestamp": time.time(),
            "signature": signature,
            "score": pos.score,
            "fill_pct": pos.fill_pct,
            "meta": {},
        }
        self._trade_history.append(trade)
        self._persist_trade(trade)
        self._log_activity(
            "success",
            f"Extended {pos.dex} position: {mint[:8]}... +{extra_sol:.4f} SOL "
            f"(avg entry now {pos.entry_price_sol:.8f} SOL)",
        )

    def close_position(self, mint: str, exit_price_sol: float, reason: str, signature: str = "", _pos_key: str = "") -> None:
        _key = _pos_key if _pos_key else mint
        pos = self.positions.pop(_key, None)
        if pos is None:
            return
        pnl = (exit_price_sol - pos.entry_price_sol) / pos.entry_price_sol if pos.entry_price_sol > 0 else 0
        pnl_sol = pnl * pos.entry_sol_spent

        # Deduct known transaction costs so P&L reflects actual wallet impact:
        # - Jito priority fee on buy (strategy B/C/D/E: 0.002 SOL; A2/BC: 0.0005 SOL)
        # - Jupiter routing fee on sell (approx 0.3% of SOL value)
        _buy_fee = 0.002 if pos.dex in ("pumpswap", "raydium", "meteora", "native_raydium") else 0.0005
        _sell_fee = pos.entry_sol_spent * 0.003
        pnl_sol -= (_buy_fee + _sell_fee)

        # Sanity cap: guard against obviously corrupted prices (±10 SOL)
        pnl_sol = max(-10.0, min(10.0, pnl_sol))
        self.daily_pnl_sol += pnl_sol

        # Only count as a consecutive loss if pnl is meaningfully negative (< -2%).
        # Break-even stall exits (±0%) should NOT trip the circuit breaker —
        # they are neutral outcomes, not losing trades.
        _is_loss = pnl < -0.02 and reason in (
            "stop_loss", "early_stop_loss", "time_limit",
            "stall_exit", "stagnant_exit", "trailing_stop_loss",
        )
        if _is_loss:
            self.consecutive_losses += 1
            _pos_dex = pos.dex if pos else "unknown"
            self._platform_consecutive_losses[_pos_dex] = (
                self._platform_consecutive_losses.get(_pos_dex, 0) + 1
            )
        elif pnl > 0 or reason not in (
            "stop_loss", "early_stop_loss", "time_limit",
            "stall_exit", "stagnant_exit", "trailing_stop_loss",
        ):
            self.consecutive_losses = 0
            if pos:
                self._platform_consecutive_losses[pos.dex] = 0
            # Win clears Level 1 caution (platform-specific streak reset)
            if self._circuit_level == 1:
                self._circuit_level = 0
        # else: pnl in [-2%, 0%] → no change to streak (neutral, keep counting)

        # ── Tiered circuit breaker ────────────────────────────────────────────
        _consec_total = self.consecutive_losses
        _consec_pumpswap = self._platform_consecutive_losses.get("pumpswap", 0)
        _session_loss_sol = abs(min(0.0, self.daily_pnl_sol))  # positive = loss amount

        # Level 3: 7 consec OR -0.20 SOL session loss → EMERGENCY STOP ALL
        if (_consec_total >= 7 or _session_loss_sol >= 0.20) and not self.circuit_broken and self._circuit_level < 3:
            self._circuit_level = 3
            self.circuit_broken = True
            self.circuit_break_at = time.time()
            _lvl3_reason = f"{_consec_total} consecutive losses" if _consec_total >= 7 else f"-{_session_loss_sol:.3f} SOL session loss"
            print(f"[circuit-breaker] 🚨🚨 LEVEL 3 EMERGENCY — {_lvl3_reason} — ALL TRADING STOPPED")
            try:
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_cb
                import asyncio as _asyncio_cb
                _asyncio_cb.create_task(_tg_cb(
                    f"🚨🚨 <b>CIRCUIT BREAKER LEVEL 3 — EMERGENCY STOP</b>\n"
                    f"Reason: {_lvl3_reason}\n"
                    f"ALL strategies halted. Manual restart required."
                ))
            except Exception:
                pass
            self._emit_event("circuit_breaker_on", {"reason": _lvl3_reason, "level": 3})

        # Level 2: 5 consec OR -0.15 SOL → pause PumpSwap 2h
        elif (_consec_total >= 5 or _session_loss_sol >= 0.15) and self._circuit_level < 2:
            self._circuit_level = 2
            _pause_secs = 7200  # 2 hours
            self._pumpswap_paused_until = time.time() + _pause_secs
            _lvl2_reason = f"{_consec_total} consecutive losses" if _consec_total >= 5 else f"-{_session_loss_sol:.3f} SOL session loss"
            print(f"[circuit-breaker] 🚨 LEVEL 2 WARNING — {_lvl2_reason} — PumpSwap paused 2h")
            try:
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_cb
                import asyncio as _asyncio_cb
                _asyncio_cb.create_task(_tg_cb(
                    f"🚨 <b>CIRCUIT BREAKER LEVEL 2 — PUMPSWAP PAUSED</b>\n"
                    f"Reason: {_lvl2_reason}\n"
                    f"PumpSwap auto-paused 2h. Raydium/Meteora continue."
                ))
            except Exception:
                pass

        # Level 1: 3 consec losses on pumpswap → alert + tighten gates
        elif _consec_pumpswap >= 3 and self._circuit_level < 1:
            self._circuit_level = 1
            print(f"[circuit-breaker] ⚠️ LEVEL 1 CAUTION — {_consec_pumpswap} consecutive PumpSwap losses — gates tightened")
            try:
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_cb
                import asyncio as _asyncio_cb
                _asyncio_cb.create_task(_tg_cb(
                    f"⚠️ <b>CIRCUIT BREAKER LEVEL 1 — CAUTION</b>\n"
                    f"{_consec_pumpswap} consecutive PumpSwap losses.\n"
                    f"Gate thresholds tightened 20%. Still trading."
                ))
            except Exception:
                pass

        # Legacy full stop at MAX_CONSECUTIVE_LOSSES (10)
        if _consec_total >= MAX_CONSECUTIVE_LOSSES and not self.circuit_broken:
            self.circuit_broken = True
            self.circuit_break_at = time.time()
            self._emit_event("circuit_breaker_on", {"reason": f"{_consec_total} consecutive losses"})

        if self._runtime:
            self._runtime.logger.info(
                f"Position closed: {mint[:8]} reason={reason} "
                f"pnl={pnl*100:+.1f}% ({pnl_sol:+.4f} SOL) "
                f"daily_pnl={self.daily_pnl_sol:+.4f} SOL",
                src="service:position_manager",
            )

        # Dashboard: record trade and emit event
        # pnl_pct uses fee-adjusted SOL so dashboard shows realistic net return
        _pnl_pct_adj = (pnl_sol / pos.entry_sol_spent * 100) if pos.entry_sol_spent > 0 else pnl * 100
        # Classify outcome
        if reason in ("rug_stop_loss",) or _pnl_pct_adj <= -40:
            _outcome = "rug"
        elif _pnl_pct_adj >= 0:
            _outcome = "win"
        else:
            _outcome = "loss"

        # Look up the matching buy record to carry entry DNA into the sell record.
        # This lets Jarvis see WHY a trade was bad — liq, vol/liq, age, h1, m5, buy_ratio.
        _buy_rec = next(
            (t for t in reversed(self._trade_history)
             if t.get("mint") == mint and t.get("side") == "buy"),
            None,
        )
        _buy_meta = (_buy_rec or {}).get("meta") or {}
        _is_paper = str((_buy_rec or {}).get("signature", "")).startswith("PAPER_")

        trade = {
            "id": str(uuid.uuid4()),
            "mint": mint,
            "dex": pos.dex,
            "side": "sell",
            "entry_price_sol": pos.entry_price_sol,
            "exit_price_sol": exit_price_sol,
            "sol_amount": pos.entry_sol_spent,
            "pnl_pct": round(_pnl_pct_adj, 2),
            "pnl_sol": round(pnl_sol, 6),
            "reason": reason,
            "timestamp": time.time(),
            "exit_utc": datetime.now(timezone.utc).isoformat(),
            "signature": signature,
            "creator_wallet": pos.creator_wallet,
            "hold_secs": round(time.time() - pos.entry_time, 1),
            "score": pos.score,
            "fill_pct": pos.fill_pct,
            "outcome": _outcome,
            "peak_pnl_pct": round((pos.peak_price / pos.entry_price_sol - 1) * 100, 1) if pos.entry_price_sol > 0 else None,
            # ── Entry DNA carried from buy record ────────────────────────────────
            "paper_trade": _is_paper,
            "token_symbol": (_buy_rec or {}).get("token_symbol") or _buy_meta.get("symbol", ""),
            "token_name": (_buy_rec or {}).get("token_name") or _buy_meta.get("name", ""),
            "liq_usd_at_entry": (_buy_rec or {}).get("liq_usd_at_entry") or float(_buy_meta.get("liq_usd", 0) or 0),
            "vol_liq_ratio_at_entry": (_buy_rec or {}).get("vol_liq_ratio_at_entry") or float(_buy_meta.get("vol_liq_ratio", 0) or 0),
            "time_since_launch_secs": (_buy_rec or {}).get("time_since_launch_secs"),
            "entry_utc": (_buy_rec or {}).get("entry_utc"),
            "pair_created_utc": (_buy_rec or {}).get("pair_created_utc"),
            "h1_pct_at_entry": float(_buy_meta.get("price_change_h1", 0) or 0),
            "m5_pct_at_entry": float(_buy_meta.get("price_change_m5", 0) or 0),
            "buy_ratio_at_entry": float(_buy_meta.get("buy_ratio", 0) or 0),
            "has_socials": bool(_buy_meta.get("has_socials", False)),
            "score_reasons": _buy_meta.get("score_reasons", []),
        }
        self._trade_history.append(trade)
        self._persist_trade(trade)
        self._emit_event("position_closed", trade)
        level = "success" if pnl >= 0 else "warning"
        self._log_activity(level, f"Closed {mint[:8]}... reason={reason} P&L={pnl*100:+.1f}% ({pnl_sol:+.4f} SOL)")
        # Write to trading journal so Jarvis can learn from every trade
        try:
            from elizaos.plugins.solana.trading_journal_writer import record_trade as _jrn_rec, update_bad_token_dna as _jrn_bad
            _jrn_rec(trade)
            _jrn_bad(trade)
        except Exception:
            pass
        self._emit_event("risk_update", self.get_risk_summary())

        # ── Post-exit price tracker — checks DexScreener at +30m/1h/4h/24h ─────
        try:
            from elizaos.plugins.solana.post_exit_tracker import record_exit as _rec_exit
            _rec_exit(
                mint=mint,
                exit_price_sol=exit_price_sol,
                exit_reason=reason,
                pnl_pct=_pnl_pct_adj,
                dex=pos.dex,
                entry_price_sol=pos.entry_price_sol,
                extra={"score": pos.score, "outcome": _outcome, "hold_secs": round(time.time() - pos.entry_time, 1)},
            )
        except Exception:
            pass  # non-fatal

        # ── Smart money wallet tracking ───────────────────────────────────────
        if pos.holder_wallets:
            try:
                from elizaos.plugins.solana.smart_money_tracker import (
                    record_win_appearances as _sm_win,
                    record_all_appearances as _sm_all,
                )
                if _outcome == "win":
                    _sm_win(mint, pos.holder_wallets)
                else:
                    _sm_all(mint, pos.holder_wallets)
            except Exception:
                pass

        # ── Developer reputation tracking ─────────────────────────────────────
        if pos.creator_wallet:
            try:
                from elizaos.plugins.solana.dev_reputation import get_reputation as _get_rep
                _rep = _get_rep()
                _hold = round(time.time() - pos.entry_time, 1)
                _outcome = _rep.classify_outcome(_pnl_pct_adj / 100, _hold, reason)
                _rep.record_outcome(
                    pos.creator_wallet, mint, _outcome,
                    pnl_pct=_pnl_pct_adj, hold_secs=_hold, dex=pos.dex,
                )
            except Exception:
                pass  # Non-fatal — never break position close

    def notify_partial_sell(self, mint: str, sold_fraction: float, exit_price_sol: float, signature: str = "", _pos_key: str = "") -> None:
        """Call after a TP partial sell to update position state."""
        pos = self.positions.get(_pos_key if _pos_key else mint)
        if not pos:
            return
        original_sol_spent = pos.entry_sol_spent
        pnl_sol = (exit_price_sol - pos.entry_price_sol) / pos.entry_price_sol * original_sol_spent * sold_fraction
        # Deduct Jupiter routing fee (0.3%) for partial sells
        pnl_sol -= original_sol_spent * sold_fraction * 0.003
        pnl_sol = max(-10.0, min(10.0, pnl_sol))
        self.daily_pnl_sol += pnl_sol
        pos.sell_fail_count = 0  # reset on successful sell
        sold_amount = int(pos.token_amount * sold_fraction)
        pos.token_amount = int(pos.token_amount * (1 - sold_fraction))
        pos.entry_sol_spent *= (1 - sold_fraction)

        # Dashboard: record partial trade
        trade = {
            "id": str(uuid.uuid4()),
            "mint": mint,
            "dex": pos.dex,
            "side": "sell",
            "entry_price_sol": pos.entry_price_sol,
            "exit_price_sol": exit_price_sol,
            "sol_amount": round(pnl_sol + original_sol_spent * sold_fraction, 6),
            "pnl_pct": round(((exit_price_sol - pos.entry_price_sol) / pos.entry_price_sol) * 100, 2) if pos.entry_price_sol > 0 else 0,
            "pnl_sol": round(pnl_sol, 6),
            "reason": "tp_partial",
            "timestamp": time.time(),
            "signature": signature,
        }
        self._trade_history.append(trade)
        self._persist_trade(trade)  # persist partial sells so they survive restarts
        level_name = "TP1" if not pos.tp1_hit else ("TP2" if not pos.tp2_hit else "TP3")
        self._emit_event("partial_sell", {"mint": mint, "level": level_name, "fraction": sold_fraction, "exit_price_sol": exit_price_sol})
        self._log_activity("success", f"Partial sell {level_name}: {mint[:8]}... {sold_fraction*100:.0f}% at {exit_price_sol:.8f} SOL")

    def positions_summary(self) -> str:
        if not self.positions:
            return "No open positions."
        lines = [f"Open positions: {len(self.positions)}"]
        for mint, pos in self.positions.items():
            age_min = pos.age_seconds() / 60
            tp_status = []
            if pos.tp1_hit:
                tp_status.append("TP1✓")
            if pos.tp2_hit:
                tp_status.append("TP2✓")
            if pos.tp3_hit:
                tp_status.append("TP3✓")
            status = " ".join(tp_status) if tp_status else "holding"
            lines.append(
                f"  {mint[:8]}...{mint[-4:]} | {pos.dex} | "
                f"entry {pos.entry_price_sol:.8f} SOL | "
                f"SL {pos.stop_loss_price:.8f} | age {age_min:.0f}m | {status}"
            )
        lines.append(
            f"Daily P&L: {self.daily_pnl_sol:+.4f} SOL | "
            f"Consecutive losses: {self.consecutive_losses} | "
            f"Circuit breaker: {'ACTIVE ⛔' if self.circuit_broken else 'OK ✅'}"
        )
        return "\n".join(lines)

    # ─────────────────────── dashboard helpers ─────────────────────────────

    def _serialize_position(self, pos: Position, current_price: float | None = None) -> dict[str, Any]:
        """Serialize a Position into a JSON-safe dict for the dashboard."""
        cp = current_price or pos.last_known_price or pos.entry_price_sol
        return {
            "mint": pos.mint,
            "dex": pos.dex,
            "entry_price_sol": pos.entry_price_sol,
            "entry_sol_spent": pos.entry_sol_spent,
            "token_amount": pos.token_amount,
            "token_decimals": pos.token_decimals,
            "stop_loss_price": pos.stop_loss_price,
            "tp1_price": pos.tp1_price,
            "tp2_price": pos.tp2_price,
            "tp3_price": pos.tp3_price,
            "peak_price": pos.peak_price,
            "trailing_stop_price": pos.trailing_stop_price,
            "tp1_hit": pos.tp1_hit,
            "tp2_hit": pos.tp2_hit,
            "tp3_hit": pos.tp3_hit,
            "entry_time": pos.entry_time,
            "age_seconds": pos.age_seconds(),
            "current_price_sol": cp,
            "pnl_pct": round(pos.pnl_pct(cp) * 100, 2),
            "unrealized_pnl_sol": round(pos.pnl_pct(cp) * pos.entry_sol_spent, 6),
            "creator_wallet": pos.creator_wallet,
            "score": pos.score,
            "fill_pct": pos.fill_pct,
        }

    def _emit_event(self, event_type: str, data: Any) -> None:
        """Push an event onto the WebSocket queue AND dispatch to runtime event handlers."""
        try:
            self._event_queue.put_nowait({"type": event_type, "data": data})
        except asyncio.QueueFull:
            pass  # discard if queue is somehow full
        # Also dispatch to registered runtime event handlers (Jarvis, Telegram, etc.)
        if self._runtime is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._runtime.emit_event(event_type, data))
            except RuntimeError:
                pass  # No running event loop

    def _log_activity(self, level: str, message: str) -> None:
        """Append an entry to the activity log."""
        self._activity_log.appendleft({
            "timestamp": time.time(),
            "level": level,
            "message": message,
        })

    def get_trade_history(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return paginated trade history (newest first)."""
        reversed_history = list(reversed(self._trade_history))
        return reversed_history[offset:offset + limit]

    def get_equity_snapshots(self, since: float = 0) -> list[dict[str, Any]]:
        """Return equity snapshots since the given timestamp."""
        return [s for s in self._equity_snapshots if s["timestamp"] >= since]

    def get_activity_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent activity entries."""
        return list(self._activity_log)[:limit]

    def get_risk_summary(self) -> dict[str, Any]:
        """Return circuit breaker and risk metrics."""
        broken_secs = (time.time() - self.circuit_break_at) if self.circuit_broken and self.circuit_break_at else 0
        _ps_rem = max(0.0, self._pumpswap_paused_until - time.time()) if self._pumpswap_paused_until > 0 else 0.0
        return {
            "daily_pnl_sol": round(self.daily_pnl_sol, 6),
            "consecutive_losses": self.consecutive_losses,
            "circuit_broken": self.circuit_broken,
            "circuit_break_at": self.circuit_break_at,
            "circuit_broken_secs": round(broken_secs),
            "day_start": self.day_start,
            "open_position_count": len(self.positions),
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT * 100,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "circuit_level": self._circuit_level,
            "platform_consecutive_losses": dict(self._platform_consecutive_losses),
            "pumpswap_paused_remaining_secs": round(_ps_rem),
        }

    async def reconcile_wallet_positions(self) -> int:
        """Reconcile orphaned on-chain token balances into open positions.

        Queries the wallet for all token balances. Any token not already tracked
        as an open position is imported as a 'recovered' position with current
        price as the entry price (best effort).  This handles the case where the
        bot restarts without persisted buy records (e.g. pre-fix deployments).
        Returns number of new positions created.
        """
        if self._runtime is None:
            return 0
        try:
            from elizaos.types import ServiceTypeRegistry
            from elizaos.plugins.solana.services.wallet import SolanaWalletService
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)
            if not isinstance(wallet_svc, SolanaWalletService):
                return 0
            balances = await wallet_svc.get_token_balances()
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.warning(
                    f"reconcile_wallet_positions: failed to get balances: {exc}",
                    src="service:position_manager",
                )
            return 0

        added = 0
        for bal in balances:
            mint = bal.get("mint", "")
            raw_amount = bal.get("raw_amount", 0)
            decimals = bal.get("decimals", 6)
            if not mint or raw_amount <= 0 or mint in self.positions:
                continue

            # Skip mints already tracked by the copy trade monitor.
            # Read directly from the copy_trade_paper_open.json file — the in-memory
            # _paper_positions dict is not populated yet at startup when this reconciler
            # runs, so a file check is the only reliable way to avoid duplicates.
            # Duplicate tracking causes: strategy SL fires bogus Jupiter sell (BC tokens
            # not on Jupiter) → pairs=null → false "POSITION CLOSED" Telegram.
            try:
                import os as _os, json as _json
                _ct_path = _os.path.join(_os.path.dirname(__file__), "..", "copy_trade_paper_open.json")
                _ct_path = _os.path.normpath(_ct_path)
                if _os.path.exists(_ct_path):
                    _ct_data = _json.load(open(_ct_path))
                    _ct_mints = set(_ct_data.get("positions", {}).keys())
                    if mint in _ct_mints:
                        if self._runtime:
                            self._runtime.logger.info(
                                f"reconcile: skipping {mint[:8]} — already in copy_trade_paper_open.json",
                                src="service:position_manager",
                            )
                        continue
            except Exception:
                pass

            # Monster strategy owns its own exits (TP1 + event-driven moonbag).
            # Reconciling a monster leftover as an orphan gave position_manager a
            # reset entry_time, which fired stall_exit 15 min later and killed
            # CATEROID's +100% moonbag (2026-04-22).
            try:
                import os as _os, json as _json
                _mon_path = _os.path.join(_os.path.dirname(__file__), "..", "monster_positions.json")
                _mon_path = _os.path.normpath(_mon_path)
                if _os.path.exists(_mon_path):
                    _mon_data = _json.load(open(_mon_path))
                    if mint in _mon_data:
                        if self._runtime:
                            self._runtime.logger.info(
                                f"reconcile: skipping {mint[:8]} — monster-managed (in monster_positions.json)",
                                src="service:position_manager",
                            )
                        continue
            except Exception:
                pass

            # Skip mints that have a sell record — position was properly closed.
            # Check this BEFORE _session_traded_mints: a mint can be in traded_mints.json
            # (meaning we bought it once) but if the bot crashed before the sell was recorded,
            # _session_traded_mints would incorrectly hide the orphaned on-chain balance.
            was_sold = any(t.get("mint") == mint and t.get("side") == "sell"
                           for t in self._trade_history[-500:])
            if was_sold:
                continue

            # Only skip traded-mints blacklist if there's NO buy record either
            # (meaning it's a legacy/dead token from a very old session with no trade history).
            hist_any_buy = any(t.get("mint") == mint and t.get("side") == "buy"
                               for t in self._trade_history[-500:])
            if not hist_any_buy and mint in self._session_traded_mints:
                # No buy record and no sell record — old dead token, skip
                continue

            # If there's a historical buy record, use its entry price and metadata so
            # TP/SL levels are correct (not relative to current market price).
            hist_buy = next(
                (t for t in reversed(self._trade_history)
                 if t.get("mint") == mint and t.get("side") == "buy"),
                None,
            )

            # Get current price via bonding curve first, then DexScreener as fallback.
            # CRITICAL: do NOT use a 1e-8 sentinel — it causes all TPs to fire immediately.
            # If we can't determine a real price, skip the position (prefer missing it
            # over firing bogus TPs that attempt to sell at wrong pool/price).
            entry_price = 0.0
            dex_guess = "pump_fun"
            try:
                from elizaos.plugins.solana.services.pump_fun import PumpFunService
                pump_svc = self._runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
                if isinstance(pump_svc, PumpFunService):
                    bc = await pump_svc.get_bonding_curve(mint)
                    if bc and bc.get("price_sol", 0.0) > 0:
                        entry_price = bc["price_sol"]
                        if bc.get("complete", False):
                            dex_guess = "pumpswap"
            except Exception:
                pass

            if entry_price <= 0:
                # Bonding curve unavailable — try DexScreener (works for graduated/Raydium tokens)
                try:
                    from elizaos.plugins.solana.services.raydium import RaydiumService
                    raydium_svc = self._runtime.get_service(ServiceTypeRegistry.LP_POOL)
                    if isinstance(raydium_svc, RaydiumService):
                        entry_price = await raydium_svc.get_price(mint, vs_token="SOL", cache_ttl=0)
                        if entry_price > 0:
                            dex_guess = "pumpswap"  # DexScreener priced → likely graduated
                except Exception:
                    pass

            if entry_price <= 0:
                # Can't determine price — skip this position rather than create a bogus one
                if self._runtime:
                    self._runtime.logger.warning(
                        f"reconcile_wallet_positions: skipping {mint[:8]} — no price available",
                        src="service:position_manager",
                    )
                continue

            # If a historical buy record is available, prefer its entry price and
            # original SOL spent so TP/SL levels reflect the real purchase.
            amount_float = raw_amount / (10 ** decimals)
            if hist_buy and hist_buy.get("entry_price_sol", 0.0) > 0:
                hist_entry = hist_buy["entry_price_sol"]
                hist_spent = hist_buy.get("sol_amount", amount_float * hist_entry)
                hist_dex   = hist_buy.get("dex", dex_guess)
                sol_spent  = hist_spent
                if self._runtime:
                    self._runtime.logger.info(
                        f"reconcile_wallet_positions: {mint[:8]} — using historical entry "
                        f"{hist_entry:.2e} SOL (current {entry_price:.2e}) from trade_history",
                        src="service:position_manager",
                    )
                entry_price = hist_entry
                dex_guess   = hist_dex
            else:
                sol_spent = amount_float * entry_price

            # Estimate SOL value and skip dust positions.
            # PumpPortal HTTP-200 "sell test" is NOT a valid liquidity check — it just
            # means PumpPortal can build the transaction.  The on-chain execution almost
            # always fails for old illiquid tokens, burning 0.001–0.01 SOL in priority
            # fees per retry cycle.  Skip any reconciled position worth < 0.002 SOL —
            # the fees to attempt selling it would exceed its value.
            if sol_spent < 0.002:
                if self._runtime:
                    self._runtime.logger.info(
                        f"reconcile_wallet_positions: skipping {mint[:8]} — dust value {sol_spent:.6f} SOL (< 0.002 SOL threshold)",
                        src="service:position_manager",
                    )
                continue

            pos = Position(
                mint=mint,
                dex=dex_guess,
                entry_price_sol=entry_price,
                entry_sol_spent=sol_spent,
                token_amount=raw_amount,
                token_decimals=decimals,
                is_reconciled=True,
            )
            self.positions[mint] = pos
            source_note = "from trade_history" if hist_buy else "current market price"
            _orphan_msg = (
                f"⚠️ ORPHANED POSITION DETECTED\n"
                f"Token: {mint[:8]}...\n"
                f"Amount: {amount_float:.0f} tokens ({sol_spent:.4f} SOL estimated)\n"
                f"Entry: ≈{entry_price:.2e} SOL ({source_note})\n"
                f"DEX: {dex_guess.upper()}\n"
                f"Applying emergency SL monitoring."
            )
            print(f"[reconcile] {_orphan_msg}")
            self._log_activity("warning", f"ORPHANED POSITION: {mint[:8]}... {amount_float:.0f} tokens — emergency monitoring active")
            # Fire Telegram alert immediately — do not wait for event system
            try:
                import asyncio as _asyncio
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_alert
                _asyncio.create_task(_tg_alert(f"🚨 {_orphan_msg}"))
            except Exception:
                pass
            added += 1

        if added > 0 and self._runtime:
            self._runtime.logger.info(
                f"reconcile_wallet_positions: imported {added} orphaned position(s)",
                src="service:position_manager",
            )
        return added

    def force_reset_circuit_breaker(self, reason: str = "manual") -> None:
        """Reset the circuit breaker — called by smart reset loop after LLM approval."""
        self.circuit_broken = False
        self.circuit_break_at = 0.0
        self.consecutive_losses = 0
        if self._runtime:
            self._runtime.logger.info(
                f"Circuit breaker RESET: {reason}",
                src="service:position_manager",
            )
        self._log_activity("success", f"Circuit breaker reset: {reason}")
        self._emit_event("circuit_breaker_off", {"reason": reason})

    def serialize_positions(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Serialize all open positions with optional current prices."""
        result = {}
        for mint, pos in self.positions.items():
            cp = prices.get(mint) if prices else None
            result[mint] = self._serialize_position(pos, cp)
        return result

    def get_config(self) -> dict[str, Any]:
        """Return bot risk-management configuration."""
        return {
            "max_position_pct": MAX_POSITION_PCT,
            "min_position_sol": MIN_POSITION_SOL,
            "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
            "stop_loss_pct": STOP_LOSS_PCT,
            "tp1_mult": TP1_MULT,
            "tp15_mult": TP15_MULT,
            "tp2_mult": TP2_MULT,
            "tp3_mult": TP3_MULT,
            "trailing_stop_pct": TRAILING_STOP_PCT,
            "stall_time_hours": STALL_TIME_SECS / 3600,
            "stall_threshold_pct": STALL_THRESHOLD * 100,
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "max_hold_pump_fun_hours": MAX_HOLD_PUMP_FUN / 3600,
            "max_hold_pumpswap_hours": MAX_HOLD_PUMPSWAP / 3600,
            "max_hold_raydium_hours": MAX_HOLD_RAYDIUM / 3600,
            "monitor_interval_secs": MONITOR_INTERVAL_SECS,
            "early_stop_loss_pct": EARLY_STOP_LOSS_PCT,
            "early_stop_loss_secs": EARLY_STOP_LOSS_SECS,
        }

    async def check_token_safety(self, mint: str) -> tuple[bool, str]:
        """Check mint/freeze authority on-chain + LP lock via rugcheck.xyz. Returns (safe, reason)."""
        try:
            from elizaos.plugins.solana.services.wallet import SolanaWalletService
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)  # type: ignore
            if not isinstance(wallet_svc, SolanaWalletService):
                return True, "wallet service unavailable — skipping filter"

            rpc = wallet_svc.rpc
            account = await rpc.get_account_info(mint, encoding="jsonParsed")
            if account is None:
                return False, f"Mint account not found: {mint}"

            data = account.get("data", {})
            parsed = (
                data.get("parsed", {}).get("info", {})
                if isinstance(data, dict) else {}
            )

            # Freeze authority must be null (burned)
            freeze_auth = parsed.get("freezeAuthority")
            if freeze_auth is not None:
                return False, f"Freeze authority NOT burned ({freeze_auth[:8]}...) — rug risk"

            # Mint authority must be null (burned) — no new token minting
            mint_auth = parsed.get("mintAuthority")
            if mint_auth is not None:
                return False, f"Mint authority NOT burned ({mint_auth[:8]}...) — inflation risk"

        except Exception as exc:
            # Non-fatal — log and allow with warning
            if self._runtime:
                self._runtime.logger.warning(
                    f"Token safety check failed for {mint}: {exc}",
                    src="service:position_manager",
                )
            return True, f"Safety check error (allowing with warning): {exc}"

        # ── LP lock check via rugcheck.xyz (non-blocking, 4s timeout) ────────
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        rug_data = await resp.json()
                        risks = rug_data.get("risks", [])
                        # Block danger-level LP risks (unlocked LP = rug mechanism)
                        danger_risks = [r["name"] for r in risks if r.get("level") == "danger"]
                        lp_risks = [r for r in danger_risks if "liquidity" in r.lower() or "lp" in r.lower()]
                        if lp_risks:
                            return False, f"LP not locked ({', '.join(lp_risks)}) — rug mechanism active"
                        # Block danger-level bundled/concentration risks (SpaceX 99%, Quality 90% pattern)
                        # Research finding 2026-04-04: extreme concentration = instant rug
                        bundle_risks = [
                            r for r in danger_risks
                            if any(kw in r.lower() for kw in ["bundl", "concentration", "insider", "single holder"])
                        ]
                        if bundle_risks:
                            return False, f"Concentrated ownership ({', '.join(bundle_risks)}) — rug risk"
                        # Block warn-level LP *unlocked* warnings — dev can drain unlocked pools.
                        # Do NOT block on "Low amount of LP Providers" or "Low Liquidity" —
                        # these are universal on freshly graduated tokens and have no rug signal.
                        warn_risks = [r["name"] for r in risks if r.get("level") == "warn"]
                        lp_unlock_warn = [
                            r for r in warn_risks
                            if ("unlocked" in r.lower() or "not locked" in r.lower())
                            and ("lp" in r.lower() or "liquidity" in r.lower())
                        ]
                        if lp_unlock_warn:
                            return False, f"LP unlock warning ({', '.join(lp_unlock_warn)}) — potential rug vector"
                        # Overall risk score — threshold is Jarvis-adjustable via live_config
                        rug_score = rug_data.get("score", 0)
                        from elizaos.plugins.solana import live_config as _lc_rug
                        _rug_max = int(_lc_rug.get("rugcheck_max_score", 8000))
                        if rug_score > _rug_max:
                            return False, f"rugcheck score {rug_score} (>{_rug_max}) — high risk"
                        # Top-holder concentration check (research finding 2026-04-04)
                        # Normal pump.fun graduation: top-1 holder ~20-21% (bonding curve pool — NORMAL)
                        # Danger zone: top-1 > 50% = single whale can rug immediately
                        # Quality: 89.88%, SpaceX: 99.14% — both dead tokens in our dataset
                        top_holders = rug_data.get("topHolders", [])
                        if top_holders:
                            _top1_pct = float(top_holders[0].get("pct", 0)) * 100
                            if _top1_pct > 50.0:
                                return False, f"Top holder owns {_top1_pct:.1f}% — extreme concentration, instant rug risk"
                            elif _top1_pct < 10.0:
                                # Quality distribution — log as bonus signal
                                import logging as _log_h
                                _log_h.getLogger(__name__).info(
                                    f"[safety] ✨ QUALITY DISTRIBUTION: top-1 holder = {_top1_pct:.1f}% "
                                    f"({mint[:8]}...) — distributed ownership, strong signal"
                                )
        except Exception:
            pass  # rugcheck unavailable — allow through, on-chain checks already passed

        return True, "Mint/freeze authority burned ✅, LP lock OK"

    # ─────────────────────── background monitor ──────────────────────────────

    async def _verify_restored_positions(self) -> None:
        """On first monitor tick after startup, write off any ghost positions.

        A ghost occurs when a close tx succeeded on-chain but the position wasn't
        removed from memory before the bot crashed/restarted. We detect them by
        checking the wallet: if no tokens are held for a live (non-paper) position,
        the position is an orphan — write it off at entry price (assume 0 P&L) to
        keep the dashboard clean.
        """
        if not self.positions:
            return
        try:
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)
            if wallet_svc is None:
                return
            balances = await wallet_svc.get_token_balances()
            held_mints = {b["mint"] for b in balances if int(b.get("raw_amount", 0)) > 0}
        except Exception:
            return

        for pos_key, pos in list(self.positions.items()):
            # Only verify live positions (paper trades never have real tokens)
            _buy_rec = next(
                (t for t in self._trade_history
                 if t.get("mint") == pos.mint and t.get("side") == "buy"
                 and t.get("pos_key", pos.mint) == pos_key),
                None
            )
            _is_paper = str((_buy_rec or {}).get("signature", "")).startswith("PAPER_")
            if _is_paper:
                continue
            if pos.mint not in held_mints:
                self._log_activity(
                    "warning",
                    f"⚠️ Ghost position detected on startup: {pos.mint[:8]}… "
                    f"(no tokens in wallet) — writing off as orphaned close"
                )
                if self._runtime:
                    self._runtime.logger.warning(
                        f"Orphaned position {pos.mint[:8]}: no tokens in wallet — writing off",
                        src="service:position_manager",
                    )
                self.close_position(
                    pos.mint,
                    pos.entry_price_sol,  # assume flat — real P&L already settled on-chain
                    "orphaned_on_restart",
                    signature=f"ORPHAN_{uuid.uuid4().hex[:12].upper()}",
                    _pos_key=pos_key,
                )

    async def _monitor_loop(self) -> None:
        """Check open positions for SL/TP/time-limit exits every 30 seconds."""
        _startup_check_done = False
        while True:
            try:
                await asyncio.sleep(MONITOR_INTERVAL_SECS)
                # On first tick after startup, verify restored live positions have real tokens.
                # Prevents ghost positions when a previous SL/close tx succeeded on-chain but
                # the position wasn't removed from memory before crash/restart.
                if not _startup_check_done and self._runtime:
                    _startup_check_done = True
                    await self._verify_restored_positions()
                await self._check_all_positions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"Position monitor error: {exc}",
                        src="service:position_manager",
                    )

    async def _liquidity_monitor_loop(self) -> None:
        """Check open position liquidity every 60s via DexScreener.

        Tiered response:
          +20% growth  → POSITIVE SIGNAL: log + consider holding
          -20% drop    → WARNING: tighten trailing stop to 10%
          -40% drop    → EMERGENCY: immediate rug exit
        """
        LIQ_CHECK_INTERVAL = 60       # seconds between checks
        LIQ_GROW_THRESHOLD  = 0.20    # +20% = positive signal
        LIQ_WARN_THRESHOLD  = 0.20    # -20% = warning + tighten trail
        LIQ_EMERGENCY_THRESH = 0.40   # -40% = emergency exit

        while True:
            try:
                await asyncio.sleep(LIQ_CHECK_INTERVAL)
                if not self.positions:
                    continue

                # Monitor all open positions: liq-drain check + health_monitor positions
                mints_to_monitor = [
                    (mint, pos) for mint, pos in list(self.positions.items())
                    if (pos.entry_liquidity_usd > 0 or pos.health_monitor_mode) and not pos.tp1_hit
                ]
                if not mints_to_monitor:
                    continue

                import aiohttp
                BATCH = 30
                all_mints = [m for m, _ in mints_to_monitor]
                # Per-mint DexScreener data — keyed by highest-liq pair for each mint
                dex_data: dict[str, dict] = {}  # mint → {liq, buy_ratio_m5, m5_pct}

                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
                    for i in range(0, len(all_mints), BATCH):
                        batch = all_mints[i:i + BATCH]
                        try:
                            async with sess.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
                            ) as resp:
                                if resp.status != 200:
                                    continue
                                data = await resp.json()
                                for p in (data.get("pairs") or []):
                                    base = (p.get("baseToken") or {}).get("address", "")
                                    if not base:
                                        continue
                                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                                    # Keep highest-liq pair per mint (bonding curve has high h24vol but 0 liq)
                                    if base in dex_data and liq <= dex_data[base]["liq"]:
                                        continue
                                    txns_m5  = (p.get("txns") or {}).get("m5") or {}
                                    buys_m5  = float(txns_m5.get("buys") or 0)
                                    sells_m5 = float(txns_m5.get("sells") or 0)
                                    total_m5 = buys_m5 + sells_m5
                                    buy_ratio = buys_m5 / total_m5 if total_m5 > 0 else 0.5
                                    m5_pct    = float((p.get("priceChange") or {}).get("m5") or 0) / 100.0
                                    dex_data[base] = {
                                        "liq":           liq,
                                        "buy_ratio_m5":  buy_ratio,
                                        "m5_pct":        m5_pct,
                                    }
                        except Exception:
                            pass

                for mint, pos in mints_to_monitor:
                    pair = dex_data.get(mint)
                    if pair is None:
                        continue
                    current_liq     = pair["liq"]
                    buy_ratio_m5    = pair["buy_ratio_m5"]
                    m5_pct          = pair["m5_pct"]

                    # Store health signals for use by _check_all_positions
                    pos._last_known_liq_usd = current_liq
                    pos._last_buy_ratio_m5  = buy_ratio_m5
                    pos._last_m5_pct        = m5_pct

                    if pos.entry_liquidity_usd <= 0 and not pos.health_monitor_mode:
                        continue

                    liq_ref    = pos.entry_liquidity_usd if pos.entry_liquidity_usd > 0 else current_liq
                    liq_change = (current_liq - liq_ref) / liq_ref if liq_ref > 0 else 0.0

                    # ── EMERGENCY: -40% liq drain — exit NOW ─────────────────────
                    if liq_change <= -LIQ_EMERGENCY_THRESH:
                        if mint in self.positions and pos._liq_state < 3:
                            pos._liq_state = 3
                            msg = (
                                f"💧 LIQ COLLAPSE: {mint[:8]}... "
                                f"${liq_ref:,.0f} → ${current_liq:,.0f} "
                                f"({liq_change*100:+.0f}%) — EMERGENCY EXIT"
                            )
                            self._log_activity("error", msg)
                            try:
                                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                                import asyncio as _asyncio
                                _asyncio.create_task(_tg(f"🚨 {msg}"))
                            except Exception:
                                pass
                            # Force rug_stop_loss on next price cycle
                            pos.zero_price_streak = 999

                    # ── WARNING: -20% liq drop — tighten trailing stop ────────────
                    elif liq_change <= -LIQ_WARN_THRESHOLD:
                        if pos._liq_state < 2:
                            pos._liq_state = 2
                            msg = (
                                f"⚠️ LIQ DROPPING: {mint[:8]}... "
                                f"${liq_ref:,.0f} → ${current_liq:,.0f} "
                                f"({liq_change*100:+.0f}%) — tightening trailing stop to 10%"
                            )
                            self._log_activity("warning", msg)
                            # Tighten trailing stop to 10% from current liq-warned level
                            pos.trailing_stop_price = max(
                                pos.trailing_stop_price,
                                pos._last_known_liq_usd * 0.90 if pos._last_known_liq_usd > 0
                                else pos.entry_price_sol * 0.90,
                            )
                            try:
                                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                                import asyncio as _asyncio
                                _asyncio.create_task(_tg(f"⚠️ {msg}"))
                            except Exception:
                                pass

                    # ── POSITIVE: +20% liq growth — real money entering ───────────
                    elif liq_change >= LIQ_GROW_THRESHOLD and pos._liq_state == 0:
                        pos._liq_state = 1
                        self._log_activity(
                            "info",
                            f"📈 LIQ GROWING: {mint[:8]}... "
                            f"${liq_ref:,.0f} → ${current_liq:,.0f} "
                            f"({liq_change*100:+.0f}%) — real money entering, hold strong",
                        )

                    # ── Health monitor scoring (only for health_monitor_mode positions) ──
                    # Runs every 60s; exits when HEALTH_EXIT_BAD_READINGS consecutive bad checks.
                    # Bad = ≥2 of: liq dropped 20%+ from health-phase peak, buy_ratio<45%, m5<-5%.
                    if not pos.health_monitor_mode or pos.health_exit_requested:
                        continue

                    # Track the highest liq seen during the health monitor phase
                    if current_liq > pos.health_peak_liq_usd:
                        pos.health_peak_liq_usd = current_liq

                    _liq_ref_health = max(pos.health_peak_liq_usd, pos.entry_liquidity_usd, 1.0)
                    _liq_drop = (pos.health_peak_liq_usd - current_liq) / _liq_ref_health
                    _bad_liq   = _liq_drop >= HEALTH_LIQ_DROP_THRESH
                    _bad_ratio = buy_ratio_m5 < HEALTH_MIN_BUY_RATIO
                    _bad_m5    = m5_pct < HEALTH_M5_DECLINE_THRESH
                    _bad_count = _bad_liq + _bad_ratio + _bad_m5  # int (0-3)

                    _peak_pct = int((pos.peak_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0

                    if _bad_count >= 2:
                        pos.health_bad_readings += 1
                        self._log_activity("warning",
                            f"🏥 Health check [{pos.health_bad_readings}/{HEALTH_EXIT_BAD_READINGS}]: "
                            f"{mint[:8]}... peak=+{_peak_pct}% — "
                            f"liq={'✗' if _bad_liq else '✓'}({_liq_drop*100:.0f}%↓) "
                            f"ratio={'✗' if _bad_ratio else '✓'}({buy_ratio_m5:.0%}) "
                            f"m5={'✗' if _bad_m5 else '✓'}({m5_pct*100:+.1f}%)")
                        if pos.health_bad_readings >= HEALTH_EXIT_BAD_READINGS:
                            pos.health_exit_requested = True
                            self._log_activity("warning",
                                f"🏥 Health exit triggered: {mint[:8]}... "
                                f"{HEALTH_EXIT_BAD_READINGS} consecutive bad readings — requesting close")
                            try:
                                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                                import asyncio as _asyncio
                                _asyncio.create_task(_tg(
                                    f"🏥 Health exit: {mint[:8]}... peak=+{_peak_pct}% — "
                                    f"fundamentals deteriorated (liq/ratio/m5)"
                                ))
                            except Exception:
                                pass
                    else:
                        if pos.health_bad_readings > 0:
                            pos.health_bad_readings = max(0, pos.health_bad_readings - 1)
                        self._log_activity("info",
                            f"🏥 Health OK: {mint[:8]}... peak=+{_peak_pct}% — "
                            f"liq={current_liq:,.0f}(peak={pos.health_peak_liq_usd:,.0f}) "
                            f"ratio={buy_ratio_m5:.0%} m5={m5_pct*100:+.1f}%")

            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _fetch_price_for_position(
        self,
        mint: str,
        pos: "Position",
        pump_svc: object,
        raydium_svc: object,
    ) -> tuple[str, float | None, bool]:
        """Fetch current price for one position. Returns (mint, price, graduated).
        `graduated=True` means pump_fun bonding curve has completed."""
        from elizaos.plugins.solana.services.pump_fun import PumpFunService
        from elizaos.plugins.solana.services.raydium import RaydiumService

        if pos.dex == "pump_fun" and isinstance(pump_svc, PumpFunService):
            try:
                bc = await pump_svc.get_bonding_curve(mint)
            except Exception:
                return mint, None, False
            if not bc:
                return mint, None, True  # graduated
            return mint, bc.get("price_sol", pos.entry_price_sol), False

        if pos.dex in ("raydium", "pumpswap", "meteora") and isinstance(raydium_svc, RaydiumService):
            try:
                # PumpSwap: try Helius RPC first (real-time pool reserves, no DexScreener lag).
                # DexScreener lags 5-30s — SL exits overshoot by ~9pp on average (-9% trigger → -18% actual).
                # Helius reads on-chain vault balances every monitor cycle → sub-second price accuracy.
                if pos.dex == "pumpswap" and pos.pool_address:
                    helius_price = await raydium_svc.get_pumpswap_price_helius(pos.pool_address)
                    if helius_price > 0:
                        return mint, helius_price, False
                # Fall back to DexScreener for non-pumpswap or missing pool_address
                price = await raydium_svc.get_price(mint, vs_token="SOL", cache_ttl=3.0)
                return mint, price if price > 0 else None, False
            except Exception:
                return mint, None, False

        return mint, None, False

    async def _check_all_positions(self) -> None:
        if not self.positions:
            # Still record equity snapshots even with no positions
            await self._maybe_record_equity()
            return

        from elizaos.plugins.solana.services.pump_fun import PumpFunService
        from elizaos.plugins.solana.services.raydium import RaydiumService

        pump_svc = None
        raydium_svc = None
        if self._runtime:
            pump_svc = self._runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
            raydium_svc = self._runtime.get_service(ServiceTypeRegistry.LP_POOL)

        to_close: list[tuple[str, str]] = []    # (pos_key, reason)
        to_partial: list[tuple[str, str, float]] = []  # (pos_key, level, fraction)

        # Snapshot positions (avoid mutating dict during iteration)
        # pos_key may be a composite "mint:A" / "mint:B" for split-buy positions.
        active = list(self.positions.items())

        # ── Hard time limit (no I/O needed) ──────────────────────────────────
        skip_keys: set[str] = set()
        for pos_key, pos in active:
            if pos.age_seconds() > pos.max_hold_seconds():
                to_close.append((pos_key, "time_limit"))
                self._log_activity("warning", f"Time limit reached: {pos.mint[:8]}...")
                skip_keys.add(pos_key)

        # ── Health monitor exit (set by _liquidity_monitor_loop) ──────────────
        for pos_key, pos in active:
            if pos_key in skip_keys:
                continue
            if pos.health_exit_requested:
                _peak_pct = int((pos.peak_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                _cur_p = pos.last_known_price if pos.last_known_price > 0 else pos.entry_price_sol
                _cur_pct = int((_cur_p / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                self._log_activity("warning",
                    f"🏥 Health exit: {pos.mint[:8]}... peak=+{_peak_pct}% current≈+{_cur_pct}% — "
                    f"fundamentals deteriorated (liq/buy_ratio/m5 signals)")
                to_close.append((pos_key, "health_exit"))
                skip_keys.add(pos_key)

        # ── Parallel price fetch (deduplicated by base mint for split positions) ──
        # Two split positions share the same pos.mint — fetch only once per unique mint.
        seen_fetch_mints: set[str] = set()
        fetch_tasks = []
        for pos_key, pos in active:
            if pos_key not in skip_keys and pos.mint not in seen_fetch_mints:
                fetch_tasks.append(self._fetch_price_for_position(pos.mint, pos, pump_svc, raydium_svc))
                seen_fetch_mints.add(pos.mint)

        # price_results is keyed by base mint address (shared across split siblings)
        price_results: dict[str, tuple[float | None, bool]] = {}
        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=False)
            for mint_r, price_r, graduated_r in results:
                price_results[mint_r] = (price_r, graduated_r)

        # ── Risk logic (no I/O) ───────────────────────────────────────────────
        for pos_key, pos in active:
            if pos_key in skip_keys:
                continue

            price_r, graduated_r = price_results.get(pos.mint, (None, False))

            if graduated_r:
                # Token graduated from bonding curve → PumpSwap
                if self._runtime:
                    self._runtime.logger.info(
                        f"{pos.mint[:8]}: graduated from bonding curve → PumpSwap",
                        src="service:position_manager",
                    )
                pos.dex = "pumpswap"
                self._log_activity("info", f"{pos.mint[:8]}... graduated → PumpSwap 🎓")
                continue

            current_price = price_r

            if current_price is None:
                # Track consecutive zero-price cycles — DexScreener returning None means
                # the pool may have been drained (rug). Only count after 60s so we don't
                # false-trigger on freshly-bought tokens not yet indexed.
                if pos.age_seconds() > 60:
                    pos.zero_price_streak += 1
                    if pos.zero_price_streak >= RUG_ZERO_PRICE_STREAK:
                        self._log_activity("error", f"🔴 {pos.mint[:8]}: {pos.zero_price_streak} consecutive no-price cycles — pool gone, emergency close")
                        to_close.append((pos_key, "rug_stop_loss"))
                        continue
                # If we've already had sell failures AND price is None, the pool has vanished.
                if pos.sell_fail_count > 0 and not pos.is_reconciled:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"{pos.mint[:8]}: price=None + sell_fail_count={pos.sell_fail_count} — pool gone, writing off as rug",
                            src="service:position_manager",
                        )
                    self._log_activity("error", f"🔴 {pos.mint[:8]}: pool gone (no price + sell failures) — writing off")
                    to_close.append((pos_key, "rug_stop_loss"))
                    continue
                # Graduated tokens (pumpswap) with no price after stall time → dead money.
                if (
                    pos.dex == "pumpswap"
                    and not pos.tp1_hit
                    and pos.age_seconds() > STALL_TIME_SECS
                ):
                    to_close.append((pos_key, "stall_exit"))
                    self._log_activity("warning", f"Stall exit (no pumpswap price): {pos.mint[:8]}...")
                continue
            # Price is valid — reset rug streak
            pos.zero_price_streak = 0

            # ── Sell retry cooldown ───────────────────────────────────────────
            # If a recent sell attempt failed (HTTP 400), throttle this position:
            # skip ALL TP/SL triggers for 30s to avoid hammering PumpPortal.
            SELL_RETRY_COOLDOWN = 30.0
            if pos.sell_fail_count > 0 and time.time() - pos.last_sell_attempt < SELL_RETRY_COOLDOWN:
                pos.last_known_price = current_price  # keep price updated
                continue

            # ── Entry-relative sanity guard ───────────────────────────────────
            # DexScreener sometimes returns a non-SOL-quote pair (e.g. token/LPPP,
            # token/USDC) whose priceNative is not the SOL price of the token.
            # These values are typically in the range 0.1–10.0 SOL, vs real token
            # prices of 1e-7 to 1e-5 SOL.  If the price is > 1000× entry it is
            # definitively wrong data — skip this cycle entirely.
            if current_price > pos.entry_price_sol * 1000:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"Price sanity rejected for {pos.mint[:8]}: {current_price:.6f} SOL >> entry {pos.entry_price_sol:.8f} SOL (>1000x) — likely wrong DexScreener pair",
                        src="service:position_manager",
                    )
                self._log_activity("warning", f"Price sanity reject {pos.mint[:8]}: {current_price:.4f} >> entry {pos.entry_price_sol:.2e} (DexScreener pair mismatch)")
                continue

            # ── Price spike guard ─────────────────────────────────────────────
            # DexScreener occasionally returns wild anomalies (stale/bad data).
            # If price jumped >100x from the last reading in a single 2s cycle,
            # it's a data error — skip TP checks entirely to avoid phantom fires.
            if pos.last_known_price > 0 and current_price > pos.last_known_price * 100:
                ratio = current_price / pos.last_known_price
                if self._runtime:
                    self._runtime.logger.warning(
                        f"Price spike rejected for {pos.mint[:8]}: {pos.last_known_price:.8f} → {current_price:.8f} ({ratio:.0f}x) — skipping this cycle",
                        src="service:position_manager",
                    )
                self._log_activity("warning", f"⚠️ {pos.mint[:8]}: price spike {ratio:.0f}x rejected (DexScreener anomaly)")
                continue

            # ── Crash velocity detector ───────────────────────────────────────
            # If price drops >35% in a single 2-second monitor cycle vs the previous
            # reading, it is a coordinated dump (rug/dev sell) where DexScreener kept
            # showing a stale "last trade" price and then suddenly updated.
            # We exit immediately before the token decays further.
            # This catches cases like -60% exits where the SL at -12% should have
            # fired minutes earlier but DexScreener lag prevented it.
            _prev_price = pos.last_known_price
            pos.update_trailing(current_price)
            pos.last_known_price = current_price  # store for use as exit price in paper mode
            self._emit_event("position_update", self._serialize_position(pos, current_price))

            # ── Stagnant tracker — reset window if price moved outside band ───
            if pos.stall_price > 0 and abs(current_price - pos.stall_price) / pos.stall_price > (PF_STAGNANT_BAND if pos.dex == "pump_fun" else STALL_THRESHOLD):
                pos.stall_price = current_price
                pos.stall_since = time.time()

            if (
                _prev_price > 0
                and not pos.tp1_hit
                and current_price < _prev_price * 0.75   # -25% in one 2s cycle = coordinated dump
                and time.time() >= pos.monitoring_starts_at
            ):
                to_close.append((pos_key, "rug_stop_loss"))
                self._log_activity(
                    "error",
                    f"💥 Crash velocity exit: {pos.mint[:8]}... price dropped "
                    f"{(_prev_price - current_price) / _prev_price * 100:.0f}% in one cycle "
                    f"({_prev_price:.2e} → {current_price:.2e})",
                )
                continue

            # ── Early stop loss (tighter SL for first 3 minutes) ─────────────
            # Applies to ALL dex types — data showed pumpswap grad-snipe entries
            # dump just as hard as bonding curve tokens (e.g. Ejm79mwM: -60% over
            # 326s because DexScreener kept showing stale price above SL, then
            # jumped to -60% on first real on-chain dump trade).
            # Age-based early SL for PumpSwap — young tokens are more volatile.
            # pump_fun: -5% (tight, volatile BC tokens)
            # pumpswap 60-120min: -15% (volatile graduation zone)
            # pumpswap 2-6hr: -12%
            # pumpswap 6hr+: -10%
            # others: -10%
            if pos.dex == "pump_fun":
                _early_sl_pct = EARLY_STOP_LOSS_PCT  # 5%
            elif pos.dex == "pumpswap":
                _tok_age_mins = float(getattr(pos, "meta", {}).get("time_since_launch_secs") or 0) / 60
                if _tok_age_mins < 120:
                    _early_sl_pct = 0.15  # 60-120 min: volatile graduation zone
                elif _tok_age_mins < 360:
                    _early_sl_pct = 0.12  # 2-6 hr: settling phase
                else:
                    _early_sl_pct = 0.10  # 6hr+: mature
            else:
                _early_sl_pct = 0.10  # 10%
            early_sl_price = pos.entry_price_sol * (1 - _early_sl_pct)
            if (
                not pos.tp1_hit
                and pos.age_seconds() >= EARLY_STOP_LOSS_GRACE_SECS
                and pos.age_seconds() <= EARLY_STOP_LOSS_SECS
                and current_price <= early_sl_price
            ):
                to_close.append((pos_key, "early_stop_loss"))
                self._log_activity(
                    "error",
                    f"Early SL triggered: {pos.mint[:8]}... at {current_price:.8f} SOL "
                    f"({pos.pnl_pct(current_price)*100:+.1f}% in {pos.age_seconds():.0f}s)",
                )
                continue

            # ── Stop loss ─────────────────────────────────────────────────────
            # monitoring_starts_at guard (10s): prevents SL firing immediately
            # when fill price > DexScreener quote (next monitor cycle sees price
            # already below SL because DexScreener is lagged).
            # Meteora extra grace: 30s minimum hold before SL can fire.
            # Data: all -84% to -100% Meteora losses happened in 10-12s — these were
            # LP-drain honeypots or price feed garbage. LP lock check (rugcheck.xyz)
            # should prevent entry, but this is a last-resort guard.
            _sl_grace_secs = 30.0 if pos.dex == "meteora" else 0.0
            if (
                not pos.tp1_hit
                and time.time() >= pos.monitoring_starts_at
                and pos.age_seconds() >= _sl_grace_secs
                and current_price <= pos.stop_loss_price
            ):
                to_close.append((pos_key, "stop_loss"))
                self._log_activity("error", f"Stop loss triggered: {pos.mint[:8]}... at {current_price:.8f} SOL")
                continue

            # ── Global trailing stop ───────────────────────────────────────────
            # Tracks 15% (configurable) below highest price seen since entry.
            # Moves UP with price, never down. Fires when price falls below level.
            # Coexists with the fixed SL — whichever fires first wins.
            # Moon mode uses its own progressive trail; skip to avoid double-exit.
            try:
                from elizaos.plugins.solana import live_config as _lc_ts
                _ts_enabled = bool(_lc_ts.get("trailing_stop_enabled", False))
                _ts_pct     = float(_lc_ts.get("trailing_stop_pct", 0.15))
            except Exception:
                _ts_enabled = False
                _ts_pct     = 0.15
            if (
                _ts_enabled
                and not pos.tp1_hit
                and not pos.moon_mode        # moon mode has its own progressive trail
                and time.time() >= pos.monitoring_starts_at
                and pos.peak_price > 0
                and current_price <= pos.peak_price * (1 - _ts_pct)
            ):
                _peak_pct = int((pos.peak_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                _cur_pct  = int(pos.pnl_pct(current_price) * 100)
                _level    = pos.peak_price * (1 - _ts_pct)
                to_close.append((pos_key, "trailing_stop_loss"))
                self._log_activity(
                    "success" if _cur_pct >= 0 else "warning",
                    f"🔽 Trailing SL: {pos.mint[:8]}... peak=+{_peak_pct}% current={_cur_pct:+}% "
                    f"(trail={_ts_pct*100:.0f}% below peak → level {_level:.2e})",
                )
                continue

            # ── pump.fun stagnant exit (3 min flat → exit, BC tokens move fast) ─
            if (
                pos.dex == "pump_fun"
                and not pos.tp1_hit
                and pos.age_seconds() > EARLY_STOP_LOSS_SECS  # give 3 min entry grace first
                and pos.stall_since > 0
                and time.time() - pos.stall_since > PF_STAGNANT_SECS
            ):
                to_close.append((pos_key, "stagnant_exit"))
                self._log_activity(
                    "warning",
                    f"Stagnant exit: {pos.mint[:8]}... price flat (±{PF_STAGNANT_BAND*100:.0f}%) for "
                    f"{PF_STAGNANT_SECS//60}min — dead money on BC",
                )
                continue

            # ── Stall/velocity exit (playbook §2.3 "Time-based exit") ─────────
            # Raydium tokens peak in 3-8 min (external research) — use 8 min stall
            # social_momentum handled above (fast stall already fired or ongoing)
            # All others: 15 min stall
            _stall_secs = (
                STALL_TIME_RAYDIUM if pos.dex in ("raydium", "native_raydium")
                else STALL_TIME_PUMP_FUN if pos.dex == "pump_fun"
                else STALL_TIME_SECS
            )
            if (
                not pos.tp1_hit
                and pos.dex not in ("pump_fun", "social_momentum")  # both have own stall logic
                and pos.age_seconds() > _stall_secs
                and current_price < pos.entry_price_sol * (1 + STALL_THRESHOLD)
            ):
                to_close.append((pos_key, "stall_exit"))
                self._log_activity(
                    "warning",
                    f"Stall exit: {pos.mint[:8]}... no +{STALL_THRESHOLD*100:.0f}% in "
                    f"{_stall_secs//60}min ({pos.dex}) — dead money exit",
                )
                continue

            # ── Grok Premium stall exit ───────────────────────────────────────────
            # If this is a grok_premium position (score ≥8): track whether price has
            # stalled (±2% movement in 60s). If stalled AND P&L > +25%, exit now
            # rather than waiting for the full +50% TP — guaranteed profit secured.
            if pos.grok_premium and not pos.tp1_hit and time.time() >= pos.monitoring_starts_at:
                _prem_stall_secs = GROK_PREMIUM_STALL_SECS
                _prem_stall_band = GROK_PREMIUM_STALL_BAND
                _prem_stall_min  = GROK_PREMIUM_STALL_MIN_GAIN
                try:
                    from elizaos.plugins.solana import live_config as _lc_prem
                    _prem_stall_secs = int(_lc_prem.get("grok_premium_stall_secs", GROK_PREMIUM_STALL_SECS))
                    _prem_stall_band = float(_lc_prem.get("grok_premium_stall_band", GROK_PREMIUM_STALL_BAND))
                    _prem_stall_min  = float(_lc_prem.get("grok_premium_stall_min_pct", GROK_PREMIUM_STALL_MIN_GAIN))
                except Exception:
                    pass
                # Initialise stall tracker on first cycle
                if pos.premium_stall_ref_price == 0.0:
                    pos.premium_stall_ref_price = current_price
                    pos.premium_stall_since = time.time()
                elif abs(current_price - pos.premium_stall_ref_price) / pos.premium_stall_ref_price > _prem_stall_band:
                    # Price moved significantly — reset the stall window
                    pos.premium_stall_ref_price = current_price
                    pos.premium_stall_since = time.time()
                else:
                    # Price within band — check if stalled long enough at a profitable level
                    _stall_duration = time.time() - pos.premium_stall_since
                    _current_gain   = pos.pnl_pct(current_price)
                    if _stall_duration >= _prem_stall_secs and _current_gain >= _prem_stall_min:
                        pos.tp1_hit = True  # mark TP so moonbag/TP2 logic doesn't re-fire
                        to_close.append((pos_key, "premium_stall_exit"))
                        self._log_activity(
                            "success",
                            f"Premium stall TP: {pos.mint[:8]}... +{_current_gain*100:.0f}% for "
                            f"{_stall_duration:.0f}s (±{_prem_stall_band*100:.0f}% band) — taking profit",
                        )
                        continue

            # ── Social Momentum (Strategy E) — fade exit & fast stall ───────────
            # Research-backed: social pumps exhaust in minutes when volume fades.
            # Two exit triggers specific to social_momentum positions:
            #   1. FADE EXIT: up >20% AND (h1 reversing OR volume collapsed) → take profit now
            #   2. FAST STALL: no +7% in 5 min (vs 15 min for pumpswap) — social noise dies fast
            if pos.dex == "social_momentum" and not pos.tp1_hit and time.time() >= pos.monitoring_starts_at:
                _gain = pos.pnl_pct(current_price)
                # Track peak gain for Jarvis reporting
                if _gain > pos.social_peak_gain:
                    pos.social_peak_gain = _gain
                # ── Fade exit: only check every 60s to avoid hammering DexScreener ──
                _now = time.time()
                if _gain >= SOCIAL_FADE_GAIN_MIN and _now - pos.social_fade_check_at >= 60:
                    pos.social_fade_check_at = _now
                    try:
                        import aiohttp as _aiohttp_fade
                        _mint_fade = pos.mint
                        async with _aiohttp_fade.ClientSession() as _fade_sess:
                            _fade_url = f"https://api.dexscreener.com/latest/dex/tokens/{_mint_fade}"
                            async with _fade_sess.get(_fade_url, timeout=_aiohttp_fade.ClientTimeout(total=8)) as _r:
                                if _r.status == 200:
                                    _fade_data = await _r.json()
                                    _pairs = _fade_data.get("pairs") or []
                                    _sol = [p for p in _pairs if p.get("chainId") == "solana" and (p.get("liquidity") or {}).get("usd", 0) > 0]
                                    if _sol:
                                        _best = max(_sol, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
                                        _cur_h1_pct  = float((_best.get("priceChange") or {}).get("h1", 0) or 0) / 100.0
                                        _cur_vol_h1  = float((_best.get("volume") or {}).get("h1", 0) or 0)
                                        # Initialise entry volume on first fade check
                                        if pos.social_entry_vol_h1 == 0 and _cur_vol_h1 > 0:
                                            pos.social_entry_vol_h1 = _cur_vol_h1
                                        # Trigger 1: h1 price change reversed (momentum gone)
                                        if _cur_h1_pct <= SOCIAL_FADE_H1_THRESH:
                                            to_close.append((pos_key, "social_fade_momentum"))
                                            self._log_activity(
                                                "warning",
                                                f"📉 Social fade exit: {pos.mint[:8]}... "
                                                f"+{_gain*100:.0f}% peak={pos.social_peak_gain*100:.0f}% "
                                                f"h1={_cur_h1_pct*100:+.0f}% — momentum reversed, banking profit",
                                            )
                                            continue
                                        # Trigger 2: volume collapsed vs entry (social pump exhausted)
                                        if (pos.social_entry_vol_h1 > 0 and _cur_vol_h1 > 0
                                                and _cur_vol_h1 < pos.social_entry_vol_h1 * (1 - SOCIAL_FADE_VOL_DROP)):
                                            to_close.append((pos_key, "social_fade_volume"))
                                            self._log_activity(
                                                "warning",
                                                f"📉 Social fade exit (vol): {pos.mint[:8]}... "
                                                f"+{_gain*100:.0f}% | vol dropped "
                                                f"{_cur_vol_h1/pos.social_entry_vol_h1*100:.0f}% of entry — exiting",
                                            )
                                            continue
                    except Exception:
                        pass  # Non-critical: don't break the monitor if DexScreener times out

                # ── Fast stall: 5 min no +7% → dead social signal, exit ──────
                _social_stall_secs = STALL_TIME_SOCIAL  # 300s
                if (
                    pos.age_seconds() > _social_stall_secs
                    and current_price < pos.entry_price_sol * (1 + STALL_THRESHOLD)
                    and pos.stall_since > 0
                    and time.time() - pos.stall_since > _social_stall_secs
                ):
                    to_close.append((pos_key, "social_stall_exit"))
                    self._log_activity(
                        "warning",
                        f"Social stall exit: {pos.mint[:8]}... no +{STALL_THRESHOLD*100:.0f}% "
                        f"in {_social_stall_secs//60}min — social signal faded, exiting",
                    )
                    continue

            # ── Moon mode: trailing stop is primary exit (ultra-fast grad / DexScreener boosted) ──
            # Activates once gain > +100%. Uses progressive trail from update_trailing():
            #   +100–200%: -12% trail | +200–500%: -8% trail | >+500%: -6% trail
            # This lets CHIBI/fine999.9 class runners ride their full move.
            if pos.moon_mode and time.time() >= pos.monitoring_starts_at:
                current_gain = pos.pnl_pct(current_price)
                if not pos.moon_trail_active and current_gain >= 1.0:
                    pos.moon_trail_active = True
                    self._log_activity(
                        "success",
                        f"🌙 Moon trail ACTIVATED: {pos.mint[:8]}... +{current_gain*100:.0f}% — trailing stop is now primary exit (trail={pos.trailing_stop_price:.8f})",
                    )
                if pos.moon_trail_active and current_price <= pos.trailing_stop_price:
                    to_close.append((pos_key, "moon_trail_exit"))
                    _peak_pct = int((pos.peak_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                    _cur_pct  = int(current_gain * 100)
                    self._log_activity(
                        "success",
                        f"🌙 Moon trail EXIT: {pos.mint[:8]}... peak=+{_peak_pct}% current=+{_cur_pct}% trail hit",
                    )
                    continue

            # ── Health monitor activation — at +60% gain, check fundamentals ─────────
            # Applies to graduated tokens only (pumpswap / raydium / meteora) — not pump_fun BC
            # or moon_mode (those have their own exit logic).
            # If fundamentals are healthy: raise TP ceiling to ×20 and let the token run.
            # The health monitor (_liquidity_monitor_loop, every 60s) watches liq, buy_ratio,
            # and m5 price change — it sets health_exit_requested when signals turn bad.
            # If fundamentals are already bad at +60%: close immediately (health_check_failed).
            if (
                not pos.health_monitor_mode
                and not pos.tp1_hit
                and not pos.moon_mode
                and pos.dex in ("pumpswap", "raydium", "native_raydium", "meteora")
                and time.time() >= pos.monitoring_starts_at
                and current_price >= pos.entry_price_sol * (1.0 + HEALTH_TRIGGER_PCT)
            ):
                _liq_ok   = pos._last_known_liq_usd >= pos.entry_liquidity_usd * 0.80 if pos.entry_liquidity_usd > 0 else True
                _ratio_ok = pos._last_buy_ratio_m5 >= HEALTH_MIN_BUY_RATIO
                _m5_ok    = pos._last_m5_pct >= HEALTH_M5_DECLINE_THRESH
                _health_ok = (_liq_ok + _ratio_ok + _m5_ok) >= 2  # 2/3 signals positive
                _gain_pct = int((current_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                if _health_ok:
                    pos.health_monitor_mode = True
                    pos.health_activated_at = time.time()
                    pos.health_peak_liq_usd = max(pos._last_known_liq_usd, pos.entry_liquidity_usd)
                    pos.tp1_price = pos.entry_price_sol * 20.0  # ceiling — health monitor + trailing stop now primary
                    self._log_activity("success",
                        f"🏥 HEALTH MONITOR ON: {pos.mint[:8]}... +{_gain_pct}% — "
                        f"liq={'✓' if _liq_ok else '✗'} ratio={'✓' if _ratio_ok else '✗'} m5={'✓' if _m5_ok else '✗'} — "
                        f"letting it run, watching fundamentals every 60s")
                    continue  # don't exit this cycle; trailing stop is now the backstop
                else:
                    self._log_activity("warning",
                        f"🏥 Health check FAILED at +{_gain_pct}%: {pos.mint[:8]}... "
                        f"liq={'✓' if _liq_ok else '✗'} ratio={'✓' if _ratio_ok else '✗'} m5={'✓' if _m5_ok else '✗'} — "
                        f"fundamentals already weak, banking gains now")
                    to_close.append((pos_key, "health_check_failed"))
                    skip_keys.add(pos_key)
                    continue

            # ── TP1: FULL EXIT for all DEXes (conservative scalping — bank it) ──────────
            # pumpswap +30%, pump.fun +60%, meteora +15%, raydium +20%, grok +80%, premium +50%
            # social_momentum: Tier1/KOL=+200%, Tier2=+100%, Tier3=+60% (from position meta)
            if (
                not pos.tp1_hit
                and not pos.moon_mode  # moon mode uses trailing stop, not fixed TP
                and time.time() >= pos.monitoring_starts_at
                and current_price >= pos.tp1_price
            ):
                pos.tp1_hit = True
                _tp_pct = int((pos.tp1_price / pos.entry_price_sol - 1) * 100) if pos.entry_price_sol > 0 else 0
                to_close.append((pos_key, "tp_full_exit"))
                _label_tag = f" [{pos.label}]" if pos.label else ""
                self._log_activity("success", f"TP hit{_label_tag}: {pos.mint[:8]}... +{_tp_pct}% — FULL EXIT ({pos.dex})")
                continue

            # ── TP1.5: +150% → sell 50% of remaining (mid-step, captures tokens
            # that peak between +80% and +300% without reaching TP2) ──────────
            if (
                pos.tp1_hit
                and not pos.tp15_hit
                and not pos.tp2_hit
                and current_price >= pos.tp15_price
            ):
                pos.tp15_hit = True
                to_partial.append((pos_key, "TP1.5", 0.50))
                self._log_activity("success", f"TP1.5 hit: {pos.mint[:8]}... +80% — selling 50% of remaining")
                continue

            # ── TP2: pump_fun +150% → FULL CLOSE; others +180% → sell 75% of remaining ──
            if (
                pos.tp1_hit
                and not pos.tp2_hit
                and (pos.dex == "pump_fun" or pos.tp15_hit)  # pump_fun skips TP1.5
                and current_price >= pos.tp2_price
            ):
                pos.tp2_hit = True
                pos.tp2_hit_time = time.time()
                if pos.dex == "pump_fun":
                    to_close.append((pos_key, "tp2_full_exit"))
                    self._log_activity("success", f"TP2 hit: {pos.mint[:8]}... +150% — FULL EXIT (pump.fun BC)")
                else:
                    to_partial.append((pos_key, "TP2", 0.75))
                    self._log_activity("success", f"TP2 hit: {pos.mint[:8]}... +180% — selling 75% of remaining")
                continue

            # ── Trailing stop (moonbag protection after TP2) ─────────────────
            if (
                pos.tp2_hit
                and not pos.tp3_hit
                and current_price <= pos.trailing_stop_price
            ):
                to_close.append((pos_key, "trailing_stop"))
                self._log_activity("warning", f"Trailing stop: {pos.mint[:8]}... moonbag exit")
                continue

            # ── Moonbag floor: if price falls back below TP1 level after TP2 → exit
            # Prevents giving back all gains (the -96%/-99% moonbag collapse problem)
            if (
                pos.tp2_hit
                and not pos.tp3_hit
                and current_price < pos.tp1_price
            ):
                to_close.append((pos_key, "moonbag_floor"))
                self._log_activity("warning", f"Moonbag floor: {pos.mint[:8]}... price fell back below TP1 — exiting")
                continue

            # ── Moonbag stall exit (15min without new high → dead money) ──────
            if (
                pos.tp2_hit
                and not pos.tp3_hit
                and pos.tp2_hit_time > 0
                and time.time() - pos.tp2_hit_time > MOONBAG_STALL_SECS
                and current_price < pos.peak_price * 0.99
            ):
                to_close.append((pos_key, "moonbag_stall"))
                self._log_activity("info", f"Moonbag stall: {pos.mint[:8]}... 15min no new high — exiting")
                continue

            # ── TP3: +900% jackpot → close moonbag ───────────────────────────
            if (
                pos.tp2_hit
                and not pos.tp3_hit
                and current_price >= pos.tp3_price
            ):
                pos.tp3_hit = True
                to_partial.append((pos_key, "TP3", 1.0))
                self._log_activity("success", f"TP3 jackpot: {pos.mint[:8]}... +400% — closing moonbag")
                continue

        # Execute partial sells and full closes (using pos_keys for dict lookup)
        for pos_key, level, fraction in to_partial:
            await self._execute_partial_sell(pos_key, level, fraction)
        for pos_key, reason in to_close:
            await self._execute_auto_close(pos_key, reason)

        # Record equity snapshot every 5 minutes
        await self._maybe_record_equity()

    async def _execute_auto_close(self, pos_key: str, reason: str) -> None:
        pos = self.positions.get(pos_key)
        if not pos:
            return
        mint = pos.mint  # actual on-chain token address (pos_key may be composite)

        if self._runtime:
            self._runtime.logger.info(
                f"Auto-close triggered: {mint[:8]} reason={reason}",
                src="service:position_manager",
            )

        try:
            from elizaos.plugins.solana.services.pump_fun import PumpFunService
            from elizaos.plugins.solana.services.raydium import RaydiumService
            from elizaos.plugins.solana.constants import WSOL_MINT
            from elizaos.plugins.solana.services.wallet import SolanaWalletService

            pump_svc = self._runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
            raydium_svc = self._runtime.get_service(ServiceTypeRegistry.LP_POOL)
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)

            sig = ""
            # In paper mode use the last price from the monitor tick that triggered the exit.
            exit_price = pos.last_known_price if pos.last_known_price > 0 else pos.entry_price_sol

            # Detect paper positions opened via paper_trading_scout=True (no real tokens held).
            # Buy signature starts with "PAPER_" in that mode even when PAPER_TRADING env is False.
            _buy_rec = next(
                (t for t in reversed(self._trade_history)
                 if t.get("mint") == mint and t.get("side") == "buy"
                 and t.get("pos_key", mint) == pos_key),
                None
            )
            _is_paper_position = PAPER_TRADING or str((_buy_rec or {}).get("signature", "")).startswith("PAPER_")

            if _is_paper_position:
                sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
            elif isinstance(raydium_svc, RaydiumService) and isinstance(wallet_svc, SolanaWalletService):
                # Sell path: Jupiter only — aggregates all Solana DEXes (PumpSwap, Raydium, Meteora)
                balances = await wallet_svc.get_token_balances()
                decimals = next((t["decimals"] for t in balances if t["mint"] == mint), 6)
                on_chain_bal = next((t["raw_amount"] for t in balances if t["mint"] == mint), pos.token_amount)
                amount_float = on_chain_bal / (10 ** decimals)

                # Fetch BC price for pump_fun to get accurate exit_price
                if pos.dex == "pump_fun" and isinstance(pump_svc, PumpFunService):
                    try:
                        bc2 = await pump_svc.get_bonding_curve(mint)
                        if bc2:
                            exit_price = bc2.get("price_sol", exit_price)
                    except Exception:
                        pass

                # Jupiter sell — escalate slippage on failure (30% → 50% → 80%)
                last_exc: Exception | None = None
                for _slip_bps in (3000, 5000, 8000):
                    try:
                        sig = await raydium_svc.swap_jupiter_sell(
                            mint, amount_float, token_decimals=decimals, slippage_bps=_slip_bps
                        )
                        if self._runtime:
                            self._runtime.logger.info(
                                f"Sell via Jupiter OK for {mint[:8]} (slip={_slip_bps//100}%): sig={sig[:12]}...",
                                src="service:position_manager",
                            )
                        self._log_activity("success", f"✅ {mint[:8]}: sold via Jupiter (slip={_slip_bps//100}%)")
                        last_exc = None
                        break
                    except Exception as _jup_exc:
                        last_exc = _jup_exc
                        if self._runtime:
                            self._runtime.logger.warning(
                                f"Jupiter sell {_slip_bps}bps failed for {mint[:8]}: {_jup_exc}",
                                src="service:position_manager",
                            )

                if last_exc is not None:
                    raise last_exc
                if not sig:
                    raise RuntimeError(f"Jupiter sell failed for {mint[:8]}")

            self.close_position(mint, exit_price, reason, signature=sig, _pos_key=pos_key)
            if self._runtime:
                self._runtime.logger.info(
                    f"Auto-close executed: {mint[:8]} sig={sig[:12]}... reason={reason}",
                    src="service:position_manager",
                )
            # Post-close sanity: verify tokens are actually gone from wallet.
            # If tokens remain the on-chain tx failed (priority fee too low, slippage, congestion).
            # Retry automatically with escalating fee + slippage before asking for manual sell.
            try:
                post_bals = await wallet_svc.get_token_balances()
                remaining_raw = next((t["raw_amount"] for t in post_bals if t["mint"] == mint), 0)
                if remaining_raw > 0:
                    remaining_human = remaining_raw / (10 ** decimals)
                    # Reconciled orphan positions are old stuck/illiquid tokens from prior sessions.
                    # Their on-chain sell txs fail due to no liquidity. Skip the escalating retry
                    # loop entirely — spending 0.003–0.01 SOL in retry fees on a dust token is
                    # worse than just accepting it as stuck.
                    if pos and pos.is_reconciled:
                        if self._runtime:
                            self._runtime.logger.warning(
                                f"POST-CLOSE (reconciled): {mint[:8]} tokens still in wallet after close — accepting as stuck/illiquid (no retry to save fees)",
                                src="service:position_manager",
                            )
                        self._log_activity("warning", f"⚠️ {mint[:8]}: reconciled position stuck in wallet — accept as illiquid dust")
                        return
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"POST-CLOSE: {remaining_human:.4f} tokens of {mint[:8]} still in wallet — retrying with higher fee",
                            src="service:position_manager",
                        )
                    # Before escalating retry: check DexScreener to see if pool still exists.
                    # If pairs=null the token is rugged — no amount of fee bumping will sell it.
                    # Accepting as stuck saves ~0.004–0.010 SOL in wasted priority fees.
                    try:
                        from elizaos.plugins.solana.constants import DEXSCREENER_TOKEN_API
                        import aiohttp as _aiohttp2
                        async with _aiohttp2.ClientSession(timeout=_aiohttp2.ClientTimeout(total=5)) as _s2:
                            async with _s2.get(f"{DEXSCREENER_TOKEN_API}/{mint}") as _r2:
                                _dex2 = await _r2.json()
                                if _dex2.get("pairs") is None:
                                    if self._runtime:
                                        self._runtime.logger.warning(
                                            f"POST-CLOSE: {mint[:8]} DexScreener pairs=null — rug, skipping retry to save fees",
                                            src="service:position_manager",
                                        )
                                    self._log_activity("error", f"🔴 {mint[:8]}: rug (no DexScreener pairs) — skipping retry, accept as lost")
                                    return
                    except Exception:
                        pass
                    # Retry via Jupiter with escalating slippage + priority fee
                    retry_recovered = False
                    for _r_slip_bps, _r_fee in ((5000, 0.0003), (8000, 0.0005), (9000, 0.001)):
                        try:
                            retry_sig = await raydium_svc.swap_jupiter_sell(
                                mint, remaining_human, token_decimals=decimals,
                                slippage_bps=_r_slip_bps, priority_fee=_r_fee,
                            )
                            if not retry_sig:
                                continue
                            await asyncio.sleep(5)
                            verify_bals = await wallet_svc.get_token_balances()
                            still_raw = next((t["raw_amount"] for t in verify_bals if t["mint"] == mint), 0)
                            if still_raw > 0:
                                remaining_human = still_raw / (10 ** decimals)
                                if self._runtime:
                                    self._runtime.logger.warning(
                                        f"Post-close retry partial: {mint[:8]} slip={_r_slip_bps}bps still {remaining_human:.4f} tokens — continuing",
                                        src="service:position_manager",
                                    )
                                continue
                            if self._runtime:
                                self._runtime.logger.info(
                                    f"Post-close retry OK: {mint[:8]} slip={_r_slip_bps}bps sig={retry_sig[:12]}",
                                    src="service:position_manager",
                                )
                            self._log_activity("success", f"✅ {mint[:8]}: retry sell cleared (slip={_r_slip_bps//100}%)")
                            retry_recovered = True
                            break
                        except Exception:
                            continue
                    if not retry_recovered:
                        if self._runtime:
                            self._runtime.logger.warning(
                                f"POST-CLOSE: Jupiter retries exhausted for {mint[:8]} — MANUAL SELL NEEDED",
                                src="service:position_manager",
                            )
                        self._log_activity("warning", f"⚠️ {mint[:8]}: {remaining_human:.4f} tokens still — MANUAL SELL NEEDED")
            except Exception:
                pass
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.error(
                    f"Auto-close FAILED for {mint[:8]}: {exc}",
                    src="service:position_manager",
                )
            # If ALL sell attempts failed on a live position, write it off as rug/illiquid.
            err_str = str(exc).lower()
            is_sell_error = "400" in err_str or "bad request" in err_str or "not found" in err_str
            pos2 = self.positions.get(pos_key)
            # Reconciled orphans (old stuck tokens): write off immediately on first failure.
            # Normal positions: allow up to 3 retries, then check DexScreener.
            # If DexScreener shows no pairs (pairs=null), write off immediately — saves fee burn.
            SELL_GIVEUP_ATTEMPTS = 1 if (pos2 and pos2.is_reconciled) else 3
            if is_sell_error and pos2 is not None and not PAPER_TRADING:
                pos2.sell_fail_count += 1
                pos2.last_sell_attempt = time.time()
                # After first failure, quick-check DexScreener liquidity.
                # If token has no pairs data, it's rugged — write off immediately.
                if pos2.sell_fail_count == 1:
                    # For pump_fun BC tokens: DexScreener pairs=null is EXPECTED (not indexed
                    # until graduation). Try PumpPortal pool="pump" before writing off.
                    if pos2.dex == "pump_fun":
                        try:
                            from elizaos.plugins.solana.services.wallet import SolanaWalletService
                            _w2 = self._runtime.get_service(ServiceTypeRegistry.WALLET) if self._runtime else None
                            if isinstance(_w2, SolanaWalletService):
                                _bals2 = await _w2.get_token_balances()
                                _dec2 = next((t["decimals"] for t in _bals2 if t["mint"] == mint), 6)
                                _raw2 = next((t["raw_amount"] for t in _bals2 if t["mint"] == mint), 0)
                                _amt2 = _raw2 / (10 ** _dec2)
                                if _amt2 > 0:
                                    import aiohttp as _aiohttp_bc
                                    _pp_url = "https://pumpportal.fun/api/trade-local"
                                    _keypair2 = _w2.keypair
                                    _pp_payload = {
                                        "action": "sell", "mint": mint,
                                        "amount": "100%", "denominatedInSol": "false",
                                        "slippage": 50, "priorityFee": 0.001,
                                        "pool": "pump",
                                        "publicKey": str(_keypair2.pubkey()),
                                    }
                                    async with _aiohttp_bc.ClientSession(timeout=_aiohttp_bc.ClientTimeout(total=15)) as _s_bc:
                                        async with _s_bc.post(_pp_url, json=_pp_payload) as _r_bc:
                                            if _r_bc.status == 200:
                                                _tx_bytes = await _r_bc.read()
                                                from solders.transaction import VersionedTransaction
                                                _vtx = VersionedTransaction.from_bytes(_tx_bytes)
                                                _signed = VersionedTransaction(_vtx.message, [_keypair2])
                                                import base64 as _b64
                                                _sig_bc = await _w2.rpc.send_raw_transaction(
                                                    _b64.b64encode(bytes(_signed)).decode(), skip_preflight=True
                                                )
                                                if _sig_bc:
                                                    self._log_activity("success", f"✅ {mint[:8]}: BC sell via PumpPortal OK sig={_sig_bc[:12]}")
                                                    self.close_position(mint, pos2.entry_price_sol, reason, signature=_sig_bc, _pos_key=pos_key)
                                                    return
                        except Exception as _bc_sell_err:
                            if self._runtime:
                                self._runtime.logger.warning(
                                    f"Auto-close {mint[:8]}: PumpPortal BC sell failed: {_bc_sell_err}",
                                    src="service:position_manager",
                                )
                    else:
                        try:
                            from elizaos.plugins.solana.constants import DEXSCREENER_TOKEN_API
                            import aiohttp as _aiohttp
                            async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=5)) as _s:
                                async with _s.get(f"{DEXSCREENER_TOKEN_API}/{mint}") as _r:
                                    _data = await _r.json()
                                    if _data.get("pairs") is None:
                                        if self._runtime:
                                            self._runtime.logger.warning(
                                                f"Auto-close {mint[:8]}: DexScreener pairs=null — rug confirmed, writing off immediately",
                                                src="service:position_manager",
                                            )
                                        self._log_activity("error", f"🔴 {mint[:8]}: rug confirmed (no DexScreener pairs) — writing off")
                                        self.close_position(mint, 0.0, f"rug_{reason}", signature="WRITE_OFF", _pos_key=pos_key)
                                        return
                        except Exception:
                            pass
                if pos2.sell_fail_count >= SELL_GIVEUP_ATTEMPTS:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"Auto-close for {mint[:8]} failed {pos2.sell_fail_count}x — giving up, writing off as rug",
                            src="service:position_manager",
                        )
                    self.close_position(mint, 0.0, f"rug_{reason}", signature="WRITE_OFF", _pos_key=pos_key)
                else:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"Auto-close for {mint[:8]} failed (attempt {pos2.sell_fail_count}/{SELL_GIVEUP_ATTEMPTS}) — keeping position, retry in 30s",
                            src="service:position_manager",
                        )
                    self._log_activity("warning", f"⚠️ {mint[:8]}: close failed (attempt {pos2.sell_fail_count}/{SELL_GIVEUP_ATTEMPTS}) — retrying in 30s")

    async def _execute_partial_sell(self, pos_key: str, level: str, fraction: float) -> None:
        pos = self.positions.get(pos_key)
        if not pos:
            return
        mint = pos.mint  # actual on-chain token address (pos_key may be composite)

        sell_amount = int(pos.token_amount * fraction)
        if sell_amount <= 0:
            return

        if self._runtime:
            self._runtime.logger.info(
                f"Partial sell {level}: {mint[:8]} selling {fraction*100:.0f}% ({sell_amount} tokens)",
                src="service:position_manager",
            )

        try:
            from elizaos.plugins.solana.services.pump_fun import PumpFunService
            from elizaos.plugins.solana.services.raydium import RaydiumService
            from elizaos.plugins.solana.constants import WSOL_MINT
            from elizaos.plugins.solana.services.wallet import SolanaWalletService

            pump_svc = self._runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
            raydium_svc = self._runtime.get_service(ServiceTypeRegistry.LP_POOL)
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)

            sig = ""
            exit_price = pos.last_known_price if pos.last_known_price > 0 else pos.peak_price

            if PAPER_TRADING:
                sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
            elif isinstance(raydium_svc, RaydiumService) and isinstance(wallet_svc, SolanaWalletService):
                # Sell path: Jupiter only — aggregates PumpSwap, Raydium, Meteora
                if pos.dex == "pump_fun" and isinstance(pump_svc, PumpFunService):
                    try:
                        bc = await pump_svc.get_bonding_curve(mint)
                        if bc:
                            exit_price = bc.get("price_sol", exit_price)
                    except Exception:
                        pass
                elif pos.dex == "pumpswap":
                    try:
                        price = await raydium_svc.get_price(mint, vs_token="SOL", cache_ttl=3.0)
                        if price > 0 and pos.entry_price_sol > 0 and price < pos.entry_price_sol * 200:
                            exit_price = price
                    except Exception:
                        pass

                balances = await wallet_svc.get_token_balances()
                decimals = next((t["decimals"] for t in balances if t["mint"] == mint), 6)
                amount_float = sell_amount / (10 ** decimals)

                # Jupiter sell — escalate slippage on failure (30% → 50% → 80%)
                last_exc: Exception | None = None
                for _slip_bps in (3000, 5000, 8000):
                    try:
                        sig = await raydium_svc.swap_jupiter_sell(
                            mint, amount_float, token_decimals=decimals, slippage_bps=_slip_bps,
                            priority_fee=0.0003,
                        )
                        if self._runtime:
                            self._runtime.logger.info(
                                f"Partial sell {level} via Jupiter (slip={_slip_bps//100}%): {mint[:8]} sig={sig[:12]}...",
                                src="service:position_manager",
                            )
                        last_exc = None
                        break
                    except Exception as _jup_exc:
                        last_exc = _jup_exc
                        if self._runtime:
                            self._runtime.logger.warning(
                                f"Partial sell Jupiter {_slip_bps}bps failed for {mint[:8]}: {_jup_exc}",
                                src="service:position_manager",
                            )
                if last_exc is not None:
                    raise last_exc
                if not sig:
                    raise RuntimeError(f"Jupiter partial sell failed for {mint[:8]}")

            self.notify_partial_sell(mint, fraction, exit_price, signature=sig, _pos_key=pos_key)  # sig now persisted
            if self._runtime:
                self._runtime.logger.info(
                    f"Partial sell executed {level}: {mint[:8]} sig={sig[:12]}...",
                    src="service:position_manager",
                )
            # TP3 sells 100% of moonbag — clean up the position from the dict.
            if fraction >= 1.0:
                closed_pos = self.positions.pop(pos_key, None)
                if closed_pos:
                    # Emit position_closed so dashboard removes the row in real-time.
                    self._emit_event("position_closed", {"mint": mint, "reason": level, "pnl_sol": 0.0})
                    self._emit_event("risk_update", self.get_risk_summary())
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.error(
                    f"Partial sell FAILED for {mint[:8]} level={level}: {exc}",
                    src="service:position_manager",
                )
            # HTTP 400 = PumpPortal can't route the sell RIGHT NOW.
            # This can be transient (freshly graduated, brief congestion) OR permanent (rug).
            # DO NOT immediately write off — keep position alive and retry every 30s.
            # Only write off after SELL_GIVEUP_ATTEMPTS consecutive failures (≈ 5 minutes).
            SELL_RETRY_COOLDOWN = 30.0
            SELL_GIVEUP_ATTEMPTS = 10
            err_str = str(exc).lower()
            is_sell_error = "400" in err_str or "bad request" in err_str or "not found" in err_str
            pos = self.positions.get(pos_key)
            if is_sell_error and pos is not None and not PAPER_TRADING:
                pos.sell_fail_count += 1
                pos.last_sell_attempt = time.time()
                if pos.sell_fail_count >= SELL_GIVEUP_ATTEMPTS:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"Partial sell {level} for {mint[:8]} failed {pos.sell_fail_count}x — giving up, writing off as rug",
                            src="service:position_manager",
                        )
                    self.close_position(mint, 0.0, f"rug_{level}", signature="WRITE_OFF", _pos_key=pos_key)
                else:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"Partial sell {level} for {mint[:8]} failed (attempt {pos.sell_fail_count}/{SELL_GIVEUP_ATTEMPTS}) — keeping position, retry in {SELL_RETRY_COOLDOWN:.0f}s",
                            src="service:position_manager",
                        )
                    self._log_activity("warning", f"⚠️ {mint[:8]}: sell failed (attempt {pos.sell_fail_count}/{SELL_GIVEUP_ATTEMPTS}) — will retry in 30s")

    async def _maybe_record_equity(self) -> None:
        """Record an equity snapshot every 5 minutes."""
        now = time.time()
        if now - self._last_equity_snapshot < 300:
            return
        self._last_equity_snapshot = now

        try:
            from elizaos.plugins.solana.services.wallet import SolanaWalletService
            if not self._runtime:
                return
            wallet_svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)
            if not isinstance(wallet_svc, SolanaWalletService):
                return

            sol_balance = await wallet_svc.get_sol_balance()
            # Estimate unrealized value from positions
            unrealized_sol = 0.0
            for pos in self.positions.values():
                unrealized_sol += pos.entry_sol_spent  # conservative: use entry spend

            equity_sol = sol_balance + unrealized_sol
            if self._start_equity_sol is None:
                self._start_equity_sol = equity_sol

            snapshot = {
                "timestamp": now,
                "equity_sol": round(equity_sol, 6),
                "sol_balance": round(sol_balance, 6),
                "unrealized_sol": round(unrealized_sol, 6),
                "realized_pnl_sol": round(self.daily_pnl_sol, 6),
                "position_count": len(self.positions),
            }
            self._equity_snapshots.append(snapshot)
            self._emit_event("equity_snapshot", snapshot)

            # Also emit wallet update
            self._emit_event("wallet_update", {
                "sol_balance": round(sol_balance, 6),
                "address": wallet_svc.get_public_key(),
            })
        except Exception:
            pass
