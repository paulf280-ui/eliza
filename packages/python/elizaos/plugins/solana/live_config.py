"""
live_config.py — Shared mutable bot configuration.

All strategy loops import this module and re-read values on every iteration,
so changes made via the dashboard API or Eliza chat take effect immediately
without restarting the bot.
"""
import os
import json
import time

_PERSIST_PATH = os.path.join(os.path.dirname(__file__), "bot_config.json")

# ── Defaults (overridden by bot_config.json on startup, then by Eliza at runtime) ──
_config: dict = {
    # Trading mode
    "paper_trading":           os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes"),
    "paper_trading_scout":     os.getenv("PAPER_TRADING_SCOUT", "false").lower() in ("true", "1", "yes"),
    "buy_sol":                 float(os.getenv("BUY_SOL", "0.1")),

    # Strategy enables — B and D are active; C and E remain off
    "strategy_b_enabled":        os.getenv("STRATEGY_B_ENABLED", "true").lower()  not in ("false", "0", "no"),
    "strategy_c_enabled":        os.getenv("STRATEGY_C_ENABLED", "false").lower() not in ("false", "0", "no"),
    "strategy_d_enabled":        os.getenv("STRATEGY_D_ENABLED", "true").lower()  not in ("false", "0", "no"),

    # Per-strategy position size overrides
    "strategy_b_buy_sol":        0.35,
    "strategy_c_buy_sol":        0.35,
    "strategy_d_buy_sol":        0.35,

    # Strategy B — PumpSwap graduation snipe
    "strategy_b_min_liq_usd":    13500,
    "strategy_b_dip_wait_secs":  20,
    # Minimum seconds post-graduation before entry (15s for pool to settle).
    # COWARDS (eXJeVGnH) was detected at 11s and went +16,737% — 60s was too long.
    # Set to 0 to use the quality gate only (liq ≥ strategy_b_min_liq_usd).
    "strategy_b_min_age_secs":   15,

    # Strategy C — Raydium/PumpSwap momentum scout
    "strategy_c_min_score":      3,
    "strategy_c_min_liq_usd":    0,      # 0 = disabled (data gathering mode)

    # Strategy E — Social Momentum Snipe (replaces Meteora)
    # Research-backed v2 defaults (2026-03-19):
    # - Entry window: 2-24h old (tighter sweet spot), h1 0-50% (not already pumped)
    # - Vol/liq ratio: >25x = wash trading filter
    # - Scoring: KOL +2, shill=0, narrative tier 1/2/3, momentum phase penalty
    # - TP: Tier1/KOL = +200%, Tier2 = +100%, Tier3 = +60% (set in position meta)
    "strategy_e_enabled":        False,          # OFF — accumulate research data first
    "strategy_e_research_mode":  True,           # True = log signals, never execute trades
    "strategy_e_buy_sol":        0.05,           # 0.05 SOL per trade (conservative until validated)
    "strategy_e_min_liq_usd":    25_000,         # min liquidity ($25k)
    "strategy_e_min_age_hours":  2.0,            # token must be >2h old (past rug window)
    "strategy_e_max_age_hours":  24.0,           # token must be <24h (sweet spot from research)
    "strategy_e_min_h1_pct":     0.0,            # allow any positive momentum (even flat)
    "strategy_e_max_h1_pct":     50.0,           # skip if h1 >50% — already pumped
    "strategy_e_max_h24_pct":    400.0,          # skip if h24 >400%
    "strategy_e_min_score":      6,              # min score (0-10) to log as WOULD_BUY
    "strategy_e_min_volume_usd": 15_000,         # min h24 volume ($15k)

    # Trading window (UTC hours, inclusive)
    "trading_window_start_utc":  21,     # 21:00 UTC = 9pm Dublin
    "trading_window_end_utc":    3,      # 03:00 UTC = 3am Dublin

    # Risk / exits
    "stop_loss_pct":             0.09,   # 9% — allows normal volatility without premature exit
    "pumpswap_stop_loss_pct":    0.09,   # match global — 7% was too tight for PumpSwap volatility
    "early_stop_loss_pct":       0.05,
    "tp1_mult":                  2.00,   # +100% safety net TP — trailing stop is primary exit
    "pf_tp1_mult":               1.60,   # pump.fun bonding curve tokens (+60%)
    "grok_tp_mult":              1.80,   # Grok-confirmed X social signal (+80%)
    "max_concurrent_positions":  2,      # hard cap: 2 positions at any time
    "max_daily_loss_pct":        0.12,

    # pump.fun BC fill window (FILL_CAP_PCT — reject if bonding curve fill > this %)
    "fill_cap_pct":              68.0,

    # Strategy A2 toggle (live_config mirrors STRATEGY_A2_ENABLED env var)
    "strategy_a2_enabled":       os.getenv("STRATEGY_A2_ENABLED", "true").lower() not in ("false", "0", "no"),

    # Per-strategy position size overrides for A2
    "a2_buy_sol":                float(os.getenv("BUY_SOL_BC", "0.25")),

    # A2 momentum floor — minimum real SOL deposited by buyers to qualify
    # Lowered from 2.0 → 1.6 SOL (2026-03-17) to increase trade frequency.
    "a2_min_real_sol":           1.6,

    # ── Entry filters (Jarvis-adjustable) ────────────────────────────────────
    "rugcheck_max_score":    8000,    # rugcheck score above this = blocked (lower = stricter)
    "a2_min_holders":        15,      # minimum holder count before A2-BC entry (raised from 10 → 15)
    "a2_require_twitter":    True,    # Gate 1: must have a real Twitter/X account
    "a2_require_telegram":   True,    # Gate 1: must have a Telegram channel
    "a2_require_website":    False,   # Gate 1: website optional — twitter+telegram sufficient
    "a2_max_whale_pct":      40.0,    # reject if top buyer holds > this % of supply
    # Holder growth rate filter — 0 = disabled
    "a2_min_holder_growth_rate":   0.0,   # holders/min over window (0 = disabled)
    "a2_holder_growth_window_secs": 300.0, # window to compute growth rate (seconds)
    "post_sl_cooldown_secs": 180.0,   # seconds all strategies freeze after any stop-loss
    # MC filter — Jarvis lesson: tiny MC tokens (<$50K) are traps; real winners $500K-$11M.
    # 0 = disabled (data-gathering mode). Set to e.g. 50000 when evidence threshold reached.
    "a2_min_mc_usd":         0,       # minimum market cap in USD (0 = disabled)

    # ── A2 liquidity filter (bonding curve proxy) ─────────────────────────────
    # Winners had $59K-$162K liq equivalent; losers had $3K-$3.7K.
    # For BC tokens: proxy = real_sol * SOL_USD_PRICE. 0 = disabled.
    "a2_min_liq_usd":        0,       # 0 = disabled; suggested: 50000 once validated

    # ── Vol/Liq ratio filters (Strategy B grad-snipe + Strategy C Raydium) ────
    # Data: winners 12x-57x | losers 172x-340x (wash trading)
    # Applied to DexScreener h1 vol / liquidity for graduated/DEX tokens.
    # 0 = disabled.
    "b_min_vol_liq_ratio":   1.0,    # floor: 1x — dead tokens are at 0.2-0.5x
    "b_max_vol_liq_ratio":   8.0,    # cap: 8x — above = already pumped / wash trading
    "c_min_vol_liq_ratio":   1.0,    # Raydium scout — same floor
    "c_max_vol_liq_ratio":   8.0,    # Raydium scout — same cap

    # ── Strategy D (Meteora DLMM) vol/liq + buy ratio ─────────────────────────
    "d_min_vol_liq_ratio":   1.0,    # Meteora scout — 1x floor
    "d_max_vol_liq_ratio":   8.0,    # Meteora scout — 8x cap
    "d_min_buy_ratio":       52.0,   # Meteora scout min buy % (0=disabled)

    # ── Buy ratio filter (Strategy B + C) ────────────────────────────────────
    # Winners averaged 63.75% buy ratio; minimum winner had 52%.
    # Below 52% = selling pressure; above 65% = strong signal.
    # Applied to DexScreener txns h1 buys/(buys+sells). 0 = disabled.
    "b_min_buy_ratio":       50.0,   # 50% iron-clad floor (29-monster dataset)
    "c_min_buy_ratio":       50.0,   # Raydium scout — same floor

    # ── A2 quality gates ─────────────────────────────────────────────────────
    # a2_min_score: 0-100 scale. 0=accept SAFE+STRONG. 50+=require STRONG only. 80+=proven creators only.
    # Jarvis set to 65 (2026-03-20): all previous BC trades scored 12 with no real filter — need STRONG only.
    "a2_min_score":          65,      # require STRONG verdict (≥50) from Claude gate
    # a2_max_age_secs: max seconds since token creation — skip old bonding curves (already pumped/dumped)
    "a2_max_age_secs":       300,     # only trade tokens created < 5 minutes ago

    # ── Copy-Trade (Axiom leaderboard whale following) ────────────────────────
    # 48-hour paper test started 2026-04-05. All keys PROTECTED while paper_trading=True.
    # copy_trade_paper_enabled: master switch for paper simulation
    # copy_trade_enabled:       live trading switch — stays False during test
    "copy_trade_paper_enabled":  True,    # always-on for paper tracking
    "copy_trade_paper_buy_sol":  0.35,    # SOL per copy-trade position
    "copy_trade_paper_balance":  2.08,    # current wallet balance (updated on restart)
    "copy_trade_min_sol":        0.2,     # ignore whale entries smaller than 0.2 SOL (2026-04-18: was 0.5, too strict — missed 10+ Walta signals/day)
    "copy_trade_consensus":      1,       # 1 = any single whale triggers entry
    "copy_trade_sl_pct":         10.0,    # stop-loss: -10% hard exit
    "copy_trade_tp_pct":         15.0,    # take-profit: +15% exit (quant-locked, see axiom_copy_trader.py REQUIRED_TP_PCT)
    "copy_trade_trail_pct":       15.0,    # trailing stop: 15% below peak (activates after TP% hit)
    "copy_trade_max_hold_mins":   60.0,    # force-close position after N minutes
    "copy_trade_daily_loss_halt_sol": 0.5,  # halt new entries if daily loss exceeds this SOL amount
    "copy_trade_min_balance_halt_sol": 1.0, # halt if wallet balance drops below this SOL amount
    # ── Partial TP ladder (2026-04-18) — scale out in tranches so a late wallet_exit
    # closes a moonbag instead of the whole position. Historical data: 52 wallet_exits
    # gave back avg 21pp from peak; this ladder traps those gains.
    "copy_trade_ladder_enabled": True,    # master toggle
    "copy_trade_ladder_l1_pct":   8.0,    # Tranche 1: fire at +8% pnl
    "copy_trade_ladder_l1_frac":  0.40,   # sell 40% of ORIGINAL position
    "copy_trade_ladder_l2_pct":  15.0,    # Tranche 2: +15% — replaces hard TP when ladder on
    "copy_trade_ladder_l2_frac":  0.30,   # sell 30% of original
    "copy_trade_ladder_l3_pct":  30.0,    # Tranche 3: +30%
    "copy_trade_ladder_l3_frac":  0.20,   # sell 20% of original (10% runner left)
    "copy_trade_compound_pct":   0.20,    # position size = wallet_balance × 20% (quant-locked, REQUIRED_COMPOUND_PCT)
    "copy_trade_enabled":        False,   # live mode — set True to execute real trades
    "copy_trade_paused":         False,   # dashboard pause button — blocks new entries only

    # ── Trenchman (fast-lane copy trader) — DISABLED 2026-04-18 ───────────────
    # 19 trades, 21% WR, -0.592 SOL net (single worst wallet in history). Re-enable
    # only after Trenchman wallet is removed from logsSubscribe target list.
    "trenchman_fast_lane":        False,   # was True — confirmed losing wallet
    "trenchman_buy_sol":          0.5,     # SOL per Trenchman trade
    "trenchman_max_positions":    2,       # global position cap (covers ALL copy positions)

    # ── Smart Wallet (Strategy SW) ────────────────────────────────────────────
    # Shadow-buy tokens purchased by tracked high-win-rate wallets.
    # Wallets are configured in smart_wallets.json. Bot watches Helius for
    # recent SWAP transactions from those wallets and copies qualifying buys.
    "smart_wallet_enabled":      False,     # OFF by default until wallets are configured
    "smart_wallet_buy_sol":      0.08,      # SOL per smart wallet shadow-buy
    "smart_wallet_max_age_secs": 120,       # only act on buys < 120s old
    "smart_wallet_poll_secs":    30,        # how often to check each wallet

    # ── Proven creator mode ────────────────────────────────────────────────────
    # When a creator is PROVEN (2+ successful pump.fun/pumpswap launches, 0 rugs),
    # the bot uses this fraction of current wallet balance as the buy size.
    # Example: wallet=0.333 SOL + proven_creator_pct=0.75 → buy=0.25 SOL
    "proven_creator_pct":        0.75,   # 75% of wallet on proven creator tokens

    # ── Grok Premium mode ──────────────────────────────────────────────────────
    # When Grok scores a token ≥grok_premium_min_score (default 8/10),
    # the bot enters with a larger position (grok_premium_buy_sol) and uses a
    # tighter TP/stall strategy instead of the standard GROK_TP_MULT (+80%).
    #   - Full TP at +50% (grok_premium_tp_mult = 1.50)
    #   - OR exit at +25% if price stalls ±2% for 60s (premium_stall_exit)
    #   - Hard SL at -9% (unchanged)
    "grok_premium_enabled":      True,
    "grok_premium_buy_sol":      0.3,       # 0.3 SOL per premium trade
    "grok_premium_min_score":    8,         # Grok confidence threshold (0-10) to trigger premium mode
    "grok_premium_tp_mult":      1.50,      # +50% full exit
    "grok_premium_stall_secs":   60,        # 60s stall window
    "grok_premium_stall_band":   0.02,      # ±2% movement band to reset stall timer
    "grok_premium_stall_min_pct": 0.25,     # must be above +25% to qualify for stall exit

    # ── Price Momentum Scorer ──────────────────────────────────────────────────
    # momentum_score = m5 × 0.5 + h1 × 0.3 + h6 × 0.2  (DexScreener intervals)
    # 0 = disabled. Positive = upward momentum. Gate: score > threshold.
    # Example: m5=+5%, h1=+20%, h6=+15% → score = 2.5 + 6.0 + 3.0 = 11.5
    # Useful for filtering stale/declining tokens from scout loops.
    "b_min_momentum_score":  0.0,   # Strategy B grad-snipe (0=disabled)
    "c_min_momentum_score":  0.0,   # Strategy C Raydium scout (0=disabled)
    "d_min_momentum_score":  0.0,   # Strategy D Meteora scout (0=disabled)

    # ── Global trailing stop ───────────────────────────────────────────────────
    # When enabled: SL always sits trailing_stop_pct below the highest price seen
    # since entry. Moves UP with price, never down. Fires as soon as price falls
    # more than trailing_stop_pct below the peak.
    # Example (15%): entry $0.001 → +100% peak $0.002 → SL at $0.0017 (+70%)
    #   → price drops to $0.0016 → EXIT at +60% locking in the gain.
    # Fixed SL (-9%) still applies alongside this; whichever fires first wins.
    "trailing_stop_enabled":     True,      # ON — primary exit mechanism for monster runs
    "trailing_stop_pct":         0.12,      # 12% below peak price

    # ── Per-platform DNA filters (Strategy C / D) ──────────────────────────────
    # Minimum market cap and liquidity per platform.  0 = disabled.
    "c_min_mc_usd":     50_000,    # Strategy C (Raydium) min MC — $50k floor prevents micro-cap rugs
    "c_min_liq_usd":    25_000,    # Strategy C (Raydium) min liq — $25k floor (monsters start at $25k+)
    "d_min_mc_usd":     50_000,    # Strategy D (Meteora) min MC
    "d_min_liq_usd":    25_000,    # Strategy D (Meteora) min liq

    # ── Graduation Confirmation Timer (Strategy B) ─────────────────────────────
    # 0 = disabled (enter immediately after dip-wait).
    # >0 = after passing initial filters, park the token for N seconds then
    # re-check price / vol / buy-ratio / liquidity before actually buying.
    # Recommended: 600 (10 min).  Skips tokens that dump during the wait.
    "graduation_confirmation_wait":  0.0,    # seconds; 0 = disabled

    # ── Monster Scanner ────────────────────────────────────────────────────────
    # Dedicated high-quality scanner: runs every interval_secs, looks at tokens
    # already 30 min–6 hrs old across all DEXes, applies full monster DNA.
    # monster_scanner_buy = False → log-only (research); True → executes trades.
    "monster_scanner_enabled":        False,
    "monster_scanner_buy":            False,
    "monster_scanner_interval_secs":  900,        # scan every 15 min
    "monster_scanner_min_liq_usd":    50_000,     # $50K liquidity floor
    "monster_scanner_min_mc_usd":     50_000,     # $50K market cap floor
    "monster_scanner_min_age_mins":   30,         # token ≥ 30 min old
    "monster_scanner_max_age_mins":   360,        # token ≤ 6 hrs old
    "monster_scanner_min_vol_liq":    5.0,        # vol/liq ≥ 5×
    "monster_scanner_max_vol_liq":    100.0,      # vol/liq ≤ 100× (wash-trade cap)
    "monster_scanner_min_buy_ratio":  52.0,       # buy ratio ≥ 52%

    # ── Split-buy ("Harvester + Monster Hunter") dual position system ──────────
    # When enabled: each qualifying token opens TWO positions simultaneously.
    # Position A (HARVESTER):       split_buy_a_sol, TP = entry × split_buy_a_tp_mult (+60%)
    # Position B (MONSTER HUNTER):  split_buy_b_sol, TP = entry × split_buy_b_tp_mult (+150%)
    # Both share: SL = entry × split_buy_sl_mult (0.80 = -20%)
    # All values are Jarvis-adjustable at runtime.
    "split_buy_enabled":        False,    # OFF by default — enable via Jarvis
    "split_buy_a_sol":          0.05,     # HARVESTER position size (SOL)
    "split_buy_b_sol":          0.025,    # MONSTER HUNTER position size (SOL)
    "split_buy_a_tp_mult":      1.60,     # HARVESTER TP: +60% full exit
    "split_buy_b_tp_mult":      2.50,     # MONSTER HUNTER TP: +150% full exit
    "split_buy_sl_mult":        0.80,     # both SL: entry × 0.80 = -20%
    "split_buy_trailing_pct":   0.15,     # trailing stop: 15% below peak (when trailing_stop_enabled=True)

    # ── Scout quality caps (Strategy C/D — Raydium, PumpSwap second-wave, Meteora) ──
    # Analysis 2026-03-23: all 4 losses had overbought m5/h1 and thin liq/MC.
    # These filters prevent buying parabolic exhaustion and thin-liquidity rugs.
    "scout_max_m5_pct":          15.0,   # monster entry is QUIET — m5>15% = mid-pump, buying exhaustion
    "scout_max_h1_pct":          80.0,   # if h1>80% the first wave is done; entry window has passed
    "scout_min_liq_mc_ratio":    0.05,   # min liq/MC = 5%; very loose floor (monsters have varied ratios)
    # Per-DEX minimum age: monster DNA shows sweet spot 2-20h.
    "scout_min_age_secs":      3600.0,   # 1h min for Raydium + Meteora scouts
    "pumpswap_min_age_secs":   7200.0,   # 2h min for PumpSwap scouts (not graduation snipe — that's Strategy B)
    "narrative_keyword_score":     0,    # bonus pts for viral keyword (0=disabled, 2=old default)
    "momentum_confirm_secs":     300,    # wait N secs after all gates pass, re-check price (0=disabled)
    # ── Macro event mode (research finding 2026-04-04) ───────────────────────
    # Enable during major political/macro events (elections, tariff announcements, Elon news).
    # Research: 8 of 15 monster tokens graduated within 72h of Trump Liberation Day tariffs.
    # When a macro event hits, political meme tokens flood pump.fun and graduation rate spikes.
    # Effect: relaxes m5 filter for Strategy B (+20% tolerance), raises FAST_GRAD_MAX_SECS to 3600s
    "macro_event_mode":        False,   # Jarvis toggles ON during major news events
    "macro_event_label":           "",  # human-readable description of the current event (e.g. "Liberation Day tariffs")
}

