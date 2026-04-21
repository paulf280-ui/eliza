"""
axiom_copy_trader.py — Real-time copy-trade monitor for Axiom leaderboard wallets.

Watches confirmed whale/smart-money wallets from the Axiom leaderboard.
When they enter a token we open a PAPER position immediately.
When they exit we close our paper position and record the P&L.

PAPER TRADING (48h test from 2026-04-05):
  Virtual balance: 10 SOL
  Size per trade:  0.8 SOL (fixed, regardless of their size)
  All strategies continue live — this runs alongside them
  Jarvis cannot change copy_trade_* keys while paper testing

Wallet profiles (deep-dive analysis 2026-04-05):
  Trenchman    Hw5UKBU5  404 SOL  pump.fun BC 80%  12min hold  +598 SOL PNL
  Radiance     G6fUXjMK  1113 SOL pump.fun BC 68%  37min hold  +410 SOL PNL
  BAr_Whale    BAr5csYt  373 SOL  pump.fun BC 53%  large conv  30 SOL on BURNIE
  Teddy        teddyYXw  47 SOL   pumpswap   67%   7h hold     +326 SOL PNL
  Bundler_FAi  FAicXNV5  ?  SOL   bundler sniper   1-2 SOL/entry, Jito tips

Excluded wallets (reasons):
  GdRSPexh  — arbitrageur routing through USD1 stablecoin pairs, not a meme sniper
  4vw54BmA  — DCA bot: exact 2.009 SOL amounts, 25x same mint, automated follower
  4uCT4g7Y  — unclear strategy, mixed tiny probes + occasional medium buys
  5ghUXGD9  — Meteora DLMM LP provider (INITIALIZE_POSITION / GO_TO_A_BIN txs, LBUZKhRx program)

Config keys (bot_config.json) — ALL PROTECTED while paper_trading=True:
  copy_trade_paper_enabled  bool   True   — master switch for paper mode
  copy_trade_paper_buy_sol  float  0.8    — fixed SOL per paper trade
  copy_trade_paper_balance  float  10.0   — starting virtual balance
  copy_trade_min_sol        float  0.5    — ignore whale entries smaller than this
  copy_trade_consensus      int    1      — wallets needed to enter (1=any single wallet)
  copy_trade_enabled        bool   False  — live trading switch (stays False during paper test)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Any

import aiohttp

# ── Watched wallets ───────────────────────────────────────────────────────────
WATCHED_WALLETS: dict[str, str] = {
    # ── REVISED 2026-04-13 — Full wallet rotation after all 4 prior wallets shifted to
    #    sub-2s micro-scalping (stale signal on every trade). New wallets selected via
    #    Kolscan weekly leaderboard + Helius on-chain hold-time verification.
    # ── REMOVED 2026-04-13: Axiom_W1 (sub-1s scalper, 0% hold >5 min), clukz (same),
    #                        Goyim (completely inactive, 0 signals in 24h),
    #                        CookDoc (sub-1s scalper, huvey/MONOgirl/Razr all stale)
    # ── BLACKLISTED: Bundler_EeX (BC bundler — all unindexed rugs, -0.59 SOL live)
    # ── BLACKLISTED: Gang_0SOL   (25%WR, worst performer, -0.37 SOL live)
    # ── BLACKLISTED: Cented      (175 trades/day, uncopyable speed)
    # ── REMOVED 2026-04-18: rambo. 7 trades, 29% WR, -0.040 SOL net, -0.006 SOL/trade.
    #   71 signals over the data window, 7 filled (10%). 54 of 64 skips were
    #   whale_already_sold — rambo is a sub-2s scalper we can't catch in time.
    #   Negative expectancy + low fill rate = remove.
    "Frost":   "4nwfXw7n98jEQn93VWY7Cuf1jnn1scHXuXCPGVYS9k6T",  # Kolscan #12 weekly | +0.408 SOL net (62% WR, 8 trades) | median hold 7 min
    # ── REMOVED 2026-04-20: Walta. n=8, net -0.1010 SOL, 1/8 WR. Worst trade -50.5%
    #   on "Don't Go To Standfor" (flash-rug at 01:06 UTC Apr 20 — SL fired at -10%
    #   but execution slipped to -50% when PumpPortal routing failed during rug).
    #   Flash-rug exposure pattern: the 1 big win (+89x on ASTEROID) is not enough
    #   to compensate for the single-trade drawdown risk.
    # ── RE-PROMOTED 2026-04-18: clukz — 20 trades, 45% WR, +0.397 SOL net. 73%
    #   zero-peak losses (bundler-adjacent) BUT 2 monster wins (+141.6%, +149.9%).
    #   Original removal reason (sub-1s scalper) may have been premature — 20 fills
    #   on live paper trading actually materialised. With 10% hard SL capping the
    #   zero-peak rugs at -0.035 SOL each, monster-win expectancy is net positive.
    "clukz":   "G6fUXjMKPJzCY1rveAE6Qm7wy5U3vZgKDJmN1VPAdiZC",  # 20 trades, 45%WR, +0.397 SOL
    # SLOT OPEN ×1 — candidates Wallet_PMJA / Teddy detected in signal log but 0
    # paper trade history yet. Consider after 3+ trades via wallet_promotion.py.
}

# Wallets using trading terminals/aggregators that never appear in logsSubscribe.
# Monitored via getSignaturesForAddress polling instead of WebSocket.
# NOTE: All 2026-04-13 wallets trade directly — no aggregator polling needed.
_POLL_ONLY_WALLETS: set[str] = {
    # (empty — new wallets all use standard DEX, visible in logsSubscribe)
}

# ── Blacklisted wallets — never copy these, ever ──────────────────────────────
_BLACKLISTED_WALLETS: set[str] = {
    "EeXvxkcGqMDZeTaVeawzxm9mbzZwqDUMmfG3bF7uzumH",  # Bundler_EeX — BC bundler, rugs
    "gangJEP5geDHjPVRhDS5dTF5e6GtRvtNogMEEVs91RV",   # Gang_0SOL   — 25%WR loser
    "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o",  # Cented — 175 trades/day, uncopyable speed
    "5hAgYC8TJCcEZV7LTXAzkTrm7YL29YXyQQJPCNrG84zM",  # Schoen — pump-and-dump, -0.33 SOL (-0.12/day), 87% stale signals, cut 2026-04-16
    "39q2g5tTQn9n7KnuapzwS2smSx3NGYqBoea11tBjsGEt",  # Walta — flash-rug exposure, -0.1010 SOL (1/8 WR), cut 2026-04-20
    # ── Retired 2026-04-13 (all confirmed micro-scalpers, 0% hold >5 min) ──
    "DsqRyTUh1R37asYcVf1KdX4CNnz5DKEFmnXvgT4NfTPE",  # Axiom_W1 — sub-1s scalper
    # clukz RE-PROMOTED 2026-04-18 — see WATCHED_WALLETS above
    "G3gZWqrYkNmYFKYCyfRCNtGuxdyuE2wiYKkZpiZn4WSS",  # Goyim — inactive 24h+
    "Dvbv5TdAyPpJk16X9mUxWFVicYtCUxTLhuof8TGuUaRv",  # CookDoc — sub-1s scalper
}

# Reverse map: address → name (for exit detection)
_ADDR_TO_NAME: dict[str, str] = {v: k for k, v in WATCHED_WALLETS.items()}

# Stablecoins / WSOL — ignore buys of these
_SKIP_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",       # WSOL
    "USD1ttGY1N17NEEH4tLSfBJBkmBQZxSBRAZrMPAbDLDp",   # USD1 stablecoin
}

# ── State ─────────────────────────────────────────────────────────────────────

# Recent entries buffer for consensus: mint → [{wallet_name, ts, sol_amount}]
_recent_entries: dict[str, list[dict]] = defaultdict(list)
_CONSENSUS_WINDOW_SECS = 300  # 5 minutes

# Rapid-flip tracker: (wallet_name, mint) → [ts, ...] of stale-signal blocks
# If a wallet has been stale-blocked on the same token 2+ times in 10 min,
# the pattern is "scalp flipping" — never enter on subsequent signals.
_stale_flip_log: dict[tuple, list[float]] = defaultdict(list)
_FLIP_WINDOW_SECS  = 600   # 10-minute rolling window
_FLIP_BLOCK_COUNT  = 2     # block after this many stale signals on same (wallet, mint)

# Latest signature seen per wallet (for new-tx detection)
_last_sig: dict[str, str] = {}

# Paper positions: mint → {entry_price, sol_spent, entry_ts, token_name, wallet_name, ...}
_paper_positions: dict[str, dict] = {}

# ── Frost Mirror Strategy — DISABLED 2026-04-17 ───────────────────────────────
# Frost is now treated as a standard copy-trade wallet (normal SL/TP rules).
# Mirror mode removed: dashboard Frost Mirror section removed, Frost joins the
# standard position monitor with the same 10% SL / 40% TP as all other wallets.
_FROST_MIRROR_WALLETS: set[str] = set()  # disabled — Frost = standard wallet

# Positions keyed by "mint:seq" to allow multiple positions per mint
_frost_positions: dict[str, dict] = {}
_frost_closed:    list[dict]      = []
_frost_seq:       int             = 0

# Frost mirror constants (user-configured, not overridable by Jarvis)
FROST_LOTTO_THRESHOLD_SOL = 0.5    # whale buy below this → lottery mode
FROST_MAIN_SOL    = 0.15           # our main-bet size
FROST_LOTTO_SOL   = 0.08           # our lottery-ticket size
FROST_MAIN_SL_PCT = 25.0           # -25% SL on main bets
FROST_LOTTO_SL_PCT = 60.0          # -60% SL on lottery tickets (wide runway)

# Paper trading equity (updated on each close)
_paper_balance: float = 10.0  # virtual SOL

# Live wallet balance — cached every 60s for compounding position sizing
_cached_wallet_sol: float = 0.0

# Daily loss tracking — reset at UTC midnight, halts trading when exceeded
_daily_loss_sol: float = 0.0
_daily_loss_reset_day: int = 0  # UTC day number of last reset

# Per-wallet rolling loss window: wallet_name → list of (timestamp, abs_loss_sol)
# Used to pause a wallet's signals after it causes too many losses in a short window.
_wallet_loss_window: dict[str, list[tuple[float, float]]] = defaultdict(list)

# ── Persistence paths ─────────────────────────────────────────────────────────
_DIR = os.path.dirname(__file__)
_ALERTS_PATH       = os.path.join(_DIR, "copy_trade_alerts.json")
_PAPER_TRADES_PATH = os.path.join(_DIR, "copy_trade_paper_trades.json")
_PAPER_OPEN_PATH   = os.path.join(_DIR, "copy_trade_paper_open.json")
_SIGNAL_LOG_PATH   = os.path.join(_DIR, "copy_trade_signal_log.json")   # every detected signal
_HOURLY_PATH       = os.path.join(_DIR, "copy_trade_hourly.json")        # hourly P&L snapshots
_SESSION_PATH      = os.path.join(_DIR, "copy_trade_session.json")       # session metadata
_FROST_TRADES_PATH = os.path.join(_DIR, "frost_mirror_trades.json")      # Frost mirror closed trades
_FROST_OPEN_PATH   = os.path.join(_DIR, "frost_mirror_open.json")        # Frost mirror open positions

_alert_log:       list[dict] = []
_paper_trades:    list[dict] = []  # closed paper trades
_signal_log:      list[dict] = []  # every detected wallet signal (entered or not)
_hourly_log:      list[dict] = []  # hourly snapshots
_session_start:   float = time.time()

# Golden rule: never re-enter a token we've ever traded (win or loss)
_traded_mints:    set[str] = set()


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_all() -> None:
    global _alert_log, _paper_trades, _paper_positions, _paper_balance
    global _signal_log, _hourly_log, _session_start, _traded_mints
    global _frost_closed, _frost_positions, _frost_seq
    try:
        if os.path.exists(_ALERTS_PATH):
            data = json.load(open(_ALERTS_PATH))
            _alert_log = data if isinstance(data, list) else []
    except Exception:
        _alert_log = []

    try:
        if os.path.exists(_PAPER_TRADES_PATH):
            data = json.load(open(_PAPER_TRADES_PATH))
            _paper_trades = data if isinstance(data, list) else []
    except Exception:
        _paper_trades = []

    # Rebuild golden-rule blacklist from all closed trades
    _traded_mints = {t["mint"] for t in _paper_trades if t.get("mint")}
    if _traded_mints:
        print(f"[copy-trade] Golden rule: {len(_traded_mints)} previously traded mints loaded — no re-entry")

    try:
        if os.path.exists(_PAPER_OPEN_PATH):
            d = json.load(open(_PAPER_OPEN_PATH))
            _paper_positions = d.get("positions", {})
            _paper_balance   = float(d.get("balance", 10.0))
    except Exception:
        _paper_positions = {}
        _paper_balance   = 10.0

    try:
        if os.path.exists(_SIGNAL_LOG_PATH):
            data = json.load(open(_SIGNAL_LOG_PATH))
            _signal_log = data if isinstance(data, list) else []
    except Exception:
        _signal_log = []

    try:
        if os.path.exists(_HOURLY_PATH):
            data = json.load(open(_HOURLY_PATH))
            _hourly_log = data if isinstance(data, list) else []
    except Exception:
        _hourly_log = []

    try:
        if os.path.exists(_SESSION_PATH):
            d = json.load(open(_SESSION_PATH))
            _session_start = float(d.get("session_start", time.time()))
    except Exception:
        pass

    # ── Frost Mirror persistence ───────────────────────────────────────────────
    try:
        if os.path.exists(_FROST_TRADES_PATH):
            data = json.load(open(_FROST_TRADES_PATH))
            _frost_closed = data if isinstance(data, list) else []
    except Exception:
        _frost_closed = []
    try:
        if os.path.exists(_FROST_OPEN_PATH):
            d = json.load(open(_FROST_OPEN_PATH))
            _frost_positions = d.get("positions", {})
            _frost_seq       = int(d.get("seq", 0))
    except Exception:
        _frost_positions = {}
        _frost_seq = 0


def _save_all() -> None:
    try:
        with open(_ALERTS_PATH, "w") as f:
            json.dump(_alert_log[-500:], f, indent=2)
    except Exception:
        pass
    try:
        with open(_PAPER_TRADES_PATH, "w") as f:
            json.dump(_paper_trades, f, indent=2)
    except Exception:
        pass
    try:
        with open(_PAPER_OPEN_PATH, "w") as f:
            json.dump({"positions": _paper_positions, "balance": _paper_balance}, f, indent=2)
    except Exception:
        pass
    try:
        with open(_SIGNAL_LOG_PATH, "w") as f:
            json.dump(_signal_log, f, indent=2)
    except Exception:
        pass
    try:
        with open(_HOURLY_PATH, "w") as f:
            json.dump(_hourly_log, f, indent=2)
    except Exception:
        pass
    try:
        with open(_FROST_TRADES_PATH, "w") as f:
            json.dump(_frost_closed, f, indent=2)
    except Exception:
        pass
    try:
        with open(_FROST_OPEN_PATH, "w") as f:
            json.dump({"positions": _frost_positions, "seq": _frost_seq}, f, indent=2)
    except Exception:
        pass


# ── Public accessors ──────────────────────────────────────────────────────────

def get_recent_alerts(n: int = 20) -> list[dict]:
    if not _alert_log and not _paper_trades:
        _load_all()
    return _alert_log[-n:]


def get_paper_stats() -> dict:
    """Return paper trading stats for the dashboard."""
    if not _paper_trades and not _alert_log:
        _load_all()
    closed = _paper_trades
    wins   = [t for t in closed if t.get("pnl_sol", 0) > 0]
    losses = [t for t in closed if t.get("pnl_sol", 0) <= 0]
    net_pnl  = sum(t.get("pnl_sol", 0) for t in closed)
    avg_win  = (sum(t.get("pnl_pct", 0) for t in wins)   / len(wins))   if wins   else 0.0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0.0
    best     = max((t.get("pnl_pct", 0) for t in closed), default=0.0)

    try:
        from elizaos.plugins.solana import live_config as _lc_s
        _base       = float(_lc_s.get("copy_trade_paper_buy_sol", 0.35))
        _copy_live  = bool(_lc_s.get("copy_trade_enabled", False))
        # Dashboard banner reflects whether ANY strategy is trading real SOL.
        # In the monster-only era copy-trade is off but monster runs LIVE.
        import os as _os
        _monster_enabled = _os.getenv("MONSTER_STRATEGY_ENABLED", "false").strip().lower() in ("1","true","yes","on")
        _monster_paper   = _os.getenv("MONSTER_PAPER_ONLY", "true").strip().lower() in ("1","true","yes","on")
        _monster_live    = _monster_enabled and not _monster_paper
        _live_mode  = _copy_live or _monster_live
        _paused     = bool(_lc_s.get("copy_trade_paused", False))
    except Exception:
        _base      = 0.35
        _live_mode = False
        _paused    = False

    if _live_mode:
        _trade_size = _base  # fixed in live mode
    else:
        _compound   = round(_paper_balance * 0.20 / 5, 3)
        _trade_size = max(_base, _compound)

    return {
        "trades":        len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      (len(wins) / len(closed) * 100) if closed else 0.0,
        "net_pnl":       round(net_pnl, 4),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "best_trade":    round(best, 2),
        "balance":       round(_paper_balance, 4),
        "trade_size":    round(_trade_size, 3),
        "live_mode":     _live_mode,
        "paused":        _paused,
        "open_count":    len(_paper_positions),
        "max_positions":  int(_lc_s.get("trenchman_max_positions", 2)),
        "open_positions": [
            {
                "mint":               mint,
                "token_name":         p["token_name"],
                "wallet":             p["wallet_name"],
                "entry_ts":           p["entry_ts"],
                "sol_spent":          p["sol_spent"],
                "entry_price":        p.get("entry_price", 0),
                "current_price":      p.get("current_price"),
                "pnl_pct":            p.get("pnl_pct"),
                "mc_usd":             p.get("mc_usd"),
                # Moonbag fields
                "tp1_hit":            p.get("tp1_hit", False),
                "tp2_hit":            p.get("tp2_hit", False),
                "locked_sol":         round(p.get("locked_sol") or 0.0, 4),
                "remaining_fraction": round(p.get("remaining_fraction") or 1.0, 3),
                "narrative":          p.get("narrative", "unknown"),
                "moonbag_ceiling_pct": p.get("moonbag_ceiling_pct", 400.0),
                # Copy-trade partial TP ladder state (2026-04-18)
                "cp_l1_hit":          p.get("cp_l1_hit", False),
                "cp_l2_hit":          p.get("cp_l2_hit", False),
                "cp_l3_hit":          p.get("cp_l3_hit", False),
                "peak_pnl_pct":       round(p.get("peak_pnl_pct") or 0.0, 1),
                "ai_entry_verdict":   p.get("ai_entry_verdict"),
                # Keep last 5 partial exits — dashboard shows tranche chips
                "partial_exits":      (p.get("partial_exits") or [])[-5:],
            }
            for mint, p in _paper_positions.items()
        ],
    }


# ── Frost Mirror — core functions ─────────────────────────────────────────────

async def _frost_open_position(mint: str, token_name: str, whale_sol: float,
                                session: aiohttp.ClientSession, runtime: Any = None) -> None:
    """Open a Frost mirror position — main bet or lottery ticket based on whale size."""
    global _frost_seq
    mode    = "lotto" if whale_sol < FROST_LOTTO_THRESHOLD_SOL else "main"
    our_sol = FROST_LOTTO_SOL if mode == "lotto" else FROST_MAIN_SOL
    sl_pct  = FROST_LOTTO_SL_PCT if mode == "lotto" else FROST_MAIN_SL_PCT

    price, _mc, _ = await _get_price_and_mc(mint, session)
    if not price or price <= 0:
        print(f"[frost-mirror] ⚠️  No price for {token_name} ({mint[:8]}) — skipping")
        return

    # ── Live execution (when copy_trade_enabled=True) ─────────────────────
    from elizaos.plugins.solana import live_config as _lc_fm
    _frost_live = bool(_lc_fm.get("copy_trade_enabled", False))
    _frost_buy_sig: str | None = None
    if _frost_live and runtime:
        _buy_ok, _frost_buy_sig, _ = await _execute_live_buy(
            mint, token_name, our_sol, session, runtime
        )
        if not _buy_ok:
            print(f"[frost-mirror] ❌ Live buy failed for {token_name} — not tracking position")
            return
        print(f"[frost-mirror] ✅ Live buy confirmed for {token_name} ({mint[:8]}) sig={(_frost_buy_sig or '')[:16]}...")

    _frost_seq += 1
    key = f"{mint}:{_frost_seq}"
    _frost_positions[key] = {
        "mint":         mint,
        "token_name":   token_name,
        "mode":         mode,
        "sol_spent":    our_sol,
        "whale_sol":    whale_sol,
        "entry_price":  price,
        "current_price": price,
        "pnl_pct":      0.0,
        "peak_pnl_pct": 0.0,
        "sl_pct":       sl_pct,
        "entry_ts":     time.time(),
        "live":         _frost_live,
        "buy_sig":      _frost_buy_sig,
    }
    _save_all()

    open_on_token = len([k for k in _frost_positions if k.startswith(mint + ":")])
    emoji = "🎰" if mode == "lotto" else "🎯"
    print(
        f"[frost-mirror] {emoji} OPEN {mode.upper()}: {token_name} ({mint[:8]}) "
        f"Frost={whale_sol:.3f} SOL → us={our_sol:.2f} SOL  SL=-{sl_pct:.0f}%  "
        f"(#{open_on_token} position on this token)"
    )
    try:
        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_f
        asyncio.create_task(_tg_f(
            f"{emoji} <b>Frost Mirror OPEN</b> [{mode.upper()}]: {token_name}\n"
            f"Frost spent {whale_sol:.3f} SOL → we put in {our_sol:.2f} SOL\n"
            f"SL: -{sl_pct:.0f}%  |  Exit: wallet mirror only"
        ))
    except Exception:
        pass


async def _frost_close_position(key: str, reason: str,
                                 session: aiohttp.ClientSession, runtime: Any = None) -> None:
    """Close one Frost mirror position by key and record it.

    If the position was opened live (pos['live']=True), executes a real on-chain sell
    before recording the close.  Jito tips are aggressive:
      - wallet_exit: 0.005 SOL — Frost already sold, we're chasing fast
      - stop_loss:   0.003 SOL — SL trigger, still want speed
      - safety_net:  0.004 SOL — safety net catch, moderate urgency
    """
    pos = _frost_positions.get(key)
    if not pos:
        return

    # ── Live execution (watertight sell before we remove the position) ────
    if pos.get("live") and runtime:
        from elizaos.plugins.solana import live_config as _lc_fc
        if bool(_lc_fc.get("copy_trade_enabled", False)):
            if "wallet_exit" in reason:
                _jito_fee = 0.005   # Frost already sold — chase fast
            elif "stop_loss" in reason:
                _jito_fee = 0.003   # SL trigger
            else:
                _jito_fee = 0.004   # safety net / other

            # ── Proportional sell when other positions share the same mint ──
            # e.g. both main (0.15 SOL) and lotto (0.08 SOL) are open → closing
            # main should sell only main's fraction of the combined token balance,
            # leaving lotto's tokens intact for its own eventual exit.
            _other_keys = [
                k for k in _frost_positions
                if k != key and k.startswith(pos["mint"] + ":")
            ]
            _token_amount_ui: float | None = None
            if _other_keys:
                # Compute this position's proportional share of combined sol_spent
                _total_sol = pos["sol_spent"] + sum(
                    _frost_positions[k]["sol_spent"]
                    for k in _other_keys
                    if k in _frost_positions
                )
                _this_frac = pos["sol_spent"] / _total_sol if _total_sol > 0 else 1.0
                # Get current on-chain balance to convert fraction → token count
                try:
                    _wallet_svc = runtime.get_service("wallet")
                    _our_pk = os.getenv("SOLANA_PUBLIC_KEY", "")
                    if _wallet_svc and _our_pk:
                        _cur_bal = await asyncio.wait_for(
                            _wallet_svc.rpc.get_token_balance(_our_pk, pos["mint"]),
                            timeout=5.0,
                        )
                        if _cur_bal > 0:
                            _token_amount_ui = _cur_bal * _this_frac
                            print(
                                f"[frost-mirror] ↕ Proportional sell: {pos.get('mode','?')} "
                                f"fraction={_this_frac:.2%} → {_token_amount_ui:.4f} tokens "
                                f"(keeping {_cur_bal - _token_amount_ui:.4f} for lotto)"
                            )
                except Exception as _frac_err:
                    print(f"[frost-mirror] Fraction calc failed ({_frac_err}) — falling back to 100% sell")

            _sell_ok, _sell_sig = await _execute_live_sell(
                pos["mint"], pos["token_name"], session, runtime,
                priority_fee=_jito_fee,
                token_amount_ui=_token_amount_ui,
            )
            if not _sell_ok:
                print(
                    f"[frost-mirror] ⚠️  Live sell failed for {pos['token_name']} "
                    f"({pos['mint'][:8]}) reason={reason} — will retry next SL tick"
                )
                # Restore position so the SL loop retries on next price refresh
                return
            print(
                f"[frost-mirror] ✅ Live sell confirmed for {pos['token_name']} "
                f"({pos['mint'][:8]}) jito={_jito_fee:.3f}SOL sig={(_sell_sig or '')[:16]}..."
            )

    # Remove from open positions (after confirmed sell or paper mode)
    _frost_positions.pop(key, None)

    current   = pos.get("current_price") or pos["entry_price"]
    entry     = pos["entry_price"]
    pnl_pct   = (current - entry) / entry * 100 if entry > 0 else 0.0
    pnl_sol   = pos["sol_spent"] * pnl_pct / 100
    hold_mins = (time.time() - pos["entry_ts"]) / 60
    mode      = pos.get("mode", "main")

    emoji = "🎰" if mode == "lotto" else "🎯"
    sign  = "✅" if pnl_pct >= 0 else "❌"
    print(
        f"[frost-mirror] {emoji} CLOSE {sign}: {pos['token_name']} ({pos['mint'][:8]}) "
        f"{pnl_pct:+.1f}% | {pnl_sol:+.4f} SOL | hold={hold_mins:.0f}m | {reason}"
    )

    record = {
        "ts":           pos["entry_ts"],
        "sell_ts":      time.time(),
        "mint":         pos["mint"],
        "token_name":   pos["token_name"],
        "mode":         mode,
        "sol_spent":    pos["sol_spent"],
        "whale_sol":    pos.get("whale_sol", 0),
        "entry_price":  entry,
        "exit_price":   current,
        "pnl_pct":      round(pnl_pct, 2),
        "pnl_sol":      round(pnl_sol, 4),
        "peak_pnl_pct": round(pos.get("peak_pnl_pct", 0), 2),
        "hold_mins":    round(hold_mins, 1),
        "reason":       reason,
    }
    _frost_closed.append(record)
    _save_all()

    try:
        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_fc
        asyncio.create_task(_tg_fc(
            f"{emoji} <b>Frost Mirror CLOSE</b> [{mode.upper()}]: {pos['token_name']}\n"
            f"P&L: {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL) | hold {hold_mins:.0f}m | {reason}"
        ))
    except Exception:
        pass


async def _frost_close_all_for_mint(mint: str, reason: str,
                                     session: aiohttp.ClientSession, runtime: Any = None) -> None:
    """Close all Frost mirror positions on a given mint (both main and lotto)."""
    keys = [k for k in list(_frost_positions.keys()) if k.startswith(mint + ":")]
    if keys:
        print(f"[frost-mirror] 🔄 Closing {len(keys)} position(s) on {mint[:8]} — {reason}")
    for key in keys:
        if key in _frost_positions:
            await _frost_close_position(key, reason, session, runtime)


async def _frost_close_mode_for_mint(mint: str, mode: str, reason: str,
                                      session: aiohttp.ClientSession,
                                      runtime: Any = None) -> None:
    """Close only positions of a specific mode (main or lotto) for a given mint.

    Used when Frost partially exits — he still holds the token (lotto tranche) so
    we should only close the matching mode, not everything.
    """
    keys = [
        k for k, p in list(_frost_positions.items())
        if p["mint"] == mint and p.get("mode") == mode
    ]
    if not keys:
        # Fallback: if we can't find a matching-mode position, close all (safer than holding)
        print(f"[frost-mirror] ⚠️  No {mode} position for {mint[:8]} — falling back to close-all")
        await _frost_close_all_for_mint(mint, reason, session, runtime)
        return
    print(f"[frost-mirror] ↗ Partial exit: closing {len(keys)} {mode} position(s) for {mint[:8]}, keeping lotto")
    for key in keys:
        if key in _frost_positions:
            await _frost_close_position(key, reason, session, runtime)


async def _frost_whale_still_holds(mint: str, frost_addr: str,
                                    session: aiohttp.ClientSession) -> bool:
    """Return True if Frost's wallet still holds a non-zero balance of mint.

    Called immediately after detecting a Frost sell to determine whether it was
    a partial exit (he still holds lotto) or a full exit (he's completely out).
    Uses a 4s timeout — if the RPC call fails we conservatively return False
    (treat as full exit, close everything) to avoid being stuck in a trade.
    """
    try:
        helius_key = os.getenv("HELIUS_API_KEY", "")
        rpc_url = (
            f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            if helius_key else os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        )
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                frost_addr,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        }
        async with session.post(
            rpc_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=4),
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            for acct in accounts:
                ui = (
                    acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                    .get("tokenAmount", {})
                    .get("uiAmount", 0) or 0
                )
                if float(ui) > 0:
                    return True
            return False
    except Exception as _e:
        print(f"[frost-mirror] _frost_whale_still_holds RPC error: {_e} — treating as full exit")
        return False


def get_frost_mirror_stats() -> dict:
    """Return Frost mirror stats for the dashboard API."""
    closed = list(_frost_closed)
    open_list = list(_frost_positions.values())

    wins   = [t for t in closed if t.get("pnl_sol", 0) > 0]
    losses = [t for t in closed if t.get("pnl_sol", 0) <= 0]
    net_pnl    = sum(t.get("pnl_sol", 0) for t in closed)
    unrealised = sum(
        pos["sol_spent"] * pos.get("pnl_pct", 0) / 100 for pos in open_list
    )
    main_closed  = [t for t in closed if t.get("mode") == "main"]
    lotto_closed = [t for t in closed if t.get("mode") == "lotto"]

    now = time.time()
    return {
        "total":         len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "net_pnl":       round(net_pnl, 4),
        "unrealised_pnl": round(unrealised, 4),
        "open_count":    len(open_list),
        "main_count":    len(main_closed),
        "lotto_count":   len(lotto_closed),
        "main_net":      round(sum(t.get("pnl_sol", 0) for t in main_closed), 4),
        "lotto_net":     round(sum(t.get("pnl_sol", 0) for t in lotto_closed), 4),
        "closed_trades": list(reversed(closed[-100:])),
        "open_positions": [
            {
                "mint":         p["mint"],
                "token_name":   p["token_name"],
                "mode":         p.get("mode", "main"),
                "sol_spent":    p.get("sol_spent", 0),
                "whale_sol":    p.get("whale_sol", 0),
                "entry_price":  p.get("entry_price", 0),
                "current_price": p.get("current_price", 0),
                "pnl_pct":      round(p.get("pnl_pct", 0), 2),
                "peak_pnl_pct": round(p.get("peak_pnl_pct", 0), 2),
                "entry_ts":     p.get("entry_ts", 0),
                "hold_mins":    round((now - p.get("entry_ts", now)) / 60, 1),
                "sl_pct":       p.get("sl_pct", FROST_MAIN_SL_PCT),
                "price_history": p.get("price_history", []),
            }
            for p in open_list
        ],
    }


# ── Helius helpers ────────────────────────────────────────────────────────────

async def _helius_get_sigs(wallet: str, session: aiohttp.ClientSession,
                           helius_key: str, limit: int = 5) -> list[str]:
    url = (f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
           f"?api-key={helius_key}&limit={limit}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            return [tx.get("signature", "") for tx in data if tx.get("signature")]
    except Exception:
        return []


async def _helius_get_tx(sig: str, session: aiohttp.ClientSession,
                         helius_key: str) -> dict | None:
    url = f"https://api.helius.xyz/v0/transactions?api-key={helius_key}"
    try:
        async with session.post(url, json={"transactions": [sig]},
                                timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data[0] if data else None
    except Exception:
        return None



async def _check_whale_still_holds(wallet_addr: str, mint: str,
                                   session: aiohttp.ClientSession) -> bool:
    """Quick Helius RPC check: does wallet still hold this token?
    Returns True (safe to enter) if the wallet has a non-zero balance OR if the check fails.
    Returns False only when balance is confirmed 0 — meaning the whale already sold.
    """
    import re as _re2
    import os as _os2
    _rpc_url = _os2.environ.get("SOLANA_RPC_URL", "")
    _m = _re2.search(r"api-key=([^ &]+)", _rpc_url)
    _hkey = _m.group(1) if _m else ""
    if not _hkey:
        return True  # can't check, assume safe
    try:
        _url = f"https://mainnet.helius-rpc.com/?api-key={_hkey}"
        _payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet_addr,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        }
        async with session.post(_url, json=_payload,
                                timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return True  # assume safe on RPC error
            data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            for acc in accounts:
                ui = (acc.get("account", {}).get("data", {})
                          .get("parsed", {}).get("info", {})
                          .get("tokenAmount", {}).get("uiAmount", 0))
                if ui and ui > 0:
                    return True  # whale still holds ✓
            return False  # balance confirmed 0 — whale already sold
    except Exception:
        return True  # assume safe on any error


def _parse_buy(tx: dict, wallet: str) -> tuple[str, float] | None:
    """Return (mint, sol_amount) if tx is a token BUY by wallet, else None.

    IMPORTANT: pump.fun BC buys also arrive as type="UNKNOWN" from Helius —
    same issue as sells. We detect by economic signature: SOL leaves + token arrives.
    """
    tx_type = tx.get("type", "")
    if tx_type in ("NFT_SALE", "NFT_MINT", "NFT_BID", "COMPRESSED_NFT_MINT",
                   "COMPRESSED_NFT_BURN", "CREATE_ACCOUNT", "CLOSE_ACCOUNT"):
        return None
    nt = tx.get("nativeTransfers", [])
    tt = tx.get("tokenTransfers", [])
    sol_out = sum(t.get("amount", 0) / 1e9 for t in nt
                  if t.get("fromUserAccount") == wallet)
    if sol_out < 0.001:
        return None
    tok_in = [
        t.get("mint", "")
        for t in tt
        if t.get("toUserAccount") == wallet and t.get("mint") not in _SKIP_MINTS
    ]
    if not tok_in:
        return None
    return tok_in[0], sol_out


def _parse_sell(tx: dict, wallet: str) -> tuple[str, float] | None:
    """Return (mint, sol_received) if tx is a token SELL by wallet, else None.

    IMPORTANT: pump.fun bonding curve sells come through Helius as type="UNKNOWN"
    or type="" — NOT type="SWAP". Restricting to SWAP only catches Jupiter sells.
    We detect a sell by the economic signature: tokens leave + SOL arrives.

    ROOT CAUSE (discovered 2026-04-09): pump.fun BC sells do NOT credit SOL via
    nativeTransfers. The bonding curve program credits SOL internally; the only
    place it appears in the enhanced API response is accountData[].nativeBalanceChange.
    Checking only nativeTransfers caused ALL BC sell signals to be silently dropped
    (sol_in = 0 → early return), so the WebSocket never detected Trenchman exits.
    Fix: fall back to accountData net SOL change when nativeTransfers shows nothing.
    """
    tx_type = tx.get("type", "")
    # Hard-exclude obvious non-trade types to avoid false positives
    if tx_type in ("NFT_SALE", "NFT_MINT", "NFT_BID", "COMPRESSED_NFT_MINT",
                   "COMPRESSED_NFT_BURN", "CREATE_ACCOUNT", "CLOSE_ACCOUNT"):
        return None

    nt = tx.get("nativeTransfers", [])
    tt = tx.get("tokenTransfers", [])

    # SOL arriving via nativeTransfers (Jupiter / PumpSwap / Raydium sells)
    sol_in = sum(t.get("amount", 0) / 1e9 for t in nt
                 if t.get("toUserAccount") == wallet)

    # Fallback: pump.fun BC sells credit SOL via the bonding curve program's
    # internal accounting — it shows up in accountData as a net positive balance
    # change, NOT in nativeTransfers. Without this fallback every BC sell is missed.
    if sol_in < 0.001:
        for acct in tx.get("accountData", []):
            if acct.get("account") == wallet:
                net = acct.get("nativeBalanceChange", 0) / 1e9
                if net > 0.001:
                    sol_in = net
                break

    if sol_in < 0.001:
        return None

    # Token leaving wallet
    tok_out = [
        t.get("mint", "")
        for t in tt
        if t.get("fromUserAccount") == wallet and t.get("mint") not in _SKIP_MINTS
    ]
    if not tok_out:
        return None
    return tok_out[0], sol_in


# ── Token name lookup ─────────────────────────────────────────────────────────

async def _get_token_name(mint: str, session: aiohttp.ClientSession) -> str:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return mint[:8] + "..."
            d = await r.json()
            pairs = d.get("pairs") or []
            if pairs:
                return (pairs[0].get("baseToken") or {}).get("name", mint[:8])
    except Exception:
        pass
    return mint[:8] + "..."


async def _dexscreener_price_and_mc(mint: str, session: aiohttp.ClientSession) -> tuple[float | None, float | None, float | None]:
    """Fetch (priceNative SOL, marketCapUSD, liquidityUSD) from DexScreener with sanity checks.
    Returns (None, None, None) on failure or phantom price detection."""
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                return None, None, None
            d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None, None, None
            pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            best = pairs[0]
            price_str = best.get("priceNative", "")
            if not price_str:
                return None, None, None
            price = float(price_str)

            if price <= 0 or price > 0.01:
                print(f"[copy-trade] ⚠️  Price sanity fail for {mint[:8]}: {price:.4e} SOL (>0.01 ceiling) — skipping")
                return None, None, None

            last = _paper_positions.get(mint, {}).get("current_price")
            if last and last > 0:
                ratio = price / last
                if ratio > 5000 or ratio < 0.0002:
                    print(f"[copy-trade] ⚠️  Price spike for {mint[:8]}: {last:.4e} → {price:.4e} ({ratio:.1f}x) — DexScreener glitch, skipping")
                    return None, None, None

            mc  = float(best.get("fdv") or best.get("marketCap") or 0)
            liq = float((best.get("liquidity") or {}).get("usd") or 0)
            return price, (mc if mc > 0 else None), (liq if liq > 0 else None)
    except Exception:
        return None, None, None


async def _get_price_and_mc(mint: str, session: aiohttp.ClientSession) -> tuple[float | None, float | None, float | None]:
    """DexScreener-first, BC-reserves-fallback price resolver.

    The bonding-curve fallback closes the `not_indexed` gap on fresh pump.fun
    launches that DexScreener hasn't picked up yet (typical lag 30-90s). Analysis
    of 1315 signals showed 157 missed buys due to this race, concentrated in the
    highest-WR wallets. See `bc_price_resolver.py` for the math and rationale.
    """
    price, mc, liq = await _dexscreener_price_and_mc(mint, session)
    if price is not None:
        return price, mc, liq
    try:
        from elizaos.plugins.solana.bc_price_resolver import get_bc_price_and_mc
        bc_price, bc_mc, bc_liq = await get_bc_price_and_mc(mint, session)
        if bc_price is not None:
            print(f"[copy-trade] 🎯 BC-fallback price for {mint[:8]}: {bc_price:.4e} SOL "
                  f"(MC=${bc_mc or 0:,.0f}, liq=${bc_liq or 0:,.0f}) — DexScreener miss")
        return bc_price, bc_mc, bc_liq
    except Exception as exc:
        print(f"[copy-trade] BC fallback error for {mint[:8]}: {exc!r}")
        return None, None, None


async def _get_current_price_sol(mint: str, session: aiohttp.ClientSession) -> float | None:
    """Backwards-compatible wrapper — returns price only."""
    price, _, _ = await _get_price_and_mc(mint, session)
    return price


def _classify_narrative(token_name: str, token_symbol: str = "") -> tuple[str, float]:
    """Classify token narrative; return (narrative_type, moonbag_ceiling_pct).

    Ceilings are realistic maximum expected gains before narrative exhaustion:
      elon_spacex=200%, ai_agent=2000%, political=800%, celebrity=300%,
      viral_cultural=1500%, absurdist=500%, unknown=400%.
    """
    text = (token_name + " " + token_symbol).lower()
    if any(w in text for w in ["elon", "elun", "spacex", "starship", "musk"]):
        return "elon_spacex", 200.0
    if any(w in text for w in [" ai", "agi", "gpt", "neural", "llm", "claude", "gemini"]):
        return "ai_agent", 2000.0
    if any(w in text for w in ["trump", "bernie", "burnie", "kamala", "biden", "maga", "pelosi", "obama"]):
        return "political", 800.0
    if any(w in text for w in ["kanye", "yeezy", "bieber", "rihanna", "celebrity", "taylor", "swift", "drake"]):
        return "celebrity", 300.0
    if any(w in text for w in ["pepe", "frog", "wojak", "cope", "chad", "doge", "shib", "floki", "bonk"]):
        return "viral_cultural", 1500.0
    if any(w in text for w in ["absurd", "random", "toilet", "poop", "fart", "weird", "wif", "hat"]):
        return "absurdist", 500.0
    return "unknown", 400.0


async def _get_market_data(mint: str, session: aiohttp.ClientSession) -> dict | None:
    """Extended DexScreener fetch: price/MC/liq + volume and txn counts for health signals.

    Returns dict with keys: price, mc, liq, vol_h1, buys_h1, sells_h1,
    vol_mc_ratio (vol_h1/mc), buy_sell_ratio (buys_h1/sells_h1).
    Returns None on failure or invalid data.

    Used by moonbag health signal checks every 30s on open positions.
    """
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                return None
            d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None
            pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
            best = pairs[0]
            price_str = best.get("priceNative", "")
            if not price_str:
                return None
            price = float(price_str)
            if price <= 0 or price > 0.01:
                return None
            mc   = float(best.get("fdv") or best.get("marketCap") or 0)
            liq  = float((best.get("liquidity") or {}).get("usd") or 0)
            vol  = best.get("volume") or {}
            txns = best.get("txns") or {}
            vol_h1   = float(vol.get("h1") or 0)
            buys_h1  = int((txns.get("h1") or {}).get("buys") or 0)
            sells_h1 = int((txns.get("h1") or {}).get("sells") or 0)
            vol_mc_ratio   = (vol_h1 / mc)        if mc > 0      else None
            buy_sell_ratio = (buys_h1 / sells_h1) if sells_h1 > 0 else None
            # SOL price in USD (for position sizing vs liquidity)
            price_usd_str = best.get("priceUsd") or ""
            try:
                _price_usd = float(price_usd_str) if price_usd_str else None
            except (ValueError, TypeError):
                _price_usd = None
            sol_price_usd = (_price_usd / price) if (_price_usd and price > 0) else None
            # Pair age (for post-grad 30-min liquidity test)
            pair_created_at = best.get("pairCreatedAt")
            try:
                pair_age_secs = (time.time() - float(pair_created_at) / 1000.0) if pair_created_at else None
            except (ValueError, TypeError):
                pair_age_secs = None
            # Holder count (for holder velocity signal)
            _info = best.get("info") or {}
            _holders_raw = _info.get("holders")
            try:
                holders = int(_holders_raw) if _holders_raw is not None else None
            except (ValueError, TypeError):
                holders = None
            return {
                "price":          price,
                "mc":             mc   if mc  > 0 else None,
                "liq":            liq  if liq > 0 else None,
                "vol_h1":         vol_h1,
                "buys_h1":        buys_h1,
                "sells_h1":       sells_h1,
                "vol_mc_ratio":   vol_mc_ratio,
                "buy_sell_ratio": buy_sell_ratio,
                "sol_price_usd":  sol_price_usd,
                "pair_age_secs":  pair_age_secs,
                "holders":        holders,
            }
    except Exception:
        return None


async def _resolve_pool_address(mint: str, session: aiohttp.ClientSession) -> str | None:
    """Return the PumpSwap pool address for a mint.

    Used once per position to unlock Helius real-time pricing.

    Try order:
    1. PumpFun API — returns pool_address immediately after graduation (canonical source,
       no indexing delay unlike DexScreener which takes 60-120s).
    2. DexScreener — fallback for non-pump tokens or PumpFun API failures.

    Returns None if not a PumpSwap token or all sources fail.
    """
    # ── 1. PumpFun API (instant for graduated tokens) ────────────────────────
    # CRITICAL: only return pool_address if complete=True (graduated to PumpSwap).
    # Non-graduated tokens also have pool_address set — it's the bonding CURVE address.
    # Feeding a BC address to get_pumpswap_price_helius reads garbage vault offsets → false prices.
    try:
        async with session.get(
            f"https://frontend-api-v3.pump.fun/coins/{mint}",
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("complete"):  # True = graduated to PumpSwap AMM
                    pool_addr = d.get("pool_address") or d.get("raydium_pool")
                    if pool_addr:
                        return pool_addr
                # Not complete = still on bonding curve — do NOT return BC address
    except Exception:
        pass

    # ── 2. DexScreener fallback ───────────────────────────────────────────────
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return None
            d = await r.json()
            pairs = d.get("pairs") or []
            pumpswap = [p for p in pairs if p.get("dexId") in ("pump-amm", "pumpswap")]
            if not pumpswap:
                return None
            # Pick highest liquidity PumpSwap pair
            best = max(pumpswap, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
            return best.get("pairAddress") or None
    except Exception:
        return None


# ── Early-entry safety check (used when DexScreener hasn't indexed the token yet) ────────────

async def _quick_safety_check(mint: str, session: aiohttp.ClientSession) -> tuple[bool, str]:
    """
    Rugcheck.xyz safety gate for tokens not yet indexed on DexScreener.
    Called when our watched wallet buys BEFORE DexScreener indexes the pair.

    Blocks if:
      - rugcheck score > 15000  (BC rug pattern — graduated PumpSwap tokens score 1-11001; BC = 20k-150k)
      - Any DANGER-level LP risk (unlocked LP = rug mechanism)
      - Bundled/concentrated ownership danger risk
      - Top-1 holder owns > 50% of supply

    Fails OPEN (allows through) on network errors — do not penalise connectivity issues.
    Returns (safe: bool, reason: str).
    """
    # BC_SCORE_THRESHOLD: graduated PumpSwap tokens score 1-11001; raw BC tokens 20k-150k.
    # 15000 gives a comfortable gap — anything above is almost certainly still on the bonding curve.
    BC_SCORE_THRESHOLD = 15_000

    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 404:
                # Brand-new token — rugcheck hasn't indexed it yet. Allow with caution.
                print(f"[copy-trade] ⚡ rugcheck 404 for {mint[:8]} (new token) — allowing with caution")
                return True, "rugcheck 404 (brand-new token)"
            if resp.status != 200:
                print(f"[copy-trade] ⚠️  rugcheck HTTP {resp.status} for {mint[:8]} — allowing (API error)")
                return True, f"rugcheck HTTP {resp.status} — allowing"
            rug_data = await resp.json()
    except asyncio.TimeoutError:
        print(f"[copy-trade] ⚠️  rugcheck timeout for {mint[:8]} — allowing")
        return True, "rugcheck timeout — allowing"
    except Exception as exc:
        print(f"[copy-trade] ⚠️  rugcheck error for {mint[:8]}: {exc} — allowing")
        return True, f"rugcheck error — allowing"

    risks = rug_data.get("risks", [])
    danger_risks = [r["name"] for r in risks if r.get("level") == "danger"]

    # Block: unlocked LP = rug vector
    lp_risks = [r for r in danger_risks if "liquidity" in r.lower() or "lp" in r.lower()]
    if lp_risks:
        return False, f"LP not locked ({', '.join(lp_risks)})"

    # Block: bundled/concentrated ownership
    bundle_risks = [
        r for r in danger_risks
        if any(kw in r.lower() for kw in ["bundl", "concentration", "insider", "single holder"])
    ]
    if bundle_risks:
        return False, f"Concentrated ownership ({', '.join(bundle_risks)})"

    # NOTE: rugcheck score check removed — our watched wallets predominantly trade
    # tokens that score 20k-150k on rugcheck by design. Blocking on score
    # was rejecting valid entries. We trust the wallet's entry signal; only block
    # on genuine danger flags (unlocked LP, concentrated ownership) above.
    rug_score = rug_data.get("score", 0)

    # Block: extreme top-holder concentration
    top_holders = rug_data.get("topHolders", [])
    if top_holders:
        top1_pct = float(top_holders[0].get("pct", 0)) * 100
        if top1_pct > 50.0:
            return False, f"Top holder owns {top1_pct:.1f}% — extreme concentration"

    return True, f"rugcheck OK (score={rug_score})"



# ── Compounding position sizing ───────────────────────────────────────────────

def _compound_position_size(base_sol: float) -> float:
    """
    20%-of-wallet compounding rule — position size grows automatically with profits.

    Formula:  clamp(max(base_sol, wallet × compound_pct),  min=base_sol,  max=compound_max_sol)

    Tiers at 20% pct, 0.4 SOL floor, 2.0 SOL cap:
      ≤ 2.0 SOL wallet  →  0.40 SOL  (floor applies — protects small wallet)
        2.5 SOL wallet  →  0.50 SOL  ← compounding kicks in here
        3.0 SOL wallet  →  0.60 SOL
        5.0 SOL wallet  →  1.00 SOL
       10.0 SOL wallet  →  2.00 SOL  (cap — never over-expose a single trade)
      >10.0 SOL wallet  →  2.00 SOL  (cap holds)

    Config keys (bot_config.json):
      copy_trade_compound_pct      float  0.20  — fraction of wallet per trade
      copy_trade_compound_max_sol  float  2.00  — hard cap on any single position
    """
    try:
        from elizaos.plugins.solana import live_config as _lc_c
        compound_pct = float(_lc_c.get("copy_trade_compound_pct", 0.20))
        compound_max = float(_lc_c.get("copy_trade_compound_max_sol", 2.0))
    except Exception:
        compound_pct = 0.20
        compound_max = 2.0
    if _cached_wallet_sol >= 0.5:
        compounded = round(_cached_wallet_sol * compound_pct, 3)
        sized = max(base_sol, compounded)
        return min(sized, compound_max)
    return base_sol




# ── Consensus check ───────────────────────────────────────────────────────────

def _check_consensus(mint: str, wallet_name: str, sol_amount: float) -> list[str]:
    """Record entry and return list of all wallets in 5-min window."""
    now = time.time()
    _recent_entries[mint].append({"wallet": wallet_name, "ts": now, "sol": sol_amount})
    _recent_entries[mint] = [
        e for e in _recent_entries[mint] if now - e["ts"] <= _CONSENSUS_WINDOW_SECS
    ]
    return list({e["wallet"] for e in _recent_entries[mint]})


# ── Paper position management ─────────────────────────────────────────────────

async def _execute_live_buy(mint: str, token_name: str, sol_amount: float,
                            session: aiohttp.ClientSession, runtime: Any,
                            fast_lane: bool = False) -> tuple[bool, str | None, str]:
    """Execute a real on-chain buy via PumpFunService.
    Returns (success, signature, pool_used).
    pool_used is "pump" (bonding curve) or "pump-amm" (PumpSwap AMM, graduated).
    """
    try:
        pump_svc = runtime.get_service("token_data") if runtime else None
        if not pump_svc:
            print("[copy-trade LIVE] No PumpFunService — cannot buy")
            return False, None, "pump"

        # Fast lane: Trenchman always buys BC tokens — skip DexScreener pool check (5s timeout)
        if fast_lane:
            pool = "pump"
        else:
            pool = "pump"
            try:
                async with session.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        pairs = d.get("pairs") or []
                        for p in pairs:
                            if p.get("dexId") in ("pump-amm", "pumpswap"):
                                pool = "pump-amm"
                                break
            except Exception:
                pass

        try:
            sig = await pump_svc.buy(mint, sol_amount, slippage=0.50, pool=pool)
        except Exception as e1:
            # PumpPortal HTTP 400: pool type mismatch (bonding-curve vs graduated AMM).
            # DexScreener indexing lags graduation by 30-120s, so our pool detection
            # can be wrong for freshly-graduated mints. Retry with the other pool type.
            _err = str(e1)
            if "400" in _err or "Bad Request" in _err:
                _alt_pool = "pump-amm" if pool == "pump" else "pump"
                print(f"[copy-trade LIVE] Pool={pool} rejected ({_err[:80]}) — retrying with pool={_alt_pool}")
                sig = await pump_svc.buy(mint, sol_amount, slippage=0.50, pool=_alt_pool)
                pool = _alt_pool
            else:
                raise
        print(f"[copy-trade LIVE] BUY CONFIRMED: {token_name} ({mint[:8]}) {sol_amount:.3f} SOL pool={pool} sig={str(sig)[:16]}...")
        return True, str(sig), pool
    except Exception as e:
        print(f"[copy-trade LIVE] Buy error: {e}")
        return False, None, "pump"


async def _execute_partial_sell(
    mint: str,
    token_name: str,
    sell_fraction: float,
    session: aiohttp.ClientSession,
    runtime: Any = None,
    priority_fee: float = 0.004,
) -> tuple[bool, str | None, float]:
    """Sell a fraction of our token holding for a partial TP exit.

    In live mode: reads on-chain token balance, sells exact token units.
    In paper mode: returns success=True with estimated proceeds (no on-chain action).

    Returns (success, signature_or_None, sol_received_estimate).
    success=False means the live sell failed and should be retried next tick.
    """
    global _paper_balance
    pos = _paper_positions.get(mint)
    if not pos:
        return False, None, 0.0

    entry_price   = pos.get("entry_price", 0.0) or 0.0
    current_price = pos.get("current_price") or entry_price
    sol_spent_orig = pos.get("sol_spent", 0.0)
    remaining_frac = float(pos.get("remaining_fraction", 1.0))

    # sell_fraction is fraction of CURRENT holdings (already reduced by prior
    # partials), so scale by remaining_frac to get cost basis being sold.
    if entry_price > 0 and current_price > 0:
        price_ratio  = current_price / entry_price
        est_sol_out  = sol_spent_orig * remaining_frac * sell_fraction * price_ratio
    else:
        est_sol_out  = sol_spent_orig * remaining_frac * sell_fraction

    # Paper mode: no live execution needed
    try:
        from elizaos.plugins.solana import live_config as _lc_ps
        _live_now = bool(_lc_ps.get("copy_trade_enabled", False))
    except Exception:
        _live_now = False

    if not _live_now or not runtime or not pos.get("live"):
        return True, None, est_sol_out

    # ── Live mode: get actual on-chain balance and sell exact units ───────────
    try:
        pump_svc   = runtime.get_service("token_data")
        wallet_svc = runtime.get_service("wallet")
        if not pump_svc or not wallet_svc:
            return True, None, est_sol_out  # fail open — paper P&L still tracked

        our_pubkey = os.getenv("SOLANA_PUBLIC_KEY", "")
        if not our_pubkey:
            return True, None, est_sol_out

        token_bal = await asyncio.wait_for(
            wallet_svc.rpc.get_token_balance(our_pubkey, mint),
            timeout=5.0,
        )
        if token_bal <= 0:
            print(f"[copy-trade LIVE] Partial sell: zero balance for {token_name} ({mint[:8]}) — skipping")
            return False, None, 0.0

        # Determine pool (pump vs pump-amm)
        pool = "pump"
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    for p in (d.get("pairs") or []):
                        if p.get("dexId") in ("pump-amm", "pumpswap"):
                            pool = "pump-amm"
                            break
        except Exception:
            pass

        token_sell_amount = int(token_bal * sell_fraction)
        if token_sell_amount <= 0:
            return False, None, 0.0

        print(
            f"[copy-trade LIVE] Partial sell {sell_fraction*100:.0f}% of {token_name} "
            f"({mint[:8]}) — {token_sell_amount} tokens, pool={pool}"
        )
        sig = await pump_svc._pumpportal_trade(
            action="sell",
            mint=mint,
            amount=token_sell_amount,
            denominated_in_sol=False,
            slippage_pct=50,
            pool=pool,
            priority_fee=priority_fee,
        )

        # Derive actual SOL received from on-chain tx
        sol_received = est_sol_out
        try:
            _tx = await wallet_svc.rpc.get_transaction(str(sig), encoding="jsonParsed")
            if _tx:
                _meta   = (_tx.get("meta") or {})
                _pre_b  = _meta.get("preBalances",  [0])
                _post_b = _meta.get("postBalances", [0])
                if _pre_b and _post_b:
                    delta = (_post_b[0] - _pre_b[0]) / 1e9
                    if delta > 0:
                        sol_received = delta
        except Exception:
            pass

        return True, str(sig), sol_received

    except Exception as e:
        print(f"[copy-trade LIVE] Partial sell error for {token_name} ({mint[:8]}): {e}")
        return True, None, est_sol_out  # fail open — still track paper P&L


async def _handle_partial_tp(
    mint: str,
    tp_level: int,
    session: aiohttp.ClientSession,
    runtime: Any = None,
) -> None:
    """Execute a partial take-profit exit (TP1 or TP2).

    TP1 (tp_level=1) at +40%: sell 60% — covers cost basis, zero downside risk.
    TP2 (tp_level=2) at +100%: sell 50% of remaining (=20% of original) — locks real profit.
    The remaining 20% is the moonbag — held via trailing stop and health signals.
    """
    global _paper_balance
    pos = _paper_positions.get(mint)
    if not pos:
        return
    if tp_level == 1 and pos.get("tp1_hit"):
        return
    if tp_level == 2 and pos.get("tp2_hit"):
        return

    token_name  = pos["token_name"]
    sol_spent   = pos["sol_spent"]
    entry_price = pos.get("entry_price", 0.0) or 0.0
    current     = pos.get("current_price") or entry_price
    pnl_now     = (current - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0

    if tp_level == 1:
        sell_fraction = 0.60
        tp_label      = "TP1 +40%"
    else:
        sell_fraction = 0.50    # 50% of remaining 40% = 20% of original
        tp_label      = "TP2 +100%"

    print(
        f"[copy-trade] 💰 {tp_label}: {token_name} ({mint[:8]}) at {pnl_now:+.1f}% — "
        f"selling {sell_fraction*100:.0f}% to lock profits"
    )

    ok, sig, sol_received = await _execute_partial_sell(
        mint, token_name, sell_fraction, session, runtime,
        priority_fee=0.005,  # fast-lane priority for TP exits
    )
    if not ok:
        print(f"[copy-trade] ⚠️  Partial sell {tp_label} FAILED for {token_name} — will retry next tick")
        return

    # ── Update position state ─────────────────────────────────────────────────
    old_remaining  = pos.get("remaining_fraction", 1.0)
    new_remaining  = old_remaining * (1.0 - sell_fraction)
    new_locked     = pos.get("locked_sol", 0.0) + sol_received

    _paper_positions[mint]["tp1_hit"]           = True if tp_level == 1 else pos.get("tp1_hit", False)
    _paper_positions[mint]["tp2_hit"]           = True if tp_level == 2 else pos.get("tp2_hit", False)
    _paper_positions[mint]["remaining_fraction"] = new_remaining
    _paper_positions[mint]["locked_sol"]         = new_locked

    if tp_level == 1:
        # Moonbag peak tracking starts from TP1 fill price
        _paper_positions[mint]["moonbag_peak_price"] = current

    _paper_positions[mint].setdefault("partial_exits", []).append({
        "tp_level":         tp_label,
        "ts":               time.time(),
        "pnl_pct_at_exit":  round(pnl_now, 1),
        "fraction_sold":    sell_fraction,
        "sol_received":     round(sol_received, 4),
        "sig":              sig,
    })

    # Credit partial proceeds to balance immediately
    _paper_balance += sol_received
    _save_all()

    emoji = "🎯" if tp_level == 1 else "💎"
    msg = (
        f"{emoji} COPY TRADE {tp_label}: {token_name} ({mint[:8]}...)\n"
        f"   Sold {sell_fraction*100:.0f}% at {pnl_now:+.1f}% — locked {sol_received:.4f} SOL\n"
        f"   Moonbag: {new_remaining*100:.0f}% remaining  •  Balance: {_paper_balance:.3f} SOL"
    )
    print(f"[copy-trade] {msg}")
    _push_alert(msg)


async def _handle_copy_ladder_tp(
    mint: str,
    tranche: int,
    session: aiohttp.ClientSession,
    runtime: Any = None,
) -> None:
    """Execute a single tranche of the copy-trade partial TP ladder.

    Scales out of the position on the way up so a late wallet_exit closes
    a moonbag instead of the whole position. Tranche sizes are expressed as
    fractions of the ORIGINAL position; we compute the fraction of CURRENT
    holdings at fire time (because prior tranches already reduced the float).

    Defaults: L1 sells 40% at +8%, L2 sells 30% at +15%, L3 sells 20% at +30%.
    Remaining 10% runs until wallet_exit, SL, peak-protection, or moonbag trail.
    """
    global _paper_balance
    pos = _paper_positions.get(mint)
    if not pos:
        return
    flag = f"cp_l{tranche}_hit"
    if pos.get(flag):
        return

    from elizaos.plugins.solana import live_config as _lc_lad
    fracs = {
        1: float(_lc_lad.get("copy_trade_ladder_l1_frac", 0.40)),
        2: float(_lc_lad.get("copy_trade_ladder_l2_frac", 0.30)),
        3: float(_lc_lad.get("copy_trade_ladder_l3_frac", 0.20)),
    }
    thresholds = {
        1: float(_lc_lad.get("copy_trade_ladder_l1_pct", 8.0)),
        2: float(_lc_lad.get("copy_trade_ladder_l2_pct", 15.0)),
        3: float(_lc_lad.get("copy_trade_ladder_l3_pct", 30.0)),
    }
    l_frac_orig  = fracs.get(tranche, 0.0)
    l_threshold  = thresholds.get(tranche, 100.0)

    remaining = float(pos.get("remaining_fraction", 1.0))
    if remaining <= 0.01 or l_frac_orig <= 0.0:
        return
    sell_frac_of_current = min(l_frac_orig / remaining, 1.0)

    entry_price = pos.get("entry_price") or 0.0
    current     = pos.get("current_price") or entry_price
    pnl_now     = (current - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
    token_name  = pos["token_name"]

    print(
        f"[copy-trade] 🪜 LADDER L{tranche} ({l_threshold:.0f}%): {token_name} ({mint[:8]}) "
        f"at {pnl_now:+.1f}% — selling {l_frac_orig*100:.0f}% of original"
    )

    ok, sig, sol_received = await _execute_partial_sell(
        mint, token_name, sell_frac_of_current, session, runtime,
        priority_fee=0.005,
    )
    if not ok:
        print(f"[copy-trade] ⚠️  Ladder L{tranche} sell FAILED for {token_name} — will retry")
        return

    new_remaining = remaining * (1.0 - sell_frac_of_current)
    new_locked    = pos.get("locked_sol", 0.0) + sol_received
    _paper_positions[mint][flag]                = True
    _paper_positions[mint]["remaining_fraction"] = new_remaining
    _paper_positions[mint]["locked_sol"]         = new_locked
    _paper_positions[mint].setdefault("partial_exits", []).append({
        "tp_level":        f"L{tranche}_ladder_{l_threshold:.0f}pct",
        "ts":              time.time(),
        "pnl_pct_at_exit": round(pnl_now, 1),
        "fraction_sold":   round(sell_frac_of_current, 4),
        "sol_received":    round(sol_received, 4),
        "sig":             sig,
    })
    _paper_balance += sol_received
    _save_all()

    msg = (
        f"🪜 LADDER L{tranche} ({l_threshold:.0f}%): {token_name} ({mint[:8]}...)\n"
        f"   Sold {l_frac_orig*100:.0f}% at {pnl_now:+.1f}% — locked {sol_received:.4f} SOL\n"
        f"   Remaining: {new_remaining*100:.0f}%  •  Balance: {_paper_balance:.3f} SOL"
    )
    _push_alert(msg)


async def _open_position(mint: str, token_name: str, sol_spent: float,
                         wallet_name: str, session: aiohttp.ClientSession,
                         runtime: Any = None, whale_buy_ts: float = 0.0) -> None:
    """Open a position: validate first, then execute live buy, then track."""
    global _paper_balance

    from elizaos.plugins.solana import live_config as _lc
    live_enabled = bool(_lc.get("copy_trade_enabled", False))

    # ── All validation BEFORE any live buy ───────────────────────────────────
    if mint in _paper_positions:
        _log_signal(mint, token_name, wallet_name, sol_spent, "skipped", "duplicate")
        return

    # ── Golden rule: never re-enter a previously traded mint ──────────────────
    if mint in _traded_mints:
        print(f"[copy-trade] 🚫 Golden rule: {token_name} ({mint[:8]}) already traded — no re-entry")
        _log_signal(mint, token_name, wallet_name, sol_spent, "skipped", "previously_traded")
        return

    # ── Per-wallet rolling loss cap ────────────────────────────────────────────
    # If a wallet has lost more than the cap in the last window, pause it.
    from elizaos.plugins.solana import live_config as _lc_wlc
    _wlc_sol     = float(_lc_wlc.get("copy_trade_wallet_loss_cap_sol", 0.3))
    _wlc_secs    = float(_lc_wlc.get("copy_trade_wallet_loss_window_secs", 7200.0))  # 2 hours
    _now_wlc     = time.time()
    _cutoff_wlc  = _now_wlc - _wlc_secs
    # Prune entries outside the window
    _wallet_loss_window[wallet_name] = [
        (ts, loss) for ts, loss in _wallet_loss_window[wallet_name] if ts >= _cutoff_wlc
    ]
    _recent_wallet_loss = sum(loss for _, loss in _wallet_loss_window[wallet_name])
    if _recent_wallet_loss >= _wlc_sol:
        _window_mins = int(_wlc_secs / 60)
        print(
            f"[copy-trade] ⏸️  WALLET PAUSED: {wallet_name} lost {_recent_wallet_loss:.3f} SOL "
            f"in last {_window_mins}min (cap={_wlc_sol:.2f} SOL) — skipping {token_name}"
        )
        _log_signal(mint, token_name, wallet_name, sol_spent, "skipped", f"wallet_loss_cap_{_recent_wallet_loss:.3f}sol")
        return

    # ── Capital protection halts ───────────────────────────────────────────────
    from elizaos.plugins.solana import live_config as _lc_halt
    _daily_halt_sol = float(_lc_halt.get("copy_trade_daily_loss_halt_sol", 0.5))
    _min_bal_halt   = float(_lc_halt.get("copy_trade_min_balance_halt_sol", 1.0))
    import datetime as _dt_op
    _today_op = _dt_op.datetime.utcnow().toordinal()
    if _daily_loss_reset_day != _today_op:
        pass  # will reset on next close, allow entry today
    if _daily_loss_sol >= _daily_halt_sol:
        print(f"[copy-trade] 🛑 DAILY LOSS LIMIT: lost {_daily_loss_sol:.3f} SOL today (limit {_daily_halt_sol:.2f}) — halted")
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_halt
            asyncio.create_task(_tg_halt(
                f"🛑 <b>Copy trade HALTED — daily loss limit hit</b>\n"
                f"Lost {_daily_loss_sol:.3f} SOL today (limit {_daily_halt_sol:.2f} SOL)\n"
                f"No new entries until UTC midnight."
            ))
        except Exception:
            pass
        return
    if _paper_balance < _min_bal_halt:
        print(f"[copy-trade] 🛑 MIN BALANCE: {_paper_balance:.3f} SOL < {_min_bal_halt:.2f} SOL — halted")
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_bal
            asyncio.create_task(_tg_bal(
                f"🛑 <b>Copy trade HALTED — minimum balance</b>\n"
                f"Wallet at {_paper_balance:.3f} SOL (minimum {_min_bal_halt:.2f} SOL)\n"
                f"Top up required before trading resumes."
            ))
        except Exception:
            pass
        return

    from elizaos.plugins.solana import live_config as _lc_cap
    MAX_COPY_POSITIONS = int(_lc_cap.get("trenchman_max_positions", 2))
    if len(_paper_positions) >= MAX_COPY_POSITIONS:
        print(f"[copy-trade] Max {MAX_COPY_POSITIONS} positions reached — skipping {token_name}")
        _log_signal(mint, token_name, wallet_name, 0.0, "skipped", "slots_full")
        return

    if _paper_balance < sol_spent:
        print(f"[copy-trade PAPER] Insufficient virtual balance ({_paper_balance:.3f} SOL) for {token_name}")
        _log_signal(mint, token_name, wallet_name, 0.0, "skipped", "insufficient_balance")
        return

    # ── Staleness guard — FAST FAIL before any API calls ─────────────────────
    # Verify the whale STILL holds this token before spending any time on DexScreener.
    # Moved here (before price/MC fetch) so we fail in ~1s instead of ~8s.
    # Catches scalp-and-dump patterns where the whale bought and sold in seconds
    # (e.g. CookDoc/Encrypt 2026-04-12). For the Kolscan wallets (2-70min holds)
    # this runs at T+0 rather than T+8s, dramatically reducing whale_already_sold misses.
    _whale_addr = WATCHED_WALLETS.get(wallet_name)
    if _whale_addr and live_enabled:
        # ── Rapid-flip guard: check before staleness test ─────────────────────
        # If this (wallet, mint) has been stale-blocked 2+ times in the last 10min,
        # the whale is scalp-flipping this token. Never enter regardless of current hold.
        _flip_key = (wallet_name, mint)
        _now_flip = time.time()
        _stale_flip_log[_flip_key] = [t for t in _stale_flip_log[_flip_key]
                                       if _now_flip - t < _FLIP_WINDOW_SECS]
        if len(_stale_flip_log[_flip_key]) >= _FLIP_BLOCK_COUNT:
            print(
                f"[copy-trade] 🔄 FLIP GUARD: {token_name} ({mint[:8]}) — {wallet_name} "
                f"scalp-flipped this token {len(_stale_flip_log[_flip_key])}× in 10min — skipping"
            )
            _log_signal(mint, token_name, wallet_name, sol_spent, "skipped", "rapid_flip_pattern")
            return

        _still_holds = await _check_whale_still_holds(_whale_addr, mint, session)
        if not _still_holds:
            # Record this stale block in the flip log
            _stale_flip_log[_flip_key].append(_now_flip)
            print(f"[copy-trade] STALE SIGNAL: {token_name} ({mint[:8]}) — {wallet_name} already sold — aborting")
            _log_signal(mint, token_name, wallet_name, sol_spent, "skipped", "whale_already_sold")
            try:
                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_stale
                asyncio.create_task(_tg_stale(
                    f"Stale signal blocked: {wallet_name} already sold {token_name}. Saved {sol_spent:.3f} SOL"
                ))
            except Exception:
                pass
            return

    # ── Price + safety gate ───────────────────────────────────────────────────
    MIN_ENTRY_MC_USD  = 1_500   # HARDCODED — never enter MC < $1.5k (rug floor; lowered 2026-04-13: wallet data median MC $3.2k)
    MIN_ENTRY_LIQ_USD = 3_000   # HARDCODED — never enter liq < $3k (lowered 2026-04-13: Walta/Frost tokens avg $3-7k liq)
    entry_price, entry_mc, entry_liq = await _get_price_and_mc(mint, session)

    _early_entry = False  # True when token isn't on DexScreener yet (price confirmed later)

    if entry_mc is None or entry_liq is None:
        # Token not indexed on DexScreener yet — run direct rugcheck safety check.
        # If our watched wallet already bought it we want to enter NOW (before +20-50% pump),
        # not after DexScreener indexes it 60-120s later (= buying exit liquidity).
        # Rugcheck distinguishes BC rug tokens (score 20k-150k) from graduated PumpSwap
        # tokens (score 1-11k) without needing DexScreener data.
        print(f"[copy-trade] ⚡ {token_name} ({mint[:8]}) not on DexScreener yet — running rugcheck for early entry")
        safe, safety_reason = await _quick_safety_check(mint, session)
        if not safe:
            print(f"[copy-trade] 🚫 EARLY ENTRY BLOCKED: {token_name} ({mint[:8]}) — {safety_reason}")
            _log_signal(mint, token_name, wallet_name, 0.0, "skipped", f"early_safety_{safety_reason[:40]}")
            return
        print(f"[copy-trade] ⚡ EARLY ENTRY: {token_name} ({mint[:8]}) — {safety_reason} — entering before DexScreener indexes")
        _early_entry = True
        entry_price = entry_price or 0.0  # 0.0 → confirmed by _confirm_entry_price in 45s

    # Try to resolve PumpSwap pool address now — enables Helius real-time pricing
    _pool_addr: str | None = None
    if not _early_entry:
        _pool_addr = await _resolve_pool_address(mint, session)

    if not _early_entry:
        # Normal path — DexScreener has data, apply MC/liq floors
        if entry_mc < MIN_ENTRY_MC_USD:
            mc_str = f"${entry_mc:,.0f}"
            print(f"[copy-trade] 🚫 MC FLOOR: {token_name} ({mint[:8]}) MC={mc_str} < ${MIN_ENTRY_MC_USD:,} — blocking")
            _log_signal(mint, token_name, wallet_name, 0.0, "skipped", f"mc_too_low_{mc_str}")
            return
        if entry_liq < MIN_ENTRY_LIQ_USD:
            liq_str = f"${entry_liq:,.0f}"
            print(f"[copy-trade] 🚫 LIQ FLOOR: {token_name} ({mint[:8]}) liq={liq_str} < ${MIN_ENTRY_LIQ_USD:,} — blocking")
            _log_signal(mint, token_name, wallet_name, 0.0, "skipped", f"liq_too_low_{liq_str}")
            return
        if not entry_price:
            entry_price = 0.0

    # ── Classify narrative before opening position ────────────────────────────
    _narrative, _ceiling_pct = _classify_narrative(token_name)

    # ── Extended market data (sizing + post-grad filters) ─────────────────────
    # Only on the normal path (DexScreener already indexed this token).
    _entry_holders: int | None = None
    if not _early_entry:
        _mdata_open = await _get_market_data(mint, session)
        if _mdata_open:
            _sol_price_usd = _mdata_open.get("sol_price_usd")
            _pair_age_secs = _mdata_open.get("pair_age_secs")
            _entry_holders = _mdata_open.get("holders")
            _open_liq      = _mdata_open.get("liq") or entry_liq or 0.0

            # ── Dynamic position sizing: never more than 2% of pool liquidity ─
            # Prevents slippage blowouts in thin pools and avoids moving the market.
            if _sol_price_usd and _sol_price_usd > 0 and _open_liq > 0:
                liq_cap_sol = _open_liq * 0.02 / _sol_price_usd
                if liq_cap_sol < sol_spent * 0.5:
                    liq_str = f"${_open_liq:,.0f}"
                    print(
                        f"[copy-trade] 🚫 POOL TOO THIN: {token_name} ({mint[:8]}) "
                        f"liq={liq_str}, cap={liq_cap_sol:.3f} SOL < 50% of trade size — skipping"
                    )
                    _log_signal(mint, token_name, wallet_name, sol_spent, "skipped",
                                f"pool_too_thin_{liq_cap_sol:.3f}sol")
                    return
                if liq_cap_sol < sol_spent:
                    print(
                        f"[copy-trade] ⚠️  LIQ CAP: {token_name} ({mint[:8]}) "
                        f"reducing size {sol_spent:.3f}→{liq_cap_sol:.3f} SOL (2% of ${_open_liq:,.0f} pool)"
                    )
                    sol_spent = round(liq_cap_sol, 3)

            # ── Zombie token age cap (added 2026-04-20) ───────────────────────
            # Whales should be flipping fresh memecoins, not 7-month-old revival
            # pumps with $20K liq. Block anything older than 48h. Losing trade
            # UBI (234 days old, -8.5%) was the trigger.
            COPY_TRADE_MAX_AGE_SECS = 48 * 3600
            if _pair_age_secs is not None and _pair_age_secs > COPY_TRADE_MAX_AGE_SECS:
                age_hrs = _pair_age_secs / 3600
                print(
                    f"[copy-trade] 🚫 ZOMBIE TOKEN: {token_name} ({mint[:8]}) "
                    f"pair age={age_hrs:.1f}h > 48h cap — likely revival pump, blocking"
                )
                _log_signal(mint, token_name, wallet_name, sol_spent, "skipped",
                            f"zombie_age_{age_hrs:.0f}h")
                return

            # ── Post-grad 30-min liquidity test ───────────────────────────────
            # Brand-new PumpSwap graduates often have shallow liquidity and thin
            # holder bases — skip them until the pool has had 30 minutes to season.
            if _pair_age_secs is not None and _pair_age_secs < 1800:
                _liq_for_grad = _open_liq
                if _liq_for_grad < 8_000:
                    print(
                        f"[copy-trade] 🚫 NEW GRAD LIQ: {token_name} ({mint[:8]}) "
                        f"pair age={_pair_age_secs:.0f}s liq=${_liq_for_grad:,.0f} < $8k — blocking"
                    )
                    _log_signal(mint, token_name, wallet_name, sol_spent, "skipped",
                                f"new_grad_liq_{_liq_for_grad:.0f}")
                    return
                if _entry_holders is not None and _entry_holders < 50:
                    print(
                        f"[copy-trade] 🚫 NEW GRAD HOLDERS: {token_name} ({mint[:8]}) "
                        f"pair age={_pair_age_secs:.0f}s holders={_entry_holders} < 50 — blocking"
                    )
                    _log_signal(mint, token_name, wallet_name, sol_spent, "skipped",
                                f"new_grad_holders_{_entry_holders}")
                    return

    # -- All checks passed — execute live buy NOW ----------------------------------
    _buy_sig: str | None = None
    _buy_pool: str = "pump"   # default until confirmed; "pump-amm" = PumpSwap AMM
    if live_enabled and runtime:
        print(f"[copy-trade LIVE] Entering {token_name} ({mint[:8]}) {sol_spent:.3f} SOL — following {wallet_name}")
        buy_ok, _buy_sig, _buy_pool = await _execute_live_buy(mint, token_name, sol_spent, session, runtime)
        if not buy_ok:
            print(f"[copy-trade LIVE] Buy failed for {token_name} — not tracking position")
            return

    _paper_balance -= sol_spent
    _paper_positions[mint] = {
        "token_name":     token_name,
        "wallet_name":    wallet_name,
        "entry_price":    entry_price,
        "sol_spent":      sol_spent,
        "entry_ts":       time.time(),
        "live":           live_enabled,
        "price_confirmed": entry_price > 0,
        "peak_pnl_pct":   0.0,
        "peak_price":     entry_price,
        "price_checkpoints": [],
        "buy_sig":        _buy_sig,
        "buy_pool":       _buy_pool,   # "pump" = bonding curve, "pump-amm" = PumpSwap AMM
        "whale_buy_ts":   whale_buy_ts,
        # Only set pool_address for PumpSwap buys — BC address must NEVER be used with
        # get_pumpswap_price_helius (different memory layout → garbage price readings).
        "pool_address":   _pool_addr if _buy_pool == "pump-amm" else None,
        # ── Moonbag partial exit fields ────────────────────────────────────
        "tp1_hit":            False,          # True after TP1 (+40%) fires
        "tp2_hit":            False,          # True after TP2 (+100%) fires
        "remaining_fraction": 1.0,            # 1.0=full, 0.4=40% left after TP1
        "locked_sol":         0.0,            # cumulative SOL locked via partial exits
        "moonbag_peak_price": entry_price,    # peak price after TP1 (trail stop base)
        "narrative":          _narrative,     # e.g. "elon_spacex", "unknown"
        "moonbag_ceiling_pct": _ceiling_pct,  # force close above this gain
        "partial_exits":      [],             # [{tp_label, ts, pnl_pct_at_exit, sol_received}]
        "last_health_check":  time.time(),    # timestamp of last vol/MC health check
        # ── Entry context for velocity signals ─────────────────────────────
        "entry_holder_count": _entry_holders, # holders at entry — used for decline signal
        # Entry MC/liq captured here so learning_engine.record_trade_entry can
        # read them. Previously entry_liq_usd was null on all 43 outcomes because
        # record_trade_entry defaulted to None instead of pulling from pos.
        "mc_usd":  entry_mc,
        "liq_usd": entry_liq,
    }
    _log_signal(mint, token_name, wallet_name, sol_spent, "entered")
    _save_all()

    # ── Learning Engine: record entry context ─────────────────────────────────
    try:
        from elizaos.plugins.solana.learning_engine import record_trade_entry
        record_trade_entry(mint, _paper_positions[mint])
    except Exception:
        pass

    # ── TradeMonitor: spawn per-position AI monitor ───────────────────────────
    try:
        from elizaos.plugins.solana.trade_monitor import spawn_monitor as _spawn_mon
        asyncio.get_event_loop().create_task(_spawn_mon(mint, session, runtime))
    except Exception as _tm_err:
        print(f"[copy-trade] TradeMonitor spawn error: {_tm_err}")

    mode = "LIVE + PAPER" if live_enabled else "PAPER"
    price_str = f"{entry_price:.8f} SOL" if entry_price > 0 else "pending (will retry in 45s)"
    msg = (
        f"{'🔴' if live_enabled else '📋'} COPY-TRADE {mode}: {token_name} ({mint[:8]}...)\n"
        f"   Following: {wallet_name}  •  Size: {sol_spent:.2f} SOL\n"
        f"   Entry price: {price_str}  •  Paper balance: {_paper_balance:.3f} SOL"
    )
    print(f"[copy-trade] {msg}")
    _push_alert(msg)

    # Schedule entry price confirmation if DexScreener hadn't indexed it yet
    if entry_price == 0:
        async def _confirm_entry_price(m: str, s: aiohttp.ClientSession) -> None:
            await asyncio.sleep(45)
            if m not in _paper_positions:
                return  # position already closed
            price = await _get_current_price_sol(m, s)
            if price and price > 0:
                _paper_positions[m]["entry_price"] = price
                _paper_positions[m]["price_confirmed"] = True
                _save_all()
                print(f"[copy-trade] Entry price confirmed for {_paper_positions[m]['token_name']}: {price:.8f} SOL")
        try:
            asyncio.get_event_loop().create_task(_confirm_entry_price(mint, session))
        except Exception:
            pass


async def _execute_live_sell(mint: str, token_name: str,
                             session: aiohttp.ClientSession,
                             runtime: Any = None,
                             fast_lane: bool = False,
                             priority_fee: float = 0.002,
                             token_amount_ui: float | None = None) -> tuple[bool, str | None]:
    """Execute a real on-chain sell with strict verification.

    WATERTIGHT SELL — never trusts a signature alone.
    1. Pre-sell: check on-chain balance exists (skip if already zero)
    2. Execute with confirm_transaction (already raises on on-chain errors)
    3. Post-sell: verify on-chain balance changed as expected
    4. Escalating retry: pump → pump-amm → pump (reversed) with increasing slippage
    Returns (True, signature) ONLY after on-chain balance confirms tokens moved.

    token_amount_ui: If provided, sell exactly this many tokens (UI units) instead
                     of 100%.  Used for Frost Mirror partial exits when main and lotto
                     positions co-exist for the same mint.  Post-sell verification
                     checks balance decreased by ≥80% of the requested amount rather
                     than checking for zero.
    priority_fee: Jito tip in SOL. Pass 0.005 for TP exits to close the execution
    gap between detection and on-chain fill. Default 0.002 for normal exits.
    """
    try:
        pump_svc = runtime.get_service("token_data") if runtime else None
        if not pump_svc:
            print("[copy-trade LIVE] No PumpFunService — cannot sell")
            return False, None

        wallet_svc = runtime.get_service("wallet") if runtime else None
        our_pubkey = os.getenv("SOLANA_PUBLIC_KEY", "")

        # ── Step 1: Pre-sell balance check ────────────────────────────────────
        # If tokens are already gone (prior attempt succeeded but confirmation
        # timed out), skip the sell — it already worked.
        pre_bal: float = 0.0
        if wallet_svc and our_pubkey:
            try:
                pre_bal = await asyncio.wait_for(
                    wallet_svc.rpc.get_token_balance(our_pubkey, mint),
                    timeout=5.0,
                )
                if pre_bal <= 0.0:
                    print(f"[copy-trade LIVE] {token_name} ({mint[:8]}) — zero balance pre-sell; already sold")
                    return True, None  # token is gone — sell was phantom of a previous success
            except Exception as _pb_err:
                print(f"[copy-trade LIVE] Pre-sell balance check failed ({_pb_err}) — proceeding anyway")

        # Determine sell amount: exact UI token count for partial sells, "100%" otherwise
        _sell_amount: float | str = "100%"
        if token_amount_ui is not None and token_amount_ui > 0:
            # Clamp to actual balance so we never ask to sell more than we have
            _sell_amount = min(token_amount_ui, pre_bal) if pre_bal > 0 else token_amount_ui
            print(f"[copy-trade LIVE] Partial sell: {_sell_amount:.4f} of {pre_bal:.4f} tokens ({token_name})")

        # ── Step 2: Determine pool — fast lane skips DexScreener ─────────────
        if fast_lane:
            pool_order = ["pump", "pump-amm"]
        else:
            pool = "pump"
            try:
                async with session.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        pairs = d.get("pairs") or []
                        for p in pairs:
                            if p.get("dexId") in ("pump-amm", "pumpswap"):
                                pool = "pump-amm"
                                break
            except Exception:
                pass
            pool_order = [pool, "pump-amm" if pool == "pump" else "pump"]

        # ── Step 3: Escalating sell attempts ─────────────────────────────────
        # Slippage ramps: 50% → 70% → 90%
        # priority_fee: caller-supplied (0.005 for TP exits, 0.002 for normal exits)
        slippage_ladder = [50, 70, 90]
        last_error = ""
        for attempt_idx, slippage_pct in enumerate(slippage_ladder):
            pool = pool_order[min(attempt_idx, len(pool_order) - 1)]
            try:
                sig = await pump_svc._pumpportal_trade(
                    action="sell",
                    mint=mint,
                    amount=_sell_amount,
                    denominated_in_sol=False,
                    slippage_pct=slippage_pct,
                    pool=pool,
                    priority_fee=priority_fee,
                )
                print(f"[copy-trade LIVE] SELL TX: {token_name} ({mint[:8]}) amount={_sell_amount} pool={pool} slippage={slippage_pct}% sig={str(sig)[:20]}...")

                # ── Step 4: Post-sell balance verification ────────────────────
                # The signature landed and confirmed (confirm_transaction already ran inside
                # _pumpportal_trade). Now verify the balance actually changed.
                if wallet_svc and our_pubkey:
                    try:
                        await asyncio.sleep(1.5)   # brief pause for RPC state to sync
                        post_bal = await asyncio.wait_for(
                            wallet_svc.rpc.get_token_balance(our_pubkey, mint),
                            timeout=5.0,
                        )
                        if token_amount_ui is None:
                            # Full sell: balance must be (near) zero
                            if post_bal > 0.01:
                                print(
                                    f"[copy-trade LIVE] ⚠️  PHANTOM SELL DETECTED — {token_name} ({mint[:8]}) "
                                    f"tx landed but {post_bal:.4f} tokens still in wallet! Retrying..."
                                )
                                last_error = f"phantom_sell_post_balance={post_bal:.4f}"
                                continue   # retry with higher slippage
                            print(f"[copy-trade LIVE] ✅ SELL VERIFIED: {token_name} ({mint[:8]}) balance confirmed zero")
                        else:
                            # Partial sell: balance should have decreased by ≥80% of requested amount
                            expected_remaining = pre_bal - float(_sell_amount)
                            decrease = pre_bal - post_bal
                            min_expected_decrease = float(_sell_amount) * 0.80
                            if decrease < min_expected_decrease:
                                print(
                                    f"[copy-trade LIVE] ⚠️  PARTIAL SELL VERIFY FAILED — {token_name} ({mint[:8]}) "
                                    f"expected {float(_sell_amount):.2f} decrease, got {decrease:.2f}. Retrying..."
                                )
                                last_error = f"partial_sell_decrease={decrease:.4f}"
                                continue
                            print(f"[copy-trade LIVE] ✅ PARTIAL SELL VERIFIED: {token_name} ({mint[:8]}) "
                                  f"sold {decrease:.4f} tokens, remaining={post_bal:.4f}")
                    except Exception as _vb_err:
                        # Can't verify — but tx was confirmed on-chain, trust it
                        print(f"[copy-trade LIVE] Post-sell verify failed ({_vb_err}) — tx confirmed, trusting sig")

                return True, str(sig)

            except Exception as e:
                last_error = str(e)
                print(f"[copy-trade LIVE] Sell attempt {attempt_idx+1}/{len(slippage_ladder)} failed: {e}")
                if attempt_idx < len(slippage_ladder) - 1:
                    await asyncio.sleep(2.0 * (attempt_idx + 1))  # backoff

        # All attempts exhausted
        print(
            f"[copy-trade LIVE] 🚨 SELL FAILED — {token_name} ({mint[:8]}) "
            f"after {len(slippage_ladder)} attempts. Last error: {last_error[:120]}"
        )
        return False, None

    except Exception as e:
        print(f"[copy-trade LIVE] Sell error: {e}")
        return False, None


async def _close_paper_position(mint: str, reason: str,
                                 session: aiohttp.ClientSession,
                                 runtime: Any = None) -> None:
    """Close a position and record P&L. Also executes live sell if live mode.

    CRITICAL ORDER:
    1. Execute live sell (if live mode)
    2. Verify sell succeeded on-chain
    3. ONLY THEN remove from _paper_positions and record as closed
    If sell fails: position stays open, alert fires, orphan scanner will retry.
    """
    global _paper_balance
    pos = _paper_positions.get(mint)   # peek — do NOT pop yet
    if not pos:
        return

    # ── Live sell (execute FIRST, verify before closing books) ───────────────
    try:
        from elizaos.plugins.solana import live_config as _lc_close
        _live_now = bool(_lc_close.get("copy_trade_enabled", False))
    except Exception:
        _live_now = False
    if _live_now and pos.get("live") and runtime:
        # TP and peak-protection exits get the highest priority fee to shrink
        # the execution gap between detection and on-chain fill.
        # Newbird AI: peaked +15.36%, filled at -1.25% because the 0.002 fee was
        # too slow for a 4-second reversal. Peak-protection also needs 0.005.
        _is_tp_exit = (
            reason.startswith("take_profit")
            or reason.startswith("tp_")
            or reason.startswith("peak_protection")
            or reason.startswith("moonbag_protection")
        )
        _sell_priority_fee = 0.005 if _is_tp_exit else 0.002
        if _is_tp_exit:
            print(f"[copy-trade LIVE] 🚀 TP/peak-protection exit — 0.005 SOL Jito tip for faster fill")
        sell_ok, _live_sell_sig = await _execute_live_sell(mint, pos["token_name"], session, runtime,
                                                           fast_lane=bool(pos.get("fast_lane")),
                                                           priority_fee=_sell_priority_fee)
        if not sell_ok:
            # SELL FAILED — position stays open so orphan scanner / safety net can retry
            print(
                f"[copy-trade LIVE] 🚨 SELL FAILED for {pos['token_name']} ({mint[:8]}) "
                f"— position kept open. Bot will retry via safety net."
            )
            _push_alert(
                f"🚨 SELL FAILED: {pos['token_name']} ({mint[:8]})\n"
                f"   Reason: {reason}\n"
                f"   Tokens still in wallet — bot will retry via safety net scan"
            )
            return  # EXIT without closing — do not record phantom profit
        # Store sell sig in position BEFORE pop so it survives into the record
        if _live_sell_sig and mint in _paper_positions:
            _paper_positions[mint]["sell_sig"] = _live_sell_sig

    # ── TradeMonitor: stop AI monitor before position is removed ─────────────
    try:
        from elizaos.plugins.solana.trade_monitor import stop_monitor as _stop_mon
        _stop_mon(mint)
    except Exception:
        pass

    # Sell confirmed (or paper mode) — now safe to remove position
    pos = _paper_positions.pop(mint, None)
    if not pos:
        return

    # ── Exit price + P&L: on-chain first, DexScreener fallback ──────────────────
    sol_spent   = pos["sol_spent"]
    entry_price = pos["entry_price"]
    exit_price  = None
    sol_received_onchain: float | None = None
    pnl_source  = "dexscreener"

    # ── Moonbag accounting ────────────────────────────────────────────────────
    # locked_sol was credited to _paper_balance during partial exits.
    # remaining_fraction is the share of the original position still open.
    # We only close the remaining fraction here; total P&L includes locked_sol.
    locked_sol      = float(pos.get("locked_sol", 0.0))
    remaining_frac  = float(pos.get("remaining_fraction", 1.0))
    effective_spent = sol_spent * remaining_frac   # what's actually still at risk

    # Priority 1: use stored sell signature to get ground-truth SOL received on-chain
    _sell_sig = pos.get("sell_sig")
    if _sell_sig and pos.get("live") and runtime:
        try:
            _w_svc = runtime.get_service("wallet")
            if _w_svc:
                _tx = await _w_svc.rpc.get_transaction(_sell_sig, encoding="jsonParsed")
                if _tx:
                    _meta   = _tx.get("meta") or {}
                    _pre_b  = _meta.get("preBalances",  [0])
                    _post_b = _meta.get("postBalances", [0])
                    if _pre_b and _post_b:
                        _raw_delta = (_post_b[0] - _pre_b[0]) / 1e9
                        if _raw_delta > 0:
                            sol_received_onchain = _raw_delta
                            pnl_source = "onchain_sell_sig"
                            print(f"[copy-trade] ✅ On-chain sell verified: {_raw_delta:.4f} SOL for {mint[:8]}")
        except Exception as _rpc_err:
            print(f"[copy-trade] on-chain P&L fetch failed: {_rpc_err}")

    # Priority 2: DexScreener price
    if sol_received_onchain is None:
        exit_price = await _get_current_price_sol(mint, session)
        if not exit_price and pos.get("current_price"):
            exit_price = pos["current_price"]
            print(f"[copy-trade] exit_price fallback: cached {exit_price:.3e} for {mint[:8]}")

    # ── Calculate P&L (accounting for partial exits already credited) ─────────
    # total_received = locked_sol (already in balance) + final_close_sol
    # total_pnl      = total_received - original sol_spent
    if sol_received_onchain is not None:
        final_exit_sol = sol_received_onchain
        pnl_sol  = locked_sol + final_exit_sol - sol_spent
        pnl_pct  = (pnl_sol / sol_spent) * 100.0 if sol_spent > 0 else 0.0
        exit_sol = final_exit_sol   # only the final tranche goes into balance now
        if not exit_price and entry_price and entry_price > 0 and remaining_frac > 0:
            final_pnl_pct = (final_exit_sol - effective_spent) / effective_spent * 100.0
            exit_price = entry_price * (1 + final_pnl_pct / 100.0)
        if locked_sol > 0:
            print(f"[copy-trade] on-chain P&L: final={final_exit_sol:.4f} SOL + locked={locked_sol:.4f} SOL → total={pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)")
        else:
            print(f"[copy-trade] on-chain P&L: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%) from sell sig")
    elif exit_price and entry_price and entry_price > 0:
        final_pnl_pct  = (exit_price - entry_price) / entry_price * 100.0
        final_exit_sol = effective_spent * (1 + final_pnl_pct / 100.0)
        pnl_sol  = locked_sol + final_exit_sol - sol_spent
        pnl_pct  = (pnl_sol / sol_spent) * 100.0 if sol_spent > 0 else 0.0
        exit_sol = final_exit_sol
    else:
        print(f"[copy-trade] WARNING: no exit price for {mint[:8]} (entry={entry_price}) — recording locked P&L only")
        pnl_sol  = locked_sol - sol_spent
        pnl_pct  = (pnl_sol / sol_spent) * 100.0 if sol_spent > 0 else 0.0
        exit_sol = 0.0

    # ── Entry lag: how many seconds after whale bought did we buy? ─────────────
    entry_lag_secs: float | None = None
    _buy_sig     = pos.get("buy_sig")
    _whale_buy_ts = float(pos.get("whale_buy_ts") or 0)
    if _buy_sig and runtime and _whale_buy_ts > 0:
        try:
            _w_svc2 = runtime.get_service("wallet")
            if _w_svc2:
                _buy_tx = await _w_svc2.rpc.get_transaction(_buy_sig, encoding="jsonParsed")
                if _buy_tx and _buy_tx.get("blockTime"):
                    entry_lag_secs = float(_buy_tx["blockTime"]) - _whale_buy_ts
                    print(f"[copy-trade] ⏱️  Entry lag: {entry_lag_secs:.1f}s after whale for {mint[:8]}")
        except Exception as _lag_err:
            print(f"[copy-trade] Entry lag fetch failed: {_lag_err}")

    # locked_sol was already credited to _paper_balance during partial exits.
    # Only add the final tranche proceeds here.
    _paper_balance += exit_sol

    # ── Daily loss tracking — reset at UTC midnight ────────────────────────────
    global _daily_loss_sol, _daily_loss_reset_day
    import datetime as _dt
    _today = _dt.datetime.utcnow().toordinal()
    if _today != _daily_loss_reset_day:
        _daily_loss_sol = 0.0
        _daily_loss_reset_day = _today
    if pnl_sol < 0:
        _daily_loss_sol += abs(pnl_sol)

    # ── Per-wallet rolling loss window ────────────────────────────────────────
    # Record the loss so _open_position can pause this wallet if needed.
    _wallet_name_for_loss = pos.get("wallet_name", "")
    if pnl_sol < 0 and _wallet_name_for_loss:
        _wallet_loss_window[_wallet_name_for_loss].append((time.time(), abs(pnl_sol)))

    hold_mins = (time.time() - pos["entry_ts"]) / 60.0
    record = {
        "ts":              time.time(),
        "dt":              __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "mint":            mint,
        "token_name":      pos["token_name"],
        "wallet":          pos["wallet_name"],
        "reason":          reason,
        "sol_spent":       sol_spent,
        "entry_price":     entry_price,
        "exit_price":      exit_price,
        "pnl_sol":         round(pnl_sol, 4),
        "pnl_pct":         round(pnl_pct, 2),
        "hold_mins":       round(hold_mins, 1),
        "peak_pnl_pct":    round(pos.get("peak_pnl_pct", 0.0), 2),
        "peak_price":      pos.get("peak_price"),
        "peak_ts":         pos.get("peak_ts"),
        "price_checkpoints": pos.get("price_checkpoints", []),
        # ── On-chain verification fields ──────────────────────────────────────
        "pnl_source":      pnl_source,
        "sell_sig":        pos.get("sell_sig"),
        "buy_sig":         pos.get("buy_sig"),
        "entry_lag_secs":  round(entry_lag_secs, 1) if entry_lag_secs is not None else None,
        "whale_buy_ts":    _whale_buy_ts if _whale_buy_ts > 0 else None,
        # ── Moonbag fields ────────────────────────────────────────────────────
        "locked_sol":      round(locked_sol, 4),
        "remaining_fraction_at_close": round(remaining_frac, 3),
        "partial_exits":   pos.get("partial_exits", []),
        "narrative":       pos.get("narrative", "unknown"),
        "tp1_hit":         pos.get("tp1_hit", False),
        "tp2_hit":         pos.get("tp2_hit", False),
    }
    _paper_trades.append(record)
    _traded_mints.add(mint)  # golden rule — never re-enter this mint
    _save_all()

    # ── Brain Memory: record rule-engine exit across all 3 brains ─────────────
    # Every SL/TP/peak/stagnant/wallet_exit becomes a training example every
    # brain sees. This closes the feedback loop: brains that recommended HOLD
    # on a mint that then hit SL will see "missed_rug" tick up in their stats.
    try:
        from elizaos.plugins.solana.brain_memory import (
            record_decision_all_brains as _rdab,
            resolve_outcome_all_brains as _roab,
        )
        _rule_action = "SELL"
        _rdab(mint, pos["token_name"], _rule_action, f"rule:{reason}", pnl_pct, source="rule_engine")
        # Map reason → outcome tag for accuracy counters
        _low = (reason or "").lower()
        if _low.startswith("stop_loss") or _low.startswith("sl_") or "rug" in _low:
            _outcome_tag = "SL_HIT"
        elif (_low.startswith("take_profit") or _low.startswith("tp_")
              or _low.startswith("peak_protection") or _low.startswith("moonbag_protection")):
            _outcome_tag = "TP_HIT"
        elif "wallet_exit" in _low:
            _outcome_tag = "WALLET_EXIT"
        elif "stagnant" in _low or "timeout" in _low or "max_hold" in _low:
            _outcome_tag = "TIMEOUT"
        else:
            _outcome_tag = "MANUAL"
        _roab(mint, _outcome_tag, pnl_pct)
    except Exception as _bm_err:
        print(f"[copy-trade] brain memory record error: {_bm_err}")

    # ── Learning Engine: record exit outcome + schedule post-exit checks ──────
    try:
        from elizaos.plugins.solana.learning_engine import record_trade_exit
        _learning_rec = dict(record)
        # Pass entry context stored on pos dict by record_trade_entry
        _learning_rec["_entry_mc"]      = pos.get("_learning_entry_mc")
        _learning_rec["_entry_liq"]     = pos.get("_learning_entry_liq")
        _learning_rec["_entry_holders"] = pos.get("_learning_entry_holders")
        record_trade_exit(mint, _learning_rec)
    except Exception as _le_err:
        print(f"[copy-trade] Learning engine record error: {_le_err}")

    # ── Debrief Reporter: build multi-brain report after every 2 trades ───────
    try:
        from elizaos.plugins.solana.trade_monitor import consume_decision_log as _cdl
        from elizaos.plugins.solana.debrief_reporter import record_completed_trade as _rct
        _monitor_log = _cdl(mint)   # None if no monitor was active
        _rct(record, _monitor_log)
    except Exception as _dr_err:
        print(f"[copy-trade] Debrief reporter error: {_dr_err}")

    emoji = "✅" if pnl_sol >= 0 else "❌"
    msg = (
        f"{emoji} COPY-TRADE PAPER CLOSE: {pos['token_name']} ({mint[:8]}...)\n"
        f"   Reason: {reason}  •  Hold: {hold_mins:.1f}min\n"
        f"   P&L: {pnl_pct:+.1f}% = {pnl_sol:+.4f} SOL  •  Balance: {_paper_balance:.3f} SOL"
    )
    print(f"[copy-trade PAPER] {msg}")
    _push_alert(msg)


def _push_alert(msg: str) -> None:
    """Fire-and-forget alert to dashboard + Telegram."""
    async def _do():
        try:
            from elizaos.plugins.solana.dashboard_api import push_system_alert
            await push_system_alert(msg)
        except Exception:
            pass
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert
            await send_alert(f"🎯 {msg}")
        except Exception:
            pass
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_do())
    except Exception:
        pass


# ── Alert log (for Jarvis command) ───────────────────────────────────────────

def _record_alert(mint: str, token_name: str, wallet_names: list[str],
                  their_sol: float, our_sol: float) -> None:
    _alert_log.append({
        "ts":         time.time(),
        "mint":       mint,
        "token_name": token_name,
        "wallets":    wallet_names,
        "their_sol":  their_sol,
        "our_sol":    our_sol,
    })
    _save_all()


def _log_signal(mint: str, token_name: str, wallet_name: str,
                their_sol: float, action: str, skip_reason: str = "") -> None:
    """Log every detected signal with full context — entered, skipped, or sold."""
    _signal_log.append({
        "ts":          time.time(),
        "dt":          __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "mint":        mint,
        "token_name":  token_name,
        "wallet":      wallet_name,
        "sol":         round(their_sol, 4),   # amount the whale spent (used by dashboard feed)
        "their_sol":   round(their_sol, 4),   # kept for backward compat
        "action":      action,           # "entered" | "skipped" | "sold_by_whale"
        "skip_reason": skip_reason,      # "" | "slots_full" | "low_sol" | "duplicate" | ...
        "slots_used":  len(_paper_positions),
        "balance":     round(_paper_balance, 4),
    })
    try:
        with open(_SIGNAL_LOG_PATH, "w") as f:
            json.dump(_signal_log, f, indent=2)
    except Exception:
        pass


def _snapshot_hourly() -> None:
    """Record a point-in-time P&L snapshot (called every hour)."""
    closed = _paper_trades
    wins   = [t for t in closed if t.get("pnl_sol", 0) > 0]
    losses = [t for t in closed if t.get("pnl_sol", 0) <= 0]
    net    = sum(t.get("pnl_sol", 0) for t in closed)
    _hourly_log.append({
        "ts":            time.time(),
        "dt":            __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "balance":       round(_paper_balance, 4),
        "closed_trades": len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "net_pnl":       round(net, 4),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "open_count":    len(_paper_positions),
        "open_positions": [
            {
                "mint":      m,
                "token":     p["token_name"],
                "wallet":    p["wallet_name"],
                "entry":     p.get("entry_price", 0),
                "current":   p.get("current_price"),
                "pnl_pct":   p.get("pnl_pct"),
                "peak_pnl":  p.get("peak_pnl_pct"),
                "age_mins":  round((time.time() - p["entry_ts"]) / 60, 1),
            }
            for m, p in _paper_positions.items()
        ],
    })
    try:
        with open(_HOURLY_PATH, "w") as f:
            json.dump(_hourly_log, f, indent=2)
    except Exception:
        pass
    print(f"[copy-trade] 📊 Hourly snapshot: balance={_paper_balance:.3f} SOL  "
          f"trades={len(closed)} ({len(wins)}W/{len(losses)}L)  "
          f"net={net:+.4f} SOL  open={len(_paper_positions)}")


# ── Main polling loop ─────────────────────────────────────────────────────────

async def run_copy_trade_monitor(runtime: Any = None) -> None:
    """Continuously poll watched wallets every 3s via Helius enhanced transactions.

    - Detects BUY txs → opens paper position
    - Detects SELL txs → closes paper position and records P&L
    - Always runs regardless of copy_trade_enabled (paper mode is separate)
    """
    _load_all()

    try:
        from elizaos.plugins.solana import live_config as lc
    except Exception:
        lc = None  # type: ignore

    def _cfg(key: str, default: Any) -> Any:
        try:
            return lc.get(key, default) if lc else default
        except Exception:
            return default

    import re as _re
    # Extract Helius key from env var (loaded via dotenv at bot startup)
    helius_key = ""
    _rpc_url = os.environ.get("SOLANA_RPC_URL", "")
    if _rpc_url:
        _m = _re.search(r"api-key=([^&\s\"]+)", _rpc_url)
        helius_key = _m.group(1) if _m else ""
    if not helius_key:
        # Fallback: read .env directly (5 levels up from solana/)
        try:
            _env_path = os.path.join(_DIR, "..", "..", "..", "..", "..", ".env")
            _env_text = open(_env_path).read()
            _m2 = _re.search(r"api-key=([^&\s\"]+)", _env_text)
            helius_key = _m2.group(1) if _m2 else ""
        except Exception:
            pass

    if not helius_key:
        print("[copy-trade] No Helius API key — copy-trade monitor disabled")
        return

    PRICE_INTERVAL = 2.0   # dedicated price refresh cadence (independent of wallet scan)
    _hourly_counter = 0
    HOURLY_EVERY    = 1800  # snapshot every 1800 price ticks (~1 hour at 2s)

    # Write session start record
    _session_start = time.time()
    try:
        with open(_SESSION_PATH, "w") as _sf:
            json.dump({
                "session_start": _session_start,
                "session_start_dt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                "watched_wallets": list(WATCHED_WALLETS.keys()),
                "mode": "paper_24h_test",
            }, _sf, indent=2)
    except Exception:
        pass

    print(f"[copy-trade] Monitor started — watching {len(WATCHED_WALLETS)} wallets")
    for name, addr in WATCHED_WALLETS.items():
        print(f"[copy-trade]   {name}: {addr[:8]}...")

    async with aiohttp.ClientSession() as session:

        # ── Take profit parameters ────────────────────────────────────────────
        # No hard TP — let monster trades run. Exit only on: whale sells, SL, stagnant, or manual/Jarvis.

        # ── Stagnant low-MC exit parameters ───────────────────────────────────
        # A low-MC token that isn't moving is dead money — exit it.
        # High-MC tokens are let to run freely (whale exit / SL only).
        STAGNANT_MC_THRESHOLD  = 150_000   # USD — below this = "low MC"
        STAGNANT_WINDOW_SECS   = 15 * 60   # look back 15 minutes of checkpoints
        STAGNANT_MIN_HOLD_SECS = 10 * 60   # don't check before 10 min hold (give it time)
        STAGNANT_MOVE_PCT      = 5.0        # less than ±5% move in window = stagnant

        # ── Moonbag constants ──────────────────────────────────────────────────────
        TP1_PCT               = 40.0   # +40%: sell 60% → covers cost, can't lose
        TP2_PCT               = 100.0  # +100%: sell 50% of remaining (=20% original)
        MOONBAG_TRAIL_PCT     = 30.0   # trail stop: -30% from moonbag peak after TP1
        HEALTH_CHECK_SECS     = 30     # how often to check vol/MC and buy/sell ratio on moonbag
        # Vol/MC and buy/sell health-signal thresholds
        # vol/MC > 5x in 1h = exceptional momentum; < 0.5x after TP1 = momentum fading
        HEALTH_VOLFADE_RATIO  = 0.5    # vol_mc_ratio < this after TP1 = fading interest
        HEALTH_BSELL_EXIT     = 0.5    # buy_sell_ratio < this = mass exit signal
        HEALTH_BSELL_PUMP     = 3.0    # buy_sell_ratio > this = artificial pump — take exit

        # ── Price refresh task — runs every 2s, completely independent of wallet scan ──
        async def _price_refresh_task() -> None:
            global _paper_balance
            _hc = 0
            while True:
                try:
                    await asyncio.sleep(PRICE_INTERVAL)
                    if not _paper_positions:
                        continue
                    sl_pct        = float(_cfg("copy_trade_sl_pct", 10.0))
                    sl_multiplier = 1.0 - (sl_pct / 100.0)
                    # Hard TP: full exit at this level. 0 = disabled → use moonbag system.
                    hard_tp_pct   = float(_cfg("copy_trade_tp_pct", 0.0))
                    to_stop:    list[tuple[str, str]] = []  # (mint, reason) — full close
                    to_partial: list[tuple[str, int]]  = []  # (mint, tp_level) — partial exit (moonbag system)
                    to_ladder:  list[tuple[str, int]]  = []  # (mint, tranche) — copy-trade partial TP ladder
                    now = time.time()

                    for mint, pos in list(_paper_positions.items()):
                        entry = pos.get("entry_price", 0)
                        if not entry or entry <= 0:
                            continue

                        # ── Price source: Helius real-time (PumpSwap) → DexScreener fallback ──
                        current: float | None = None
                        mc_usd: float | None  = pos.get("mc_usd")

                        _pool_addr  = pos.get("pool_address")
                        _buy_pool_t = pos.get("buy_pool", "pump")  # "pump"=BC, "pump-amm"=PumpSwap
                        _ray_svc    = runtime.get_service("lp_pool") if runtime else None

                        _helius_ok = False
                        # Only use Helius for PumpSwap (graduated) tokens.
                        # Bonding curve addresses have a completely different memory layout —
                        # get_pumpswap_price_helius reads garbage vault offsets → false prices.
                        if _pool_addr and _ray_svc and _buy_pool_t == "pump-amm":
                            try:
                                _helius_price = await asyncio.wait_for(
                                    _ray_svc.get_pumpswap_price_helius(_pool_addr),
                                    timeout=3.0,
                                )
                                if _helius_price and _helius_price > 0:
                                    current = _helius_price
                                    _helius_ok = True
                            except Exception as _he:
                                # Log first failure per position to surface Helius issues
                                if not pos.get("_helius_err_logged"):
                                    print(f"[copy-trade] ⚠️  Helius price fail for {mint[:8]}: {_he!r} — DexScreener fallback")
                                    _paper_positions[mint]["_helius_err_logged"] = True

                        if not current:
                            current, _mc_fresh, _ = await _get_price_and_mc(mint, session)
                            if _mc_fresh:
                                mc_usd = _mc_fresh
                            # Only attempt pool resolution for PumpSwap buys.
                            # BC tokens (pool=pump) may graduate later — re-check every 2min.
                            if not _pool_addr and current:
                                if _buy_pool_t == "pump-amm":
                                    # Already a PumpSwap buy — just missing pool address, resolve now
                                    _resolved = await _resolve_pool_address(mint, session)
                                    if _resolved:
                                        _paper_positions[mint]["pool_address"] = _resolved
                                        print(f"[copy-trade] 🔗 Pool resolved for {mint[:8]}: {_resolved[:16]}... — Helius pricing active")
                                else:
                                    # BC buy — check if it has graduated to PumpSwap since entry
                                    _last_grad_check = pos.get("_last_grad_check", 0)
                                    if time.time() - _last_grad_check >= 120:  # check every 2min
                                        _paper_positions[mint]["_last_grad_check"] = time.time()
                                        _resolved = await _resolve_pool_address(mint, session)
                                        if _resolved:
                                            _paper_positions[mint]["pool_address"] = _resolved
                                            _paper_positions[mint]["buy_pool"]     = "pump-amm"
                                            print(f"[copy-trade] 🎓 {pos.get('token_name', mint[:8])} GRADUATED — PumpSwap pool {_resolved[:16]}... — Helius pricing active")

                        if not current or current <= 0:
                            continue

                        if mc_usd:
                            _paper_positions[mint]["mc_usd"] = mc_usd

                        pnl_now = (current - entry) / entry * 100.0
                        _paper_positions[mint]["current_price"] = current
                        _paper_positions[mint]["pnl_pct"]       = pnl_now

                        if pnl_now > _paper_positions[mint].get("peak_pnl_pct", 0.0):
                            _paper_positions[mint]["peak_pnl_pct"] = pnl_now
                            _paper_positions[mint]["peak_price"]   = current
                            _paper_positions[mint]["peak_ts"]      = now

                        _cps = _paper_positions[mint].setdefault("price_checkpoints", [])
                        _cps.append({"ts": now, "price": current, "pnl_pct": round(pnl_now, 2)})

                        # ── FIX 1: Stale price detector ──────────────────────────────────
                        # The Boys: DexScreener frozen for 30s while token crashed -62% on-chain.
                        # SL never fired because every reading showed flat entry price.
                        # If price hasn't moved > 0.5% in 15 consecutive ticks (30s) AND we
                        # are not using Helius (i.e., relying solely on DexScreener), the feed
                        # is likely frozen. Exit to stop a blind dump ending in full stop-loss.
                        STALE_TICK_LIMIT = 15  # 30 seconds of no movement
                        if not _helius_ok and len(_cps) >= STALE_TICK_LIMIT:
                            _recent_prices = [cp["price"] for cp in _cps[-STALE_TICK_LIMIT:]]
                            _stale_range = (max(_recent_prices) - min(_recent_prices)) / entry * 100
                            _stale_count = _paper_positions[mint].get("_stale_count", 0)
                            if _stale_range < 0.5:  # price completely frozen
                                _stale_count += 1
                                _paper_positions[mint]["_stale_count"] = _stale_count
                                if _stale_count == 1:
                                    print(
                                        f"[copy-trade] ⚠️  STALE PRICE: {pos['token_name']} ({mint[:8]}) "
                                        f"DexScreener frozen {STALE_TICK_LIMIT} ticks — real price unknown"
                                    )
                                if _stale_count >= 3:  # 3 consecutive stale checks = 90s frozen
                                    print(
                                        f"[copy-trade] 🚪 STALE EXIT: {pos['token_name']} ({mint[:8]}) "
                                        f"price frozen 90s — exiting blind position"
                                    )
                                    to_stop.append((mint, "stale_price_90s"))
                                    continue
                            else:
                                _paper_positions[mint]["_stale_count"] = 0  # reset on movement

                        # ── Read moonbag state ────────────────────────────────────────────
                        tp1_hit    = pos.get("tp1_hit", False)
                        tp2_hit    = pos.get("tp2_hit", False)
                        ceiling    = float(pos.get("moonbag_ceiling_pct", 400.0))
                        mbag_peak  = float(pos.get("moonbag_peak_price") or entry)

                        # ── PRE-TP1: SL logic ────────────────────────────────────────────
                        # Hard SL ALWAYS fires at the configured threshold (-10%).
                        # TradeMonitor can fire EARLIER as an additional exit condition
                        # but can NEVER block or delay the hard SL.
                        # Previous design gave TradeMonitor SL authority → it replaced the
                        # hard -10% with a -20% backstop → Groq going down left a -10% to
                        # -20% gap with zero protection. Fixed 2026-04-14.
                        if not tp1_hit:
                            if current <= entry * sl_multiplier:
                                drop = (current - entry) / entry * 100
                                print(
                                    f"[copy-trade] 🛑 SL: {pos['token_name']} ({mint[:8]}) "
                                    f"{drop:.1f}% (threshold -{sl_pct:.0f}%) — full exit"
                                )
                                to_stop.append((mint, f"stop_loss_{sl_pct:.0f}pct"))
                                continue

                        # ── Peak-Protection Trailing Stop (PPTS) ────────────────────────
                        # Fires BEFORE the hard TP if price reverses from a meaningful peak.
                        # Root cause for Newbird AI (-1.25% miss): peaked at +15.36% then
                        # crashed 16% in 4 seconds while the 2-confirmation delay was waiting.
                        # Fix: once peak ≥ PP_ACTIVATION_PCT, any pullback ≥ PP_DRAWDOWN_ABS
                        # percentage points from the peak fires an immediate full exit.
                        # This is faster than hard TP — no confirmation needed, fires on
                        # the FIRST tick where peak - current >= threshold.
                        _peak_now = _paper_positions[mint].get("peak_pnl_pct", 0.0)
                        PP_ACTIVATION_PCT  = float(_cfg("copy_trade_pp_activation_pct", 8.0))
                        PP_DRAWDOWN_ABS    = float(_cfg("copy_trade_pp_drawdown_abs", 3.5))
                        if _peak_now >= PP_ACTIVATION_PCT and not tp1_hit:
                            _pp_drawdown = _peak_now - pnl_now
                            if _pp_drawdown >= PP_DRAWDOWN_ABS:
                                print(
                                    f"[copy-trade] 🔒 PEAK PROTECTION: {pos['token_name']} ({mint[:8]}) "
                                    f"peak={_peak_now:+.1f}% → now={pnl_now:+.1f}% "
                                    f"(dropped {_pp_drawdown:.1f}% from peak, threshold {PP_DRAWDOWN_ABS:.1f}%) — exit"
                                )
                                to_stop.append((mint, f"peak_protection_{_peak_now:.0f}pct"))
                                continue

                        # ── Moonbag Peak-Protection (MPP) ───────────────────────────
                        # Post-TP1 the original PPTS was gated off, letting the moonbag
                        # run until wallet_exit. Result: My First Ankle peaked +95%
                        # and closed -9.6% on wallet_exit:clukz — unacceptable for a
                        # quant setup. MPP fires AFTER tp1_hit with drawdown tiers
                        # scaled to peak size: big peaks get wider leash, small peaks
                        # are cut tighter.
                        if tp1_hit:
                            if _peak_now >= 100.0:
                                _mpp_threshold = 40.0
                            elif _peak_now >= 50.0:
                                _mpp_threshold = 25.0
                            elif _peak_now >= 25.0:
                                _mpp_threshold = 15.0
                            else:
                                _mpp_threshold = 10.0
                            _mpp_drawdown = _peak_now - pnl_now
                            if _mpp_drawdown >= _mpp_threshold:
                                print(
                                    f"[copy-trade] 🌙🔒 MOONBAG PROTECTION: {pos['token_name']} ({mint[:8]}) "
                                    f"peak={_peak_now:+.1f}% → now={pnl_now:+.1f}% "
                                    f"(dropped {_mpp_drawdown:.1f}%, tier threshold {_mpp_threshold:.0f}%) — exit moonbag"
                                )
                                to_stop.append((mint, f"moonbag_protection_peak{_peak_now:.0f}pct"))
                                continue

                        # ── Copy-trade partial TP ladder ─────────────────────────────────
                        # Data: 52 wallet_exits gave back avg 21pp from peak → tranche profits
                        # on the way up so a late wallet_exit closes a moonbag, not the whole
                        # position. Defaults: L1 sells 40% at +8%, L2 sells 30% at +15%,
                        # L3 sells 20% at +30%. Remaining 10% runs until wallet_exit / SL /
                        # PP / trailing stop. Disabled by default; toggle with
                        # copy_trade_ladder_enabled=true. When ON, hard TP is suppressed
                        # (L2 at +15% does the same job with only 30% of the position).
                        _ladder_on = bool(_cfg("copy_trade_ladder_enabled", False))
                        if _ladder_on:
                            _l1 = float(_cfg("copy_trade_ladder_l1_pct", 12.0))
                            _l2 = float(_cfg("copy_trade_ladder_l2_pct", 20.0))
                            _l3 = float(_cfg("copy_trade_ladder_l3_pct", 35.0))
                            # Cascade fire: when a tick shows price already past multiple
                            # thresholds, queue ALL qualifying tiers at once so they execute
                            # back-to-back in this tick's _handle_copy_ladder_tp loop.
                            # Root cause Saleem Wif Hat (-2.7% on +41% peak): ladder fired one
                            # tier per 2s tick, giving the wallet dump time to crash real
                            # execution prices below the cached pnl reading between tiers.
                            _cascade: list[int] = []
                            if not pos.get("cp_l1_hit") and pnl_now >= _l1:
                                _cascade.append(1)
                            if not pos.get("cp_l2_hit") and pnl_now >= _l2:
                                _cascade.append(2)
                            if not pos.get("cp_l3_hit") and pnl_now >= _l3:
                                _cascade.append(3)
                            if _cascade:
                                for _t in _cascade:
                                    to_ladder.append((mint, _t))
                                _paper_positions[mint]["_spike_confirm"] = 0
                                continue
                            # Ladder active but no tier triggered → suppress hard TP and
                            # fall through to moonbag / health checks on the runner
                            _paper_positions[mint]["_spike_confirm"] = 0

                        # ── Hard TP: full exit at configured level (skips moonbag system) ──
                        # Single-tick for normal pumps, but with a DexScreener lag-spike guard.
                        # Root cause The Felon (-5.6% loss on +38.6% peak): price was frozen at
                        # -5.31% for 30 ticks, then DexScreener caught up and showed +38.63% in
                        # ONE tick. TP fired, sell executed, filled at -5.6% (price had already
                        # reversed). Fix: if the current tick's jump vs the previous tick is
                        # >15% absolute (a DexScreener catch-up spike), require 1 more
                        # confirmation. Normal progressive pumps (0→5→12→15%) have max 7%
                        # single-tick jumps and fire immediately.
                        if not _ladder_on and hard_tp_pct > 0 and pnl_now >= hard_tp_pct:
                            _prev_pnl = _cps[-2]["pnl_pct"] if len(_cps) >= 2 else 0.0
                            _single_tick_jump = pnl_now - _prev_pnl
                            SPIKE_GUARD_PCT = 15.0  # DexScreener catch-up threshold
                            if _single_tick_jump > SPIKE_GUARD_PCT:
                                # Suspicious: price jumped > 15% in a single 2s tick
                                # This is the DexScreener "frozen then catch-up" signature
                                _spike_conf = pos.get("_spike_confirm", 0) + 1
                                _paper_positions[mint]["_spike_confirm"] = _spike_conf
                                if _spike_conf < 2:
                                    print(
                                        f"[copy-trade] ⚠️  SPIKE GUARD: {pos['token_name']} ({mint[:8]}) "
                                        f"single-tick jump {_prev_pnl:+.1f}%→{pnl_now:+.1f}% "
                                        f"(+{_single_tick_jump:.1f}%) — DexScreener lag? waiting 1 more tick"
                                    )
                                    continue
                                # 2nd consecutive tick at TP+ after a spike — real move
                                print(
                                    f"[copy-trade] 🎯 HARD TP (spike confirmed): {pos['token_name']} ({mint[:8]}) "
                                    f"+{pnl_now:.1f}% — exit"
                                )
                            else:
                                _paper_positions[mint]["_spike_confirm"] = 0
                                print(
                                    f"[copy-trade] 🎯 HARD TP: {pos['token_name']} ({mint[:8]}) "
                                    f"+{pnl_now:.1f}% ≥ {hard_tp_pct:.0f}% — exit"
                                )
                            to_stop.append((mint, f"tp_{hard_tp_pct:.0f}pct"))
                            continue
                        else:
                            _paper_positions[mint]["_spike_confirm"] = 0

                        # ── TP1 at +40%: sell 60%, lock cost basis (used only when hard_tp_pct=0) ──
                        if hard_tp_pct <= 0 and not tp1_hit and pnl_now >= TP1_PCT:
                            to_partial.append((mint, 1))
                            continue

                        # ── TP2 at +100%: sell 50% of remaining (=20% original) ───────────
                        if hard_tp_pct <= 0 and tp1_hit and not tp2_hit and pnl_now >= TP2_PCT:
                            to_partial.append((mint, 2))
                            continue

                        # ── MOONBAG PHASE: After TP1, trailing stop + health signals ───────
                        if hard_tp_pct <= 0 and tp1_hit:
                            # Update moonbag peak — also resets AI trail-check so each
                            # new high gives the AI a fresh consultation on the next drawdown
                            if current > mbag_peak:
                                _paper_positions[mint]["moonbag_peak_price"] = current
                                mbag_peak = current
                                # Reset AI trail gate: new high → fresh check on next dip
                                _paper_positions[mint].pop("moonbag_trail_ai_checked", None)
                                _paper_positions[mint].pop("moonbag_trail_hold_until", None)

                            # Trailing stop: -30% from moonbag peak price
                            # AI cascade gate: Groq gets ONE chance to say HOLD (45s grace).
                            # After grace expires the rule fires unconditionally.
                            trail_trigger = mbag_peak * (1.0 - MOONBAG_TRAIL_PCT / 100.0)
                            if current <= trail_trigger:
                                drop_from_peak = (current - mbag_peak) / mbag_peak * 100

                                # ── Check if we're inside an active AI grace window ────────
                                _trail_hold_until = float(pos.get("moonbag_trail_hold_until", 0))
                                if now < _trail_hold_until:
                                    # AI said hold — we're still in the 45s grace period
                                    continue

                                # ── First trigger for this high: ask Groq ─────────────────
                                _trail_ai_checked = pos.get("moonbag_trail_ai_checked", False)
                                if not _trail_ai_checked:
                                    _paper_positions[mint]["moonbag_trail_ai_checked"] = True
                                    try:
                                        from elizaos.plugins.solana.trade_monitor import (
                                            ask_moonbag_hold as _ask_hold,
                                        )
                                        _ai_hold = await _ask_hold(
                                            mint, current, pnl_now, drop_from_peak, session
                                        )
                                    except Exception:
                                        _ai_hold = False

                                    if _ai_hold:
                                        # Groq says hold — 45s reprieve
                                        _paper_positions[mint]["moonbag_trail_hold_until"] = now + 45
                                        continue  # skip this tick

                                # AI said SELL, unavailable, or grace has expired — fire
                                print(
                                    f"[copy-trade] 🌙 TRAIL STOP: {pos['token_name']} ({mint[:8]}) "
                                    f"dropped {drop_from_peak:.1f}% from moonbag peak — closing"
                                )
                                to_stop.append((mint, f"moonbag_trail_stop_{abs(drop_from_peak):.0f}pct"))
                                continue

                            # Narrative ceiling: force exit if gain exceeds narrative expectation
                            if ceiling > 0 and pnl_now >= ceiling:
                                narr = pos.get("narrative", "unknown")
                                print(
                                    f"[copy-trade] 🎯 CEILING: {pos['token_name']} ({mint[:8]}) "
                                    f"+{pnl_now:.0f}% hit {narr} ceiling ({ceiling:.0f}%) — exiting"
                                )
                                to_stop.append((mint, f"narrative_ceiling_{narr}_{ceiling:.0f}pct"))
                                continue

                            # Health signals: vol/MC ratio and buy/sell ratio — every 30s
                            last_hc = float(pos.get("last_health_check", 0))
                            if now - last_hc >= HEALTH_CHECK_SECS:
                                _paper_positions[mint]["last_health_check"] = now
                                mdata = await _get_market_data(mint, session)
                                if mdata:
                                    bsr = mdata.get("buy_sell_ratio")
                                    vmr = mdata.get("vol_mc_ratio")
                                    # Mass exit signal: sells dominate
                                    if bsr is not None and bsr < HEALTH_BSELL_EXIT:
                                        print(
                                            f"[copy-trade] 🚨 HEALTH: {pos['token_name']} ({mint[:8]}) "
                                            f"buy/sell={bsr:.2f} < {HEALTH_BSELL_EXIT} — mass exit, closing moonbag"
                                        )
                                        to_stop.append((mint, f"health_mass_exit_bsr{bsr:.2f}"))
                                        continue
                                    # Artificial pump: buy/sell too lopsided
                                    if bsr is not None and bsr > HEALTH_BSELL_PUMP:
                                        print(
                                            f"[copy-trade] ⚠️  HEALTH: {pos['token_name']} ({mint[:8]}) "
                                            f"buy/sell={bsr:.2f} > {HEALTH_BSELL_PUMP} — artificial pump, closing moonbag"
                                        )
                                        to_stop.append((mint, f"health_artificial_pump_bsr{bsr:.2f}"))
                                        continue
                                    # Volume fading after TP1: momentum is gone
                                    if vmr is not None and vmr < HEALTH_VOLFADE_RATIO:
                                        print(
                                            f"[copy-trade] 💤 HEALTH: {pos['token_name']} ({mint[:8]}) "
                                            f"vol/MC={vmr:.2f} < {HEALTH_VOLFADE_RATIO} — volume fading, closing moonbag"
                                        )
                                        to_stop.append((mint, f"health_vol_fade_vmr{vmr:.2f}"))
                                        continue
                                    # Holder count velocity: 15%+ decline from entry = distribution
                                    _entry_hc = pos.get("entry_holder_count")
                                    _curr_hc  = mdata.get("holders")
                                    if _entry_hc and _curr_hc and _curr_hc < _entry_hc * 0.85:
                                        _decline_pct = (1.0 - _curr_hc / _entry_hc) * 100.0
                                        print(
                                            f"[copy-trade] 📉 HEALTH: {pos['token_name']} ({mint[:8]}) "
                                            f"holders {_entry_hc}→{_curr_hc} "
                                            f"(-{_decline_pct:.0f}%) — distribution signal, closing moonbag"
                                        )
                                        to_stop.append((mint, f"health_holder_decline_{_curr_hc}"))
                                        continue

                        # ── Max hold time (safety net) ─────────────────────────────────────
                        _max_hold_mins = float(_cfg("copy_trade_max_hold_mins", 60.0))
                        age_mins = (now - pos.get("entry_ts", now)) / 60.0
                        if _max_hold_mins > 0 and age_mins >= _max_hold_mins:
                            print(
                                f"[copy-trade] ⏰ MAX HOLD: {pos['token_name']} ({mint[:8]}) "
                                f"held {age_mins:.0f}m (limit {_max_hold_mins:.0f}m) — exiting"
                            )
                            to_stop.append((mint, f"max_hold_{_max_hold_mins:.0f}min"))
                            continue

                        # ── Stagnant low-MC exit (only before TP1 — moonbag runs free) ──────
                        if not tp1_hit:
                            hold_secs = now - pos.get("entry_ts", now)
                            if (
                                mc_usd
                                and mc_usd < STAGNANT_MC_THRESHOLD
                                and hold_secs >= STAGNANT_MIN_HOLD_SECS
                            ):
                                window_start = now - STAGNANT_WINDOW_SECS
                                window_pts = [
                                    cp for cp in pos.get("price_checkpoints", [])
                                    if cp["ts"] >= window_start
                                ]
                                if len(window_pts) >= 5:
                                    prices_in_window = [cp["price"] for cp in window_pts]
                                    lo = min(prices_in_window)
                                    hi = max(prices_in_window)
                                    spread_pct = (hi - lo) / lo * 100 if lo > 0 else 0
                                    if spread_pct < STAGNANT_MOVE_PCT:
                                        mc_k = f"${mc_usd/1000:.0f}k"
                                        print(
                                            f"[copy-trade] 💤 STAGNANT: {pos['token_name']} ({mint[:8]}) "
                                            f"MC={mc_k} | {spread_pct:.1f}% range in 15min "
                                            f"| P&L={pnl_now:+.1f}% — cutting dead weight"
                                        )
                                        to_stop.append((mint, f"stagnant_low_mc_{mc_k}"))

                    # ── Execute partial TPs (before full closes) ──────────────────────
                    for mint, tp_level in to_partial:
                        if mint in _paper_positions:
                            await _handle_partial_tp(mint, tp_level, session, runtime)

                    # ── Execute copy-trade ladder tranches ────────────────────────────
                    for mint, tranche in to_ladder:
                        if mint in _paper_positions:
                            await _handle_copy_ladder_tp(mint, tranche, session, runtime)

                    # ── Execute full closes ───────────────────────────────────────────
                    for mint, reason in to_stop:
                        if mint in _paper_positions:
                            await _close_paper_position(mint, reason, session, runtime)

                    # ── Frost Mirror: price refresh + SL monitoring ───────────────────
                    if _frost_positions:
                        frost_prices: dict[str, tuple[float, float | None]] = {}
                        # Batch-fetch prices for unique mints in frost positions
                        frost_mints = list({k.split(":")[0] for k in _frost_positions})
                        for fm in frost_mints:
                            fp, _fmc, _ = await _get_price_and_mc(fm, session)
                            frost_prices[fm] = (fp or 0.0, _fmc)

                        frost_to_close: list[tuple[str, str]] = []
                        for fkey, fpos in list(_frost_positions.items()):
                            fmint = fpos["mint"]
                            fp, _ = frost_prices.get(fmint, (0.0, None))
                            if not fp or fp <= 0:
                                continue
                            entry = fpos["entry_price"]
                            pnl   = (fp - entry) / entry * 100.0 if entry > 0 else 0.0
                            _frost_positions[fkey]["current_price"] = fp
                            _frost_positions[fkey]["pnl_pct"]       = pnl
                            if pnl > fpos.get("peak_pnl_pct", 0.0):
                                _frost_positions[fkey]["peak_pnl_pct"] = pnl
                            # Append to price history (capped at 120 points ≈ 4 mins at 2s ticks)
                            _ph = _frost_positions[fkey].get("price_history", [])
                            _ph.append({"ts": now, "pnl_pct": round(pnl, 2)})
                            if len(_ph) > 120:
                                _ph = _ph[-120:]
                            _frost_positions[fkey]["price_history"] = _ph
                            sl = fpos.get("sl_pct", FROST_MAIN_SL_PCT)
                            if pnl <= -sl:
                                mode_tag = fpos.get("mode", "main")
                                print(
                                    f"[frost-mirror] 🛑 SL hit on {fpos['token_name']} ({fmint[:8]}) "
                                    f"[{mode_tag}] {pnl:+.1f}% ≤ -{sl:.0f}% — closing"
                                )
                                frost_to_close.append((fkey, f"stop_loss_{sl:.0f}pct"))

                        for fkey, freason in frost_to_close:
                            if fkey in _frost_positions:
                                await _frost_close_position(fkey, freason, session, runtime)

                    # Hourly snapshot
                    _hc += 1
                    if _hc >= HOURLY_EVERY:
                        _hc = 0
                        _snapshot_hourly()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"[copy-trade] Price refresh error: {exc}")

        price_task = asyncio.get_event_loop().create_task(_price_refresh_task())

        # ── Wallet balance refresh — updates compounding base every 60s ───────
        async def _wallet_balance_refresh() -> None:
            global _cached_wallet_sol
            while True:
                await asyncio.sleep(60)
                try:
                    if runtime:
                        _ws = runtime.get_service("wallet")
                        if _ws:
                            _cached_wallet_sol = float(
                                await asyncio.wait_for(_ws.get_sol_balance(), timeout=5.0)
                            )
                            _base   = float(_cfg("copy_trade_paper_buy_sol", 0.4))
                            _sized  = _compound_position_size(_base)
                            _cpct   = float(_cfg("copy_trade_compound_pct", 0.20))
                            _raw    = round(_cached_wallet_sol * _cpct, 3)
                            _src    = "compound" if _raw > _base else "floor"
                            print(f"[compound] Wallet: {_cached_wallet_sol:.4f} SOL × {_cpct*100:.0f}% "
                                  f"= {_raw:.3f} SOL  →  trade size: {_sized:.3f} SOL ({_src})")
                except Exception:
                    pass

        wallet_refresh_task = asyncio.get_event_loop().create_task(_wallet_balance_refresh())

        # ── Jarvis 45-minute self-audit ───────────────────────────────────────
        # Runs independently every 45 minutes. Checks that SL/TP settings are still
        # at the correct values and that the bot is healthy. Alerts via dashboard + Telegram.
        AUDIT_INTERVAL_SECS = 45 * 60  # 45 minutes

        # ── Hardwired exit constants — these are never config-driven ─────────
        REQUIRED_SL_PCT       = 10.0   # hard SL before TP1 — must not exceed this
        REQUIRED_TP1_PCT      = 40.0   # TP1 trigger — sell 60% here
        REQUIRED_TP2_PCT      = 100.0  # TP2 trigger — sell 50% of remaining here
        REQUIRED_TRAIL_PCT    = 30.0   # moonbag trail stop — -30% from peak after TP1
        REQUIRED_HEALTH_SECS  = 30     # health check interval on moonbag

        # Hardwired compounding system constants — quant-locked 2026-04-14
        # 91-trade simulation result: +1.68 SOL vs +0.015 SOL actual.
        # Auto-corrected by self-audit if ANY brain drifts these values.
        REQUIRED_TP_PCT         = 15.0   # hard TP trigger — full exit
        REQUIRED_COMPOUND_PCT   = 0.20   # 20% of wallet rule
        REQUIRED_COMPOUND_MAX   = 2.0    # 2.0 SOL cap per trade

        async def _jarvis_self_audit() -> None:
            """Periodic compounding system integrity check — runs every 45 min.

            Verifies and auto-corrects:
            - SL still at 10%
            - TP still at 15% (hard full exit)
            - Compound pct still 20% of wallet
            - Compound max still 2.0 SOL cap
            - No position has been stuck bleeding for > 2h
            - Wallet loss cap is active
            - Reports compound tier and position status
            """
            await asyncio.sleep(AUDIT_INTERVAL_SECS)
            while True:
                try:
                    issues  = []
                    notices = []
                    now_a   = time.time()

                    # ── Check SL config hasn't drifted ────────────────────────
                    current_sl = float(_cfg("copy_trade_sl_pct", 10.0))
                    if current_sl > REQUIRED_SL_PCT:
                        issues.append(
                            f"⚠️ SL drifted to {current_sl:.0f}% — auto-resetting to {REQUIRED_SL_PCT:.0f}%"
                        )
                        from elizaos.plugins.solana import live_config as _lc_audit
                        _lc_audit.set_value("copy_trade_sl_pct", REQUIRED_SL_PCT,
                                            changed_by="jarvis_audit", reason="auto-corrected SL drift")

                    # ── Check TP hasn't drifted from 15% ──────────────────────
                    from elizaos.plugins.solana import live_config as _lc_audit2
                    current_tp = float(_cfg("copy_trade_tp_pct", REQUIRED_TP_PCT))
                    if abs(current_tp - REQUIRED_TP_PCT) > 0.5:
                        issues.append(
                            f"⚠️ TP drifted to {current_tp:.0f}% — auto-resetting to {REQUIRED_TP_PCT:.0f}%"
                        )
                        _lc_audit2.set_value("copy_trade_tp_pct", REQUIRED_TP_PCT,
                                             changed_by="jarvis_audit", reason="auto-corrected TP drift")

                    # ── Check compound_pct hasn't drifted from 20% ────────────
                    current_cpct = float(_cfg("copy_trade_compound_pct", REQUIRED_COMPOUND_PCT))
                    if abs(current_cpct - REQUIRED_COMPOUND_PCT) > 0.02:
                        issues.append(
                            f"⚠️ Compound pct drifted to {current_cpct:.2f} — auto-resetting to {REQUIRED_COMPOUND_PCT:.2f}"
                        )
                        _lc_audit2.set_value("copy_trade_compound_pct", REQUIRED_COMPOUND_PCT,
                                             changed_by="jarvis_audit", reason="auto-corrected compound_pct drift")

                    # ── Check compound_max hasn't drifted from 2.0 SOL ────────
                    current_cmax = float(_cfg("copy_trade_compound_max_sol", REQUIRED_COMPOUND_MAX))
                    if abs(current_cmax - REQUIRED_COMPOUND_MAX) > 0.05:
                        issues.append(
                            f"⚠️ Compound max drifted to {current_cmax:.2f} SOL — auto-resetting to {REQUIRED_COMPOUND_MAX:.2f}"
                        )
                        _lc_audit2.set_value("copy_trade_compound_max_sol", REQUIRED_COMPOUND_MAX,
                                             changed_by="jarvis_audit", reason="auto-corrected compound_max drift")

                    # ── Check live trading status ──────────────────────────────
                    live_on = bool(_cfg("copy_trade_enabled", False))
                    paused  = bool(_cfg("copy_trade_paused", False))

                    # ── Wallet loss cap status ─────────────────────────────────
                    wlc_sol = float(_cfg("copy_trade_wallet_loss_cap_sol", 0.3))
                    wlc_win = int(float(_cfg("copy_trade_wallet_loss_window_secs", 7200)) // 60)
                    _wlc_secs_a = float(_cfg("copy_trade_wallet_loss_window_secs", 7200))
                    _paused_wallets = []
                    for _wn, _wl in _wallet_loss_window.items():
                        _recent = sum(l for ts, l in _wl if ts >= now_a - _wlc_secs_a)
                        if _recent >= wlc_sol:
                            _paused_wallets.append(f"{_wn} (-{_recent:.3f} SOL)")

                    # ── Open position audit — moonbag health check ─────────────
                    open_count = len(_paper_positions)
                    pos_lines  = []
                    for mint, pos in list(_paper_positions.items()):
                        pnl      = pos.get("pnl_pct", 0.0) or 0.0
                        tp1      = pos.get("tp1_hit", False)
                        tp2      = pos.get("tp2_hit", False)
                        locked   = pos.get("locked_sol", 0.0) or 0.0
                        remaining= pos.get("remaining_fraction", 1.0) or 1.0
                        narr     = pos.get("narrative", "unknown")
                        ceil     = pos.get("moonbag_ceiling_pct", 400.0) or 400.0
                        age_m    = (now_a - pos.get("entry_ts", now_a)) / 60
                        age_s    = f"{age_m:.0f}min" if age_m < 60 else f"{age_m/60:.1f}h"

                        _audit_hard_tp = float(_cfg("copy_trade_tp_pct", 0.0))
                        if tp2:
                            stage = f"💎 moonbag 20% | locked={locked:.4f} SOL"
                        elif tp1:
                            stage = f"🎯 TP1 done | {remaining*100:.0f}% running | locked={locked:.4f} SOL"
                        elif _audit_hard_tp > 0:
                            stage = f"📍 full pos | SL -{REQUIRED_SL_PCT:.0f}% | TP +{_audit_hard_tp:.0f}% (hard exit)"
                        else:
                            stage = f"📍 full pos | SL at -{REQUIRED_SL_PCT:.0f}% | TP1 triggers at +{REQUIRED_TP1_PCT:.0f}%"

                        pos_lines.append(
                            f"  • {pos['token_name'][:18]:18s} ({pos['wallet_name']}) "
                            f"P&L={pnl:+.1f}% | age={age_s} | {stage}"
                        )
                        if narr != "unknown":
                            pos_lines.append(f"      narrative={narr} ceiling={ceil:.0f}%")

                        # Stuck pre-TP1 and bleeding: flag for review
                        if not tp1 and age_m > 120 and pnl < -5:
                            issues.append(
                                f"⚠️  {pos['token_name']} stuck {age_s} at {pnl:+.1f}% — TP1 not hit, "
                                f"review or close manually"
                            )
                        # Moonbag close to ceiling: proactive notice
                        if tp1 and ceil > 0 and pnl >= ceil * 0.80:
                            notices.append(
                                f"ℹ️  {pos['token_name']} at {pnl:+.1f}% — approaching {narr} "
                                f"narrative ceiling ({ceil:.0f}%). Trail stop will catch the turn."
                            )

                    # ── Compound tier snapshot ────────────────────────────────
                    _cpct_a = float(_cfg("copy_trade_compound_pct", REQUIRED_COMPOUND_PCT))
                    _cmax_a = float(_cfg("copy_trade_compound_max_sol", REQUIRED_COMPOUND_MAX))
                    _floor_a = float(_cfg("copy_trade_paper_buy_sol", 0.4))
                    _tp_a    = float(_cfg("copy_trade_tp_pct", REQUIRED_TP_PCT))
                    if _cached_wallet_sol >= 0.5:
                        _raw_c = round(_cached_wallet_sol * _cpct_a, 3)
                        _cur_trade = min(max(_floor_a, _raw_c), _cmax_a)
                    else:
                        _cur_trade = _floor_a
                    _next_thresh = round(_floor_a / _cpct_a, 2)
                    _in_compound = _cached_wallet_sol >= _next_thresh

                    # ── Build audit report ────────────────────────────────────
                    status = "🔴 ISSUES FOUND — auto-corrected" if issues else "✅ All nominal"
                    report_lines = [
                        f"🤖 JARVIS 45-MIN AUDIT — Compounding System",
                        f"{'─'*42}",
                        f"Status: {status}",
                        f"",
                        f"EXIT RULES (hardwired — auto-corrected if drifted):",
                        f"  TP:  +{_tp_a:.0f}% → full exit  {'✅' if abs(_tp_a - REQUIRED_TP_PCT) < 0.5 else '🔴 DRIFTED'}",
                        f"  SL:  -{current_sl:.0f}% → full exit  {'✅' if current_sl <= REQUIRED_SL_PCT else '🔴 DRIFTED'}",
                        f"",
                        f"COMPOUNDING STATE:",
                        f"  Wallet:       {_cached_wallet_sol:.4f} SOL",
                        f"  Trade size:   {_cur_trade:.3f} SOL  ({'compounding' if _in_compound else f'floor — kicks in at {_next_thresh:.2f} SOL'})",
                        f"  Rule:         {_cpct_a*100:.0f}% of wallet, {_floor_a:.2f} floor, {_cmax_a:.1f} SOL cap  {'✅' if abs(_cpct_a - REQUIRED_COMPOUND_PCT) < 0.02 else '🔴 DRIFTED'}",
                        f"",
                        f"SAFEGUARDS:",
                        f"  Wallet loss cap:   {wlc_sol:.2f} SOL / {wlc_win}min",
                        f"  Paused wallets:    {', '.join(_paused_wallets) if _paused_wallets else 'none'}",
                        f"  Live trading:      {'🔴 ON' if live_on else '📋 PAPER'}{'  (PAUSED)' if paused else ''}",
                        f"",
                        f"OPEN POSITIONS ({open_count}):",
                    ]
                    report_lines += pos_lines if pos_lines else ["  none"]

                    if issues:
                        report_lines += ["", "⚠️  ISSUES AUTO-CORRECTED:"] + [f"  {i}" for i in issues]
                    if notices:
                        report_lines += ["", "ℹ️  NOTICES:"] + [f"  {n}" for n in notices]

                    report = "\n".join(report_lines)
                    print(f"[audit] {report}")
                    _push_alert(report)

                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"[audit] Self-audit error: {exc}")

                await asyncio.sleep(AUDIT_INTERVAL_SECS)

        audit_task = asyncio.get_event_loop().create_task(_jarvis_self_audit())

        # ── Learning Engine: bootstrap from history + background scheduler ────────
        try:
            from elizaos.plugins.solana.learning_engine import (
                run_learning_scheduler,
                bootstrap_from_copy_trades,
                run_pattern_analysis,
            )
            # Seed outcomes from historical paper trades if we're starting cold
            _bootstrapped = bootstrap_from_copy_trades()
            if _bootstrapped >= 10:
                # Enough new data — trigger Opus pattern analysis so brains have lessons
                asyncio.get_event_loop().create_task(run_pattern_analysis(session))
                print(f"[copy-trade] 🔬 Pattern analysis triggered ({_bootstrapped} new outcomes)")
            asyncio.get_event_loop().create_task(run_learning_scheduler(session))
            print("[copy-trade] 🧠 Learning engine scheduler started")
        except Exception as _le_start_err:
            print(f"[copy-trade] Learning engine start error: {_le_start_err}")

        # ── Brain memory refresh loop — starts immediately on bot boot ────────
        try:
            from elizaos.plugins.solana import brain_memory as _bm_startup
            _bm_startup.ensure_memory_loop_started()
            print("[copy-trade] 🧠 Brain memory refresh loop started (120s cycle)")
        except Exception as _bm_err:
            print(f"[copy-trade] Brain memory start error: {_bm_err}")

        # ── WebSocket transaction handler — called for every confirmed tx ─────
        async def _handle_tx(tx: dict, wallet_addr: str, wallet_name: str,
                             t_ws_notify: float = 0.0, t_tx_fetched: float = 0.0) -> None:
            """Process a single transaction from the WebSocket stream."""
            import aiohttp as _aio
            async with _aio.ClientSession() as _ws_session:
                paper_enabled = bool(_cfg("copy_trade_paper_enabled", True))
                if not paper_enabled:
                    return

                min_sol       = float(_cfg("copy_trade_min_sol", 0.5))
                consensus_req = int(_cfg("copy_trade_consensus", 1))
                from elizaos.plugins.solana import live_config as _lc_c2
                MAX_COPY_POSITIONS = int(_lc_c2.get("trenchman_max_positions", 2))
                # Compounding: starts at base SOL, scales up with wallet balance as profits grow
                _base_copy_sol = float(_cfg("copy_trade_paper_buy_sol", 0.10))
                paper_buy_sol  = _compound_position_size(_base_copy_sol)

                # ── Check for SELL first ───────────────────────────────────────
                sell_result = _parse_sell(tx, wallet_addr)
                if sell_result:
                    sell_mint, sol_received = sell_result
                    tok_name = _paper_positions.get(sell_mint, {}).get("token_name", sell_mint[:8])
                    _log_signal(sell_mint, tok_name, wallet_name, sol_received, "sold_by_whale")
                    if sell_mint in _paper_positions:
                        t_sell_start = time.time()
                        fetch_ms = round((t_tx_fetched - t_ws_notify) * 1000) if t_ws_notify else 0
                        print(
                            f"[copy-trade] 📤 {wallet_name} SOLD "
                            f"{_paper_positions[sell_mint]['token_name']} "
                            f"({sell_mint[:8]}...) — closing position"
                            + (f" [WS→TX fetch: {fetch_ms}ms]" if fetch_ms else "")
                        )
                        await _close_paper_position(
                            sell_mint, f"wallet_exit:{wallet_name}", _ws_session, runtime
                        )
                        t_sell_done = time.time()
                        total_ms = round((t_sell_done - t_ws_notify) * 1000) if t_ws_notify else round((t_sell_done - t_sell_start) * 1000)
                        print(f"[copy-trade] ⏱️  EXIT LATENCY: {total_ms}ms total (fetch={fetch_ms}ms + close={round((t_sell_done-t_sell_start)*1000)}ms)")
                    # ── Close Frost mirror positions on this mint ─────────────
                    if wallet_name in _FROST_MIRROR_WALLETS:
                        # Check if Frost still holds any of this token (partial exit)
                        # vs fully sold out (full exit).  We need this because Frost
                        # uses a two-tranche strategy: he may exit his MAIN position
                        # first and hold the LOTTO tranche for a moonshot.
                        # On partial exit: close our main only, keep lotto running.
                        # On full exit:    close everything.
                        _frost_still_holds = await _frost_whale_still_holds(
                            sell_mint, wallet_addr, _ws_session
                        )
                        if _frost_still_holds:
                            print(
                                f"[frost-mirror] 🔀 Frost partial exit on {sell_mint[:8]} "
                                f"— he still holds tokens → closing MAIN only, keeping LOTTO"
                            )
                            await _frost_close_mode_for_mint(
                                sell_mint, "main",
                                f"wallet_partial_exit:{wallet_name}",
                                _ws_session, runtime
                            )
                        else:
                            print(
                                f"[frost-mirror] 🚪 Frost full exit on {sell_mint[:8]} "
                                f"— closing ALL mirror positions"
                            )
                            await _frost_close_all_for_mint(
                                sell_mint, f"wallet_exit:{wallet_name}", _ws_session, runtime
                            )
                    return

                # ── Check for BUY ─────────────────────────────────────────────
                buy_result = _parse_buy(tx, wallet_addr)
                if not buy_result:
                    return

                mint, sol_amount = buy_result

                t_buy_detected = time.time()
                fetch_ms = round((t_tx_fetched - t_ws_notify) * 1000) if t_ws_notify else 0

                # Capture whale's on-chain block timestamp for entry lag calculation
                whale_buy_ts = float(tx.get("blockTime") or 0)

                # ── Frost Mirror: route BEFORE standard copy-trade path ────────
                # Frost uses a two-tranche strategy (large scout + tiny lottery).
                # We mirror both independently, exit only when Frost exits, no TP.
                # Completely separate from the standard 15% TP / 10% SL system.
                if wallet_name in _FROST_MIRROR_WALLETS:
                    token_name = await _get_token_name(mint, _ws_session)
                    print(
                        f"[frost-mirror] 🔔 Frost bought {token_name} ({mint[:8]}) "
                        f"{sol_amount:.3f} SOL — routing to mirror strategy"
                        + (f" [WS→fetch: {fetch_ms}ms]" if fetch_ms else "")
                    )
                    await _frost_open_position(mint, token_name, sol_amount, _ws_session, runtime)
                    return

                # ── Standard path for all other wallets ───────────────────────
                token_name = await _get_token_name(mint, _ws_session)
                wallets_in = _check_consensus(mint, wallet_name, sol_amount)

                print(
                    f"[copy-trade] 🔔 {wallet_name} bought {token_name} "
                    f"{sol_amount:.2f} SOL  ({len(wallets_in)} wallet(s) in window)"
                    + (f" [WS→fetch: {fetch_ms}ms]" if fetch_ms else "")
                )
                _record_alert(mint, token_name, wallets_in, sol_amount, paper_buy_sol)

                if bool(_cfg("copy_trade_paused", False)):
                    print(f"[copy-trade] ⏸️  PAUSED — skipping {token_name} entry")
                    return

                # ── Standard copy trade path for rambo / Walta / Schoen ────────
                if sol_amount < min_sol:
                    return

                if len(wallets_in) >= consensus_req:
                    await _open_position(
                        mint, token_name, paper_buy_sol,
                        wallet_name, _ws_session, runtime,
                        whale_buy_ts=whale_buy_ts,
                    )
                    t_buy_done = time.time()
                    total_ms = round((t_buy_done - t_ws_notify) * 1000) if t_ws_notify else round((t_buy_done - t_buy_detected) * 1000)
                    dex_ms   = round((t_buy_done - t_buy_detected) * 1000)
                    print(f"[copy-trade] ⏱️  ENTRY LATENCY: {total_ms}ms total (fetch={fetch_ms}ms + validate+buy={dex_ms}ms)")

        # ── Fetch full tx from Helius REST (fallback only) ───────────────────
        async def _fetch_and_handle(sig: str, wallet_addr: str, wallet_name: str, hkey: str) -> None:
            """Fallback: fetch tx via REST when transactionSubscribe data is missing."""
            t_ws_notify = time.time()
            tx = await _helius_get_tx(sig, session, hkey)
            t_tx_fetched = time.time()
            if tx:
                await _handle_tx(tx, wallet_addr, wallet_name, t_ws_notify, t_tx_fetched)

        # ── Per-wallet WebSocket subscription loop ────────────────────────────
        async def _wallet_ws_loop(wallet_name: str, wallet_addr: str) -> None:
            """Subscribe via Helius transactionSubscribe — full tx arrives with the WebSocket
            message, eliminating the separate REST fetch entirely (~50-150ms saved per signal).
            Falls back to logsSubscribe + REST fetch if transactionSubscribe data is missing.
            Reconnects automatically on disconnect."""
            import json as _j
            import aiohttp as _aio
            import os as _os

            ws_url = _os.getenv("SOLANA_WS_URL", f"wss://mainnet.helius-rpc.com/?api-key={helius_key}")

            # logsSubscribe: works on all Helius plans. Fires on every tx mentioning wallet_addr.
            # We get the signature then fetch the full tx via REST (_fetch_and_handle).
            subscribe_msg = _j.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [wallet_addr]},
                    {"commitment": "confirmed"},
                ],
            })

            backoff = 2.0
            while True:
                try:
                    async with _aio.ClientSession() as _s:
                        async with _s.ws_connect(
                            ws_url,
                            heartbeat=30,
                            timeout=_aio.ClientWSTimeout(ws_close=15.0),
                        ) as ws:
                            await ws.send_str(subscribe_msg)
                            backoff = 2.0
                            print(f"[copy-trade] ⚡ WebSocket connected (logsSubscribe) — watching {wallet_name} ({wallet_addr[:8]}...)")

                            async for msg in ws:
                                if msg.type == _aio.WSMsgType.TEXT:
                                    try:
                                        data = _j.loads(msg.data)
                                        method = data.get("method", "")

                                        if method == "logsNotification":
                                            t_ws_notify = time.time()
                                            result = data.get("params", {}).get("result", {})
                                            sig = result.get("value", {}).get("signature")
                                            if sig:
                                                asyncio.create_task(
                                                    _fetch_and_handle(sig, wallet_addr, wallet_name, helius_key)
                                                )

                                    except Exception:
                                        pass
                                elif msg.type in (_aio.WSMsgType.CLOSED, _aio.WSMsgType.ERROR):
                                    break

                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    print(f"[copy-trade] WS {wallet_name} disconnected ({exc}) — reconnecting in {backoff:.0f}s")

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

        # ── Launch one WebSocket task per wallet (skip poll-only wallets) ─────
        ws_tasks = []
        for wname, waddr in WATCHED_WALLETS.items():
            if waddr not in _BLACKLISTED_WALLETS and waddr not in _POLL_ONLY_WALLETS:
                ws_tasks.append(
                    asyncio.get_event_loop().create_task(_wallet_ws_loop(wname, waddr))
                )

        print(f"[copy-trade] ⚡ WebSocket mode — {len(ws_tasks)} wallet stream(s) active (zero polling)")

        # ── Poll-based monitor for aggregator wallets (not visible in logsSubscribe) ──
        async def _wallet_poll_loop(wallet_name: str, wallet_addr: str) -> None:
            """Poll getSignaturesForAddress every 3s for wallets whose txs never fire
            logsSubscribe (e.g. Goyim uses GMGN aggregator -- address not in log text)."""
            seen_sigs: set = set()
            POLL_INTERVAL = 3.0
            backoff = POLL_INTERVAL
            _rpc_endpoint = os.environ.get(
                "SOLANA_RPC_URL",
                f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
            )
            print(f"[copy-trade] \U0001f504 Poll monitor started -- watching {wallet_name} ({wallet_addr[:8]}...) every {POLL_INTERVAL:.0f}s")
            async with aiohttp.ClientSession() as _poll_sess:
                while True:
                    try:
                        payload = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getSignaturesForAddress",
                            "params": [wallet_addr, {"limit": 10, "commitment": "confirmed"}],
                        }
                        async with _poll_sess.post(
                            _rpc_endpoint,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as resp:
                            data = await resp.json()
                        result = data.get("result") or []
                        if result:
                            new_sigs = [
                                s["signature"] for s in result
                                if s.get("signature") not in seen_sigs and not s.get("err")
                            ]
                            if seen_sigs:  # skip seed round -- don't fire on old txs at startup
                                for sig in reversed(new_sigs):
                                    asyncio.create_task(_fetch_and_handle(sig, wallet_addr, wallet_name, helius_key))
                            for s in result:
                                seen_sigs.add(s.get("signature"))
                            if len(seen_sigs) > 200:
                                seen_sigs = set(list(seen_sigs)[-100:])
                        backoff = POLL_INTERVAL
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        print(f"[copy-trade] Poll {wallet_name} error: {exc} -- retrying in {backoff:.0f}s")
                        backoff = min(backoff * 2, 30.0)
                    await asyncio.sleep(backoff)

        for wname, waddr in WATCHED_WALLETS.items():
            if waddr not in _BLACKLISTED_WALLETS and waddr in _POLL_ONLY_WALLETS:
                ws_tasks.append(asyncio.get_event_loop().create_task(_wallet_poll_loop(wname, waddr)))
                print(f"[copy-trade] \U0001f504 Poll monitor -- {wname} uses aggregator, polling every 3s")

        # ── Safety net: periodic wallet balance scan ──────────────────────────
        # WebSocket sell detection can miss pump.fun BC sells (type=UNKNOWN).
        # Every 20s, fetch each watched wallet's on-chain token balances.
        # If a wallet no longer holds a token we're mirroring → close immediately.
        # Reduced from 90s to 20s (2026-04-14): Schoen/Frost scalp trades last
        # only 60s — a 90s scan window missed every single one. At 20s we catch
        # whale exits within the first DexScreener freeze window.
        async def _wallet_balance_safety_net() -> None:
            """Periodic Helius wallet scan — catches any sell the WebSocket missed."""
            SCAN_INTERVAL = 20  # seconds between scans
            while True:
                await asyncio.sleep(SCAN_INTERVAL)
                try:
                    if not _paper_positions:
                        continue
                    helius_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
                    async with aiohttp.ClientSession() as _scan_sess:
                        for wname, waddr in WATCHED_WALLETS.items():
                            # Find positions we hold that were opened following this wallet
                            mirrored = {
                                mint: pos for mint, pos in list(_paper_positions.items())
                                if pos.get("wallet_name") == wname
                            }
                            if not mirrored:
                                continue
                            # Fetch this wallet's current token balances via Helius
                            try:
                                payload = {
                                    "jsonrpc": "2.0", "id": 1,
                                    "method": "getTokenAccountsByOwner",
                                    "params": [
                                        waddr,
                                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                        {"encoding": "jsonParsed"},
                                    ],
                                }
                                async with _scan_sess.post(
                                    helius_url,
                                    json=payload,
                                    timeout=aiohttp.ClientTimeout(total=8),
                                ) as resp:
                                    if resp.status != 200:
                                        continue
                                    data = await resp.json()
                                    accounts = data.get("result", {}).get("value", [])
                                    wallet_mints = {
                                        acc["account"]["data"]["parsed"]["info"]["mint"]
                                        for acc in accounts
                                        if acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("uiAmount", 0) > 0
                                    }
                            except Exception:
                                continue

                            # Check each mirrored position against wallet's actual holdings
                            for mint, pos in list(mirrored.items()):
                                if mint not in wallet_mints:
                                    token = pos.get("token_name", mint[:10])
                                    # ── False-positive guard ─────────────────────
                                    # A brief RPC indexing gap right after a buy can show a
                                    # temporary 0-balance, causing premature exits. This only
                                    # happens within the first ~60s of a position (observed
                                    # 2026-04-09: clukz 3x3H8rEL fired 4 min early due to
                                    # indexing lag right after buy). After 60s the balance
                                    # is fully settled so we trust it directly.
                                    # Reduced from 120s to 60s (2026-04-12): the pre-entry
                                    # staleness check now catches stale signals before entry,
                                    # so the safety-net can fire faster when whale already sold.
                                    pos_age_secs = time.time() - pos.get("entry_ts", 0)
                                    sell_confirmed = True  # default: trust balance=0

                                    if pos_age_secs < 60:
                                        # Young position: verify a real sell tx exists before closing
                                        sell_confirmed = False
                                        try:
                                            verify_url = (
                                                f"https://api.helius.xyz/v0/addresses/{waddr}"
                                                f"/transactions?api-key={helius_key}&limit=25"
                                            )
                                            async with _scan_sess.get(
                                                verify_url,
                                                timeout=aiohttp.ClientTimeout(total=5),
                                            ) as vr:
                                                if vr.status == 200:
                                                    for vtx in await vr.json():
                                                        sr = _parse_sell(vtx, waddr)
                                                        if sr and sr[0] == mint:
                                                            sell_confirmed = True
                                                            break
                                        except Exception:
                                            # Verification failed — proceed with close
                                            sell_confirmed = True

                                    if not sell_confirmed:
                                        print(
                                            f"[safety-net] ⚠️  {wname} balance=0 for {token} ({mint[:8]}) "
                                            f"but position only {pos_age_secs:.0f}s old — skipping (indexing lag guard)"
                                        )
                                        continue

                                    print(f"[safety-net] 🎯 {wname} sold {token} ({mint[:8]}) — closing mirror position")
                                    try:
                                        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                                        asyncio.create_task(_tg(
                                            f"🕵️ <b>Safety net catch</b>: {wname} sold {token}\n"
                                            f"WebSocket missed the signal — closing via balance scan"
                                        ))
                                    except Exception:
                                        pass
                                    asyncio.create_task(
                                        _close_paper_position(mint, f"wallet_exit:{wname}", _scan_sess, runtime)
                                    )

                    # ── Frost Mirror safety net — scan Frost wallet for exited tokens ──
                    if _frost_positions:
                        frost_addr = WATCHED_WALLETS.get("Frost")
                        if frost_addr:
                            try:
                                frost_payload = {
                                    "jsonrpc": "2.0", "id": 2,
                                    "method": "getTokenAccountsByOwner",
                                    "params": [
                                        frost_addr,
                                        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                        {"encoding": "jsonParsed"},
                                    ],
                                }
                                async with _scan_sess.post(
                                    helius_url,
                                    json=frost_payload,
                                    timeout=aiohttp.ClientTimeout(total=8),
                                ) as fr:
                                    if fr.status == 200:
                                        fdata = await fr.json()
                                        faccounts = fdata.get("result", {}).get("value", [])
                                        frost_wallet_mints = {
                                            acc["account"]["data"]["parsed"]["info"]["mint"]
                                            for acc in faccounts
                                            if acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("uiAmount", 0) > 0
                                        }
                                        # Close frost positions for mints Frost no longer holds
                                        frost_mints_tracked = list({
                                            fpos["mint"] for fpos in _frost_positions.values()
                                        })
                                        for fmint in frost_mints_tracked:
                                            if fmint not in frost_wallet_mints:
                                                # Only close if position is >60s old (indexing lag guard)
                                                keys_for_mint = [
                                                    k for k, p in _frost_positions.items()
                                                    if p["mint"] == fmint
                                                ]
                                                for fk in keys_for_mint:
                                                    if fk not in _frost_positions:
                                                        continue
                                                    fpos_age = time.time() - _frost_positions[fk].get("entry_ts", 0)
                                                    if fpos_age < 60:
                                                        continue
                                                    tok = _frost_positions[fk].get("token_name", fmint[:10])
                                                    print(f"[frost-mirror] 🕵️ Safety net: Frost sold {tok} ({fmint[:8]}) — closing mirror")
                                                    asyncio.create_task(
                                                        _frost_close_position(fk, "wallet_exit:Frost", _scan_sess, runtime)
                                                    )
                            except Exception as _fse:
                                print(f"[frost-mirror] safety-net error: {_fse}")

                except asyncio.CancelledError:
                    return
                except Exception as _sn_err:
                    print(f"[safety-net] scan error: {_sn_err}")

        safety_net_task = asyncio.get_event_loop().create_task(_wallet_balance_safety_net())
        print("[copy-trade] 🕵️ Safety net scan active — checking wallet balances every 20s")

        # ── Our-wallet orphan scanner — catches tokens we hold but don't track ───
        # Fires every 3 min. Sells any token in OUR wallet not tracked by the copy
        # trader OR the main strategy position manager.  Catches:
        #   - Buy confirmed on-chain but position tracking failed (exception/crash)
        #   - _close_paper_position popped the position before sell succeeded
        #   - Any other way tokens land in the wallet without an open position record
        async def _our_wallet_orphan_scanner() -> None:
            SCAN_INTERVAL = 180   # seconds between scans
            GRACE_PERIOD  = 120   # ignore tokens < 2 min old (async tracking race)
            our_wallet = os.getenv("SOLANA_PUBLIC_KEY", "")
            if not our_wallet:
                print("[orphan-scan] ⚠️  SOLANA_PUBLIC_KEY not set — orphan scanner disabled")
                return
            hw_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            _first_seen: dict[str, float] = {}   # mint → first time we saw it untracked
            _sell_failed: dict[str, float] = {}  # mint → timestamp of last failed sell (30min cooldown)

            while True:
                await asyncio.sleep(SCAN_INTERVAL)
                try:
                    async with aiohttp.ClientSession() as _orp_sess:
                        # Fetch our wallet's token accounts
                        payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTokenAccountsByOwner",
                            "params": [
                                our_wallet,
                                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                {"encoding": "jsonParsed"},
                            ],
                        }
                        async with _orp_sess.post(
                            hw_url,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            accounts = data.get("result", {}).get("value", [])

                        # Get strategy position_manager tracked mints (avoid selling live trades)
                        pm_mints: set[str] = set()
                        try:
                            pm_svc = runtime.get_service("position_manager") if runtime else None
                            if pm_svc and hasattr(pm_svc, "positions"):
                                pm_mints = set(pm_svc.positions.keys())
                        except Exception:
                            pass

                        now = time.time()
                        current_mints: set[str] = set()

                        for acc in accounts:
                            info = (acc.get("account", {})
                                    .get("data", {})
                                    .get("parsed", {})
                                    .get("info", {}))
                            mint      = info.get("mint", "")
                            ui_amount = info.get("tokenAmount", {}).get("uiAmount", 0) or 0

                            if not mint or ui_amount <= 0 or mint in _SKIP_MINTS:
                                continue

                            current_mints.add(mint)

                            # Token is tracked — no problem
                            if mint in _paper_positions or mint in pm_mints:
                                _first_seen.pop(mint, None)
                                continue

                            # Untracked: record when we first noticed it
                            if mint not in _first_seen:
                                _first_seen[mint] = now
                                continue  # wait one more scan cycle before acting

                            if now - _first_seen[mint] < GRACE_PERIOD:
                                continue  # still within grace period

                            # ── Confirmed orphan: untracked for > GRACE_PERIOD ──
                            token_name = mint[:8] + "..."
                            print(
                                f"[orphan-scan] 🚨 ORPHANED TOKEN: {token_name} "
                                f"({mint[:8]}) {ui_amount:.0f} tokens — "
                                f"not tracked by copy-trade or strategy — SELLING NOW"
                            )
                            try:
                                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_orp
                                asyncio.create_task(_tg_orp(
                                    f"🚨 <b>ORPHANED TOKEN — AUTO-SELL</b>\n"
                                    f"Token: {token_name}\n"
                                    f"Amount: {ui_amount:.0f} tokens\n"
                                    f"Untracked for >{GRACE_PERIOD}s — emergency sell triggered"
                                ))
                            except Exception:
                                pass

                            # Don't spam retries — if sell failed recently, wait 30 min
                            SELL_FAIL_COOLDOWN = 1800
                            last_fail = _sell_failed.get(mint, 0.0)
                            if now - last_fail < SELL_FAIL_COOLDOWN:
                                print(f"[orphan-scan] ⏳ {token_name} sell cooldown ({int((SELL_FAIL_COOLDOWN - (now - last_fail)) / 60)}min left) — skipping")
                                continue

                            from elizaos.plugins.solana import live_config as _lc_orp
                            if bool(_lc_orp.get("copy_trade_enabled", False)) and runtime:
                                sell_ok, _ = await _execute_live_sell(
                                    mint, token_name, _orp_sess, runtime
                                )
                                if sell_ok:
                                    print(f"[orphan-scan] ✅ {token_name} sold — SOL recovered")
                                    _first_seen.pop(mint, None)
                                    _sell_failed.pop(mint, None)
                                else:
                                    print(f"[orphan-scan] ❌ {token_name} sell failed — 30min cooldown before retry")
                                    _sell_failed[mint] = now
                            else:
                                print(f"[orphan-scan] ⚠️  Paper/live-off mode — orphan {token_name} logged but not sold")

                        # ── Reconciliation: open positions with NO tokens in wallet ──
                        # A position might have been popped from _paper_positions by a
                        # phantom sell that actually succeeded but we retried. Or a position
                        # is tracked as open but the token was sold by other means (manual,
                        # prior run). Force-close anything with zero on-chain balance.
                        for cp_mint, cp_pos in list(_paper_positions.items()):
                            if cp_mint in pm_mints:
                                continue   # strategy position — don't touch
                            if cp_mint in current_mints:
                                continue   # tokens are there, all good
                            cp_age = now - cp_pos.get("entry_ts", now)
                            if cp_age < GRACE_PERIOD:
                                continue   # too young — indexing lag
                            print(
                                f"[orphan-scan] 🔄 RECONCILE: {cp_pos.get('token_name', cp_mint[:8])} "
                                f"tracked as OPEN but zero balance on-chain — forcing close as recovered_sell"
                            )
                            asyncio.create_task(
                                _close_paper_position(cp_mint, "reconcile_zero_balance", _orp_sess, runtime)
                            )

                        # ── Frost Mirror ghost position reconciler ────────────────
                        # Frost positions tracked in _frost_positions where OUR wallet
                        # holds zero tokens are ghost/orphan entries (buy failed, prior
                        # session artifact, etc.). Force-clear them so dashboard is clean.
                        for fkey, fpos in list(_frost_positions.items()):
                            fmint = fpos.get("mint", "")
                            if not fmint:
                                continue
                            if fmint in current_mints:
                                continue  # we hold tokens — position is real
                            fage = now - fpos.get("entry_ts", now)
                            if fage < GRACE_PERIOD:
                                continue  # too young — indexing lag guard
                            ftok = fpos.get("token_name", fmint[:10])
                            fmode = fpos.get("mode", "?")
                            print(
                                f"[frost-mirror] 🔄 GHOST RECONCILE: {ftok} ({fmint[:8]}) "
                                f"mode={fmode} tracked as OPEN but zero balance in OUR wallet "
                                f"— removing ghost position"
                            )
                            try:
                                from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_fg
                                asyncio.create_task(_tg_fg(
                                    f"👻 <b>Frost ghost position cleared</b>\n"
                                    f"Token: {ftok}\nMode: {fmode}\n"
                                    f"Zero balance in our wallet — removed from tracking"
                                ))
                            except Exception:
                                pass
                            # Remove directly (no sell needed — we hold nothing)
                            _frost_positions.pop(fkey, None)

                        # Clean up stale grace-period entries for tokens no longer in wallet
                        for m in list(_first_seen.keys()):
                            if m not in current_mints:
                                _first_seen.pop(m, None)

                except asyncio.CancelledError:
                    return
                except Exception as _orp_err:
                    print(f"[orphan-scan] error: {_orp_err}")

        orphan_task = asyncio.get_event_loop().create_task(_our_wallet_orphan_scanner())
        print("[copy-trade] 🔍 Orphan scanner active — checking our wallet every 3 min")

        # ── Fast startup Frost ghost position purge ──────────────────────────────
        # Fires 30s after start. Clears any _frost_positions entries where our
        # wallet has zero balance — catches ghost positions from prior sessions
        # without waiting 3 min for the full orphan scanner cycle.
        async def _frost_startup_ghost_purge() -> None:
            await asyncio.sleep(30)
            if not _frost_positions:
                return
            our_pk = os.getenv("SOLANA_PUBLIC_KEY", "")
            if not our_pk:
                return
            hw_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            try:
                async with aiohttp.ClientSession() as _sg_sess:
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            our_pk,
                            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                            {"encoding": "jsonParsed"},
                        ],
                    }
                    async with _sg_sess.post(
                        hw_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.json()
                        accounts = data.get("result", {}).get("value", [])
                        our_mints = {
                            acc["account"]["data"]["parsed"]["info"]["mint"]
                            for acc in accounts
                            if acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("uiAmount", 0) > 0
                        }
                    removed = 0
                    for fkey, fpos in list(_frost_positions.items()):
                        fmint = fpos.get("mint", "")
                        if fmint and fmint not in our_mints:
                            ftok = fpos.get("token_name", fmint[:10])
                            fmode = fpos.get("mode", "?")
                            print(f"[frost-mirror] 👻 Startup purge: removing ghost {ftok} (mode={fmode}) — zero balance")
                            _frost_positions.pop(fkey, None)
                            removed += 1
                    if removed:
                        print(f"[frost-mirror] 👻 Startup ghost purge complete — removed {removed} ghost position(s)")
            except Exception as _sgp_err:
                print(f"[frost-mirror] startup ghost purge error: {_sgp_err}")

        asyncio.get_event_loop().create_task(_frost_startup_ghost_purge())
        print("[copy-trade] 👻 Frost ghost purge scheduled — runs in 30s")

        # ── Jarvis guardian loop — monitors open copy trade positions ────────────
        guardian_task = asyncio.get_event_loop().create_task(
            _jarvis_guardian_loop(runtime)
        )

        try:
            # Just keep the coroutine alive; all work happens in ws_tasks
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            print("[copy-trade] Monitor stopped")
        finally:
            for t in ws_tasks:
                t.cancel()
            price_task.cancel()
            guardian_task.cancel()
            safety_net_task.cancel()
            orphan_task.cancel()
            wallet_refresh_task.cancel()


async def _jarvis_guardian_loop(runtime: Any) -> None:
    """
    Jarvis actively monitors ALL copy trade positions every 3 minutes.

    - ROCKET: peaked high and still climbing → Jarvis decides whether to exit now
    - MC collapse: -12% in 5 mins
    - Peak drawdown: gave back 30%+ from a 15%+ peak
    - Bleed: sitting at -20% from entry with no recovery
    """
    GUARDIAN_INTERVAL     = 180    # check every 3 minutes
    MIN_HOLD_SECS         = 300    # don't act in first 5 min — give it time to develop
    MOMENTUM_WINDOW_SECS  = 300    # look at last 5 mins of price action
    MOMENTUM_ALERT_PCT    = -12.0  # -12% in 5 mins = MC getting hammered
    PEAK_DRAWDOWN_ALERT   = -30.0  # gave back 30% from peak = alert
    BLEED_ALERT_PCT       = -20.0  # sitting at -20% from entry with no recovery
    ROCKET_THRESHOLD_PCT  = 60.0   # position peaked at +60% = rocket territory
    ROCKET_MOMENTUM_MIN   = 5.0    # must be actively rising (>5% in 5m) to call rocket
    _alerted: dict[str, float] = {}  # mint → last alert timestamp

    print("[guardian] Jarvis guardian started — monitoring ALL positions every 3m (rockets + negative)")

    while True:
        await asyncio.sleep(GUARDIAN_INTERVAL)
        try:
            if not _paper_positions:
                continue

            now = time.time()
            for mint, pos in list(_paper_positions.items()):
                age = now - pos.get("entry_ts", now)
                if age < MIN_HOLD_SECS:
                    continue

                current  = pos.get("current_price", 0.0)
                entry    = pos.get("entry_price", 0.0)
                peak     = pos.get("peak_price", current)
                pnl_pct  = pos.get("pnl_pct", 0.0)
                peak_pnl = pos.get("peak_pnl_pct", 0.0)

                if not current or not entry:
                    continue

                # ── 5-minute momentum from price checkpoints ──────────────────
                checkpoints = pos.get("price_checkpoints", [])
                recent = [c for c in checkpoints if now - c["ts"] <= MOMENTUM_WINDOW_SECS]
                momentum_5m = 0.0
                if len(recent) >= 2:
                    p0 = recent[0]["price"]
                    p1 = recent[-1]["price"]
                    momentum_5m = (p1 - p0) / p0 * 100.0 if p0 > 0 else 0.0

                # ── Peak drawdown ─────────────────────────────────────────────
                peak_drawdown = (current - peak) / peak * 100.0 if peak > 0 else 0.0

                # ── Decide if Jarvis should look at this ──────────────────────
                reason = None

                # Rocket: peaked high and still climbing
                if peak_pnl >= ROCKET_THRESHOLD_PCT and momentum_5m >= ROCKET_MOMENTUM_MIN:
                    reason = f"ROCKET: peaked at {peak_pnl:+.1f}%, still rising {momentum_5m:+.1f}%/5m"
                elif momentum_5m < MOMENTUM_ALERT_PCT:
                    reason = f"MC collapse: {momentum_5m:+.1f}% in last 5 mins"
                elif peak_drawdown < PEAK_DRAWDOWN_ALERT and peak_pnl > 15.0:
                    reason = f"Peak drawdown: {peak_drawdown:+.1f}% from peak (was {peak_pnl:+.1f}%)"
                elif pnl_pct < BLEED_ALERT_PCT:
                    reason = f"Position bleeding: {pnl_pct:+.1f}% from entry"

                if not reason:
                    continue

                # Throttle: only alert Jarvis once every 5 minutes per mint on rockets,
                # 10 minutes on negative scenarios
                throttle_secs = 300 if "ROCKET" in reason else 600
                last_alert = _alerted.get(mint, 0.0)
                if now - last_alert < throttle_secs:
                    continue
                _alerted[mint] = now

                token   = pos.get("token_name", mint[:10])
                wallet  = pos.get("wallet_name", "?")
                hold_m  = int(age / 60)
                mc_usd  = pos.get("mc_usd")
                mc_str  = f"${mc_usd:,.0f}" if mc_usd else "unknown"
                if "ROCKET" in reason:
                    alert_msg = (
                        f"🚀 ROCKET ALERT — {token} ({wallet}) is running hard!\n"
                        f"Reason: {reason}\n"
                        f"P&L now: {pnl_pct:+.1f}% | Peak: {peak_pnl:+.1f}% | "
                        f"Hold: {hold_m}m | MC: {mc_str}\n\n"
                        f"TP is set at +40% (hard exit). The token is still climbing past that. "
                        f"Should I close NOW for a guaranteed {pnl_pct:+.1f}%? "
                        f"Or is momentum strong enough to let the TP fire naturally? "
                        f"Use close_position('{mint}') to exit immediately."
                    )
                else:
                    # Negative scenario — standard alert
                    alert_msg = (
                        f"⚠️ GUARDIAN ALERT — {token} ({wallet}) needs your attention.\n"
                        f"Reason: {reason}\n"
                        f"P&L now: {pnl_pct:+.1f}% | Peak was: {peak_pnl:+.1f}% | "
                        f"Peak drawdown: {peak_drawdown:+.1f}%\n"
                        f"MC: {mc_str} | Hold: {hold_m}m | Mint: {mint[:16]}\n\n"
                        f"Should I exit this position? Assess the situation and use "
                        f"close_position('{mint}') if you judge we should cut it. "
                        f"If you think it can recover, say so and explain why."
                    )

                print(f"[guardian] 🔍 Alerting Jarvis about {token}: {reason}")
                try:
                    from elizaos.plugins.solana import jarvis_command_centre as _jcc
                    response = await _jcc.route(alert_msg, runtime)
                    print(f"[guardian] Jarvis decision on {token}: {response[:300]}")
                    # Push Jarvis's decision to Telegram
                    try:
                        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                        emoji = "🚀" if "ROCKET" in reason else "🤖"
                        asyncio.create_task(_tg(
                            f"{emoji} <b>Jarvis guardian — {token}</b>\n"
                            f"<i>{reason}</i>\n\n"
                            f"{response[:400]}"
                        ))
                    except Exception:
                        pass
                except Exception as _ge:
                    print(f"[guardian] Jarvis evaluation error: {_ge}")

        except asyncio.CancelledError:
            return
        except Exception as _loop_exc:
            print(f"[guardian] loop error: {_loop_exc}")


# ── Jarvis command handler ────────────────────────────────────────────────────

async def handle_jarvis_command(cmd: str) -> str:
    """Handle Jarvis chat commands for copy-trade system."""
    cmd = cmd.strip().lower()

    if cmd in ("copy trade status", "copy trade", "copytrade"):
        stats = get_paper_stats()
        if stats["trades"] == 0 and stats["open_count"] == 0:
            return (
                "**Copy-Trade Paper Test** — no closed trades yet.\n"
                f"  Virtual balance: {stats['balance']:.3f} SOL  •  "
                f"Watching: {len(WATCHED_WALLETS)} wallets"
            )
        lines = [
            f"**Copy-Trade Paper Test** (48h from 2026-04-05):",
            f"  Balance: {stats['balance']:.3f} SOL  •  "
            f"Net P&L: {stats['net_pnl']:+.4f} SOL",
            f"  Closed: {stats['trades']} trades ({stats['wins']}W / {stats['losses']}L) "
            f"= {stats['win_rate']:.0f}% WR",
            f"  Avg win: +{stats['avg_win']:.1f}%  Avg loss: {stats['avg_loss']:.1f}%  "
            f"Best: +{stats['best_trade']:.1f}%",
        ]
        if stats["open_count"] > 0:
            lines.append(f"  Open: {stats['open_count']} position(s):")
            for p in stats["open_positions"]:
                age_mins = (time.time() - p["entry_ts"]) / 60.0
                lines.append(
                    f"    {p['token_name']} ({p['mint'][:8]}...)  "
                    f"following {p['wallet']}  {age_mins:.0f}min"
                )
        return "\n".join(lines)

    if cmd.startswith("copy trade wallets") or cmd == "watched wallets":
        lines = ["**Watched wallets (Axiom leaderboard):**"]
        for name, addr in WATCHED_WALLETS.items():
            lines.append(f"  {name}: `{addr[:8]}...{addr[-4:]}`")
        return "\n".join(lines)

    return ""