# ── Change log for Eliza to report back ──────────────────────────────────────
_change_log: list[dict] = []


def _load_persisted() -> None:
    """Load saved config from disk (survives bot restarts)."""
    try:
        if os.path.exists(_PERSIST_PATH):
            with open(_PERSIST_PATH) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in _config:
                    _config[k] = v
    except Exception:
        pass  # Use defaults if file is corrupt


# Keys locked to False — analysis loop cannot re-enable these.
# strategy_b and strategy_d are intentionally UN-PARKED (2026-04-17) — enabled by user.
_PARKED_STRATEGIES: set[str] = {
    "strategy_a2_enabled",
    "strategy_c_enabled",
    "strategy_e_enabled",
}


def _save_persisted() -> None:
    """Persist current config to disk, keeping parked strategies locked off."""
    try:
        data = dict(_config)
        for key in _PARKED_STRATEGIES:
            data[key] = False   # always write False regardless of in-memory state
        with open(_PERSIST_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def get(key: str, default=None):
    """Read a config value."""
    return _config.get(key, default)


def set_value(key: str, value, changed_by: str = "system", reason: str = "") -> tuple[bool, str]:
    """
    Update a config value. Returns (success, message).
    Validates type and range before applying.
    """
    if key not in _config:
        return False, f"Unknown config key: {key}"

    old = _config[key]
    old_type = type(old)

    # Type coercion
    try:
        if old_type is bool:
            if isinstance(value, str):
                value = value.lower() in ("true", "1", "yes", "on")
            else:
                value = bool(value)
        elif old_type is float:
            value = float(value)
        elif old_type is int:
            value = int(value)
    except (ValueError, TypeError):
        return False, f"Invalid value '{value}' for {key} (expected {old_type.__name__})"

    # Range validation
    _ranges = {
        "stop_loss_pct":          (0.05, 0.25),   # min 5% — prevents Jarvis ratcheting to 2%
        "pumpswap_stop_loss_pct": (0.04, 0.20),   # min 4% for PumpSwap (tighter market)
        "early_stop_loss_pct":    (0.03, 0.15),
        "tp1_mult":               (1.20, 10.0),   # min +20% TP — prevents Jarvis ratcheting to 5%
        "pf_tp1_mult":            (1.20, 5.0),    # min +20% for pump.fun BC
        "grok_tp_mult":           (1.30, 10.0),
        "fill_cap_pct":           (55.0, 82.0),
        "buy_sol":                (0.001, 10.0),
        "a2_buy_sol":             (0.001, 10.0),
        "strategy_b_min_liq_usd": (0, 200_000),
        "strategy_c_min_score":   (0, 16),
        "strategy_e_buy_sol":         (0.001, 10.0),
        "strategy_e_min_liq_usd":    (1000, 5_000_000),
        "strategy_e_min_age_hours":  (0.0, 72.0),
        "strategy_e_max_age_hours":  (1.0, 720.0),
        "strategy_e_min_h1_pct":     (-50.0, 100.0),
        "strategy_e_max_h1_pct":     (5.0, 500.0),
        "strategy_e_max_h24_pct":    (50.0, 10000.0),
        "strategy_e_min_score":      (0, 10),
        "strategy_e_min_volume_usd": (0, 10_000_000),
        "max_concurrent_positions": (1, 20),
        "max_daily_loss_pct":     (0.01, 1.0),
        "strategy_b_dip_wait_secs": (0, 300),
        "trading_window_start_utc": (0, 23),
        "trading_window_end_utc":   (0, 23),
        "strategy_b_buy_sol":       (0.0, 10.0),
        "strategy_c_buy_sol":       (0.0, 10.0),
        "strategy_d_buy_sol":       (0.0, 10.0),
        "a2_min_real_sol":          (0.5, 3.0),   # floor 0.5, cap 3.0 — Jarvis drifts to 8 which blocks all BC
        "a2_min_score":             (0, 100),
        "a2_max_age_secs":          (30, 3600),
        "rugcheck_max_score":       (6000, 200000),  # BC tokens score 20k-150k+ naturally; paper mode uses 100k
        "a2_min_holders":           (1, 100),
        "a2_max_whale_pct":         (10.0, 95.0),
        "a2_min_holder_growth_rate":    (0.0, 100.0),   # 0 = disabled
        "a2_holder_growth_window_secs": (60.0, 1800.0),
        "a2_min_mc_usd":            (0, 10_000_000),  # 0 = disabled, up to $10M cap
        "a2_min_liq_usd":           (0, 500_000),     # 0 = disabled
        "b_min_vol_liq_ratio":      (1.0, 5.0),     # monster entry: 1-3x. Floor 1x, cap 5x (above = post-pump)
        "b_max_vol_liq_ratio":      (5.0, 15.0),    # max cap range: 5-15x allowed
        "c_min_vol_liq_ratio":      (1.0, 5.0),     # Raydium scout — same
        "c_max_vol_liq_ratio":      (5.0, 15.0),    # 8x default; cap 15x absolute max
        "d_min_vol_liq_ratio":      (1.0, 5.0),
        "d_max_vol_liq_ratio":      (5.0, 15.0),
        # Buy ratio — 29-monster dataset: 50-65% is iron-clad. Floor 50%, ceiling 65%.
        # No exception for Raydium: even PISS/NoHat/LOLA were ~54-55%. 50% is safe floor.
        "d_min_buy_ratio":          (50.0, 65.0),
        "b_min_buy_ratio":          (50.0, 65.0),
        "c_min_buy_ratio":          (50.0, 65.0),   # raised from 45 — 50% is the iron-clad minimum
        "b_min_momentum_score":     (-100.0, 200.0),   # 0=disabled
        "c_min_momentum_score":     (-100.0, 200.0),
        "d_min_momentum_score":     (-100.0, 200.0),
        "post_sl_cooldown_secs":    (0.0, 1800.0),
        "smart_wallet_buy_sol":     (0.01, 5.0),
        "smart_wallet_max_age_secs": (10, 600),
        "smart_wallet_poll_secs":   (10, 300),
        "grok_premium_buy_sol":     (0.05, 10.0),
        "grok_premium_min_score":   (5, 10),
        "grok_premium_tp_mult":     (1.10, 5.0),
        "grok_premium_stall_secs":  (10, 300),
        "grok_premium_stall_band":  (0.005, 0.20),
        "grok_premium_stall_min_pct": (0.05, 0.90),
        "proven_creator_pct":         (0.10, 0.95),  # 10-95% of wallet
        "trailing_stop_pct":          (0.05, 0.50),  # 5-50% below peak
        "c_min_mc_usd":               (20_000, 500_000),   # $20k floor, $500k cap
        "c_min_liq_usd":              (20_000, 150_000),   # $20k floor (monsters start at $25k+)
        "d_min_mc_usd":               (20_000, 500_000),
        "d_min_liq_usd":              (20_000, 150_000),
        "d_min_liq_usd":              (0, 500_000),
        "graduation_confirmation_wait": (0.0, 1800.0),   # 0–30 min
        "monster_scanner_interval_secs": (60, 7200),
        "monster_scanner_min_liq_usd":  (0, 10_000_000),
        "monster_scanner_min_mc_usd":   (0, 10_000_000),
        "monster_scanner_min_age_mins": (0, 1440),
        "monster_scanner_max_age_mins": (1, 1440),
        "monster_scanner_min_vol_liq":  (0.0, 200.0),
        "monster_scanner_max_vol_liq":  (0.0, 1000.0),
        "monster_scanner_min_buy_ratio": (0.0, 95.0),
        "split_buy_a_sol":         (0.001, 10.0),
        "split_buy_b_sol":         (0.001, 10.0),
        "split_buy_a_tp_mult":     (1.10, 20.0),
        "split_buy_b_tp_mult":     (1.10, 20.0),
        "split_buy_sl_mult":       (0.50, 0.95),   # 0.80 = -20% SL, 0.50 = -50% SL
        "split_buy_trailing_pct":  (0.05, 0.50),
        # Scout quality caps
        "scout_max_m5_pct":        (20.0, 50.0),   # floor 20 — health check enforces this; Jarvis kept drifting to 15
        "scout_max_h1_pct":        (50.0, 300.0),   # floor 50 — last night's losses all had h1 >100%; don't let Jarvis set below 50
        "scout_min_liq_mc_ratio":  (0.0, 1.0),
        "scout_min_age_secs":      (1800.0, 86400.0),  # floor 30min — Raydium monsters were 6-10h old
        "pumpswap_min_age_secs":   (7200.0, 86400.0),  # floor 2h — PumpSwap monsters were 14-48h at peak; never enter <2h
        "narrative_keyword_score": (0, 0),   # HARDWIRED at 0 — viral keyword bonus disabled (rewarded meme rugs)
        "momentum_confirm_secs":   (0, 600),
        # Macro event mode — only settable via explicit Jarvis command, not analysis drift
        "macro_event_mode":        (False, True),   # bool only (validated separately as bool)
        "macro_event_label":       (0, 0),          # string key — range check skipped for strings
    }
    if key in _ranges:
        lo, hi = _ranges[key]
        if not (lo <= value <= hi):
            return False, f"{key} must be between {lo} and {hi}, got {value}"

    _config[key] = value
    _save_persisted()

    entry = {
        "timestamp": time.time(),
        "key": key,
        "old_value": old,
        "new_value": value,
        "changed_by": changed_by,
        "reason": reason,
    }
    _change_log.append(entry)
    if len(_change_log) > 100:
        _change_log.pop(0)

    return True, f"{key}: {old} → {value}"


def all_config() -> dict:
    """Return a snapshot of the full config."""
    return dict(_config)


def recent_changes(n: int = 10) -> list[dict]:
    """Return the last n config changes."""
    return list(reversed(_change_log[-n:]))


# Load persisted config on module import
_load_persisted()
