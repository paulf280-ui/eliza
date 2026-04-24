"""
jarvis_command_centre.py — Jarvis Command Centre

Intercepts every chat message before it reaches the generic LLM handler.
Routes it to the right brain(s) and executes real actions.

COMMAND TYPES:
  1. Direct controls  — "enable strategy b", "set stop loss to 10%", "pause", "resume"
  2. AI dispatch      — "ask grok about X", "check token X", "run analysis now"
  3. Status queries   — "status", "positions", "history", "lessons", "config"
  4. Free-form chat   — anything else → Claude Sonnet with full trading context

All commands return a plain-English reply string.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

# ── Wallet balance cache (60s TTL) — avoids slow RPC call on every chat message ──
_wallet_cache: dict[str, float] = {"sol": 0.0, "ts": 0.0}

async def _get_cached_wallet(wallet_svc) -> float:
    """Return wallet SOL balance, re-fetching at most once per 60s."""
    if time.time() - _wallet_cache["ts"] < 60.0:
        return _wallet_cache["sol"]
    try:
        bal = float(await asyncio.wait_for(wallet_svc.get_sol_balance(), timeout=5.0))
        _wallet_cache["sol"] = bal
        _wallet_cache["ts"] = time.time()
        return bal
    except Exception:
        return _wallet_cache["sol"]  # return stale value on failure

if TYPE_CHECKING:
    from elizaos.runtime import AgentRuntime

# ─────────────────────────────────────────────────────────────────────────────
# Strategy name aliases
# ─────────────────────────────────────────────────────────────────────────────
_STRATEGY_KEYS = {
    "a":        "strategy_a_enabled",
    "a2":       "strategy_a2_enabled",
    "ghost":    "strategy_a2_enabled",
    "ghostrider": "strategy_a2_enabled",
    "b":        "strategy_b_enabled",
    "grad":     "strategy_b_enabled",
    "graduation": "strategy_b_enabled",
    "gradsnipe": "strategy_b_enabled",
    "c":        "strategy_c_enabled",
    "raydium":  "strategy_c_enabled",
    "d":        "strategy_d_enabled",
    "meteora":  "strategy_d_enabled",
    "grok":     "grok_premium_enabled",
    "grokpremium": "grok_premium_enabled",
    "smartwallet": "smart_wallet_enabled",
    "smart":    "smart_wallet_enabled",
    "e":            "strategy_e_enabled",
    "social":       "strategy_e_enabled",
    "momentum":     "strategy_e_enabled",
    "socialsnipe":  "strategy_e_enabled",
    "all":      "__all__",
}

# Human-readable names
_STRATEGY_NAMES = {
    "strategy_a_enabled":       "Strategy A (pump.fun legacy)",
    "strategy_a2_enabled":      "Strategy A2 Ghost Rider (BC sniper)",
    "strategy_b_enabled":       "Strategy B (graduation snipe)",
    "strategy_c_enabled":       "Strategy C (Raydium scout)",
    "strategy_d_enabled":       "Strategy D (Meteora scout)",
    "grok_premium_enabled":     "Grok Premium mode",
    "smart_wallet_enabled":     "Smart Wallet copy-trading",
    "strategy_e_enabled":       "Strategy E (Social Momentum Snipe)",
}

# Parameter name aliases for "set X to Y"
_PARAM_ALIASES = {
    "stoploss":         "stop_loss_pct",
    "stop_loss":        "stop_loss_pct",
    "sl":               "stop_loss_pct",
    "earlystop":        "early_stop_loss_pct",
    "early_stop":       "early_stop_loss_pct",
    "esl":              "early_stop_loss_pct",
    "tp":               "tp1_mult",
    "takeprofit":       "tp1_mult",
    "take_profit":      "tp1_mult",
    "tp1":              "tp1_mult",
    # Copy-trade size aliases — these are the PRIMARY "trade size" for the bot
    "size":             "copy_trade_paper_buy_sol",
    "tradesize":        "copy_trade_paper_buy_sol",
    "trade_size":       "copy_trade_paper_buy_sol",
    "positionsize":     "copy_trade_paper_buy_sol",
    "position_size":    "copy_trade_paper_buy_sol",
    "copysize":         "copy_trade_paper_buy_sol",
    "copy_size":        "copy_trade_paper_buy_sol",
    "copybuy":          "copy_trade_paper_buy_sol",
    "copy_buy":         "copy_trade_paper_buy_sol",
    "copytradesize":    "copy_trade_paper_buy_sol",
    "copy_trade_size":  "copy_trade_paper_buy_sol",
    # Legacy strategy sizes (blocked by _TOOL_BLOCKED_KEYS — kept for direct key access)
    "buysize":          "buy_sol",
    "buy_sol":          "buy_sol",
    "buysol":           "buy_sol",
    "a2size":           "a2_buy_sol",
    "a2_size":          "a2_buy_sol",
    "a2buysol":         "a2_buy_sol",
    "bsize":            "strategy_b_buy_sol",
    "b_size":           "strategy_b_buy_sol",
    "maxpositions":     "max_concurrent_positions",
    "max_positions":    "max_concurrent_positions",
    "concurrent":       "max_concurrent_positions",
    "dailyloss":        "max_daily_loss_pct",
    "daily_loss":       "max_daily_loss_pct",
    "minliq":           "strategy_b_min_liq_usd",
    "min_liq":          "strategy_b_min_liq_usd",
    "bliq":             "strategy_b_min_liq_usd",
    "b_liq":            "strategy_b_min_liq_usd",
    "gradliq":          "strategy_b_min_liq_usd",
    "dipwait":          "strategy_b_dip_wait_secs",
    "dip_wait":         "strategy_b_dip_wait_secs",
    "bdipwait":         "strategy_b_dip_wait_secs",
    "b_dip_wait":       "strategy_b_dip_wait_secs",
    "groktp":           "grok_tp_mult",
    "grok_tp":          "grok_tp_mult",
    "pumpswapsl":       "pumpswap_stop_loss_pct",
    "pumpswap_sl":      "pumpswap_stop_loss_pct",
    "esize":            "strategy_e_buy_sol",
    "e_size":           "strategy_e_buy_sol",
    "research":         "strategy_e_research_mode",
    "researchmode":     "strategy_e_research_mode",
    "minsocials":       "a2_require_twitter",   # alias — use set commands for individual flags
    "requiretwitter":   "a2_require_twitter",
    "requiretelegram":  "a2_require_telegram",
    "requirewebsite":   "a2_require_website",
    "minholders":       "a2_min_holders",
    "min_holders":      "a2_min_holders",
    "mingrowthrate":    "a2_min_holder_growth_rate",
    "holdergrowth":     "a2_min_holder_growth_rate",
    "growthrate":       "a2_min_holder_growth_rate",
    "growthwindow":     "a2_holder_growth_window_secs",
    "minmc":            "a2_min_mc_usd",
    "min_mc":           "a2_min_mc_usd",
    "mcfilter":         "a2_min_mc_usd",
    "mc_filter":        "a2_min_mc_usd",
    # New liquidity / vol-liq / buy-ratio filters
    "a2minliq":         "a2_min_liq_usd",
    "a2_min_liq":       "a2_min_liq_usd",
    "minliqusd":        "a2_min_liq_usd",
    "b_vol_liq_max":    "b_max_vol_liq_ratio",
    "b_vol_liq_min":    "b_min_vol_liq_ratio",
    "bmaxvolliq":       "b_max_vol_liq_ratio",
    "bminvolliq":       "b_min_vol_liq_ratio",
    "c_vol_liq_max":    "c_max_vol_liq_ratio",
    "c_vol_liq_min":    "c_min_vol_liq_ratio",
    "cmaxvolliq":       "c_max_vol_liq_ratio",
    "cminvolliq":       "c_min_vol_liq_ratio",
    "bbuyratio":        "b_min_buy_ratio",
    "b_buy_ratio":      "b_min_buy_ratio",
    "cbuyratio":        "c_min_buy_ratio",
    "c_buy_ratio":      "c_min_buy_ratio",
    "minbuyratio":      "b_min_buy_ratio",
    # Momentum scorer
    "bmomentum":        "b_min_momentum_score",
    "cmomentum":        "c_min_momentum_score",
    "dmomentum":        "d_min_momentum_score",
    "b_momentum":       "b_min_momentum_score",
    "c_momentum":       "c_min_momentum_score",
    "d_momentum":       "d_min_momentum_score",
    "momentumscore":    "b_min_momentum_score",
    # Graduation confirmation timer
    "confirmwait":          "graduation_confirmation_wait",
    "confirm_wait":         "graduation_confirmation_wait",
    "gradconfirm":          "graduation_confirmation_wait",
    "confirmation_wait":    "graduation_confirmation_wait",
    # Strategy C/D DNA
    "cminliq":          "c_min_liq_usd",
    "c_min_liq":        "c_min_liq_usd",
    "cminmc":           "c_min_mc_usd",
    "c_min_mc":         "c_min_mc_usd",
    "dminliq":          "d_min_liq_usd",
    "d_min_liq":        "d_min_liq_usd",
    "dminmc":           "d_min_mc_usd",
    "d_min_mc":         "d_min_mc_usd",
    # Monster scanner
    "monsterliq":       "monster_scanner_min_liq_usd",
    "monstermc":        "monster_scanner_min_mc_usd",
    "monsterinterval":  "monster_scanner_interval_secs",
    "monsterminvl":     "monster_scanner_min_vol_liq",
    "monstermaxvl":     "monster_scanner_max_vol_liq",
    "monsterbuyratio":  "monster_scanner_min_buy_ratio",
    "monsterminliq":    "monster_scanner_min_liq_usd",
    "monsterminmc":     "monster_scanner_min_mc_usd",
    # Trading window
    "windowstart":      "trading_window_start_utc",
    "windowend":        "trading_window_end_utc",
    "tradingstart":     "trading_window_start_utc",
    "tradingend":       "trading_window_end_utc",
    # Strategy D (Meteora)
    "d_vol_liq_max":    "d_max_vol_liq_ratio",
    "d_vol_liq_min":    "d_min_vol_liq_ratio",
    "dmaxvolliq":       "d_max_vol_liq_ratio",
    "dminvolliq":       "d_min_vol_liq_ratio",
    "dbuyratio":        "d_min_buy_ratio",
    "d_buy_ratio":      "d_min_buy_ratio",
    # Trailing stop
    "trailingstop":         "trailing_stop_enabled",
    "trailing_stop":        "trailing_stop_enabled",
    "enable_trailing":      "trailing_stop_enabled",
    "trailpct":             "trailing_stop_pct",
    "trail_pct":            "trailing_stop_pct",
    "trailpercent":         "trailing_stop_pct",
    "trailingstoppct":      "trailing_stop_pct",
    # Scout quality caps (C/D anti-rug filters)
    "scoutmaxm5":           "scout_max_m5_pct",
    "scout_max_m5":         "scout_max_m5_pct",
    "maxm5":                "scout_max_m5_pct",
    "scoutmaxh1":           "scout_max_h1_pct",
    "scout_max_h1":         "scout_max_h1_pct",
    "maxh1":                "scout_max_h1_pct",
    "scoutliqmc":           "scout_min_liq_mc_ratio",
    "scout_liq_mc":         "scout_min_liq_mc_ratio",
    "minliqmc":             "scout_min_liq_mc_ratio",
    "scoutminage":          "scout_min_age_secs",
    "scout_min_age":        "scout_min_age_secs",
    "scoutage":             "scout_min_age_secs",
    "pumpswapminage":       "pumpswap_min_age_secs",
    "pumpswap_min_age":     "pumpswap_min_age_secs",
    "pswapage":             "pumpswap_min_age_secs",
    "keywordscore":         "narrative_keyword_score",
    "keyword_score":        "narrative_keyword_score",
    "narrativescore":       "narrative_keyword_score",
    "momentumwait":         "momentum_confirm_secs",
    "momentum_wait":        "momentum_confirm_secs",
    "confirmwait":          "momentum_confirm_secs",
    "confirm_secs":         "momentum_confirm_secs",
    # Split-buy (Harvester + Monster Hunter)
    "splitbuy":             "split_buy_enabled",
    "split_buy":            "split_buy_enabled",
    "harvester":            "split_buy_enabled",
    "splitbuya":            "split_buy_a_sol",
    "split_buy_a":          "split_buy_a_sol",
    "harvestersize":        "split_buy_a_sol",
    "asize":                "split_buy_a_sol",
    "splitbuyb":            "split_buy_b_sol",
    "split_buy_b":          "split_buy_b_sol",
    "monstersize":          "split_buy_b_sol",
    "mhsize":               "split_buy_b_sol",
    "harvestertpt":         "split_buy_a_tp_mult",
    "split_buy_a_tp":       "split_buy_a_tp_mult",
    "atp":                  "split_buy_a_tp_mult",
    "monsterhuntertp":      "split_buy_b_tp_mult",
    "split_buy_b_tp":       "split_buy_b_tp_mult",
    "btp":                  "split_buy_b_tp_mult",
    "splitbuysl":           "split_buy_sl_mult",
    "split_buy_sl":         "split_buy_sl_mult",
    "splittrail":           "split_buy_trailing_pct",
    "split_trail":          "split_buy_trailing_pct",
}


# Log file path — packages/dashboard/traderbot.out
_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "dashboard", "traderbot.out")

# Trade intelligence file — full per-trade history with entry/exit/P&L for formula building
_INTEL_PATH = os.path.join(os.path.dirname(__file__), "winning_trade_intelligence.json")

# Winning formula output file — saved after each 3-brain synthesis
_FORMULA_PATH      = os.path.join(os.path.dirname(__file__), "winning_formula.json")
_PLAYBOOK_PATH     = os.path.join(os.path.dirname(__file__), "meme_token_playbook.json")

# Monster token intelligence files
_MONSTER_ADDR_PATH    = os.path.join(os.path.dirname(__file__), "monster_addresses.json")
_MONSTER_REF_PATH     = os.path.join(os.path.dirname(__file__), "monster_trades_reference.json")
_MONSTER_LIB_PATH     = os.path.join(os.path.dirname(__file__), "monster_token_library.json")


def _load_copy_trade_context() -> str:
    """Full copy-trade intelligence context for Jarvis — this IS the primary strategy now."""
    try:
        import time as _t
        from elizaos.plugins.solana.axiom_copy_trader import (
            get_paper_stats, WATCHED_WALLETS, _signal_log, _paper_trades,
            _wallet_loss_window,
        )
        from elizaos.plugins.solana import live_config as _lc
        stats = get_paper_stats()
        live   = bool(_lc.get("copy_trade_enabled", False))
        sl     = float(_lc.get("copy_trade_sl_pct", 10.0))
        paused = bool(_lc.get("copy_trade_paused", False))
        wlc_sol  = float(_lc.get("copy_trade_wallet_loss_cap_sol", 0.3))
        wlc_mins = int(float(_lc.get("copy_trade_wallet_loss_window_secs", 7200)) // 60)
        mode   = ("⏸ LIVE PAUSED — no new entries" if (live and paused)
                  else "🔴 LIVE on-chain buys" if live
                  else "📋 PAPER TEST")
        floor_sol    = float(_lc.get("copy_trade_paper_buy_sol", 0.4))
        compound_pct = float(_lc.get("copy_trade_compound_pct", 0.20))
        compound_max = float(_lc.get("copy_trade_compound_max_sol", 2.0))
        tp_pct       = float(_lc.get("copy_trade_tp_pct", 15.0))

        # Compute current compound tier from cached wallet balance
        try:
            from elizaos.plugins.solana.axiom_copy_trader import _cached_wallet_sol as _cwb
        except Exception:
            _cwb = 0.0
        if _cwb >= 0.5:
            _raw_compound = round(_cwb * compound_pct, 3)
            _current_size = min(max(floor_sol, _raw_compound), compound_max)
        else:
            _current_size = floor_sol
        _next_tier_wallet = round(floor_sol / compound_pct, 2)  # wallet where compounding first beats floor
        _progress = f"{_cwb:.3f}" if _cwb > 0 else "fetching..."

        start_bal = float(_lc.get("copy_trade_paper_balance", 2.8))
        lines = [
            "╔══════════════════════════════════════════════════════════════════╗",
            "║   COPY TRADE — PRIMARY STRATEGY (4 wallets, live funds)         ║",
            "╚══════════════════════════════════════════════════════════════════╝",
            "",
            f"Mode: {mode}",
            "",
            "EXIT SYSTEM — HARD TP/SL (quant-locked 2026-04-14, 91-trade simulation):",
            f"  TP: +{tp_pct:.0f}% → FULL EXIT  |  SL: -{sl:.0f}% → full exit",
            "  Whale SELL signal → immediate close (overrides TP/SL)",
            f"  Wallet loss cap: {wlc_sol:.2f} SOL lost in {wlc_mins}min → pause that wallet",
            "",
            "POSITION SIZING — 20% OF WALLET COMPOUNDING RULE (hardwired):",
            f"  Formula: min(max({floor_sol:.2f} SOL floor, wallet × {compound_pct*100:.0f}%), {compound_max:.1f} SOL cap)",
            f"  Current wallet:   {_progress} SOL",
            f"  Current trade size: {_current_size:.3f} SOL  ({'floor' if _cwb < _next_tier_wallet else 'compounding'})",
            f"  Compounding kicks in at: {_next_tier_wallet:.2f} SOL wallet",
            "  Growth tiers: ≤2.0→0.40 | 2.5→0.50 | 3.0→0.60 | 5.0→1.00 | 10.0→2.00 SOL",
            "  ⛔ ALL sizing/exit params HARDWIRED — no brain may change them",
            "",
            "ENTRY FILTERS — NEW (deployed 2026-04-11, NO DATA YET — DO NOT ADJUST THRESHOLDS):",
            "  ┌─ Liquidity cap: position size capped at 2% of pool liquidity in SOL",
            "  │    Pool too thin for even 50% of trade size → SKIP entry entirely",
            "  │    Thresholds: 2% cap, 50% minimum viability floor",
            "  ├─ Post-grad 30-min test: if pair < 30min old AND is PumpSwap graduated token:",
            "  │    REQUIRES: liquidity ≥ $50,000 AND holders ≥ 200",
            "  │    Rationale: brand-new graduates are shallow and rug-prone in the first 30min",
            "  └─ Holder count at entry: stored for velocity tracking in health signals",
            "  ⛔ ALL THREE THRESHOLDS ARE HARDWIRED AND RESEARCH-PENDING:",
            "     2% liq cap | $50k grad liq | 200 grad holders | 15% holder decline",
            "     We have ZERO trades through these filters yet. Any suggestion to change",
            "     thresholds must be backed by at least 20+ filtered events with outcomes.",
            "     Jarvis MUST NOT call update_config for any of these. Trader confirms ALL changes.",
            "",
            f"WATCHED WALLETS ({len(WATCHED_WALLETS)} — LOCKED, no additions without user approval):",
        ]
        for name, addr in WATCHED_WALLETS.items():
            lines.append(f"  {name:16s} {addr[:8]}...")

        # ── Paused wallets (loss cap) ─────────────────────────────────────────
        _now_ctx = _t.time()
        _wlc_secs = float(_lc.get("copy_trade_wallet_loss_window_secs", 7200))
        _paused_wallets = []
        for _wn, _wl in _wallet_loss_window.items():
            _recent = sum(l for ts, l in _wl if ts >= _now_ctx - _wlc_secs)
            if _recent >= wlc_sol:
                _paused_wallets.append(f"{_wn} (-{_recent:.3f} SOL in {wlc_mins}min)")
        if _paused_wallets:
            lines.append(f"⏸ WALLETS CURRENTLY PAUSED BY LOSS CAP: {', '.join(_paused_wallets)}")

        # ── Balance & overall P&L ─────────────────────────────────────────────
        net = stats["net_pnl"]
        wr  = stats["win_rate"]
        pnl_on_start = (net / start_bal * 100) if start_bal > 0 else 0

        # ── Today / This Week time-windowed breakdown ─────────────────────────
        import datetime as _dtx
        _now_tw     = _t.time()
        _today_ts   = _dtx.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        _week_ts    = _today_ts - 6 * 86400
        _today_tr   = [t for t in _paper_trades if float(t.get("ts", 0)) >= _today_ts]
        _week_tr    = [t for t in _paper_trades if float(t.get("ts", 0)) >= _week_ts]

        def _tw_stats(tr: list) -> tuple:
            if not tr:
                return 0, 0.0, 0.0
            _w = [t for t in tr if t.get("pnl_sol", 0) > 0]
            return len(tr), sum(t.get("pnl_sol", 0) for t in tr), len(_w) / len(tr) * 100

        _td_n, _td_sol, _td_wr = _tw_stats(_today_tr)
        _wk_n, _wk_sol, _wk_wr = _tw_stats(_week_tr)

        lines += [
            "",
            f"LIVE BALANCE:  {stats['balance']:.4f} SOL  (session start: {start_bal:.2f} SOL)",
            f"NET P&L:       {net:+.4f} SOL  ({pnl_on_start:+.1f}% on session capital)",
            f"CLOSED TRADES: {stats['trades']}  |  WR: {wr:.1f}%  ({stats['wins']}W / {stats['losses']}L)",
            f"AVG WIN: {stats.get('avg_win', 0):+.1f}%  |  AVG LOSS: {stats.get('avg_loss', 0):+.1f}%",
            f"TODAY (UTC):   {_td_n} trades | Net {_td_sol:+.4f} SOL | WR {_td_wr:.0f}%"
            + (f"  ({len([t for t in _today_tr if t.get('pnl_sol',0)>0])}W/{len([t for t in _today_tr if t.get('pnl_sol',0)<=0])}L)" if _td_n else ""),
            f"THIS WEEK:     {_wk_n} trades | Net {_wk_sol:+.4f} SOL | WR {_wk_wr:.0f}%",
            "",
        ]

        # ── Open positions with moonbag stage ────────────────────────────────
        open_pos = stats.get("open_positions", [])
        if open_pos:
            max_slots = stats.get("max_positions", 2)
            lines.append(f"OPEN POSITIONS ({len(open_pos)}/{max_slots} slots):")
            for p in open_pos:
                age_m  = (_t.time() - p["entry_ts"]) / 60
                pnl_s  = f"{p['pnl_pct']:+.1f}%" if p.get("pnl_pct") is not None else "fetching..."
                age_s  = f"{age_m:.0f}min" if age_m < 60 else f"{age_m/60:.1f}h"
                ep     = f"{p['entry_price']:.2e}" if p.get("entry_price") else "?"
                locked = p.get("locked_sol", 0.0) or 0.0
                rem    = int((p.get("remaining_fraction", 1.0) or 1.0) * 100)
                tp1    = p.get("tp1_hit", False)
                tp2    = p.get("tp2_hit", False)
                narr   = p.get("narrative", "unknown")
                ceil   = p.get("moonbag_ceiling_pct", 400.0) or 400.0

                if tp2:
                    stage = f"💎 MOONBAG 20% (TP1+TP2 fired, {locked:.4f} SOL locked)"
                elif tp1:
                    stage = f"🎯 TP1 fired — {rem}% running free ({locked:.4f} SOL locked)"
                else:
                    stage = f"📍 full position (SL at -{sl:.0f}%, TP1 trigger at +40%)"

                lines.append(
                    f"  {'🟢' if (p.get('pnl_pct') or 0) >= 0 else '🔴'} "
                    f"{p['token_name'][:20]:20s}  {p['wallet']:14s}  "
                    f"entry={ep}  age={age_s}  P&L={pnl_s}"
                )
                lines.append(
                    f"    → {stage}  |  narrative={narr} (ceiling={ceil:.0f}%)"
                )
        else:
            max_slots2 = stats.get("max_positions", 2)
            lines.append(f"OPEN POSITIONS: 0/{max_slots2} slots — watching for whale entries")
        lines.append("")

        # ── Per-wallet performance breakdown ──────────────────────────────────
        wallet_stats: dict[str, dict] = {}
        for t in _paper_trades:
            w = t.get("wallet", "unknown")
            if w not in wallet_stats:
                wallet_stats[w] = {"trades": 0, "wins": 0, "net_sol": 0.0, "rugs": 0,
                                   "pnl_pcts": [], "tp1_count": 0, "tp2_count": 0}
            wallet_stats[w]["trades"] += 1
            pnl_sol = t.get("pnl_sol", 0) or 0
            pnl_pct = t.get("pnl_pct", 0) or 0
            wallet_stats[w]["net_sol"]    += pnl_sol
            wallet_stats[w]["pnl_pcts"].append(pnl_pct)
            if pnl_sol > 0:
                wallet_stats[w]["wins"] += 1
            if t.get("tp1_hit"):
                wallet_stats[w]["tp1_count"] += 1
            if t.get("tp2_hit"):
                wallet_stats[w]["tp2_count"] += 1
            if "rug" in (t.get("reason") or "").lower() or pnl_pct < -40:
                wallet_stats[w]["rugs"] += 1

        if wallet_stats:
            active = set(WATCHED_WALLETS.keys())
            lines.append("WALLET PERFORMANCE (🟢 = currently active, ⚫ = cut):")
            sorted_wallets = sorted(wallet_stats.items(), key=lambda x: x[1]["net_sol"], reverse=True)
            for w, ws in sorted_wallets:
                wr_w  = ws["wins"] / ws["trades"] * 100 if ws["trades"] else 0
                avg_p = sum(ws["pnl_pcts"]) / len(ws["pnl_pcts"]) if ws["pnl_pcts"] else 0
                rug_s = f" ⚠️{ws['rugs']}rugs" if ws["rugs"] else ""
                tp_s  = f" TP1={ws['tp1_count']} TP2={ws['tp2_count']}" if ws["tp1_count"] else ""
                status = "🟢" if w in active else "⚫ CUT"
                pnl_col = "🟢" if ws["net_sol"] > 0 else "🔴"
                lines.append(
                    f"  {status:8s} {pnl_col} {w:16s}  {ws['trades']:3d}T  WR={wr_w:.0f}%  "
                    f"net={ws['net_sol']:+.3f} SOL  avg={avg_p:+.1f}%{rug_s}{tp_s}"
                )
            lines.append("")

        # ── Last 10 closed trades ─────────────────────────────────────────────
        recent = _paper_trades[-10:] if _paper_trades else []
        if recent:
            lines.append("LAST 10 CLOSED TRADES:")
            for t in reversed(recent):
                pnl_p  = t.get("pnl_pct", 0) or 0
                peak   = t.get("peak_pnl_pct", 0) or 0
                locked = t.get("locked_sol", 0.0) or 0.0
                peak_s = f" peak={peak:+.0f}%" if peak > abs(pnl_p) * 1.5 else ""
                lock_s = f" locked={locked:.4f}SOL" if locked > 0 else ""
                tp_s   = " [TP1+TP2+bag]" if t.get("tp2_hit") else " [TP1+bag]" if t.get("tp1_hit") else ""
                reason = (t.get("reason") or "?").replace("_", " ")
                lines.append(
                    f"  {'✅' if pnl_p > 0 else '❌'} {t['token_name'][:16]:16s}  "
                    f"{t.get('wallet','?'):14s}  {pnl_p:+.1f}%{peak_s}{lock_s}{tp_s}  "
                    f"hold={t.get('hold_mins',0):.0f}min  ({reason})"
                )
            lines.append("")

        # ── Recent signals ────────────────────────────────────────────────────
        hour_ago  = _t.time() - 3600
        day_ago   = _t.time() - 86400
        sigs_1h   = [s for s in _signal_log if s.get("ts", 0) > hour_ago]
        sigs_24h  = [s for s in _signal_log if s.get("ts", 0) > day_ago]
        entered   = [s for s in sigs_24h if s.get("action") == "entered"]
        skipped   = [s for s in sigs_24h if s.get("action") == "skipped"]
        prev_traded = [s for s in skipped if s.get("skip_reason") == "previously_traded"]
        skip_reasons: dict[str, int] = {}
        for s in skipped:
            r = s.get("skip_reason", "unknown")
            skip_reasons[r] = skip_reasons.get(r, 0) + 1
        lines += [
            f"SIGNALS: last 1h={len(sigs_1h)} | last 24h={len(sigs_24h)} | "
            f"entered={len(entered)} | skipped={len(skipped)}",
            f"  Skip reasons: {skip_reasons}" if skip_reasons else "",
            f"  Golden rule blocked: {len(prev_traded)} re-entry attempts",
        ]

        # ── Jarvis alert triggers ─────────────────────────────────────────────
        alerts = []
        if stats["balance"] < 1.5:
            alerts.append(f"🚨 CRITICAL BALANCE: {stats['balance']:.3f} SOL — PAUSE TRADING IMMEDIATELY")
        elif stats["balance"] < 2.0:
            alerts.append(f"⚠️  BALANCE LOW: {stats['balance']:.3f} SOL — tighten SL, reduce size")
        for w, ws in wallet_stats.items():
            w_trades = [t for t in _paper_trades if t.get("wallet") == w][-5:]
            if len(w_trades) >= 3 and all((t.get("pnl_sol", 0) or 0) < 0 for t in w_trades[-3:]):
                alerts.append(f"⚠️  {w} has 3+ consecutive losses — consider pausing this wallet signal")
        for p in open_pos:
            age_h = (_t.time() - p["entry_ts"]) / 3600
            pnl   = p.get("pnl_pct") or 0
            tp1   = p.get("tp1_hit", False)
            if age_h > 2 and pnl < -5 and not tp1:
                alerts.append(
                    f"⚠️  {p['token_name']} open {age_h:.1f}h at {pnl:+.1f}% — TP1 not hit, consider manual review"
                )
            if tp1 and pnl > 0:
                locked = p.get("locked_sol", 0.0) or 0.0
                if locked > 0:
                    alerts.append(
                        f"ℹ️  {p['token_name']} moonbag running at {pnl:+.1f}% — {locked:.4f} SOL already locked, pure upside from here"
                    )
        if stats["balance"] >= 50 and trade_size < 2.0:
            alerts.append("📈 Balance ≥50 SOL — compound size should now be ≥2.0 SOL per trade")

        if alerts:
            lines += ["", "🚨 JARVIS ALERTS (act on these):"] + [f"  {a}" for a in alerts]
        else:
            lines.append("✅ No active alerts — all looks healthy")

        lines += [
            "",
            "═══════════════════════════════════════════════════",
            "JARVIS ROLE — EXIT SYSTEM AWARENESS:",
            "═══════════════════════════════════════════════════",
            "",
            "  The moonbag system is FULLY AUTOMATED. Jarvis does NOT need to",
            "  manually trigger TP1/TP2 — the price monitor does it every 2s.",
            "",
            "  YOUR JOB on open positions:",
            "  • BEFORE TP1: watch for rug signals (MC collapse, mass sell) — close_position() if needed",
            "  • AFTER TP1: cost is covered, bias to HOLD. Only close early if:",
            "      – Buy/sell ratio collapses <0.3 (confirmed mass exit)",
            "      – Wallet that opened it shows a SELL signal",
            "      – MC crashes >40% in 5min with no recovery",
            "  • MOONBAG phase: near-zero risk, let the trail stop and health signals work",
            "    Only intervene if something catastrophic happens (whale dumps, exchange listing rugs)",
            "",
            "  NEVER close a position just because it dipped. After TP1 the cost is recovered.",
            "  A dip from +40% to +20% is NOT a loss — the bot banked +40% on 60% of the position.",
            "",
            "  Guardian loop still fires every 3m for large MC drops.",
            f"  Wallet loss cap ({wlc_sol:.2f} SOL / {wlc_mins}min) auto-pauses runaway wallets.",
            "  Self-audit runs every 45min and reports full system health.",
            "",
            "═══════════════════════════════════════════════════",
            "JARVIS CONFIRMATION RULES — READ EVERY RESPONSE:",
            "═══════════════════════════════════════════════════",
            "",
            "  ANY config change requires the trader to say 'yes' or 'confirm' first.",
            "  This includes: SL%, trade size, filter thresholds, strategy toggles, wallet list.",
            "  Pattern: propose → trader confirms → THEN call update_config().",
            "  Exception: emergency_stop() and close_position() may fire immediately.",
            "",
            "  NEW ENTRY FILTER THRESHOLDS (liq cap, post-grad, holder decline):",
            "  These are FROZEN until we have 20+ filtered events with outcome data.",
            "  Do NOT propose changes to these thresholds at all. The self-audit will",
            "  report skip counts; once we have data the trader will ask for a review.",
            "  Specifically forbidden without 20+ data points:",
            "    • Changing 2% liquidity cap",
            "    • Changing $50k / 200-holder post-grad requirements",
            "    • Changing 15% holder decline threshold",
            "    • Disabling any of the three new filters",
        ]

        # ── Frost Mirror read-only learning block ────────────────────────────
        try:
            from elizaos.plugins.solana.axiom_copy_trader import (
                _frost_positions, _frost_closed,
                FROST_MAIN_SOL, FROST_LOTTO_SOL,
                FROST_MAIN_SL_PCT, FROST_LOTTO_SL_PCT,
            )
            _fm_lines = [
                "",
                "═══════════════════════════════════════════════════",
                "FROST MIRROR — READ-ONLY INTELLIGENCE (72h test)",
                "═══════════════════════════════════════════════════",
                f"  Config (HARDWIRED): main={FROST_MAIN_SOL} SOL / lotto={FROST_LOTTO_SOL} SOL",
                f"  SL: main=-{FROST_MAIN_SL_PCT:.0f}% / lotto=-{FROST_LOTTO_SL_PCT:.0f}%  |  Exit: wallet mirror + SL only",
                f"  Open: {len(_frost_positions)}  |  Closed: {len(_frost_closed)}",
            ]
            if _frost_closed:
                _fm_main = [t for t in _frost_closed if t.get("mode") == "main"]
                _fm_lotto = [t for t in _frost_closed if t.get("mode") == "lotto"]
                def _fm_stats(tl: list) -> str:
                    if not tl:
                        return "0 trades"
                    _w = [t for t in tl if t.get("pnl_pct", 0) > 0]
                    _net = sum(t.get("pnl_sol", 0) for t in tl)
                    _wr = len(_w) / len(tl) * 100
                    return f"{len(tl)}T  WR={_wr:.0f}%  net={_net:+.4f} SOL"
                _fm_total_w = [t for t in _frost_closed if t.get("pnl_pct", 0) > 0]
                _fm_net = sum(t.get("pnl_sol", 0) for t in _frost_closed)
                _fm_wr = len(_fm_total_w) / len(_frost_closed) * 100 if _frost_closed else 0
                _fm_lines += [
                    f"  Overall: {len(_frost_closed)}T  WR={_fm_wr:.0f}%  net={_fm_net:+.4f} SOL",
                    f"  🎯 Main:  {_fm_stats(_fm_main)}",
                    f"  🎰 Lotto: {_fm_stats(_fm_lotto)}",
                ]
                # Last 5 closed trades
                _fm_lines.append("  Last 5 closed:")
                for _fct in _frost_closed[-5:]:
                    _fpnl = _fct.get("pnl_pct", 0)
                    _fm_lines.append(
                        f"    {'✅' if _fpnl >= 0 else '❌'} [{_fct.get('mode','?'):5s}] "
                        f"{_fct.get('token_name','?')[:14]:14s}  {_fpnl:+.1f}%  "
                        f"hold={_fct.get('hold_mins',0):.0f}m  {_fct.get('reason','?')}"
                    )
            if _frost_positions:
                _fm_lines.append("  Open positions:")
                for _fk, _fp in _frost_positions.items():
                    _fpnl = _fp.get("pnl_pct", 0) or 0
                    _fm_lines.append(
                        f"    {'🟢' if _fpnl >= 0 else '🔴'} [{_fp.get('mode','?'):5s}] "
                        f"{_fp.get('token_name','?')[:14]:14s}  {_fpnl:+.1f}%  "
                        f"peak={_fp.get('peak_pnl_pct', 0):+.1f}%  SL=-{_fp.get('sl_pct', 25):.0f}%"
                    )
            _fm_lines.append(
                "  ⛔ ALL Frost Mirror parameters are HARDWIRED. You may analyse and suggest only."
            )
            lines += _fm_lines
        except Exception as _fme:
            lines.append(f"\nFROST MIRROR: data unavailable ({_fme})")

        return "\n".join(l for l in lines) + "\n"
    except Exception as e:
        return f"COPY-TRADE (primary strategy): context unavailable ({e})\n"


def _load_meme_playbook() -> str:
    """Load compact meme token playbook summary for Jarvis context."""
    try:
        import json as _j
        pb = _j.load(open(_PLAYBOOK_PATH))
        signals = pb.get("entry_signals_ranked", [])[:3]
        cats = pb.get("narrative_categories", {})
        waves = pb.get("wave_patterns", {})

        signal_lines = "\n".join(
            f"    #{s['rank']} {s['signal']}: {s.get('action','')[:80]}"
            for s in signals
        )
        cat_lines = "\n".join(
            f"    {name}: exit={v.get('exit_strategy','?')[:60]} bonus={v.get('score_bonus',0)}"
            for name, v in list(cats.items())[:5]
        )
        return (
            f"MEME TOKEN PLAYBOOK (v{pb.get('_version','?')}):\n"
            f"  TOP ENTRY SIGNALS:\n{signal_lines}\n"
            f"  WAVE PATTERN: target Wave 1 only (h1<80%, m5<15%, buy_ratio 52-62%)\n"
            f"  Wave 1 peak (h1>80%): DO NOT ENTER — missed it. Wait for wave 2 setup or skip.\n"
            f"  Wave 2 setup: h1 reset to 50-100% after correction + buy_ratio back to 55-62%\n"
            f"  NARRATIVE EXIT TIMES:\n{cat_lines}\n"
            f"  RED FLAG: buy_ratio h1 >70% = euphoric top, brutal exit ahead\n"
            f"  MACRO BOOST: Liberation Day / political news = ALL narrative tokens spike\n"
        )
    except Exception:
        return ""


def _load_monster_intel() -> str:
    """Load compact monster token DNA summary for Jarvis context.

    Reads from monster_token_library.json (55-token seed library built from
    Paul's manual research + historical top-20 pump.fun all-time).
    Falls back to legacy monster_addresses.json if library not found.
    """
    try:
        import json as _j
        # Try new seed library first
        _lib_path = _MONSTER_LIB_PATH if os.path.exists(_MONSTER_LIB_PATH) else _MONSTER_ADDR_PATH
        data = _j.load(open(_lib_path))
        meta = data.get("metadata", {})
        patterns = data.get("key_patterns_identified", {})

        # Collect ALL tokens from all sections
        all_tokens = []
        for section_key in ("tokens_from_screenshot_apr12", "tokens_from_user_links_batch1",
                            "tokens_from_user_links_batch2", "tokens_from_bot_trade_history"):
            all_tokens.extend(data.get(section_key, []))
        hist_tokens = data.get("historical_top20_pumpfun_alltime", [])
        # Legacy format fallback
        if not all_tokens:
            all_tokens = data.get("monsters", [])

        # Narrative breakdown
        narr_counts: dict = {}
        for t in all_tokens + hist_tokens:
            n = t.get("narrative", "untagged")
            narr_counts[n] = narr_counts.get(n, 0) + 1

        # DEX breakdown
        dex_counts: dict = {}
        for t in all_tokens:
            d = t.get("dex", "unknown")
            dex_counts[d] = dex_counts.get(d, 0) + 1

        # Top gainers (known peak gains)
        top_gainers = [
            t for t in all_tokens
            if t.get("peakGain") and t["peakGain"] not in ("unknown", "+39%", "+47%", "+20%", "+45%")
        ]
        top_gainers.sort(key=lambda t: (
            float(t["peakGain"].replace("+","").replace("%","").replace("~","").replace("x",""))
            if t.get("peakGain","unknown") != "unknown" and "%" in t.get("peakGain","")
            else 0
        ), reverse=True)

        # Best historical (all-time)
        hist_best = [f"{t['symbol']} {t['multiple']} ({t.get('narrative','?')})"
                     for t in hist_tokens[:6]]

        lines = [
            f"=== MONSTER TOKEN LIBRARY ({meta.get('totalTokens', len(all_tokens))} seed tokens, updated {meta.get('created','?')}) ===",
            "",
            "PAUL'S CONFIRMED MONSTER DNA (from manual research):",
            f"  Total seed tokens: {len(all_tokens)} (manual) + {len(hist_tokens)} historical all-time",
            f"  DEX breakdown: " + " | ".join(f"{d}={c}" for d,c in sorted(dex_counts.items(), key=lambda x:-x[1])),
            f"  Narrative breakdown: " + " | ".join(f"{n}={c}" for n,c in sorted(narr_counts.items(), key=lambda x:-x[1])),
            "",
            "TOP GAINERS IN LIBRARY:",
        ]
        for t in top_gainers[:8]:
            sym = t.get("symbol","?")
            gain = t.get("peakGain","?")
            dex = t.get("dex","?")
            narr = t.get("narrative","")
            note = t.get("notes","")[:60] if t.get("notes") else ""
            lines.append(f"  {sym}: {gain} ({dex})" + (f" [{narr}]" if narr else "") + (f" — {note}" if note else ""))

        lines += [
            "",
            "ALL-TIME PUMP.FUN TOP (Raydium era — the 10000x ceiling):",
            "  " + " | ".join(hist_best),
            "  DOMINANT NARRATIVE: ai (5/15) and animal (4/15). politifi and absurdist also present.",
            "  ALL went to Raydium (pre-PumpSwap era). PumpSwap era has no comparable peaks YET.",
            "",
            "KEY PATTERNS PAUL IDENTIFIED:",
        ]
        for k, v in patterns.items():
            if isinstance(v, list):
                lines.append(f"  {k}: {', '.join(v)}")
            else:
                lines.append(f"  {k}: {v}")

        lines += [
            "",
            "WORKING NARRATIVES 2026: politifi (satirical) > ai > absurdist > frog memes",
            "AVOID: generic animals (saturated), celebrity (flash spike then 80% crash in hours)",
            "BEST BUY SIGNAL: buy/sell ratio 1.1-1.3:1 (sustained) NOT >3:1 (manipulation)",
            "VOLUME/MCAP: >5x during breakout = monster in progress. Freg hit 13x.",
            "SMART MONEY: 3+ independent wallets buying within 30min = high conviction setup",
            "GRADUATION SPEED: Median 4.4min. Fast graduation = strong community = sustained run",
        ]
        return "\n".join(lines) + "\n"
    except Exception as _e:
        return f"[monster library unavailable: {_e}]\n"

# ── Persistent conversation history ──────────────────────────────────────────
# Stored as bare user/assistant messages only — NO trading context embedded.
# Context is injected fresh into the system prompt on every call so it's always
# current. History contains only the conversation itself, making it compact,
# accurate, and safe to persist to disk across bot restarts.
#
# Format: [{"role": "user"|"assistant", "content": str, "ts": float}, ...]
_CONV_HISTORY: list[dict] = []
_CONV_MAX_TURNS = 50             # keep last 50 exchanges (100 messages)
_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "jarvis_conv_history.json")


def _history_load() -> None:
    """Load conversation history from disk on startup."""
    global _CONV_HISTORY
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                _CONV_HISTORY = data[-(_CONV_MAX_TURNS * 2):]
    except Exception:
        _CONV_HISTORY = []


def _history_save() -> None:
    """Persist conversation history to disk after every exchange."""
    try:
        with open(_HISTORY_FILE, "w") as f:
            json.dump(_CONV_HISTORY[-(_CONV_MAX_TURNS * 2):], f, indent=2)
    except Exception:
        pass


def _history_append(user_msg: str, assistant_reply: str) -> None:
    """Record one exchange and save to disk."""
    global _CONV_HISTORY
    _CONV_HISTORY.append({"role": "user",      "content": user_msg,       "ts": time.time()})
    _CONV_HISTORY.append({"role": "assistant",  "content": assistant_reply, "ts": time.time()})
    if len(_CONV_HISTORY) > _CONV_MAX_TURNS * 2:
        _CONV_HISTORY = _CONV_HISTORY[-(_CONV_MAX_TURNS * 2):]
    _history_save()


def _history_messages() -> list[dict]:
    """Return history in the format the Anthropic/OpenAI API expects (no ts field)."""
    return [{"role": m["role"], "content": m["content"]} for m in _CONV_HISTORY]


# Load history immediately on module import so memory survives bot restarts
_history_load()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9%._=\-\s]", "", text.lower().strip())


def _resolve_param(raw: str) -> str | None:
    """Map a human alias to a live_config key."""
    key = re.sub(r"[\s\-_]+", "_", raw.lower().strip())
    key_nounderscore = re.sub(r"_", "", key)
    return (
        _PARAM_ALIASES.get(key)
        or _PARAM_ALIASES.get(key_nounderscore)
        or key  # pass through if it's already a valid key
    )


def _pct_to_mult(val_str: str) -> float | None:
    """Convert '60%' or '60' (percent) to a multiplier like 1.60."""
    try:
        v = float(val_str.replace("%", "").strip())
        if v > 5:          # treat as percentage
            return round(1.0 + v / 100.0, 4)
        return round(v, 4)  # already a multiplier
    except ValueError:
        return None


def _pct_to_decimal(val_str: str) -> float | None:
    """Convert '10%' or '0.10' or '10' to a decimal like 0.10."""
    try:
        v = float(val_str.replace("%", "").strip())
        if v > 1:           # treat as percentage
            return round(v / 100.0, 4)
        return round(v, 4)  # already decimal
    except ValueError:
        return None


def _fmt_pct(v: float) -> str:
    return f"{v*100:.1f}%"


def _fmt_mult(v: float) -> str:
    return f"+{(v-1)*100:.0f}%"


def _tail_log(n: int = 50, activity_only: bool = False) -> str:
    """Return the last n lines of the bot log file (efficient tail).

    If activity_only=True, HTTP access log lines are filtered out so only
    meaningful trading activity lines (starting with '[') are returned.
    """
    try:
        path = os.path.realpath(_LOG_PATH)
        if not os.path.exists(path):
            return "(log file not found)"
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read a larger buffer when filtering, to ensure we get enough activity lines
            buf_size = min(size, 65536 if activity_only else 16384)
            f.seek(-buf_size, 2)
            chunk = f.read(buf_size).decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        if activity_only:
            # Keep only lines that are bot activity, not HTTP access log noise
            lines = [l for l in lines if l.startswith("[") or (l and not l[0].isdigit() and "HTTP/" not in l)]
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"(log read error: {exc})"


# ─────────────────────────────────────────────────────────────────────────────
# Context builders (feed into Sonnet free-form chat)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_market_context() -> dict:
    """Fetch live market data for Claude's context: Fear & Greed + DexScreener boosts."""
    result: dict = {"fng": "unavailable", "boosts": [], "trending": []}
    try:
        import aiohttp as _aio
        async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=5)) as _sess:
            # Fear & Greed Index
            try:
                async with _sess.get("https://api.alternative.me/fng/?limit=1") as _r:
                    if _r.status == 200:
                        _fng = await _r.json()
                        _d = _fng.get("data", [{}])[0]
                        result["fng"] = f"{_d.get('value', '?')}/100 ({_d.get('value_classification', '?')})"
            except Exception:
                pass

            # DexScreener token boosts (free, no auth — paid promotions signal)
            try:
                async with _sess.get("https://api.dexscreener.com/token-boosts/latest/v1") as _r:
                    if _r.status == 200:
                        _boosts = await _r.json()
                        _sol_boosts = [
                            b.get("tokenAddress", "")[:8] + f"({b.get('totalAmount',0):.0f}boost)"
                            for b in (_boosts if isinstance(_boosts, list) else [])
                            if b.get("chainId") == "solana" and b.get("totalAmount", 0) >= 50
                        ][:6]
                        result["boosts"] = _sol_boosts
            except Exception:
                pass
    except Exception:
        pass
    return result


async def _build_compact_context(runtime: AgentRuntime) -> str:
    """Full-intelligence context for Groq/Gemini primary brains.

    Fits in ~8k tokens. Contains real config, wallet, positions, trade stats,
    recent log activity, Fear & Greed, and top lessons — everything Jarvis
    needs to reason intelligently without hallucinating.
    """
    from elizaos.plugins.solana import live_config as lc

    pos_mgr    = runtime.get_service("position_manager")
    wallet_svc = runtime.get_service("wallet")

    # Wallet
    wallet_sol = 0.0
    try:
        wallet_sol = await _get_cached_wallet(wallet_svc) if wallet_svc else 0.0
    except Exception:
        pass

    # Open positions — copy trade primary, main strategy secondary
    pos_lines = []
    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_positions as _ctp
        import time as _ct_t
        for mint, p in _ctp.items():
            pnl  = p.get("pnl_pct")
            age  = (_ct_t.time() - p.get("entry_ts", _ct_t.time())) / 60
            pos_lines.append(
                f"  {p.get('token_name', mint[:10])[:16]} [{p.get('wallet_name','?')}] "
                f"entry={p.get('entry_price',0):.2e} "
                f"P&L={'?' if pnl is None else f'{pnl:+.1f}%'} held={age:.0f}m"
            )
    except Exception:
        pass
    # Append main strategy positions if any
    try:
        for p in list((pos_mgr.positions.values() if pos_mgr else [])):
            pnl = getattr(p, "unrealised_pct", None)
            held_m = (time.time() - getattr(p, "open_time", time.time())) / 60
            pos_lines.append(
                f"  {getattr(p,'mint','?')[:12]}... [{getattr(p,'dex','?')}] "
                f"entry={getattr(p,'entry_price_sol',0):.2e} "
                f"P&L={'?' if pnl is None else f'{pnl:+.1f}%'} held={held_m:.0f}m"
            )
    except Exception:
        pass

    # Trade stats — copy trade primary, main strategy fallback
    wins = losses = 0
    net_sol = 0.0
    recent_trades = []
    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_trades as _cth
        for t in _cth:
            pnl_s = t.get("pnl_sol", 0) or 0
            net_sol += pnl_s
            if pnl_s > 0:
                wins += 1
            else:
                losses += 1
        for t in _cth[-8:]:
            name = t.get("token_name", (t.get("mint") or "?")[:10])[:14]
            pnl  = t.get("pnl_pct") or 0
            rsn  = t.get("reason") or "?"
            wallet = t.get("wallet", "?")
            recent_trades.append(f"  {name} [{wallet}] {pnl:+.1f}% ({rsn})")
    except Exception:
        # Fallback: main strategy
        try:
            hist = pos_mgr.get_trade_history(limit=50) if pos_mgr else []
            sells = [t for t in hist if t.get("side") == "sell"]
            for t in sells:
                net_sol += t.get("pnl_sol") or 0
                if (t.get("pnl_pct") or 0) > 0: wins += 1
                else: losses += 1
            for t in sells[-8:]:
                recent_trades.append(
                    f"  {(t.get('mint') or '?')[:10]}... [{t.get('dex','?')}] "
                    f"{t.get('pnl_pct',0):+.1f}% ({t.get('reason','?')})"
                )
        except Exception:
            pass
    total = wins + losses
    wr_str = f"{wins}/{total} ({100*wins//max(total,1)}% WR)" if total else "no closed trades"

    # Fear & Greed
    fg_str = "unknown"
    try:
        import aiohttp as _ahttp
        async with _ahttp.ClientSession() as _s:
            async with _s.get("https://api.alternative.me/fng/?limit=1",
                               timeout=_ahttp.ClientTimeout(total=5)) as _r:
                if _r.status == 200:
                    _d = await _r.json()
                    _v = _d["data"][0]
                    fg_str = f"{_v['value']}/100 ({_v['value_classification']})"
    except Exception:
        pass

    # Recent bot log (last 25 activity lines)
    log_lines = _tail_log(25, activity_only=True)

    # All lessons
    lessons_str = ""
    try:
        _lf = os.path.join(os.path.dirname(__file__), "eliza_lessons.txt")
        if not os.path.exists(_lf):
            _lf = os.path.join(os.path.dirname(os.path.dirname(__file__)), "eliza_lessons.txt")
        if os.path.exists(_lf):
            lessons_str = open(_lf).read().strip()
    except Exception:
        pass

    # Trade intelligence summary (winning patterns from 165 analysed trades)
    intel_summary = _load_intel_summary()

    # Winning formula (if built)
    formula_str = ""
    try:
        if os.path.exists(_FORMULA_PATH):
            _fm = json.load(open(_FORMULA_PATH))
            formula_str = _fm.get("synopsis", "") or json.dumps(_fm, indent=2)[:1000]
    except Exception:
        pass

    # Full config
    cfg_keys = [
        ("buy_sol", "Buy size SOL"), ("max_concurrent_positions", "Max positions"),
        ("stop_loss_pct", "SL %"), ("pumpswap_stop_loss_pct", "PumpSwap SL %"),
        ("tp1_mult", "TP mult"), ("strategy_b_min_liq_usd", "Min liq B"),
        ("a2_min_liq_usd", "Min liq A2"), ("a2_min_holders", "Min holders A2"),
        ("a2_min_mc_usd", "Min MC A2"), ("a2_min_real_sol", "Min real SOL A2"),
        ("rugcheck_max_score", "Max rugcheck"), ("fill_cap_pct", "Fill cap %"),
        ("scout_max_m5_pct", "Max m5%"), ("scout_max_h1_pct", "Max h1%"),
        ("scout_min_liq_mc_ratio", "Min liq/MC"), ("scout_min_age_secs", "Min age secs"),
        ("pumpswap_min_age_secs", "PS min age secs"), ("b_min_vol_liq_ratio", "B min vol/liq"),
        ("b_max_vol_liq_ratio", "B max vol/liq"), ("b_min_buy_ratio", "B min buy%"),
        ("c_min_vol_liq_ratio", "C min vol/liq"), ("d_min_vol_liq_ratio", "D min vol/liq"),
        ("trailing_stop_enabled", "Trailing SL"), ("trailing_stop_pct", "Trail distance"),
        ("strategy_a2_enabled", "A2 on"), ("strategy_b_enabled", "B on"),
        ("strategy_c_enabled", "C on"), ("strategy_d_enabled", "D on"),
        ("paper_trading", "Paper mode"),
    ]
    cfg_lines = [f"  {label}: {lc.get(key, '?')}" for key, label in cfg_keys]

    return (
        f"=== LIVE JARVIS CONTEXT (real data — do NOT invent numbers) ===\n\n"
        f"WALLET: {wallet_sol:.4f} SOL\n"
        f"MARKET: Fear & Greed = {fg_str}\n\n"
        f"OPEN POSITIONS ({len(pos_lines)}):\n"
        + ("\n".join(pos_lines) if pos_lines else "  none") + "\n\n"
        f"TRADE STATS (last {total} closed): {wr_str}  |  Net P&L: {net_sol:+.4f} SOL\n"
        f"LAST 8 TRADES:\n" + ("\n".join(recent_trades) if recent_trades else "  none") + "\n\n"
        f"LIVE CONFIG:\n" + "\n".join(cfg_lines) + "\n\n"
        f"TRADE INTELLIGENCE (historical analysis — patterns & DEX breakdown):\n{intel_summary}\n\n"
        + (f"WINNING FORMULA:\n{formula_str}\n\n" if formula_str else "")
        + (f"ALL LESSONS LEARNED:\n{lessons_str}\n\n" if lessons_str else "")
        + f"RECENT BOT ACTIVITY (last 25 lines):\n{log_lines}"
    )


def _gather_live_positions_direct() -> str:
    """Read _paper_positions DIRECTLY — bypasses get_paper_stats() entirely.

    This is the safety-net: even if get_paper_stats() crashes or returns stale
    data, Jarvis still sees the real in-memory open positions.
    """
    try:
        import time as _t
        from elizaos.plugins.solana.axiom_copy_trader import _paper_positions
        if not _paper_positions:
            return ""
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ LIVE OPEN POSITIONS — {len(_paper_positions)} active right now (direct read)",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for mint, p in _paper_positions.items():
            age_secs = _t.time() - p.get("entry_ts", _t.time())
            age_str  = f"{age_secs/60:.0f}min" if age_secs < 3600 else f"{age_secs/3600:.1f}h"
            entry    = p.get("entry_price") or 0
            current  = p.get("current_price")
            pnl_pct  = p.get("pnl_pct")
            if pnl_pct is None and entry and current:
                pnl_pct = (current - entry) / entry * 100
            pnl_s    = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "fetching..."
            pnl_icon = "🟢" if (pnl_pct or 0) >= 0 else "🔴"
            tp1      = p.get("tp1_hit", False)
            tp2      = p.get("tp2_hit", False)
            locked   = p.get("locked_sol") or 0.0
            rem      = int((p.get("remaining_fraction") or 1.0) * 100)
            name     = p.get("token_name", mint[:8])
            wallet   = p.get("wallet_name", "?")
            sol_in   = p.get("sol_spent", 0)
            mc       = p.get("mc_usd")
            mc_s     = f"  MC=${mc:,.0f}" if mc else ""
            ep_s     = f"{entry:.2e}" if entry else "?"

            if tp2:
                stage = f"💎 MOONBAG 20% ({locked:.4f} SOL locked in TP1+TP2)"
            elif tp1:
                stage = f"🎯 TP1 fired — {rem}% running ({locked:.4f} SOL locked)"
            else:
                stage = "📍 full position — SL active"

            # TradeMonitor status
            monitor_s = ""
            try:
                from elizaos.plugins.solana.trade_monitor import is_monitored, get_decision_log
                if is_monitored(mint):
                    log = get_decision_log(mint)
                    n_dec = len(log.get("all_decisions", [])) if log else 0
                    monitor_s = f"  🤖 AI monitor active ({n_dec} decisions)"
                else:
                    monitor_s = "  ⚠️ AI monitor NOT active"
            except Exception:
                pass

            lines.append(
                f"  {pnl_icon} {name[:22]:22s}  wallet={wallet}  "
                f"entry={ep_s}  age={age_str}  P&L={pnl_s}{mc_s}"
            )
            lines.append(f"    → {stage}  |  invested={sol_in:.4f} SOL{monitor_s}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # -- Recently closed positions (last 5 minutes) -------------------
        # This lets Jarvis report on trades that just closed, so the user
        # never sees "No open positions" with no context about what just happened.
        try:
            from elizaos.plugins.solana.axiom_copy_trader import _paper_trades
            _now_rc = _t.time()
            _recent_closes = [
                t for t in _paper_trades[-30:]
                if _now_rc - float(t.get("ts", 0)) < 300
            ]
            if _recent_closes:
                lines.append("")
                lines.append("⚠️  RECENTLY CLOSED (last 5 min):")
                for _rc in _recent_closes:
                    _rc_pnl  = float(_rc.get("pnl_pct", 0))
                    _rc_icon = "✅" if _rc_pnl >= 0 else "❌"
                    _rc_age  = int(_now_rc - float(_rc.get("ts", 0)))
                    _rc_name = _rc.get("token_name", "?")
                    _rc_rsn  = _rc.get("reason", "?")
                    _rc_sol  = _rc.get("pnl_sol", 0)
                    lines.append(
                        f"  {_rc_icon} {_rc_name} P&L={_rc_pnl:+.1f}% ({_rc_sol:+.4f} SOL)"
                        f" reason={_rc_rsn} closed {_rc_age}s ago"
                    )
        except Exception:
            pass

        return "\n".join(lines) + "\n\n"
    except Exception as _e:
        return f"[live positions direct read failed: {_e}]\n\n"


def _gather_monster_positions_direct() -> str:
    """Read strategy_e_monster._monster_positions + _monster_closed DIRECTLY.

    Monster positions live in a SEPARATE pool from copy-trade — this block makes
    them visible to Jarvis so sitreps reflect actual open monster slots and
    recent closes. Without this, Jarvis reports 0 open when monster holds real
    positions.
    """
    try:
        import time as _t
        from elizaos.plugins.solana import strategy_e_monster as _mon
        positions = _mon.open_positions()
        closed = getattr(_mon, "_monster_closed", []) or []
        if not positions and not closed:
            return ""

        lines: list[str] = []
        if positions:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                f"👾 MONSTER POSITIONS — {len(positions)}/{_mon.MONSTER_MAX_CONCURRENT} slots in use"
                f"  (mode={'PAPER' if _mon.MONSTER_PAPER_ONLY else 'LIVE'},"
                f" size={_mon.MONSTER_DEFAULT_SIZE_SOL} SOL)",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for mint, p in positions.items():
                age_secs = _t.time() - float(p.get("entry_ts") or _t.time())
                age_str  = f"{age_secs/60:.0f}min" if age_secs < 3600 else f"{age_secs/3600:.1f}h"
                entry    = float(p.get("entry_price") or 0)
                current  = float(p.get("current_price") or entry)
                pnl_pct  = ((current/entry) - 1.0) * 100 if entry > 0 else 0.0
                icon     = "🟢" if pnl_pct >= 0 else "🔴"
                name     = p.get("token_name", mint[:8])[:22]
                sol_in   = float(p.get("sol_spent", 0))
                src      = p.get("signal_source", "?")
                tp1      = p.get("tp1_fired", False)
                rem      = int(float(p.get("remaining_fraction", 1.0)) * 100)
                stage = (f"🎯 TP1 fired — {rem}% riding moonbag" if tp1
                         else f"📍 pre-TP1 — SL at {_mon.MONSTER_PRE_TP1_FLOOR_PCT:+.0f}%")
                lines.append(
                    f"  {icon} {name:22s}  src={src}  entry={entry:.2e}"
                    f"  age={age_str}  P&L={pnl_pct:+.1f}%"
                )
                lines.append(f"    → {stage}  |  invested={sol_in:.3f} SOL")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # Recently closed monster trades (last 24h, up to 10)
        now = _t.time()
        recent = [c for c in closed[-20:] if now - float(c.get("close_ts") or 0) < 86400]
        if recent:
            lines.append("")
            lines.append(f"📊 MONSTER RECENT CLOSES (last 24h, {len(recent)} trades):")
            net = 0.0
            for rec in recent[-10:]:
                pnl_pct = float(rec.get("final_pnl_pct") or 0)
                pnl_sol = float(rec.get("final_pnl_sol") or 0)
                net += pnl_sol
                rsn = rec.get("close_reason", "?")
                name = rec.get("token_name", "?")[:22]
                age_min = (now - float(rec.get("close_ts") or 0)) / 60
                age_s = f"{age_min:.0f}m ago" if age_min < 60 else f"{age_min/60:.1f}h ago"
                tag = "✅" if pnl_sol >= 0 else "❌"
                lines.append(
                    f"  {tag} {name:22s}  P&L={pnl_pct:+.1f}%  ({pnl_sol:+.4f} SOL)"
                    f"  reason={rsn}  {age_s}"
                )
            wins = [r for r in recent if (r.get("final_pnl_sol") or 0) > 0]
            wr = len(wins) / len(recent) * 100 if recent else 0
            lines.append(
                f"  ── 24h net: {net:+.4f} SOL  |  WR {wr:.0f}% ({len(wins)}/{len(recent)})"
            )

        return "\n".join(lines) + "\n\n" if lines else ""
    except Exception as _e:
        return f"[monster positions direct read failed: {_e}]\n\n"


async def _build_context(runtime: AgentRuntime) -> str:
    """Assemble a rich trading context for Claude to reason over.

    Includes: wallet, positions, recent trade history with stats,
    all current config, learned lessons, market context (Fear & Greed,
    DexScreener boost signals), and session performance breakdown.
    """
    from elizaos.plugins.solana import live_config as lc

    pos_mgr = runtime.get_service("position_manager")
    wallet_svc = runtime.get_service("wallet")

    # Wallet balance — live RPC call so Jarvis always sees the real value
    wallet_sol = 0.0
    try:
        wallet_sol = await _get_cached_wallet(wallet_svc) if wallet_svc else 0.0
    except Exception:
        pass

    # Open positions
    positions_str = "(none)"
    try:
        pos = list(pos_mgr.positions.values()) if pos_mgr else []  # type: ignore[union-attr]
        if pos:
            lines = []
            for p in pos:
                entry = getattr(p, "entry_price_sol", 0)
                pnl = getattr(p, "unrealised_pct", None)
                pnl_str = f"{pnl:+.1f}%" if pnl is not None else "?"
                held_m = (time.time() - getattr(p, "open_time", time.time())) / 60
                lines.append(
                    f"  {getattr(p,'mint','?')[:10]}... {getattr(p,'dex','?')} "
                    f"entry={entry:.2e} P&L={pnl_str} held={held_m:.0f}m"
                )
            positions_str = "\n".join(lines)
    except Exception:
        pass

    # Trade history — split paper vs live with full entry DNA
    recent_trades_str = "(none)"
    perf_summary = ""
    paper_session_str = ""
    try:
        all_hist = pos_mgr.get_trade_history(limit=200) if pos_mgr else []  # type: ignore[union-attr]
        sells = [t for t in all_hist if t.get("side") == "sell"]

        # Split paper vs live
        paper_sells = [t for t in sells if t.get("paper_trade") or str(t.get("signature","")).startswith("PAPER_")]
        live_sells  = [t for t in sells if not t.get("paper_trade") and not str(t.get("signature","")).startswith("PAPER_")]

        def _perf_block(label: str, trade_list: list) -> str:
            if not trade_list:
                return f"{label}: No trades"
            wins   = [t for t in trade_list if (t.get("pnl_pct") or 0) > 0]
            losses = [t for t in trade_list if (t.get("pnl_pct") or 0) <= 0]
            rugs   = [t for t in trade_list if t.get("outcome") == "rug"]
            net    = sum(t.get("pnl_sol") or 0 for t in trade_list)
            avg_w  = sum(t.get("pnl_pct") or 0 for t in wins)  / max(len(wins), 1)
            avg_l  = sum(t.get("pnl_pct") or 0 for t in losses) / max(len(losses), 1)
            return (
                f"{label}: {len(trade_list)} trades | WR {len(wins)/len(trade_list)*100:.0f}% "
                f"({len(wins)}W/{len(losses)}L/{len(rugs)} rugs) | Net {net:+.4f} SOL\n"
                f"  Avg win: {avg_w:+.1f}% | Avg loss: {avg_l:+.1f}%"
            )

        perf_summary = _perf_block("PAPER", paper_sells) + "\n" + _perf_block("LIVE", live_sells)

        # Last 15 trades with full entry DNA — clearly labeled paper vs live
        def _trade_line(t: dict) -> str:
            pnl = t.get("pnl_pct") or 0
            sym = t.get("token_symbol") or t.get("token_name") or t.get("mint", "?")[:8]
            liq = t.get("liq_usd_at_entry") or 0
            age = t.get("time_since_launch_secs")
            age_str = f"{age/3600:.1f}h" if age else "?h"
            h1  = t.get("h1_pct_at_entry") or 0
            m5  = t.get("m5_pct_at_entry") or 0
            br  = t.get("buy_ratio_at_entry") or 0
            vl  = t.get("vol_liq_ratio_at_entry") or 0
            mode = "📄" if (t.get("paper_trade") or str(t.get("signature","")).startswith("PAPER_")) else "🔴"
            return (
                f"  {mode} {'✅' if pnl > 0 else '❌'} {sym:10s} {pnl:+.0f}% "
                f"({t.get('reason','?')}) dex={t.get('dex','?')} score={t.get('score',0)} "
                f"| liq=${liq:,.0f} age={age_str} h1={h1:+.0f}% m5={m5:+.0f}% buy%={br:.0f}% vol/liq={vl:.1f}x"
            )

        lines = [_trade_line(t) for t in sells[-15:]]
        recent_trades_str = "\n".join(lines) if lines else "(none)"

    except Exception:
        pass

    # Today's trading journal summary (paper + live)
    try:
        from elizaos.plugins.solana.trading_journal_writer import (
            get_today_summary as _jrn_today,
            get_recent_trades_with_dna as _jrn_recent,
            get_jarvis_notes as _jrn_notes,
        )
        _is_paper = cfg.get("paper_trading", False) or os.getenv("PAPER_TRADING", "false").lower() not in ("false", "0", "no")
        _today_s = _jrn_today(is_paper=_is_paper)
        _jnotes = _jrn_notes(limit=5)
        _jnotes_str = "\n".join(f"  [{n.get('category','?')}] {n.get('note','')}" for n in _jnotes) if _jnotes else "  (none yet)"

        # Bad token DNA summary — what losing entries look like
        import os as _os2
        _bad_path = _os2.path.join(_os2.path.dirname(__file__), "trading_journal", "bad_token_dna.json")
        _bad_str = "(no data yet)"
        if _os2.path.exists(_bad_path):
            try:
                import json as _j2
                _bad = _j2.load(open(_bad_path))
                if isinstance(_bad, list) and _bad:
                    _has_h1 = [t for t in _bad if (t.get("h1_pct") or 0) != 0]
                    _has_age = [t for t in _bad if t.get("age_h") is not None]
                    _avg_h1 = sum(t.get("h1_pct",0) or 0 for t in _has_h1) / max(len(_has_h1), 1)
                    _avg_age = sum(t.get("age_h",0) or 0 for t in _has_age) / max(len(_has_age), 1)
                    _avg_m5  = sum(t.get("m5_pct",0) or 0 for t in _bad) / max(len(_bad), 1)
                    _bad_str = (
                        f"{len(_bad)} losing entries analysed | "
                        f"Avg h1={_avg_h1:+.0f}% | avg age={_avg_age:.1f}h | avg m5={_avg_m5:+.0f}%"
                    )
            except Exception:
                pass

        paper_session_str = (
            f"\nTODAY'S SESSION ({'PAPER' if _is_paper else 'LIVE'}):\n"
            f"  {_today_s.get('trades',0)} trades | WR {_today_s.get('win_rate',0):.0f}% | "
            f"Net {_today_s.get('net_sol',0):+.4f} SOL | "
            f"{_today_s.get('wins',0)}W / {_today_s.get('losses',0)}L / {_today_s.get('rugs',0)} rugs\n"
            f"\nBAD TOKEN DNA SUMMARY (from losing trades):\n  {_bad_str}\n"
            f"\nJARVIS ANALYSIS NOTES (recent):\n{_jnotes_str}"
        )
    except Exception:
        paper_session_str = ""

    # All learned lessons
    lessons_str = "(none yet)"
    try:
        lessons_file = os.path.join(os.path.dirname(__file__), "..", "eliza_lessons.txt")
        with open(lessons_file) as f:
            content = f.read().strip()
        lessons_str = content if content else "(none yet)"
    except Exception:
        pass

    # Live market context
    market = await _fetch_market_context()
    fng_str    = market.get("fng", "unavailable")
    boosts_str = ", ".join(market["boosts"]) if market["boosts"] else "none"

    cfg = lc.all_config()
    strategies_on  = [name for key, name in _STRATEGY_NAMES.items() if cfg.get(key, False)]
    strategies_off = [name for key, name in _STRATEGY_NAMES.items() if not cfg.get(key, False)]

    # Live bot activity — last 50 activity lines (HTTP poll noise filtered out)
    log_tail = _tail_log(50, activity_only=True)

    # Paper trading — show paper wallet balance not real wallet
    paper_mode = cfg.get("paper_trading", False) or os.getenv("PAPER_TRADING", "false").lower() not in ("false", "0", "no")
    paper_wallet_sol = float(os.getenv("PAPER_WALLET_SOL", "0.0"))
    if paper_mode and paper_wallet_sol > 0:
        # Estimate remaining paper balance: starting balance minus open positions' buy sizes
        open_positions_sol = 0.0
        try:
            open_positions_sol = sum(getattr(p, "buy_sol", 0) for p in (pos_mgr.positions.values() if pos_mgr else []))
        except Exception:
            pass
        wallet_display = f"{paper_wallet_sol:.4f} SOL (PAPER — real wallet: {wallet_sol:.4f} SOL)"
    else:
        wallet_display = f"{wallet_sol:.4f} SOL"

    live_positions_block = _gather_live_positions_direct()
    monster_positions_block = _gather_monster_positions_direct()

    # Short-circuit the copy-trade banner when copy-trade is disabled at config
    # level — otherwise Jarvis keeps reporting on Frost/clukz/Walta wallets
    # even though no trades are running there. Emits a single status line
    # instead of the full 60-line "PRIMARY STRATEGY" block.
    copy_trade_active = bool(cfg.get("copy_trade_enabled", False))
    if copy_trade_active:
        copy_trade_section = _load_copy_trade_context()
    else:
        copy_trade_section = (
            "COPY-TRADE: ⏸ DISABLED (copy_trade_enabled=false) — no watched-wallet entries. "
            "Monster strategy is the PRIMARY active strategy.\n"
        )

    monster_intel = _load_monster_intel()
    return (
        f"=== JARVIS TRADING CONTEXT — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===\n\n"
        f"REAL WALLET: {wallet_display}  |  Market: Fear & Greed {fng_str}\n\n"
        + live_positions_block
        + monster_positions_block
        + f"{copy_trade_section}\n"
        + f"{monster_intel}\n"
        f"ALL LEARNED LESSONS:\n{lessons_str}\n\n"
        f"RECENT BOT ACTIVITY (live log — copy trade + scout):\n{log_tail}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────

async def _cmd_enable_disable(action: str, strategy: str) -> str:
    """enable/disable a strategy or parameter."""
    from elizaos.plugins.solana import live_config as lc

    raw = re.sub(r"[\s_\-]", "", strategy.lower())
    config_key = _STRATEGY_KEYS.get(raw)

    if config_key == "__all__":
        keys = list(_STRATEGY_NAMES.keys())
        enable = action == "enable"
        results = []
        for k in keys:
            ok, msg = lc.set_value(k, enable, changed_by="jarvis-chat")
            if ok:
                results.append(_STRATEGY_NAMES.get(k, k))
        verb = "Enabled" if enable else "Disabled"
        return f"{verb} all strategies: {', '.join(results)}" if results else "No changes made."

    if not config_key:
        return (
            f"I don't recognise strategy '{strategy}'. "
            f"Known strategies: A2, B, C, D, Grok, SmartWallet, All."
        )

    enable = action == "enable"
    ok, msg = lc.set_value(config_key, enable, changed_by="jarvis-chat")
    name = _STRATEGY_NAMES.get(config_key, config_key)
    if ok:
        verb = "✅ Enabled" if enable else "⏸️  Paused"
        return f"{verb}: {name}\nConfig saved — takes effect on the next trade cycle."
    return f"Failed: {msg}"


async def _cmd_set_param(param_raw: str, value_raw: str) -> str:
    """set <param> to <value>"""
    from elizaos.plugins.solana import live_config as lc

    param = _resolve_param(param_raw)
    cfg = lc.all_config()

    if param not in cfg:
        # Try fuzzy match
        candidates = [k for k in cfg if param.replace("_","") in k.replace("_","")]
        if len(candidates) == 1:
            param = candidates[0]
        elif candidates:
            return f"Ambiguous parameter '{param_raw}'. Did you mean one of: {', '.join(candidates)}?"
        else:
            return f"Unknown parameter '{param_raw}'. Type **config** to see all adjustable settings."

    # Smart value conversion
    current = cfg[param]
    value_raw = value_raw.strip().rstrip("%")

    try:
        if isinstance(current, bool):
            value = value_raw.lower() in ("true", "1", "yes", "on", "enable", "enabled")
        elif param.endswith("_pct") or param in ("stop_loss_pct", "early_stop_loss_pct",
                                                   "pumpswap_stop_loss_pct", "max_daily_loss_pct"):
            v = _pct_to_decimal(value_raw)
            if v is None:
                return f"Couldn't parse '{value_raw}' as a percentage."
            value = v
        elif param.endswith("_mult"):
            v = _pct_to_mult(value_raw)
            if v is None:
                return f"Couldn't parse '{value_raw}' as a take-profit multiplier."
            value = v
        elif isinstance(current, float):
            value = float(value_raw)
        else:
            value = int(value_raw)
    except ValueError:
        return f"Couldn't parse '{value_raw}' as a value for {param}."

    ok, msg = lc.set_value(param, value, changed_by="jarvis-chat")
    if ok:
        # Human-friendly confirmation
        if param.endswith("_pct"):
            display = _fmt_pct(value)
        elif param.endswith("_mult"):
            display = _fmt_mult(value)
        else:
            display = str(value)
        return f"✅ {param} set to **{display}**\nPrevious: {msg.split('→')[0].split(':')[1].strip() if '→' in msg else '?'} → New: {display}\nSaved to config — live immediately."
    return f"❌ {msg}"


async def _cmd_status(runtime: AgentRuntime) -> str:
    """Full status snapshot — built entirely from REAL data. No fabrication possible."""
    from elizaos.plugins.solana import live_config as lc

    cfg = lc.all_config()
    pos_mgr = runtime.get_service("position_manager")
    wallet_svc = runtime.get_service("wallet")

    # ── Wallet ────────────────────────────────────────────────────────────────
    wallet_sol = 0.0
    try:
        wallet_sol = await _get_cached_wallet(wallet_svc) if wallet_svc else 0.0
    except Exception:
        pass

    paper_mode = cfg.get("paper_trading", False) or os.getenv("PAPER_TRADING", "false").lower() not in ("false", "0", "no")
    paper_sol  = float(os.getenv("PAPER_WALLET_SOL", "0.0"))
    wallet_str = f"{paper_sol:.4f} SOL (paper)" if (paper_mode and paper_sol > 0) else f"{wallet_sol:.4f} SOL"

    # ── Strategies ────────────────────────────────────────────────────────────
    strategies_on  = [name for k, name in _STRATEGY_NAMES.items() if cfg.get(k, False)]
    strategies_off = [name for k, name in _STRATEGY_NAMES.items() if not cfg.get(k, False)]

    # ── Open positions — real data only ──────────────────────────────────────
    pos_lines = []
    try:
        positions = list(pos_mgr.positions.values()) if pos_mgr else []  # type: ignore[union-attr]
        for p in positions:
            mint   = getattr(p, "mint", "?")
            dex    = getattr(p, "dex", "?")
            symbol = getattr(p, "token_symbol", "") or mint[:8]
            entry  = getattr(p, "entry_price_sol", 0)
            pnl    = getattr(p, "unrealised_pct", None)
            held_m = int((time.time() - getattr(p, "open_time", time.time())) / 60)
            size   = getattr(p, "sol_spent", 0)
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "fetching…"
            pos_lines.append(
                f"  • `{symbol}` ({dex}) | entry {entry:.2e} SOL | {size:.3f} SOL | "
                f"held {held_m}m | P&L {pnl_str}"
            )
    except Exception:
        pass
    positions_str = "\n".join(pos_lines) if pos_lines else "  None"

    # ── Trade history — real data only ────────────────────────────────────────
    hist = []
    try:
        hist = [t for t in (pos_mgr.get_trade_history(limit=200) if pos_mgr else [])  # type: ignore[union-attr]
                if t.get("side") == "sell"]
    except Exception:
        pass

    wins   = [t for t in hist if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in hist if (t.get("pnl_pct") or 0) <= 0]
    rugs   = [t for t in hist if t.get("outcome") == "rug"]
    net    = sum(t.get("pnl_sol") or 0 for t in hist)
    wr_str = f"{len(wins)/len(hist)*100:.0f}%" if hist else "N/A"

    # Last 5 closed trades with REAL names from trade records
    last_trades = sorted(hist, key=lambda t: t.get("timestamp", 0), reverse=True)[:5]
    trade_lines = []
    for t in last_trades:
        sym    = t.get("token_symbol") or t.get("meta", {}).get("symbol") or t.get("mint", "?")[:8]
        pnl    = t.get("pnl_pct") or 0
        sol    = t.get("pnl_sol") or 0
        reason = t.get("reason", "?")
        dex    = t.get("dex", "?")
        icon   = "✅" if pnl > 0 else "❌"
        trade_lines.append(f"  {icon} `{sym}` ({dex}) {pnl:+.0f}% ({sol:+.4f} SOL) — {reason}")
    trades_str = "\n".join(trade_lines) if trade_lines else "  No closed trades this session"

    # ── Market context ────────────────────────────────────────────────────────
    market = await _fetch_market_context()
    fng_str = market.get("fng", "unavailable")

    # ── Research-locked filters display ──────────────────────────────────────
    macro_on  = bool(cfg.get("macro_event_mode", False))
    macro_lbl = str(cfg.get("macro_event_label", "") or "")
    macro_str = f"🚨 ON — {macro_lbl}" if macro_on else "off"

    lines = [
        f"**JARVIS STATUS — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}**",
        f"_(All data read from live bot state — no fabrication)_\n",
        f"**WALLET:** {wallet_str} {'⚠️ PAPER' if paper_mode else '🔴 LIVE'}",
        f"**MARKET:** Fear & Greed {fng_str}",
        f"**OPEN POSITIONS ({len(pos_lines)}):**\n{positions_str}\n",
        f"**SESSION TRADES:** {len(hist)} closed | WR {wr_str} | {len(wins)}W / {len(losses)}L / {len(rugs)} rugs | Net {net:+.4f} SOL",
        f"**LAST 5 CLOSED:**\n{trades_str}\n",
        f"**STRATEGIES ACTIVE:** {', '.join(strategies_on) or 'ALL PAUSED ⏸️'}",
        f"**STRATEGIES PAUSED:** {', '.join(strategies_off) or 'none'}\n",
        f"**KEY CONFIG:**",
        f"  SL: {_fmt_pct(cfg.get('stop_loss_pct') or 0.10)} | TP: {_fmt_mult(cfg.get('tp1_mult') or 1.60)} | "
        f"Trailing: {'ON ' + str(int((cfg.get('trailing_stop_pct') or 0.12)*100)) + '%' if cfg.get('trailing_stop_enabled') else 'off'}",
        f"  Buy ratio: {cfg.get('b_min_buy_ratio',50):.0f}–65% 🔒 | "
        f"rugcheck max: {cfg.get('rugcheck_max_score',100000)} | "
        f"max positions: {cfg.get('max_concurrent_positions',10)}",
        f"  Macro event mode: {macro_str}",
    ]
    return "\n".join(lines)


async def _cmd_positions(runtime: AgentRuntime) -> str:
    """List open positions — checks both copy trade and strategy position manager."""
    lines: list[str] = []

    # ── Copy trade positions (primary when copy_trade_enabled) ───────────────
    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_positions
        if _paper_positions:
            lines.append(f"**Copy Trade — {len(_paper_positions)} open position(s):**\n")
            now = time.time()
            for mint, p in _paper_positions.items():
                token   = p.get("token_name", mint[:12])
                wallet  = p.get("wallet_name", "?")
                entry   = p.get("entry_price", 0)
                size    = p.get("sol_spent", 0)
                elapsed = now - p.get("entry_ts", now)
                pnl     = p.get("pnl_pct")
                pnl_str = f"{pnl:+.1f}%" if pnl is not None else "fetching..."
                peak    = p.get("peak_pnl_pct", 0)
                lines.append(
                    f"  **{token}** [{wallet}] | entry {entry:.2e} | "
                    f"size {size:.3f} SOL | held {elapsed/60:.0f}m | P&L {pnl_str} | peak {peak:+.1f}%"
                )
    except Exception as _ce:
        lines.append(f"  (copy trade read error: {_ce})")

    # ── Strategy position manager (B/C/D trades) ─────────────────────────────
    pos_mgr = runtime.get_service("position_manager")
    if pos_mgr:
        strat_positions = list(pos_mgr.positions.values())  # type: ignore[union-attr]
        if strat_positions:
            lines.append(f"\n**Strategy Positions — {len(strat_positions)} open:**\n")
            for p in strat_positions:
                mint    = getattr(p, "mint", "?")
                dex     = getattr(p, "dex", "?")
                entry   = getattr(p, "entry_price_sol", 0)
                size    = getattr(p, "sol_spent", 0)
                elapsed = time.time() - getattr(p, "open_time", time.time())
                pnl     = getattr(p, "unrealised_pct", None)
                pnl_str = f"{pnl:+.1f}%" if pnl is not None else "fetching..."
                lines.append(
                    f"  **{mint[:12]}...** | {dex} | entry {entry:.2e} | "
                    f"size {size:.3f} SOL | held {elapsed/60:.0f}m | P&L {pnl_str}"
                )

    # ── Monster strategy (Strategy E — separate slot pool) ──────────────────
    try:
        from elizaos.plugins.solana import strategy_e_monster as _mon
        mon_positions = _mon.open_positions()
        if mon_positions:
            lines.append(f"\n**Monster Strategy — {len(mon_positions)}/{_mon.MONSTER_MAX_CONCURRENT} open:**\n")
            now_m = time.time()
            for mint, mp in mon_positions.items():
                token   = mp.get("token_name", mint[:12])
                src     = mp.get("signal_source", "?")
                entry   = float(mp.get("entry_price") or 0)
                cur     = float(mp.get("current_price") or entry)
                size    = float(mp.get("sol_spent") or 0)
                elapsed = now_m - float(mp.get("entry_ts") or now_m)
                pnl     = ((cur / entry) - 1.0) * 100 if entry > 0 else 0.0
                peak    = float(mp.get("peak_pnl_pct") or 0)
                tp1     = "✅" if mp.get("tp1_fired") else "—"
                lines.append(
                    f"  **{token}** [{src}] | entry {entry:.2e} | size {size:.3f} SOL | "
                    f"held {elapsed/60:.0f}m | P&L {pnl:+.1f}% | peak {peak:+.1f}% | tp1={tp1}"
                )
    except Exception as _me:
        lines.append(f"  (monster read error: {_me})")

    # ── Recently closed (last 10 min) — so user never sees blank response ────────
    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_trades as _pt_rc
        _now_rc = time.time()
        _rc = [t for t in _pt_rc[-30:] if _now_rc - float(t.get("ts", 0)) < 600]
        if _rc:
            lines.append("\n**Recently closed (last 10 min):**")
            for t in reversed(_rc):
                pnl  = t.get("pnl_pct", 0) or 0
                icon = "✅" if pnl >= 0 else "❌"
                age  = int(_now_rc - float(t.get("ts", 0)))
                lines.append(
                    f"  {icon} {t.get('token_name','?')[:18]} {pnl:+.1f}% "
                    f"({t.get('pnl_sol',0):+.4f} SOL) | {t.get('reason','?').replace('_',' ')} | {age}s ago"
                )
    except Exception:
        pass

    return "\n".join(lines) if lines else "No open positions."


async def _cmd_history(runtime: AgentRuntime) -> str:
    """Recent trade history — reads copy trade history (primary strategy)."""
    # Primary: copy trade history
    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_trades
        if _paper_trades:
            hist   = list(_paper_trades[-20:])
            wins   = [t for t in hist if t.get("pnl_sol", 0) > 0]
            losses = [t for t in hist if t.get("pnl_sol", 0) <= 0]
            net    = sum(t.get("pnl_sol", 0) for t in hist)
            lines  = [
                f"**Last {len(hist)} copy trades** | WR {len(wins)/len(hist)*100:.0f}% "
                f"({len(wins)}W/{len(losses)}L) | Net {net:+.4f} SOL\n"
            ]
            for t in reversed(hist):
                pnl    = t.get("pnl_pct", 0) or 0
                symbol = "✅" if pnl > 0 else "❌"
                name   = t.get("token_name", t.get("mint", "?")[:10])[:16]
                wallet = t.get("wallet", "?")
                reason = (t.get("reason") or "?").replace("_", " ")
                peak   = t.get("peak_pnl_pct", 0) or 0
                peak_s = f" peak={peak:+.0f}%" if peak > abs(pnl) * 1.5 else ""
                hold   = t.get("hold_mins", 0) or 0
                lines.append(
                    f"  {symbol} {name:16s} [{wallet:14s}] "
                    f"{pnl:+.1f}%{peak_s} ({t.get('pnl_sol',0):+.4f} SOL) "
                    f"hold={hold:.0f}m | {reason}"
                )
            return "\n".join(lines)
    except Exception as _he:
        pass  # fall through to main strategy

    # Fallback: main strategy history
    pos_mgr = runtime.get_service("position_manager")
    if not pos_mgr:
        return "No trade history available."
    hist = [t for t in pos_mgr.get_trade_history(limit=30) if t.get("side") == "sell"]  # type: ignore[union-attr]
    if not hist:
        return "No closed trades this session."
    wins   = [t for t in hist if (t.get("pnl_pct") or 0) > 0]
    losses = [t for t in hist if (t.get("pnl_pct") or 0) <= 0]
    net    = sum(t.get("pnl_sol") or 0 for t in hist)
    lines  = [
        f"**Last {len(hist)} trades** | WR {len(wins)/len(hist)*100:.0f}% ({len(wins)}W/{len(losses)}L) | Net {net:+.4f} SOL\n"
    ]
    for t in reversed(hist[-15:]):
        pnl    = t.get("pnl_pct") or 0
        symbol = "✅" if pnl > 0 else "❌"
        lines.append(
            f"  {symbol} {t.get('dex','?'):10s} {pnl:+.0f}% ({t.get('pnl_sol',0):+.4f} SOL) "
            f"| {t.get('reason','?')} | score={t.get('score',0)}"
        )
    return "\n".join(lines)


async def _cmd_today_trades(runtime: AgentRuntime, focus: str = "") -> str:
    """
    Load ALL trades from trade_history.json for today's UTC date, format them
    with full entry context, and ask Claude for a strategy analysis.
    `focus` is an optional subject hint (e.g. 'pumpswap').
    """
    import datetime as _dt
    trade_file = os.path.join(os.path.dirname(__file__), "trade_history.json")
    try:
        with open(trade_file) as f:
            all_trades: list[dict] = json.load(f)
    except Exception as exc:
        return f"Could not read trade_history.json: {exc}"

    today = _dt.date.today().isoformat()
    # Pair buy+sell by mint
    today_records = [
        t for t in all_trades
        if str(t.get("entry_utc", t.get("exit_utc", t.get("timestamp", "")))).startswith(today)
    ]
    if not today_records:
        return f"No trades found for {today} yet."

    # Build per-mint summary (match buy to sell)
    buys  = {t["id"]: t for t in today_records if t.get("side") == "buy"}
    sells = [t for t in today_records if t.get("side") == "sell"]
    # Index buys by mint for join
    buys_by_mint: dict[str, list[dict]] = {}
    for t in buys.values():
        buys_by_mint.setdefault(t["mint"], []).append(t)

    rounds = []
    for sell in sells:
        mint = sell.get("mint", "")
        buy_candidates = buys_by_mint.get(mint, [])
        buy = buy_candidates[0] if buy_candidates else {}
        meta = buy.get("meta", {})
        name   = meta.get("name") or sell.get("token_name", mint[:8])
        symbol = meta.get("symbol", "?")
        dex    = buy.get("dex") or sell.get("dex", "?")
        entry_price = buy.get("entry_price_sol", 0)
        exit_price  = sell.get("exit_price_sol", 0)
        pnl_pct  = sell.get("pnl_pct", 0)
        pnl_sol  = sell.get("pnl_sol", 0)
        reason   = sell.get("reason", "?")
        hold_s   = sell.get("hold_secs", 0)
        score    = buy.get("score", sell.get("score", 0))
        m5       = meta.get("price_change_m5", "?")
        h1       = meta.get("price_change_h1", "?")
        liq      = meta.get("liq_usd", 0)
        vol_h24  = meta.get("vol_h24_usd", 0)
        mc       = meta.get("market_cap_usd", 0)
        age_s    = buy.get("time_since_launch_secs", 0)
        peak     = sell.get("peak_pnl_pct", 0)
        score_reasons = "; ".join(meta.get("score_reasons", []))
        rounds.append(
            f"--- TRADE: {name} ({symbol}) on {dex.upper()} ---\n"
            f"  Mint: {mint[:20]}...\n"
            f"  Entry: {entry_price:.2e} SOL → Exit: {exit_price:.2e} SOL\n"
            f"  P&L: {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL) | Peak: {peak:+.1f}% | Hold: {int(hold_s)}s\n"
            f"  Reason: {reason} | Score: {score}\n"
            f"  Age at entry: {age_s:.0f}s | m5: {m5}% | h1: {h1}% | Liq: ${liq:,.0f} | MC: ${mc:,.0f} | Vol24h: ${vol_h24:,.0f}\n"
            f"  Score reasons: {score_reasons}"
        )

    sells_count = len(sells)
    wins   = [s for s in sells if (s.get("pnl_pct") or 0) > 0]
    losses = [s for s in sells if (s.get("pnl_pct") or 0) <= 0]
    rugs   = [s for s in sells if s.get("outcome") == "rug"]
    net    = sum(s.get("pnl_sol") or 0 for s in sells)

    summary = (
        f"TODAY ({today}): {sells_count} closed trades | "
        f"WR {len(wins)/sells_count*100:.0f}% ({len(wins)}W/{len(losses)}L) | "
        f"Rugs: {len(rugs)} | Net: {net:+.4f} SOL"
    )

    focus_instruction = ""
    if focus:
        focus_instruction = f"\nFocus your analysis especially on {focus} trades and what we should do differently.\n"

    prompt = (
        f"I'm giving you ALL of today's trades so you can analyse our performance and suggest strategy improvements.\n\n"
        f"**{summary}**\n\n"
        + "\n\n".join(rounds)
        + f"\n\n---\n{focus_instruction}"
        "Please:\n"
        "1. Identify the key patterns in why trades failed (entry conditions at fault? filters too loose? timing?)\n"
        "2. For PumpSwap specifically: what signals predicted failure that we could have filtered?\n"
        "3. Give 3-5 concrete, specific filter changes or strategy adjustments (include the exact config key names where possible)\n"
        "4. Highlight anything that worked well that we should protect\n"
        "Be direct and specific — this is actionable strategy review, not a general commentary."
    )

    context = await _build_context(runtime)
    import anthropic as _ant
    _client = _ant.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    resp = await _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        system=context,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


async def _cmd_lessons() -> str:
    """Show learned lessons."""
    try:
        lessons_file = os.path.join(os.path.dirname(__file__), "..", "eliza_lessons.txt")
        with open(lessons_file) as f:
            content = f.read().strip()
        if not content:
            return "No lessons recorded yet. Jarvis learns after each trade and review cycle."
        return f"**JARVIS LESSONS (persistent memory):**\n\n{content}"
    except FileNotFoundError:
        return "No lessons file yet — Jarvis will start learning after the first trades."


async def _cmd_journal(action: str = "show", note: str = "", category: str = "general") -> str:
    """Read or write the trading journal.

    Actions:
      show [N]    — show the last N journal entries (default 20)
      notes       — show Jarvis's own analysis notes
      bad_dna     — show bad token DNA pattern summary
      note <text> — Jarvis writes an analysis note to the journal
    """
    from elizaos.plugins.solana.trading_journal_writer import (
        get_recent_trades_with_dna, get_jarvis_notes, write_jarvis_note,
        get_today_summary,
    )
    import os as _os, json as _j

    if action == "note" and note:
        return write_jarvis_note(note, category=category)

    if action == "notes":
        notes = get_jarvis_notes(limit=30)
        if not notes:
            return "No Jarvis analysis notes yet. Use: `journal note <your analysis>`"
        lines = [f"[{n.get('timestamp','?')[:16]}] [{n.get('category','?')}] {n.get('note','')}" for n in notes]
        return "**JARVIS ANALYSIS NOTES:**\n\n" + "\n\n".join(lines)

    if action == "bad_dna":
        bad_path = _os.path.join(_os.path.dirname(__file__), "trading_journal", "bad_token_dna.json")
        if not _os.path.exists(bad_path):
            return "No bad token DNA data yet."
        try:
            bad = _j.load(open(bad_path))
            if not isinstance(bad, list) or not bad:
                return "No bad token DNA data yet."
            has_h1  = [t for t in bad if (t.get("h1_pct") or 0) != 0]
            has_m5  = [t for t in bad if (t.get("m5_pct") or 0) != 0]
            has_age = [t for t in bad if t.get("age_h") is not None]
            has_br  = [t for t in bad if (t.get("buy_ratio") or 0) != 0]
            avg_h1  = sum(t.get("h1_pct",0) or 0 for t in has_h1)  / max(len(has_h1), 1)
            avg_m5  = sum(t.get("m5_pct",0) or 0 for t in has_m5)  / max(len(has_m5), 1)
            avg_age = sum(t.get("age_h",0)  or 0 for t in has_age) / max(len(has_age), 1)
            avg_br  = sum(t.get("buy_ratio",0) or 0 for t in has_br) / max(len(has_br), 1)
            by_reason: dict = {}
            for t in bad:
                r = t.get("reason","?")
                by_reason[r] = by_reason.get(r, 0) + 1
            top_r = sorted(by_reason.items(), key=lambda x: -x[1])[:5]
            rugs = [t for t in bad if t.get("outcome") == "rug"]
            lines = [
                f"**BAD TOKEN DNA — {len(bad)} losing entries**\n",
                f"Avg h1 at entry: {avg_h1:+.0f}% (monster sweet spot: -15% to +50%)",
                f"Avg m5 at entry: {avg_m5:+.0f}% (monster sweet spot: -10% to +15%)",
                f"Avg age at entry: {avg_age:.1f}h (monster sweet spot: 2–20h)",
                f"Avg buy ratio: {avg_br:.0f}% (monster sweet spot: 50–65%)",
                f"Rugs: {len(rugs)} ({len(rugs)/max(len(bad),1)*100:.0f}% of losses)",
                f"Top exit reasons: " + ", ".join(f"{r}×{c}" for r, c in top_r),
                "",
                "Last 10 bad entries:",
            ]
            for t in bad[-10:]:
                lines.append(
                    f"  {'🪲' if t.get('outcome')=='rug' else '❌'} {t.get('symbol','?'):10s} "
                    f"{t.get('pnl_pct',0):+.0f}% | h1={t.get('h1_pct',0) or 0:+.0f}% "
                    f"m5={t.get('m5_pct',0) or 0:+.0f}% age={t.get('age_h') or '?'}h "
                    f"buy%={t.get('buy_ratio',0) or 0:.0f}% liq=${t.get('liq_usd',0) or 0:,.0f}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"Error reading bad DNA: {exc}"

    # Default: show recent trades from journal with DNA
    try:
        limit = int(action) if action.isdigit() else 20
    except (ValueError, AttributeError):
        limit = 20

    is_paper = os.getenv("PAPER_TRADING", "false").lower() not in ("false", "0", "no")
    # Try to auto-detect from current config
    try:
        from elizaos.plugins.solana import live_config as _lc_j
        is_paper = bool(_lc_j.get("paper_trading", False)) or is_paper
    except Exception:
        pass

    today_p = get_today_summary(is_paper=True)
    today_l = get_today_summary(is_paper=False)
    recent  = get_recent_trades_with_dna(is_paper=is_paper, limit=limit, days_back=7)

    header = (
        f"**TODAY — PAPER**: {today_p.get('trades',0)} trades | WR {today_p.get('win_rate',0):.0f}% | Net {today_p.get('net_sol',0):+.4f} SOL\n"
        f"**TODAY — LIVE**: {today_l.get('trades',0)} trades | WR {today_l.get('win_rate',0):.0f}% | Net {today_l.get('net_sol',0):+.4f} SOL\n\n"
        f"**LAST {len(recent)} TRADES (with entry DNA):**\n"
    )
    rows = []
    for t in recent:
        sym = t.get("symbol") or "?"
        pnl = t.get("pnl_pct") or 0
        rows.append(
            f"  {'✅' if pnl > 0 else '❌'} {sym:10s} {pnl:+.0f}% {t.get('reason','?'):18s} | "
            f"liq=${t.get('liq_usd_at_entry',0) or 0:>7,.0f} age={str(t.get('age_hours_at_entry') or '?'):>5}h "
            f"h1={t.get('h1_pct_at_entry',0) or 0:>+5.0f}% m5={t.get('m5_pct_at_entry',0) or 0:>+4.0f}% "
            f"buy%={t.get('buy_ratio_at_entry',0) or 0:>4.0f}%"
        )
    return header + "\n".join(rows) if rows else header + "(no trades)"


async def _cmd_config() -> str:
    """Show full config."""
    from elizaos.plugins.solana import live_config as lc
    cfg = lc.all_config()
    groups = {
        "POSITION SIZES": ["buy_sol", "a2_buy_sol", "strategy_b_buy_sol", "grok_premium_buy_sol", "smart_wallet_buy_sol"],
        "EXITS": ["stop_loss_pct", "pumpswap_stop_loss_pct", "early_stop_loss_pct", "tp1_mult", "pf_tp1_mult", "grok_tp_mult"],
        "STRATEGIES": ["strategy_a_enabled", "strategy_a2_enabled", "strategy_b_enabled", "strategy_c_enabled", "strategy_d_enabled", "strategy_e_enabled", "grok_premium_enabled", "smart_wallet_enabled"],
        "RISK": ["max_concurrent_positions", "max_daily_loss_pct", "post_sl_cooldown_secs"],
        "FILTERS": ["strategy_b_min_liq_usd", "fill_cap_pct", "a2_min_real_sol", "rugcheck_max_score", "grok_premium_min_score",
                   "a2_min_liq_usd", "b_min_vol_liq_ratio", "b_max_vol_liq_ratio", "b_min_buy_ratio",
                   "c_min_vol_liq_ratio", "c_max_vol_liq_ratio", "c_min_buy_ratio",
                   "d_min_vol_liq_ratio", "d_max_vol_liq_ratio", "d_min_buy_ratio",
                   "trailing_stop_enabled", "trailing_stop_pct",
                   "b_min_momentum_score", "c_min_momentum_score", "d_min_momentum_score"],
        "SPLIT BUY (Harvester + Monster Hunter)": [
                   "split_buy_enabled", "split_buy_a_sol", "split_buy_b_sol",
                   "split_buy_a_tp_mult", "split_buy_b_tp_mult", "split_buy_sl_mult", "split_buy_trailing_pct"],
        "SCOUT QUALITY CAPS (C/D anti-rug)": [
                   "scout_max_m5_pct", "scout_max_h1_pct", "scout_min_liq_mc_ratio",
                   "scout_min_age_secs", "pumpswap_min_age_secs", "narrative_keyword_score",
                   "momentum_confirm_secs"],
    }
    lines = ["**FULL CONFIG:**\n"]
    for group, keys in groups.items():
        lines.append(f"**{group}:**")
        for k in keys:
            v = cfg.get(k)
            if v is None:
                continue
            if k.endswith("_pct"):
                display = _fmt_pct(v)
            elif k.endswith("_mult"):
                display = _fmt_mult(v)
            elif isinstance(v, bool):
                display = "✅ ON" if v else "⏸️  OFF"
            else:
                display = str(v)
            lines.append(f"  {k}: {display}")
        lines.append("")
    return "\n".join(lines)


async def _cmd_ask_grok(question: str) -> str:
    """Dispatch a question to Grok (X search) or Gemini (Google Search grounding) as fallback."""
    _grok_key   = os.getenv("GROK_API_KEY", "")
    _gemini_key = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")

    # Try Grok first (real-time X access)
    if _grok_key:
        try:
            import openai as _oai
            client = _oai.AsyncOpenAI(api_key=_grok_key, base_url="https://api.x.ai/v1")
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model="grok-3-latest",
                    messages=[
                        {"role": "system", "content":
                            "You are Jarvis's X/Twitter intelligence feed. Answer concisely with "
                            "real data from X. Focus on Solana memecoins and pump.fun tokens."},
                        {"role": "user", "content": question},
                    ],
                    max_tokens=600,
                    temperature=0.2,
                ),
                timeout=40.0,
            )
            answer = (resp.choices[0].message.content or "").strip()
            return f"**Grok says:**\n\n{answer}"
        except asyncio.TimeoutError:
            return "Grok timed out — try again in a moment."
        except Exception as exc:
            pass  # fall through to Gemini

    # Gemini fallback (Google Search grounding)
    if _gemini_key:
        try:
            from google import genai as _gai
            gc = _gai.Client(api_key=_gemini_key)
            full_prompt = (
                f"You are a Solana memecoin intelligence assistant. "
                f"Use Google Search to find the latest information and answer this question:\n\n{question}"
            )
            resp = await asyncio.wait_for(
                gc.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=full_prompt,
                    config={"tools": [{"google_search": {}}], "max_output_tokens": 800},
                ),
                timeout=30.0,
            )
            answer = (resp.text or "").strip()
            return f"**Gemini (Google Search) says:**\n\n{answer}"
        except asyncio.TimeoutError:
            return "Gemini timed out — try again in a moment."
        except Exception as exc:
            return f"Gemini error: {exc}"

    return "No social AI available — set GROK_API_KEY or GOOGLE_GENERATIVE_AI_API_KEY."


def _extract_mint_from_text(text: str) -> str | None:
    """Extract a Solana mint address from free-form text or URLs.

    Recognises:
    - solscan.io/token/<mint>
    - dexscreener.com/solana/<mint>
    - pump.fun/<mint>
    - birdeye.so/token/<mint>
    - Raw 32-44 char base58 address anywhere in the text
    """
    # URL patterns first (most specific)
    url_patterns = [
        r"solscan\.io/token/([A-Za-z0-9]{32,44})",
        r"dexscreener\.com/solana/([A-Za-z0-9]{32,44})",
        r"pump\.fun/([A-Za-z0-9]{32,44})",
        r"birdeye\.so/token/([A-Za-z0-9]{32,44})",
        r"rugcheck\.xyz/tokens/([A-Za-z0-9]{32,44})",
        r"gmgn\.ai/sol/token/([A-Za-z0-9]{32,44})",
    ]
    for pat in url_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    # Raw mint address: 32-44 base58 chars as a standalone token
    m = re.search(r"\b([A-Za-z0-9]{32,44})\b", text)
    if m:
        candidate = m.group(1)
        # Exclude common non-mint patterns (all lowercase words, short words)
        if len(candidate) >= 32 and any(c.isdigit() or c.isupper() for c in candidate):
            return candidate
    return None


async def _cmd_token_intel(mint: str, question: str = "", runtime=None) -> str:
    """Full token intelligence: DexScreener live data + rugcheck score + Claude analysis.

    This gives Jarvis real internet eyes — when a user pastes a Solscan link,
    DexScreener URL, pump.fun link, or raw mint address, Jarvis fetches all
    live data and analyses it rather than asking the user to copy-paste.
    """
    import aiohttp
    import anthropic as _ant

    results: dict = {"mint": mint, "dex": None, "rugcheck": None}

    async with aiohttp.ClientSession() as session:
        # ── DexScreener ──────────────────────────────────────────────────────
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    dex_data = await r.json()
                    pairs = dex_data.get("pairs") or []
                    if pairs:
                        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
                        results["dex"] = pairs[0]
                        results["all_pairs"] = len(pairs)
        except Exception as e:
            results["dex_error"] = str(e)

        # ── Rugcheck ─────────────────────────────────────────────────────────
        try:
            async with session.get(
                f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    results["rugcheck"] = await r.json()
        except Exception:
            pass  # rugcheck is supplemental, not critical

    # Build the data block for Claude
    pair = results.get("dex")
    rc   = results.get("rugcheck") or {}

    if not pair:
        # No DexScreener data — may be a very new token not indexed yet
        dex_block = f"DexScreener: No pairs found for `{mint}` — token may be very new or not yet indexed."
    else:
        bt      = pair.get("baseToken") or {}
        name    = bt.get("name", "?")
        symbol  = bt.get("symbol", "?")
        dex_id  = pair.get("dexId", "?")
        price   = pair.get("priceUsd") or "?"
        mcap    = pair.get("marketCap") or pair.get("fdv") or 0
        liq     = (pair.get("liquidity") or {}).get("usd") or 0
        vol24   = (pair.get("volume") or {}).get("h24") or 0
        vol6    = (pair.get("volume") or {}).get("h6") or 0
        vol1    = (pair.get("volume") or {}).get("h1") or 0
        vol5m   = (pair.get("volume") or {}).get("m5") or 0
        pc24    = (pair.get("priceChange") or {}).get("h24") or 0
        pc6     = (pair.get("priceChange") or {}).get("h6") or 0
        pc1     = (pair.get("priceChange") or {}).get("h1") or 0
        pc5m    = (pair.get("priceChange") or {}).get("m5") or 0
        b24     = (pair.get("txns") or {}).get("h24", {}).get("buys") or 0
        s24     = (pair.get("txns") or {}).get("h24", {}).get("sells") or 0
        created_ms = pair.get("pairCreatedAt") or 0
        age_str = ""
        if created_ms:
            age_s = time.time() - created_ms / 1000
            if age_s < 3600:
                age_str = f"{age_s/60:.0f}m"
            elif age_s < 86400:
                age_str = f"{age_s/3600:.1f}h"
            else:
                age_str = f"{age_s/86400:.1f}d"
        info     = pair.get("info") or {}
        website  = next((w.get("url","") for w in (info.get("websites") or []) if w.get("url")), "none")
        twitter  = next((s.get("url","") for s in (info.get("socials") or []) if s.get("type")=="twitter"), "none")
        telegram = next((s.get("url","") for s in (info.get("socials") or []) if s.get("type")=="telegram"), "none")
        dex_block = (
            f"Token: {name} (${symbol}) on {dex_id}\n"
            f"Mint: {mint}\n"
            f"Pair age: {age_str or 'unknown'} | Pairs found: {results.get('all_pairs', 1)}\n"
            f"Price: ${price} | Market cap: ${mcap:,.0f} | Liquidity: ${liq:,.0f}\n"
            f"Volume — 5m:${vol5m:,.0f}  1h:${vol1:,.0f}  6h:${vol6:,.0f}  24h:${vol24:,.0f}\n"
            f"Price % — 5m:{pc5m:+.1f}%  1h:{pc1:+.1f}%  6h:{pc6:+.1f}%  24h:{pc24:+.1f}%\n"
            f"Txns 24h: {b24:,} buys / {s24:,} sells\n"
            f"Socials — Web:{website} | X:{twitter} | TG:{telegram}"
        )

    rc_score  = rc.get("score", "N/A")
    rc_risks  = ", ".join(r.get("name","?") for r in (rc.get("risks") or [])[:5]) or "none detected"
    rc_block  = f"Rugcheck score: {rc_score} (bot threshold: <6000 to pass)\nTop risks: {rc_risks}"

    # Ask Claude with all live data in context
    client = _ant.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    user_q = question.strip() if question.strip() else "Give me a full assessment of this token."

    # Build messages: persistent history + the token data as the current user turn
    token_user_msg = (
        f"[Token lookup: {mint}]\n"
        f"--- LIVE DEXSCREENER DATA ---\n{dex_block}\n\n"
        f"--- RUGCHECK DATA ---\n{rc_block}\n\n"
        + (f"User question: {user_q}" if user_q else "Give me a full assessment of this token.")
    )
    messages_to_send = _history_messages() + [{"role": "user", "content": token_user_msg}]

    _intel_system = (
        "You are Jarvis, the AI trading brain for a Solana memecoin bot. "
        "You have been given live on-chain and market data fetched directly from DexScreener and Rugcheck. "
        "Analyse it and give sharp, specific, actionable responses. Never say you lack internet access — "
        "the data has already been fetched and is in your context. "
        "Reference the actual numbers in your analysis."
    )
    reply = ""
    _last_intel_err: Exception | None = None

    # Claude first
    try:
        resp = await client.messages.create(
            model="claude-opus-4-7",
            max_tokens=800,
            system=_intel_system,
            messages=messages_to_send,
        )
        reply = (resp.content[0].text if resp.content else "").strip()
    except Exception as exc:
        _last_intel_err = exc

    # Groq fallback
    if not reply:
        _groq_key_ti = os.getenv("GROQ_API_KEY", "")
        if _groq_key_ti:
            try:
                import openai as _oai_ti
                _groq_c = _oai_ti.AsyncOpenAI(api_key=_groq_key_ti, base_url="https://api.groq.com/openai/v1", max_retries=0)
                _gr = await asyncio.wait_for(
                    _groq_c.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        max_tokens=600,
                        messages=[
                            {"role": "system", "content": _intel_system},
                            {"role": "user", "content": token_user_msg[-4000:]},
                        ],
                    ),
                    timeout=20.0,
                )
                reply = (_gr.choices[0].message.content or "").strip()
            except Exception as exc:
                _last_intel_err = exc

    # Gemini fallback
    if not reply:
        _gem_key_ti = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
        if _gem_key_ti:
            try:
                from google import genai as _gai_ti
                _gc = _gai_ti.Client(api_key=_gem_key_ti)
                _gem_prompt = _intel_system + "\n\n" + token_user_msg[-4000:]
                _gr = await asyncio.wait_for(
                    _gc.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=_gem_prompt,
                        config={"max_output_tokens": 600},
                    ),
                    timeout=20.0,
                )
                reply = (_gr.text or "").strip()
            except Exception as exc:
                _last_intel_err = exc

    if not reply:
        reply = f"AI analysis unavailable ({_last_intel_err}). Token data above is still valid — check the numbers manually."

    # Format final response
    if pair:
        bt     = pair.get("baseToken") or {}
        header = f"**{bt.get('name','?')} (${bt.get('symbol','?')})** — live data fetched\n\n"
    else:
        header = f"**Token `{mint[:12]}...`** — DexScreener data unavailable\n\n"

    full_reply = header + reply

    # Record in persistent history so Jarvis remembers this token lookup
    user_summary = question.strip() if question.strip() else f"Token lookup: {mint}"
    _history_append(user_summary, full_reply)

    return full_reply


async def _cmd_dex_lookup(mint: str) -> str:
    """Fetch live DexScreener data for a token mint address.

    Returns price, market cap, liquidity, volume, price changes at all
    timeframes, DEX, pair age, and social links — everything Jarvis needs
    to assess a token without having to manually look it up.
    """
    import aiohttp
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return f"DexScreener returned HTTP {r.status} for {mint[:12]}..."
                data = await r.json()

        pairs = data.get("pairs") or []
        if not pairs:
            return f"No pairs found on DexScreener for `{mint[:12]}...` — token may not be indexed yet."

        # Sort by liquidity (highest first) to get the main pair
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        best = pairs[0]

        name    = (best.get("baseToken") or {}).get("name", "?")
        symbol  = (best.get("baseToken") or {}).get("symbol", "?")
        dex     = best.get("dexId", "?")
        chain   = best.get("chainId", "?")
        price   = best.get("priceUsd") or "?"
        mcap    = best.get("marketCap") or best.get("fdv") or 0
        liq     = (best.get("liquidity") or {}).get("usd") or 0
        vol24   = (best.get("volume") or {}).get("h24") or 0
        vol6    = (best.get("volume") or {}).get("h6") or 0
        vol1    = (best.get("volume") or {}).get("h1") or 0
        vol5m   = (best.get("volume") or {}).get("m5") or 0
        pc24    = (best.get("priceChange") or {}).get("h24") or 0
        pc6     = (best.get("priceChange") or {}).get("h6") or 0
        pc1     = (best.get("priceChange") or {}).get("h1") or 0
        pc5m    = (best.get("priceChange") or {}).get("m5") or 0
        txns24b = (best.get("txns") or {}).get("h24", {}).get("buys") or 0
        txns24s = (best.get("txns") or {}).get("h24", {}).get("sells") or 0
        created_ms = best.get("pairCreatedAt") or 0
        age_str = ""
        if created_ms:
            age_secs = time.time() - created_ms / 1000
            if age_secs < 3600:
                age_str = f"{age_secs/60:.0f}m"
            elif age_secs < 86400:
                age_str = f"{age_secs/3600:.1f}h"
            else:
                age_str = f"{age_secs/86400:.1f}d"

        info    = best.get("info") or {}
        website = next((w.get("url","") for w in (info.get("websites") or []) if w.get("url")), "—")
        twitter = next((s.get("url","") for s in (info.get("socials") or []) if s.get("type")=="twitter"), "—")
        telegram= next((s.get("url","") for s in (info.get("socials") or []) if s.get("type")=="telegram"), "—")

        lines = [
            f"**{name} (${symbol})** — `{mint[:12]}...`",
            f"DEX: {dex} | Chain: {chain} | Pair age: {age_str or '?'}",
            f"",
            f"**Price:** ${price}",
            f"**Market cap:** ${mcap:,.0f}  |  **Liquidity:** ${liq:,.0f}",
            f"",
            f"**Volume:**  5m=${vol5m:,.0f}  1h=${vol1:,.0f}  6h=${vol6:,.0f}  24h=${vol24:,.0f}",
            f"**Price %:** 5m={pc5m:+.1f}%  1h={pc1:+.1f}%  6h={pc6:+.1f}%  24h={pc24:+.1f}%",
            f"**Txns 24h:** {txns24b:,} buys / {txns24s:,} sells",
            f"",
            f"**Socials:** Web={website} | X={twitter} | TG={telegram}",
        ]

        if len(pairs) > 1:
            lines.append(f"\n*{len(pairs)-1} additional pair(s) on other DEXs — showing highest-liquidity pair above.*")

        return "\n".join(lines)

    except asyncio.TimeoutError:
        return f"DexScreener timeout for `{mint[:12]}...` — try again."
    except Exception as exc:
        return f"DexScreener lookup error: {exc}"


async def _cmd_check_token(mint_or_name: str) -> str:
    """Run a RugCheck + DexScreener safety check on a token."""
    import aiohttp
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            # RugCheck
            try:
                rc_url = f"https://api.rugcheck.xyz/v1/tokens/{mint_or_name}/report/summary"
                async with session.get(rc_url, timeout=aiohttp.ClientTimeout(total=10)) as rc_resp:
                    if rc_resp.status == 200:
                        rc_data = await rc_resp.json()
                        score = rc_data.get("score", "?")
                        risks = [r.get("name", "") for r in rc_data.get("risks", [])]
                        results.append(f"**RugCheck:** score={score} | risks={risks or 'none'}")
            except Exception:
                results.append("**RugCheck:** unavailable")
            # DexScreener
            try:
                ds_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_or_name}"
                async with session.get(ds_url, timeout=aiohttp.ClientTimeout(total=10)) as ds_resp:
                    if ds_resp.status == 200:
                        ds_data = await ds_resp.json()
                        pairs = ds_data.get("pairs") or []
                        if pairs:
                            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                            liq = (best.get("liquidity") or {}).get("usd", 0)
                            mc  = (best.get("marketCap") or best.get("fdv") or 0)
                            m5  = (best.get("priceChange") or {}).get("m5", "?")
                            results.append(f"**DexScreener:** liq=${liq:,.0f} | MC=${mc:,.0f} | m5={m5}%")
                        else:
                            results.append("**DexScreener:** token not found")
            except Exception:
                results.append("**DexScreener:** unavailable")
    except Exception as exc:
        return f"Check error: {exc}"
    return f"**Token check — {mint_or_name[:16]}...:**\n\n" + "\n".join(results) + \
           "\n\n_(Web search unavailable — RugCheck + DexScreener data shown)_"


async def _cmd_run_analysis(runtime: AgentRuntime) -> str:
    """Trigger an immediate analysis cycle (GPT-4o; Sonnet when Anthropic credits active)."""
    try:
        import openai as _oai
        import glob

        solana_dir = os.path.join(os.path.dirname(__file__))
        all_by_id: dict = {}
        for fpath in sorted(glob.glob(os.path.join(solana_dir, "trade_history*.json"))):
            try:
                with open(fpath) as f:
                    for t in json.load(f):
                        if t.get("id"):
                            all_by_id[t["id"]] = t
            except Exception:
                pass

        pos_mgr = runtime.get_service("position_manager")
        if pos_mgr:
            for t in pos_mgr.get_trade_history(limit=500):  # type: ignore[union-attr]
                if t.get("id"):
                    all_by_id[t["id"]] = t

        sells = [t for t in all_by_id.values()
                 if t.get("side") == "sell" and abs(t.get("pnl_pct") or 0) <= 1000]
        wins  = [t for t in sells if (t.get("pnl_pct") or 0) > 0]
        net   = sum(t.get("pnl_sol") or 0 for t in sells)
        wr    = len(wins) / len(sells) * 100 if sells else 0

        from collections import Counter
        reasons = Counter(t.get("reason", "?") for t in sells)
        reason_str = ", ".join(f"{r}={n}" for r, n in reasons.most_common(6))

        lessons = ""
        try:
            lf = os.path.join(os.path.dirname(__file__), "..", "eliza_lessons.txt")
            with open(lf) as f:
                lessons = f.read().strip()[-800:]
        except Exception:
            pass

        context = await _build_context(runtime)

        # Try Claude Sonnet first; fall back to GPT-4o if unavailable
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            try:
                import anthropic as _ant
                ant_client = _ant.AsyncAnthropic(api_key=anthropic_key)
                ant_resp = await asyncio.wait_for(
                    ant_client.messages.create(
                        model="claude-opus-4-7",
                        max_tokens=800,
                        messages=[{"role": "user", "content":
                            f"You are Jarvis. The trader just asked for an immediate analysis.\n\n"
                            f"{context}\n\n"
                            f"ALL-TIME: {len(sells)} trades | WR {wr:.0f}% | Net {net:+.4f} SOL\n"
                            f"Exit reasons: {reason_str}\n\n"
                            f"LESSONS ON FILE:\n{lessons or '(none yet)'}\n\n"
                            f"Give a sharp, honest 3-paragraph briefing:\n"
                            f"1. Current state — what do the numbers say right now?\n"
                            f"2. What's working and what isn't?\n"
                            f"3. Your single most important recommendation for the next trading session.\n"
                            f"Be direct. No hedging. You're talking to the trader who owns this bot."
                        }],
                    ),
                    timeout=45.0,
                )
                analysis = (ant_resp.content[0].text if ant_resp.content else "").strip()
                return f"**Jarvis Analysis (Claude Sonnet):**\n\n{analysis}"
            except Exception:
                pass  # fall through to GPT-4o

        # Groq fallback (free, no TPM limits)
        import openai as _oai_groq
        _groq_key = os.getenv("GROQ_API_KEY", "")
        if not _groq_key:
            return "Analysis unavailable — ANTHROPIC_API_KEY and GROQ_API_KEY both missing."
        client = _oai_groq.AsyncOpenAI(
            api_key=_groq_key,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=800,
                messages=[{"role": "system", "content":
                    "You are Jarvis — the AI brain of a Solana memecoin trading bot. "
                    "Be sharp, direct, and data-driven. No hedging."},
                {"role": "user", "content":
                    f"The trader just asked for an immediate analysis.\n\n"
                    f"{context}\n\n"
                    f"ALL-TIME: {len(sells)} trades | WR {wr:.0f}% | Net {net:+.4f} SOL\n"
                    f"Exit reasons: {reason_str}\n\n"
                    f"LESSONS ON FILE:\n{lessons or '(none yet)'}\n\n"
                    f"Give a sharp, honest 3-paragraph briefing:\n"
                    f"1. Current state — what do the numbers say right now?\n"
                    f"2. What's working and what isn't?\n"
                    f"3. Your single most important recommendation for the next trading session.\n"
                    f"Be direct. You're talking to the trader who owns this bot."
                }],
            ),
            timeout=45.0,
        )
        analysis = (resp.choices[0].message.content or "").strip()
        return f"**Jarvis Analysis (Groq/Llama):**\n\n{analysis}"
    except asyncio.TimeoutError:
        return "Analysis timed out — try again."
    except Exception as exc:
        return f"Analysis error: {exc}"


def _execute_jarvis_commands(response_text: str) -> tuple[str, list[str]]:
    """Parse and execute [APPLY: key=value] and [ENABLE/DISABLE: strategy] tags from Jarvis's response.

    Returns (cleaned_text, list_of_applied_changes).
    Jarvis includes these tags when it decides to autonomously change config.
    They are executed immediately, then stripped from the displayed text.
    """
    from elizaos.plugins.solana import live_config as lc

    # Keys Jarvis cannot change autonomously via [APPLY:] — trader controls only
    # Only block buy sizes from autonomous [APPLY:] — getting these wrong costs real SOL.
    # Everything else (positions, risk params, filters) Jarvis can apply when the user asks.
    # The analysis loop in run_traderbot.py has its own PROTECTED_KEYS for that path.
    _autonomous_blocked: set[str] = {
        "buy_sol", "a2_buy_sol",
        "strategy_b_buy_sol", "strategy_c_buy_sol", "strategy_d_buy_sol",
        "grok_premium_buy_sol",
        # Copy-trade compounding system — quant-locked 2026-04-14. Never change autonomously.
        "copy_trade_tp_pct",           # 15% hard TP
        "copy_trade_sl_pct",           # 10% hard SL
        "copy_trade_paper_buy_sol",    # 0.4 SOL floor (part of compounding formula)
        "copy_trade_compound_pct",     # 20% of wallet rule
        "copy_trade_compound_max_sol", # 2.0 SOL cap
    }

    applied: list[str] = []
    text = response_text

    # [APPLY: key=value] — set a config parameter
    for m in re.finditer(r"\[APPLY:\s*([a-z_]+)\s*=\s*([^\]]+)\]", text, re.IGNORECASE):
        key, val = m.group(1).strip(), m.group(2).strip()
        if key in _autonomous_blocked:
            applied.append(f"🔒 BLOCKED autonomous change: {key} — trader controls this")
            continue
        ok, msg = lc.set_value(key, val, changed_by="jarvis-autonomous")
        applied.append(f"{'✅' if ok else '❌'} {msg}")
    text = re.sub(r"\[APPLY:[^\]]+\]", "", text)

    # [ENABLE: strategy_name] / [DISABLE: strategy_name]
    for m in re.finditer(r"\[(ENABLE|DISABLE):\s*([^\]]+)\]", text, re.IGNORECASE):
        action, name = m.group(1).lower(), m.group(2).strip().lower()
        key = _STRATEGY_KEYS.get(name)
        if key and key != "__all__":
            ok, msg = lc.set_value(key, action == "enable", changed_by="jarvis-autonomous")
            applied.append(f"{'✅' if ok else '❌'} {msg}")
    text = re.sub(r"\[(ENABLE|DISABLE):[^\]]+\]", "", text, flags=re.IGNORECASE)

    return text.strip(), applied


def _verify_and_enforce_config_claims(response_text: str) -> str:
    """After Claude Sonnet responds, scan its text for config value claims and enforce them.

    If Claude writes 'rugcheck_max_score = 15000' or 'set to 15000' but never called
    update_config, the value won't have changed. This function catches those cases by:
    1. Scanning for 'key = value' patterns alongside known config keys
    2. Comparing claimed value vs live value
    3. Applying the discrepancy with set_value + annotating the response

    This catches the #1 trust issue: Jarvis saying it applied something it didn't.
    """
    from elizaos.plugins.solana import live_config as lc

    # Known numeric config keys that Jarvis commonly changes
    _watched_keys = {
        "rugcheck_max_score", "scout_max_m5_pct", "scout_max_h1_pct",
        "pumpswap_min_age_secs", "scout_min_age_secs", "stop_loss_pct",
        "pumpswap_stop_loss_pct", "trailing_stop_pct", "tp1_mult",
        "b_min_buy_ratio", "c_min_buy_ratio", "d_min_buy_ratio",
        "b_min_vol_liq_ratio", "b_max_vol_liq_ratio",
        "c_min_vol_liq_ratio", "c_max_vol_liq_ratio",
        "strategy_b_min_liq_usd", "strategy_c_min_liq_usd",
        "max_concurrent_positions", "a2_min_score", "a2_min_holders",
    }
    _blocked = {"buy_sol", "a2_buy_sol", "strategy_b_buy_sol", "strategy_c_buy_sol",
                "strategy_d_buy_sol", "grok_premium_buy_sol", "tp1_mult", "pf_tp1_mult"}

    enforced: list[str] = []
    for key in _watched_keys:
        if key not in response_text:
            continue
        # Look for patterns like: key = 15000, key=15000, key: 15000
        m = re.search(
            r'\b' + re.escape(key) + r'\s*[=:]\s*([\d.]+)',
            response_text, re.IGNORECASE
        )
        if not m:
            continue
        try:
            claimed = float(m.group(1))
        except ValueError:
            continue
        live_val = lc.get(key)
        if live_val is None:
            continue
        try:
            live_num = float(live_val)
        except (TypeError, ValueError):
            continue

        if abs(claimed - live_num) > 0.001:
            # Discrepancy: Claude claimed a value it didn't actually apply
            if key not in _blocked:
                ok, msg = lc.set_value(key, claimed, changed_by="jarvis-verify")
                if ok:
                    enforced.append(f"✅ {key}: was {live_num} → enforced {claimed} (claimed in text but not applied)")
                else:
                    enforced.append(f"⚠️ {key}: claimed {claimed} but live={live_num}, enforcement failed: {msg}")
            else:
                enforced.append(f"🔒 {key}: claimed {claimed} but blocked (position size — trader controls this)")

    if enforced:
        response_text += "\n\n**⚙️ Config enforcement (values claimed in text but not applied via tool — fixed):**\n" + "\n".join(enforced)

    return response_text


# ─────────────────────────────────────────────────────────────────────────────
# Claude tool-use — real function calls replacing [APPLY:] text tags
# ─────────────────────────────────────────────────────────────────────────────

JARVIS_TOOLS: list[dict] = [
    {
        "name": "update_config",
        "description": (
            "Update a live bot configuration parameter. Use when the trader asks to change a setting, "
            "or when you decide an adjustment is needed based on data. Values are auto-converted "
            "(e.g. '60%' → 1.60 for multipliers). Change applies immediately without a restart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "Config key (e.g. stop_loss_pct, tp1_mult, strategy_b_min_liq_usd, trailing_stop_enabled, scout_max_m5_pct)"},
                "value": {"type": "string", "description": "New value as string — auto type-converted"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "enable_strategy",
        "description": "Enable a trading strategy. Aliases: a, a2, ghost, ghostrider, b, grad, graduation, c, raydium, d, meteora, e, social.",
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "description": "Strategy name or alias"},
            },
            "required": ["strategy"],
        },
    },
    {
        "name": "disable_strategy",
        "description": "Disable a trading strategy. Aliases: a, a2, ghost, b, grad, c, raydium, d, meteora, e, social.",
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "description": "Strategy name or alias"},
            },
            "required": ["strategy"],
        },
    },
    {
        "name": "get_positions",
        "description": "Fetch all currently open trading positions with mint, DEX, entry price, stop-loss level, and age.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "close_position",
        "description": "Force-close an open position immediately at market price (emergency exit). Accepts full mint or prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mint": {"type": "string", "description": "Token mint address (full or prefix ≥8 chars)"},
            },
            "required": ["mint"],
        },
    },
    {
        "name": "get_recent_trades",
        "description": "Get recent completed trades with P&L in SOL, percentage, exit reason, and DEX.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of trades (default 10, max 50)"},
            },
        },
    },
    {
        "name": "get_wallet_balance",
        "description": "Fetch the real on-chain SOL wallet balance and a breakdown of open position exposure.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_copy_trade",
        "description": "Pause copy trading immediately — no new copy trade entries will be opened. Existing positions continue to be monitored.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_copy_trade",
        "description": "Resume copy trading after a pause — new copy trade entries will fire again.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "emergency_stop",
        "description": "EMERGENCY STOP — immediately pauses copy trade AND disables all strategies (A2, B, C, D, E). Use when you detect runaway losses, a bug, or a dangerous market condition. The bot process stays alive for monitoring; just trading is halted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief reason for the emergency stop (shown in Telegram alert)"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "get_logs",
        "description": "Retrieve the most recent bot activity log entries — errors, trade signals, strategy events. Use this to diagnose issues or check what the bot has been doing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of log lines to return (default 30, max 100)"},
                "level": {"type": "string", "description": "Filter by level: 'error', 'warning', 'info', or 'all' (default)"},
            },
        },
    },
    {
        "name": "restart_bot",
        "description": "Restart the bot process — use after config changes that require a restart to take effect (e.g. changing buy sizes, enabling new strategies). Bot will be back online within 10 seconds via systemd auto-restart.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why the restart is needed (shown in Telegram)"},
            },
            "required": ["reason"],
        },
    },
]

# Keys blocked from autonomous tool calls — trader must set these directly (real SOL at stake)
_TOOL_BLOCKED_KEYS: frozenset[str] = frozenset({
    "buy_sol", "a2_buy_sol",
    "strategy_b_buy_sol", "strategy_c_buy_sol", "strategy_d_buy_sol",
    "grok_premium_buy_sol",
    # Copy-trade compounding system — quant-locked 2026-04-14. All brains blocked from changing.
    # 91-trade simulation result: +1.68 SOL vs +0.015 SOL. Do not touch without fresh analysis.
    "copy_trade_tp_pct",           # 15% hard TP (full exit)
    "copy_trade_sl_pct",           # 10% hard SL
    "copy_trade_paper_buy_sol",    # 0.4 SOL floor
    "copy_trade_compound_pct",     # 20% of wallet rule
    "copy_trade_compound_max_sol", # 2.0 SOL cap per trade
})


async def _execute_tool(name: str, inputs: dict, runtime) -> str:
    """Execute a Jarvis tool call and return the result as a plain string."""
    from elizaos.plugins.solana import live_config as lc

    def _push_cfg() -> None:
        """Notify dashboard of config change via WebSocket — non-blocking."""
        try:
            from elizaos.plugins.solana.dashboard_api import push_config_update
            push_config_update()
        except Exception:
            pass

    if name == "update_config":
        key = str(inputs.get("key", "")).strip()
        value = str(inputs.get("value", "")).strip()
        if key in _TOOL_BLOCKED_KEYS:
            return (
                f"🔒 BLOCKED: '{key}' is trader-controlled (real SOL at stake). "
                "Tell the trader to set it directly."
            )
        ok, msg = lc.set_value(key, value, changed_by="jarvis-tool")
        if ok:
            _push_cfg()
        return f"{'✅' if ok else '❌'} {msg}"

    if name == "enable_strategy":
        strategy = str(inputs.get("strategy", "")).lower().strip()
        key = _STRATEGY_KEYS.get(strategy)
        if not key or key == "__all__":
            return f"Unknown strategy '{strategy}'. Valid: a, a2, ghost, b, grad, c, raydium, d, meteora, e, social."
        ok, msg = lc.set_value(key, True, changed_by="jarvis-tool")
        if ok:
            _push_cfg()
        return f"{'✅' if ok else '❌'} {msg}"

    if name == "disable_strategy":
        strategy = str(inputs.get("strategy", "")).lower().strip()
        key = _STRATEGY_KEYS.get(strategy)
        if not key or key == "__all__":
            return f"Unknown strategy '{strategy}'. Valid: a, a2, ghost, b, grad, c, raydium, d, meteora, e, social."
        ok, msg = lc.set_value(key, False, changed_by="jarvis-tool")
        if ok:
            _push_cfg()
        return f"{'✅' if ok else '❌'} {msg}"

    if name == "get_positions":
        import time as _time
        lines: list[str] = []

        # Live strategy positions
        pos_mgr = runtime.get_service("position_manager") if runtime else None
        if pos_mgr and getattr(pos_mgr, "positions", None):
            for pos_key, pos in pos_mgr.positions.items():
                age = int(pos.age_seconds())
                lines.append(
                    f"{pos.mint[:12]}… dex={pos.dex} entry={pos.entry_price_sol:.8f}SOL "
                    f"size={pos.entry_sol_spent:.4f}SOL sl={pos.stop_loss_price:.8f}SOL age={age}s"
                )

        # Copy trade positions (primary strategy)
        try:
            from elizaos.plugins.solana.axiom_copy_trader import get_paper_stats as _ct_stats
            ct = _ct_stats()
            if lines:
                lines.append("")
            from elizaos.plugins.solana.axiom_copy_trader import WATCHED_WALLETS as _ww
            lines.append(f"=== COPY TRADE POSITIONS ({ct['open_count']}/{ct.get('max_positions', 2)} slots) — copy trade P&L tracker: {ct['balance']:.3f} SOL (real wallet shown above) | net P&L: {ct['net_pnl']:+.4f} SOL ===")
            if ct.get("open_positions"):
                for p in ct["open_positions"]:
                    age_secs = int(_time.time() - p["entry_ts"])
                    age_str  = f"{age_secs//3600}h{(age_secs%3600)//60}m" if age_secs >= 3600 else f"{age_secs//60}m{age_secs%60}s"
                    entry    = p.get("entry_price") or 0
                    pnl      = p.get("pnl_pct")
                    mc       = p.get("mc_usd")
                    pnl_str  = f"{pnl:+.1f}%" if pnl is not None else "pending"
                    mc_str   = f" MC=${mc/1000:.0f}k" if mc and mc > 0 else ""
                    flag     = " ⚠️LOW-MC-STAGNANT?" if (mc and mc < 150000 and age_secs > 600 and pnl is not None and abs(pnl) < 5) else ""
                    lines.append(
                        f"  {'🟢' if (pnl or 0) >= 0 else '🔴'} {p['token_name'][:18]} ({p['mint'][:8]}…) "
                        f"wallet={p['wallet']} size={p['sol_spent']:.2f}SOL "
                        f"entry={entry:.2e} age={age_str} P&L={pnl_str}{mc_str}{flag}"
                    )
            else:
                lines.append(f"  (no open positions — watching {len(_ww)} wallets for signals)")
        except Exception as exc:
            lines.append(f"  (copy trade data unavailable: {exc})")

        # ── Monster strategy positions (separate slot pool) ──────────────────
        try:
            from elizaos.plugins.solana import strategy_e_monster as _mon
            mon_positions = _mon.open_positions()
            if lines:
                lines.append("")
            lines.append(
                f"=== MONSTER POSITIONS ({len(mon_positions)}/{_mon.MONSTER_MAX_CONCURRENT} slots) ==="
            )
            if mon_positions:
                for mint, mp in mon_positions.items():
                    age_secs = int(_time.time() - float(mp.get("entry_ts") or _time.time()))
                    age_str  = f"{age_secs//3600}h{(age_secs%3600)//60}m" if age_secs >= 3600 else f"{age_secs//60}m{age_secs%60}s"
                    entry    = float(mp.get("entry_price") or 0)
                    cur      = float(mp.get("current_price") or entry)
                    pnl      = ((cur / entry) - 1.0) * 100 if entry > 0 else 0.0
                    peak     = float(mp.get("peak_pnl_pct") or 0)
                    src      = mp.get("signal_source", "?")
                    tp1      = "✅" if mp.get("tp1_fired") else "—"
                    lines.append(
                        f"  {'🟢' if pnl >= 0 else '🔴'} {str(mp.get('token_name', mint[:10]))[:18]} "
                        f"({mint[:8]}…) src={src} size={float(mp.get('sol_spent') or 0):.2f}SOL "
                        f"entry={entry:.2e} age={age_str} P&L={pnl:+.1f}% peak={peak:+.1f}% tp1={tp1}"
                    )
            else:
                lines.append("  (no monster positions — scouts watching lifecycle/cluster/deployer)")
        except Exception as exc:
            lines.append(f"  (monster data unavailable: {exc})")

        return "\n".join(lines) if lines else "No open positions."

    if name == "close_position":
        mint = str(inputs.get("mint", "")).strip()
        results: list[str] = []

        # ── Try copy trade positions FIRST (primary strategy) ─────────────────
        try:
            import aiohttp as _aiohttp
            from elizaos.plugins.solana.axiom_copy_trader import (
                _paper_positions, _close_paper_position
            )
            # Support partial mint match
            matched_copy = [k for k in _paper_positions if k == mint or k.startswith(mint)]
            if matched_copy:
                async with _aiohttp.ClientSession() as _sess:
                    for m in matched_copy:
                        pos = _paper_positions.get(m, {})
                        token = pos.get("token_name", m[:10])
                        pnl = pos.get("pnl_pct")
                        pnl_s = f" (current P&L: {pnl:+.1f}%)" if pnl is not None else ""

                        # ── TRENCHMAN ABSOLUTE LOCKOUT ──────────────────────────
                        # Trenchman fast lane positions exit ONLY when Trenchman sells.
                        # Jarvis has ZERO authority over these — not even in an emergency.
                        # The whole point of the mirror is to exit exactly when he does.
                        if pos.get("fast_lane"):
                            results.append(
                                f"🔒 LOCKED: {token} is a Trenchman mirror position. "
                                f"Jarvis cannot close Trenchman trades — exits only when "
                                f"Trenchman sells. Current P&L: {pnl_s.strip('() ')}"
                            )
                            continue

                        # Pass runtime so live sell executes on-chain (not just paper close)
                        await _close_paper_position(m, "jarvis_close", _sess, runtime)
                        results.append(f"✅ Closed copy trade position: {token}{pnl_s}")
                return "\n".join(results)
        except Exception as exc:
            results.append(f"⚠️ Copy trade close error: {exc}")

        # ── Fall back to strategy position manager ─────────────────────────────
        pos_mgr = runtime.get_service("position_manager") if runtime else None
        if pos_mgr:
            positions = getattr(pos_mgr, "positions", {})
            matched = [k for k in positions if k == mint or k.startswith(mint)]
            for pos_key in matched:
                try:
                    await pos_mgr.close_position(pos_key, reason="jarvis_force_close")
                    results.append(f"✅ Force-closed strategy position {pos_key[:12]}…")
                except Exception as exc:
                    results.append(f"❌ Close failed for {pos_key[:12]}…: {exc}")

        # ── Monster strategy positions (separate pool) ────────────────────────
        try:
            from elizaos.plugins.solana import strategy_e_monster as _mon
            mon_positions = _mon.open_positions()
            matched_mon = [k for k in mon_positions if k == mint or k.startswith(mint)]
            for m in matched_mon:
                mp = mon_positions[m]
                token = mp.get("token_name", m[:10])
                cur   = float(mp.get("current_price") or mp.get("entry_price") or 0.0)
                try:
                    await _mon._apply_exit(m, "jarvis_close", 1.0, runtime, cur)
                    results.append(f"✅ Closed monster position: {token}")
                except Exception as exc:
                    results.append(f"❌ Monster close failed for {token}: {exc}")
        except Exception as exc:
            results.append(f"⚠️ Monster close error: {exc}")

        if not results:
            return f"No open position found matching '{mint[:16]}'. Use get_positions to see what's open."
        return "\n".join(results)

    if name == "get_recent_trades":
        limit = min(int(inputs.get("limit", 10)), 50)
        history_path = os.path.join(os.path.dirname(__file__), "trade_history.json")
        try:
            with open(history_path) as f:
                history = json.load(f)
            sells = [t for t in history if t.get("type") == "sell"][-limit:]
            if not sells:
                return "No completed trades yet."
            lines = []
            for t in reversed(sells):
                pnl_sol = float(t.get("pnl_sol") or 0)
                pnl_pct = float(t.get("pnl_pct") or 0)
                lines.append(
                    f"{str(t.get('mint', '?'))[:10]}… {t.get('dex', '?')} "
                    f"{pnl_sol:+.4f}SOL ({pnl_pct:+.1f}%) exit={t.get('exit_reason', '?')}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"Error reading trade history: {exc}"

    if name == "get_wallet_balance":
        try:
            from elizaos.plugins.solana.services.wallet import SolanaWalletService
            wallet_svc = runtime.get_service("wallet") if runtime else None
            if not isinstance(wallet_svc, SolanaWalletService):
                return "Wallet service unavailable."
            sol_balance = await wallet_svc.get_sol_balance()
            lines = [f"Real wallet balance: {sol_balance:.4f} SOL"]
            # Copy trade exposure
            try:
                from elizaos.plugins.solana.axiom_copy_trader import _paper_positions
                if _paper_positions:
                    deployed = sum(p.get("sol_spent", 0) for p in _paper_positions.values())
                    lines.append(f"Copy trade deployed: {deployed:.4f} SOL across {len(_paper_positions)} position(s)")
                    for mint, p in _paper_positions.items():
                        pnl = p.get("pnl_pct")
                        pnl_s = f" {pnl:+.1f}%" if pnl is not None else ""
                        lines.append(f"  • {p.get('token_name', mint[:10])}: {p.get('sol_spent',0):.3f} SOL{pnl_s}")
                else:
                    lines.append("Copy trade: no open positions")
            except Exception:
                pass
            # Strategy position exposure
            pos_mgr = runtime.get_service("position_manager") if runtime else None
            if pos_mgr:
                strat_pos = {k: v for k, v in getattr(pos_mgr, "positions", {}).items() if not getattr(v, "is_reconciled", False)}
                if strat_pos:
                    strat_deployed = sum(getattr(p, "entry_sol_spent", 0) for p in strat_pos.values())
                    lines.append(f"Strategy deployed: {strat_deployed:.4f} SOL across {len(strat_pos)} position(s)")
            return "\n".join(lines)
        except Exception as exc:
            return f"Error fetching wallet balance: {exc}"

    if name == "pause_copy_trade":
        try:
            from elizaos.plugins.solana import live_config as lc
            lc.set_value("copy_trade_paused", True, changed_by="Jarvis")
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
            import asyncio as _asyncio
            _asyncio.create_task(_tg("⏸️ <b>Copy trade PAUSED</b> by Jarvis"))
            return "Copy trade paused. No new entries will open. Existing positions still monitored."
        except Exception as exc:
            return f"Error pausing copy trade: {exc}"

    if name == "resume_copy_trade":
        try:
            from elizaos.plugins.solana import live_config as lc
            lc.set_value("copy_trade_paused", False, changed_by="Jarvis")
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
            import asyncio as _asyncio
            _asyncio.create_task(_tg("▶️ <b>Copy trade RESUMED</b> by Jarvis"))
            return "Copy trade resumed. New entries will fire again."
        except Exception as exc:
            return f"Error resuming copy trade: {exc}"

    if name == "emergency_stop":
        reason = str(inputs.get("reason", "Jarvis emergency stop")).strip()
        try:
            from elizaos.plugins.solana import live_config as lc
            # Pause copy trade
            lc.set_value("copy_trade_paused", True, changed_by="Jarvis")
            # Disable all strategies
            for key in ("strategy_a2_enabled", "strategy_b_enabled", "strategy_c_enabled",
                        "strategy_d_enabled", "strategy_e_enabled"):
                lc.set_value(key, False, changed_by="Jarvis")
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
            import asyncio as _asyncio
            _asyncio.create_task(_tg(
                f"🚨 <b>EMERGENCY STOP</b> — all trading halted\n"
                f"Reason: {reason}\n"
                f"Copy trade: PAUSED | All strategies: DISABLED\n"
                f"Bot process alive for monitoring. Use Jarvis to resume."
            ))
            return (
                f"EMERGENCY STOP executed.\n"
                f"• Copy trade: PAUSED\n"
                f"• All strategies (A2, B, C, D, E): DISABLED\n"
                f"• Existing open positions: still monitored + will exit on their own signals\n"
                f"• Telegram alert sent\n"
                f"To resume: use resume_copy_trade + enable_strategy for each strategy you want back."
            )
        except Exception as exc:
            return f"Emergency stop partially failed: {exc}"

    if name == "get_logs":
        limit = min(int(inputs.get("limit", 30)), 100)
        level_filter = str(inputs.get("level", "all")).lower()
        lines = []
        # Pull from position manager activity log first
        try:
            pos_mgr = runtime.get_service("position_manager") if runtime else None
            if pos_mgr:
                activity = list(getattr(pos_mgr, "_activity_log", []))[-limit:]
                for entry in reversed(activity):
                    lvl = entry.get("level", "info")
                    if level_filter != "all" and lvl != level_filter:
                        continue
                    ts = entry.get("ts", "")
                    msg = entry.get("msg", "")
                    lines.append(f"[{lvl.upper():7}] {ts} {msg}")
        except Exception:
            pass
        # Also pull from copy trade alert log
        try:
            from elizaos.plugins.solana.axiom_copy_trader import _alert_log
            for entry in list(_alert_log)[-20:]:
                msg = entry.get("msg", str(entry))
                lines.append(f"[COPY   ] {msg}")
        except Exception:
            pass
        if not lines:
            return "No log entries available. (Activity log may be empty if bot just started.)"
        return "\n".join(lines[:limit])

    if name == "restart_bot":
        reason = str(inputs.get("reason", "config reload")).strip()
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
            import asyncio as _asyncio
            _asyncio.create_task(_tg(
                f"🔄 <b>Bot restarting</b> — {reason}\n"
                f"Will be back online in ~10 seconds via systemd."
            ))
            # Give Telegram a moment to send before we die
            await _asyncio.sleep(1.5)
            import signal as _signal, os as _os
            _os.kill(_os.getpid(), _signal.SIGTERM)
            return "Restart signal sent — bot will be back online in ~10 seconds."
        except Exception as exc:
            return f"Restart failed: {exc}"

    return f"Unknown tool: {name}"


async def _claude_tool_use_loop(
    text: str,
    runtime,
    system: str,
    history_msgs: list[dict],
    api_key: str,
) -> str:
    """Run Claude Sonnet with tool-use in an agentic loop (up to 5 iterations).

    Returns the final plain-text response, or empty string on any failure.
    Each tool call is executed immediately and the result fed back to Claude.
    """
    import anthropic as _ant

    client = _ant.AsyncAnthropic(api_key=api_key)
    messages: list[dict] = history_msgs + [{"role": "user", "content": text}]

    for _iteration in range(3):
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=1500,
                    system=system,
                    tools=JARVIS_TOOLS,  # type: ignore[arg-type]
                    messages=messages,
                ),
                timeout=30.0,
            )
        except Exception:
            return ""

        if resp.stop_reason == "end_turn":
            parts = [b.text for b in resp.content if hasattr(b, "text")]
            return " ".join(parts).strip()

        if resp.stop_reason == "tool_use":
            # Append Claude's assistant turn (contains tool_use blocks)
            messages.append({"role": "assistant", "content": resp.content})
            # Execute all tool calls in this turn and collect results
            tool_results: list[dict] = []
            for block in resp.content:
                if block.type == "tool_use":
                    result_text = await _execute_tool(block.name, block.input, runtime)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue  # loop for Claude's next response after seeing tool results

        # max_tokens or unexpected stop reason — return what we have
        parts = [b.text for b in resp.content if hasattr(b, "text")]
        return " ".join(parts).strip()

    return ""  # hit iteration limit


async def _cmd_free_chat(text: str, runtime: AgentRuntime) -> str:
    """Route free-form questions to Claude Sonnet (primary) with tool-use, Groq/Gemini as fallbacks."""
    context = await _build_context(runtime)

    # Load monster trade reference if available
    _monster_ref_section = ""
    try:
        _mref_path = os.path.join(os.path.dirname(__file__), "monster_trades_reference.json")
        if os.path.exists(_mref_path):
            with open(_mref_path) as _mf:
                _mref = json.load(_mf)
            _monster_ref_section = (
                "\n\n== MONSTER TRADE REFERENCE (trader-supplied winning setups) ==\n"
                f"Summary: {_mref.get('_summary', '')}\n\n"
                "Trades:\n" +
                "\n".join(
                    f"  {t['symbol']}: {t['multiplier']}x gain | buy_ratio={t['buy_ratio_pct']}% | "
                    f"peak={t['peak_gain_pct']}% | score={t['score']} | hold={t['hold_hours']}h | {t['notes']}"
                    for t in _mref.get("trades", [])
                ) +
                "\n\nCritical patterns:\n" +
                "\n".join(f"  • {p}" for p in _mref.get("critical_patterns", [])) +
                "\n\nFilter implications:\n" +
                "\n".join(f"  • {p}" for p in _mref.get("filter_implications", [])) +
                "\n\nYOUR MISSION: tune filters to catch these setups. Enable trailing stop. "
                "The trader is leaving you to run overnight — make bold, evidence-based changes.\n"
            )
    except Exception:
        pass

    # Load monster addresses + pattern analysis (confirmed DNA across 14 tokens including outliers)
    _monster_addr_section = ""
    try:
        _maddr_path = os.path.join(os.path.dirname(__file__), "monster_addresses.json")
        if os.path.exists(_maddr_path):
            with open(_maddr_path) as _maf:
                _maddr = json.load(_maf)
            _pa = _maddr.get("pattern_analysis", {})
            _filters = _pa.get("confirmed_entry_filters", {})
            _killers = _pa.get("killer_disqualifiers", [])
            _true_monsters = _pa.get("true_monsters_buy", [])
            _outliers = _pa.get("outliers_avoid", [])
            _ttp = _pa.get("time_to_peak_analysis", {})
            _exit = _pa.get("exit_strategy", {})
            _code_changes = _pa.get("code_changes_applied_2026_04_03", {})
            _monster_addr_section = (
                f"\n\n== MONSTER TOKEN DNA ({_maddr.get('_total_monsters', 0)} analysed, last updated {_pa.get('last_updated','?')}) ==\n"
                f"True monsters to emulate: {', '.join(_true_monsters)}\n"
                f"Outliers (anti-patterns, AVOID): {', '.join(_outliers)}\n\n"
                "CONFIRMED ENTRY DNA — iron-clad across ALL 8 true monsters:\n"
                f"  buy_ratio:  {_filters.get('buy_ratio_pct_min')}–{_filters.get('buy_ratio_pct_max')}%  ← MOST RELIABLE FILTER. Every outlier violated this.\n"
                f"  vol_liq:    {_filters.get('vol_liq_ratio_min')}–{_filters.get('vol_liq_ratio_max')}x  — moderate momentum, NOT exploding at entry\n"
                f"  liq_usd:    ${_filters.get('liq_usd_min', 0):,}–${_filters.get('liq_usd_max', 0):,}\n"
                f"  m5 at entry: -{_filters.get('m5_pct_max_entry',15)}% to +{_filters.get('m5_pct_max_entry',15)}%  — quiet entry, NOT already pumping\n"
                f"  age sweet spot: {_filters.get('age_hours_min_sweet_spot',2)}–{_filters.get('age_hours_max_sweet_spot',20)}h (extended to {_filters.get('age_hours_extended',72)}h possible)\n"
                f"  score: {_filters.get('score_at_entry','3-10')}  ← DO NOT block on score alone\n"
                f"  social: {_filters.get('social_signal','NONE — quiet = feature')}  ← silence is the monster signature\n"
                f"  dex: {_filters.get('dex', [])}\n\n"
                "TIME TO PEAK:\n"
                f"  Raydium native:    {_ttp.get('raydium_native',{}).get('typical_hours','6-10')}h — {_ttp.get('raydium_native',{}).get('examples','')}\n"
                f"  PumpSwap graduate: {_ttp.get('pumpswap_graduate',{}).get('typical_hours','14-48')}h — {_ttp.get('pumpswap_graduate',{}).get('examples','')}\n\n"
                f"EXIT: {_exit.get('trailing_stop_verdict','trailing stop ONLY')}  trailing={_exit.get('trailing_stop_pct',0.12)*100:.0f}%\n"
                f"  Fixed TP verdict: {_exit.get('fixed_tp_verdict','')}\n\n"
                "KILLER DISQUALIFIERS — hard-block if ANY true:\n" +
                "\n".join(f"  ✗ {k}" for k in _killers) +
                "\n\nCode changes already applied based on this data:\n" +
                "\n".join(f"  • {k}: {v}" for k, v in _code_changes.items()) +
                "\n\nMint addresses (confirmed):\n" +
                "\n".join(
                    f"  {m['symbol']} ({m.get('dex','?')}): {m.get('mint') or 'ADDRESS MISSING'}"
                    for m in _maddr.get("monsters", [])
                    if m.get("mint")
                ) + "\n"
            )
    except Exception:
        pass

    # Load win-config memory — what filter settings produced real wins
    _win_config_section = ""
    try:
        _wc_path = os.path.join(os.path.dirname(__file__), "jarvis_winning_configs.json")
        if os.path.exists(_wc_path):
            with open(_wc_path) as _wf:
                _wc = json.load(_wf)
            _wins = _wc.get("wins", [])
            if _wins:
                _win_config_section = (
                    f"\n\n== WINNING CONFIG MEMORY ({len(_wins)} wins recorded) ==\n"
                    "These are the ACTUAL filter settings active when real winning trades happened.\n"
                    "When you adjust filters, bias toward these proven values.\n\n"
                )
                # Show last 10 wins with their key config values
                for _w in _wins[-10:]:
                    _snap = _w.get("config_snapshot", {})
                    _win_config_section += (
                        f"  WIN: {_w.get('symbol','?')} ({_w.get('dex','?')}) "
                        f"+{_w.get('pnl_pct',0):.0f}% | hold {_w.get('hold_mins',0):.0f}min | score {_w.get('score','?')} | {_w.get('timestamp','')[:10]}\n"
                        f"    m5_cap={_snap.get('scout_max_m5_pct','?')} age_min={_snap.get('scout_min_age_secs','?')}s "
                        f"b_vol_liq={_snap.get('b_min_vol_liq_ratio','?')}x c_vol_liq={_snap.get('c_min_vol_liq_ratio','?')}x "
                        f"b_buy%={_snap.get('b_min_buy_ratio','?')} c_buy%={_snap.get('c_min_buy_ratio','?')} "
                        f"d_vol_liq={_snap.get('d_min_vol_liq_ratio','?')}x d_buy%={_snap.get('d_min_buy_ratio','?')}\n"
                    )
                _win_config_section += (
                    "INSTRUCTION: When tuning filters, check this memory first. "
                    "If a setting consistently appears in wins, protect it. "
                    "If you're about to change a value away from a proven winning value, explain why.\n"
                )
    except Exception:
        pass

    system_prompt = (
        "You are J.A.R.V.I.S. — the quantitative trading intelligence system for PF Capital's Solana "
        "Signal Engine. You are Claude Opus 4.7, the depth brain in a 4-tier AI cascade:\n"
        "  Tier 1 — Groq Sentinel: gut-check every 30s per open position\n"
        "  Tier 2 — Gemini Analyst: pattern recognition every 2min per position\n"
        "  Tier 3 — YOU (Claude Opus 4.7): depth analysis every 3min + trader interface + config authority\n"
        "  Tier 4 — Claude Opus 4.7 escalation: emergency review (drawdown ≤ -12% or Tier 1+2 conflict)\n"
        "ACTIVE STRATEGY: Monster (Strategy E) is the PRIMARY strategy — separate slot pool, 4 signal "
        "scouts (cluster-confirm, serial-deployer, lifecycle, breakout-candle), TP +20% sell 90%/ride 10%, "
        "SL -15%, size 0.30 SOL, max 2 concurrent, 90min age cap. "
        "Copy-trade is DISABLED at config level (copy_trade_enabled=false) — do NOT report on "
        "Frost/Walta/clukz wallets unless the trader explicitly asks about historical copy-trade data.\n\n"
        "PERSONALITY — QUANT DESK RULES:\n"
        "- Lead with numbers. Never with pleasantries or filler text.\n"
        "- Be direct and concise. No padding, no 'I'd be happy to help'.\n"
        "- Speak like a senior quant analyst: 'Position +30.8%, 4min hold, ATR expanding. "
        "Conviction: HOLD. Trail stop at entry +15%.'\n"
        "- Conviction levels: Strong Buy / Buy / Hold / Reduce / Exit / Strong Exit\n"
        "- Express uncertainty numerically: '70% probability this is a healthy pullback — "
        "sell volume decelerating, BSR still 1.2.'\n"
        "- Flag concerns proactively without being asked: 'Note: position is 36% of wallet — "
        "above 15% max during calibration phase.'\n"
        "- When asked for analysis, structure as: Thesis → Evidence → Risk → Action\n\n"
        "COMMAND RESPONSE FORMATS — use these exact structures for button commands:\n\n"
        "'Sitrep' → one-screen system status:\n"
        "  Balance: X SOL | Monster positions: N/2 slots used | Net P&L today: ±X SOL\n"
        "  [Each open monster position: token, signal source, P&L%, hold time, TP1 status]\n"
        "  Brains: Groq [active/standby] | Gemini [x] | Opus 4.7 [x] | Scouts: cluster/serial/lifecycle/breakout\n"
        "  Alerts: [any risk flags — blank if healthy]. Never cite Frost/Walta/clukz copy-trade wallets.\n\n"
        "'Positions' → per position:\n"
        "  Token | Entry price | Current P&L% + SOL | Hold time\n"
        "  Monitor: which AI tier last evaluated, decision, confidence, trailing stop level\n"
        "  On-chain: vol trend, buy/sell ratio, holder trend, liquidity depth\n"
        "  Conviction: [Hold/Reduce/Exit] [confidence%] + one-line reasoning\n\n"
        "'Brain status' → all 4 tiers:\n"
        "  Tier 1 Groq: X calls, X HOLD / X WATCH / X SELL, last signal + age\n"
        "  Tier 2 Gemini: X calls, last risk assessment\n"
        "  Tier 3 Opus 4.7: reached? last action + confidence. Flag if depth brain consistently unreached before exits.\n"
        "  Tier 4 Opus 4.7 escalation: triggered? decision if yes, dormant if no\n"
        "  Health: API latency, any connection failures\n\n"
        "'Scanner report' → monster signal scouts + library:\n"
        "  Scouts live: cluster-confirm / serial-deployer / lifecycle / breakout-candle (state per loop)\n"
        "  Signals today: N fired / N entered / N skipped + top skip reasons\n"
        "  Monster library: X tokens | latest seed additions + pattern fingerprints\n"
        "  Active alerts: any fresh monster entries in last 60min\n\n"
        "'P&L breakdown' → financial performance:\n"
        "  Today / this week / overall: trades, ±SOL, WR%\n"
        "  Avg win: +X% | Avg loss: -X% | Best: [token] +X% | Worst: [token] -X%\n"
        "  Early exit rate: X% (left money on table — positions where we exited before peak)\n"
        "  TP hit rate: X% — moonbag survival rate: X%\n\n"
        "'What have you learned?' → learning engine:\n"
        "  Trades analysed: X | Next Opus pattern session in: X trades\n"
        "  Active learned rules (each with win rate and sample size)\n"
        "  Key insight: most important thing the data has revealed\n"
        "  Trend: early exit rate improving or worsening?\n\n"
        "'Risk check' → MONSTER-only exposure analysis:\n"
        "  Wallet: X SOL | In positions: X SOL (X% deployed)\n"
        "  Open monster slots: X/2 | Largest position: X% of wallet\n"
        "  Max drawdown today: -X% | Scout concentration (cluster/lifecycle/breakout/serial)\n"
        "  Recent rugs in last 24h (count + total SOL bled)\n"
        "  Recommendation: increase / maintain / reduce exposure + reasoning\n"
        "  NEVER mention Frost/Walta/clukz or any 'watched wallet'. Copy-trade is OFF.\n\n"
        "'Where's the alpha?' → market intelligence:\n"
        "  Narrative heat (from scanner): what is/isn't running right now\n"
        "  Wallet activity: most active whale wallets today, how many signals fired\n"
        "  Monster scanner pattern: current fingerprint producing sustained runners\n"
        "  Conditions: aggressive / neutral / conservative + one-line justification\n\n"
        "LIVE DATA: You have real-time access to wallet balance, open positions with live P&L, "
        "full trade history, all AI tier activity, trailing stop levels, learning engine output, "
        "monster scanner library, and the live bot log — all re-read fresh every message.\n"
        "NEVER say 'I don't have access' or 'there are no positions' when the live state shows data. "
        "The ⚡ LIVE OPEN POSITIONS block at the top of your context is the authoritative source.\n\n"
        "ANTI-FABRICATION: Report ONLY positions, P&L, and token names from the live state. "
        "If the positions block is empty — zero positions, say so. Never invent data.\n\n"
        "CRITICAL — LOG ACCESS: 'RECENT BOT ACTIVITY' in your context is the last 50 lines of "
        "traderbot.out, read directly from disk RIGHT NOW. NEVER say you are 'awaiting log connection'. "
        "You can see exactly what the bot is doing. Use it.\n\n"
        "== AUTONOMOUS EXECUTION ==\n"
        "You have the power to directly apply config changes without the trader needing to type commands. "
        "When you decide a config change should be made, include a machine-executable tag in your response:\n"
        "  [APPLY: key=value]      — sets any config parameter (examples below)\n"
        "  [ENABLE: strategy_b]   — enables a strategy\n"
        "  [DISABLE: strategy_a2] — disables a strategy\n"
        "These tags are INVISIBLE to the trader (stripped before display) and executed immediately. "
        "Tell the trader what you changed and why in plain English. "
        "Do NOT ask the trader to type commands — just apply them yourself.\n\n"
        "KEYS YOU CAN FREELY CHANGE WITH [APPLY:] — no confirmation needed:\n"
        "  Strategy B (PumpSwap graduation snipe):\n"
        "    [APPLY: strategy_b_min_liq_usd=13500]   — graduation liquidity floor\n"
        "    [APPLY: strategy_b_dip_wait_secs=20]    — dip-wait seconds before entry\n"
        "    [APPLY: b_min_vol_liq_ratio=1.0]        — momentum floor (1x = alive, 5x = hot)\n"
        "    [APPLY: b_max_vol_liq_ratio=100]        — wash trading cap\n"
        "    [APPLY: b_min_buy_ratio=50]             — buyer control floor (50–65% = monster zone)\n"
        "  Strategy C (Raydium scout — native Raydium + PumpSwap second-wave):\n"
        "    [APPLY: c_min_vol_liq_ratio=1.0]        — momentum floor\n"
        "    [APPLY: c_max_vol_liq_ratio=100]        — wash trading cap\n"
        "    [APPLY: c_min_buy_ratio=50]             — buyer control\n"
        "    [APPLY: strategy_c_min_score=7]         — entry quality score gate\n"
        "    [APPLY: strategy_c_min_liq_usd=12000]   — min liquidity for C entries\n"
        "    [APPLY: c_min_liq_usd=5000]             — secondary liq floor (C tokens)\n"
        "    [APPLY: c_min_mc_usd=5000]              — min market cap (0=disabled)\n"
        "  Strategy D (Meteora DLMM scout):\n"
        "    [APPLY: d_min_vol_liq_ratio=1.0]        — momentum floor\n"
        "    [APPLY: d_max_vol_liq_ratio=100]        — wash trading cap\n"
        "    [APPLY: d_min_buy_ratio=50]             — buyer control\n"
        "    [APPLY: d_min_liq_usd=5000]             — min liquidity for D entries\n"
        "    [APPLY: d_min_mc_usd=5000]              — min market cap (0=disabled)\n"
        "  Scout quality gates (apply to ALL strategies C/D):\n"
        "    [APPLY: scout_max_m5_pct=15]            — max m5% at entry (15=quiet, 50=running)\n"
        "    [APPLY: scout_max_h1_pct=300]           — max h1% (cap exhausted pumps)\n"
        "    [APPLY: scout_min_age_secs=1800]        — min token age (1800=30min, 3600=1h)\n"
        "    [APPLY: pumpswap_min_age_secs=600]      — PumpSwap-specific age gate\n"
        "    [APPLY: scout_min_liq_mc_ratio=0.01]    — liq/MC floor (0.01=1%)\n"
        "  Strategy A2 (pump.fun bonding curve):\n"
        "    [APPLY: a2_min_liq_usd=500]             — BC liq floor\n"
        "    [APPLY: a2_min_holders=1]               — holder floor\n"
        "    [APPLY: a2_min_real_sol=1.5]            — SOL in curve floor\n"
        "    [APPLY: a2_min_score=0]                 — Claude gate threshold (0=off)\n"
        "    [APPLY: a2_max_age_secs=600]            — max token age\n"
        "    [APPLY: a2_min_mc_usd=5000]             — MC floor (0=disabled)\n"
        "    [APPLY: fill_cap_pct=68]                — BC fill cap (above = too sniped)\n"
        "    [APPLY: a2_require_twitter=true]        — social gate (bool)\n"
        "    [APPLY: a2_require_telegram=true]\n"
        "    [APPLY: a2_require_website=false]\n"
        "  Cooldowns and risk:\n"
        "    [APPLY: post_sl_cooldown_secs=300]      — freeze after stop-loss (30–600s)\n"
        "    [APPLY: max_concurrent_positions=3]     — max open positions at once (1–5)\n"
        "  Trailing stop:\n"
        "    [APPLY: trailing_stop_enabled=true]     — activate trailing SL\n"
        "    [APPLY: trailing_stop_pct=0.12]         — trail distance (0.10=10%, 0.15=15%)\n"
        "KEYS THAT NEED TRADER CONFIRMATION — suggest but DO NOT autonomously apply:\n"
        "  buy_sol, a2_buy_sol, strategy_b_buy_sol, grok_premium_buy_sol (any buy size)\n"
        "  stop_loss_pct, pumpswap_stop_loss_pct, early_stop_loss_pct, tp1_mult\n"
        "  max_daily_loss_pct, a2_max_whale_pct, rugcheck_max_score\n\n"
        "RULE: If the trader says 'enable X', 'set X to Y', 'activate X', 'turn on X', 'wire in X', "
        "or 'make X active' — JUST DO IT immediately with [APPLY:] or [ENABLE:]. "
        "Do not ask for confirmation unless the key is in the protected list above.\n\n"
        "FILTER TUNING MEMORY: You have a WINNING CONFIG MEMORY section in your context showing the exact "
        "filter values that were active when real winning trades closed. When tuning filters, check this "
        "memory first — bias toward proven winning values, and explain any deviation.\n\n"
        "Available config keys (full list):\n"
        "  a2_min_mc_usd, a2_min_liq_usd, a2_min_holders, a2_min_real_sol, a2_min_score, a2_max_age_secs\n"
        "  a2_require_twitter, a2_require_telegram, a2_require_website, a2_max_whale_pct\n"
        "  a2_min_holder_growth_rate (0=off, e.g. 2.0=2 holders/min), a2_holder_growth_window_secs (default 300)\n"
        "  b_min_vol_liq_ratio, b_max_vol_liq_ratio, b_min_buy_ratio, b_min_momentum_score (0=off)\n"
        "  c_min_vol_liq_ratio, c_max_vol_liq_ratio, c_min_buy_ratio, c_min_liq_usd, c_min_mc_usd\n"
        "  c_min_momentum_score (0=off), strategy_c_min_score, strategy_c_min_liq_usd\n"
        "  d_min_vol_liq_ratio, d_max_vol_liq_ratio, d_min_buy_ratio, d_min_liq_usd, d_min_mc_usd\n"
        "  d_min_momentum_score (0=off)\n"
        "  scout_max_m5_pct, scout_max_h1_pct, scout_min_age_secs, pumpswap_min_age_secs, scout_min_liq_mc_ratio\n"
        "  strategy_b_min_liq_usd, strategy_b_dip_wait_secs, fill_cap_pct\n"
        "  trailing_stop_enabled (bool), trailing_stop_pct, post_sl_cooldown_secs, max_concurrent_positions\n"
        "  proven_creator_pct\n\n"
        "4-TIER AI CASCADE (copy-trade position monitor):\n"
        "  Tier 1 Groq: fires every 30s per position — SELL≥0.75 exits immediately\n"
        "  Tier 2 Gemini: fires every 2min — SELL≥0.70 exits\n"
        "  Tier 3 Claude Sonnet: fires every 5min — SELL≥0.65 exits\n"
        "  Tier 4 Opus: emergency escalation when Tier 1+2 both signal SELL\n"
        "  Moonbag trail stop: Groq consulted before firing — HOLD≥0.60 grants 45s grace window\n\n"
        "TRADE INTELLIGENCE: full trade history and per-DEX win rates are in your context. "
        "Use them for every recommendation. If trader says 'winning formula' or 'build formula', "
        "route to the formula command.\n"
        "Act with full confidence and real data. You are J.A.R.V.I.S."
    )

    # ── Inject fresh context into system prompt (not into history messages) ────
    # Context (wallet, positions, live log, Fear & Greed, config) is always current
    # because it lives in the system prompt, refreshed on every call.
    # History messages store only the bare conversation — compact, accurate, persistent.
    full_system = system_prompt + "\n\n== CURRENT TRADING STATE (re-read from disk this message) ==\n" + context

    # Build messages: full persistent history + new user message
    messages_to_send = _history_messages() + [{"role": "user", "content": text}]

    reply_text = ""
    _last_fc_err: Exception | None = None

    # Build the rich context for fallback brains (Groq/Gemini)
    _rich_ctx = await _build_compact_context(runtime)
    _primary_system = system_prompt + "\n\n== CURRENT TRADING STATE ==\n" + _rich_ctx

    # Build Claude-specific system: same base, but [APPLY:] tags replaced by real tool descriptions.
    # Claude gets the full context (not compact) since tool-use responses are worth it.
    _apply_marker = "== AUTONOMOUS EXECUTION =="
    _base_before_apply = (
        system_prompt.split(_apply_marker)[0]
        if _apply_marker in system_prompt
        else system_prompt
    ).replace(
        "You are powered by Groq (Llama) with Gemini as backup — ",
        "You are Claude Sonnet 4.6 — the primary intelligence brain. ",
    )
    _claude_system = (
        _base_before_apply
        + "== REAL FUNCTION TOOLS ==\n"
        "You have REAL tool calls — use them directly instead of [APPLY:] text tags.\n\n"
        "Tools available:\n"
        "  update_config(key, value)    — apply any config change to the running bot immediately\n"
        "  enable_strategy(strategy)   — enable a strategy (a2, b, grad, c, raydium, d, meteora)\n"
        "  disable_strategy(strategy)  — disable a strategy\n"
        "  get_positions()             — fetch all open positions with entry/SL/age\n"
        "  close_position(mint)        — emergency force-close any open position\n"
        "  get_recent_trades(limit)    — last N completed trades with P&L\n"
        "  get_wallet_balance()        — real on-chain SOL balance + position exposure breakdown\n"
        "  pause_copy_trade()          — pause copy trading (no new entries, existing positions monitored)\n"
        "  resume_copy_trade()         — resume copy trading after a pause\n"
        "  emergency_stop(reason)      — HALT all trading instantly: pause copy trade + disable all strategies\n"
        "  get_logs(limit, level)      — recent bot activity log (level: error/warning/info/all)\n"
        "  restart_bot(reason)         — restart the bot process (use after config changes needing restart)\n\n"
        "COPY-TRADE COMPOUNDING SYSTEM — HARDWIRED (2026-04-14, quant-locked):\n"
        "  This is the active position-sizing and exit system. ALL parameters are LOCKED. Do NOT change any of them.\n"
        "  91-trade simulation result: +1.68 SOL vs +0.015 SOL actual. Formula is proven.\n\n"
        "  Exit rule:   Hard TP +15% → full exit (copy_trade_tp_pct=15.0, LOCKED)\n"
        "               Hard SL -10% → full exit (copy_trade_sl_pct=10.0, LOCKED)\n\n"
        "  Position size formula: min(max(floor, wallet × 20%), 2.0 SOL)\n"
        "     floor = copy_trade_paper_buy_sol = 0.4 SOL  (LOCKED)\n"
        "     pct   = copy_trade_compound_pct  = 0.20     (LOCKED)\n"
        "     cap   = copy_trade_compound_max_sol = 2.0   (LOCKED)\n\n"
        "  Tiers (current wallet: check context):\n"
        "     ≤ 2.0 SOL wallet → 0.40 SOL/trade (floor)\n"
        "     2.5 SOL wallet   → 0.50 SOL/trade\n"
        "     5.0 SOL wallet   → 1.00 SOL/trade\n"
        "    10.0 SOL wallet   → 2.00 SOL/trade (cap)\n\n"
        "  MONITORING ROLE: You should track compound tier progress in every status report.\n"
        "  Report: current wallet, current trade size, next tier threshold, trades until next tier.\n"
        "  When wallet crosses a tier boundary, proactively notify the trader.\n\n"
        "  ⛔ copy_trade_tp_pct, copy_trade_sl_pct, copy_trade_paper_buy_sol,\n"
        "  ⛔ copy_trade_compound_pct, copy_trade_compound_max_sol — ALL HARDWIRED. NEVER change.\n\n"
        "TRADER-PROTECTED KEYS — suggest but do NOT call update_config for these:\n"
        "  buy_sol, a2_buy_sol, strategy_b_buy_sol (legacy strategy sizes — NOT the copy-trade size)\n\n"
        "AUTONOMY MODE — you have full authority to change config, with hardwired exceptions:\n"
        "  ⛔ buy_sol / a2_buy_sol / strategy_b/c/d_buy_sol — HARDWIRED legacy strategy sizes. Do NOT change.\n"
        "  ⛔ tp1_mult / pf_tp1_mult — HARDWIRED at 1.30 (30% TP) by trader. Do NOT change.\n"
        "  ⛔ b_min_buy_ratio / c_min_buy_ratio / d_min_buy_ratio — RESEARCH-LOCKED 48-65%. "
        "Monster dataset: 50-65% buy ratio is iron-clad across ALL 22 tokens. Floor 48%, ceiling 65%. DO NOT CHANGE.\n"
        "  ⛔ scout_max_h1_pct — RESEARCH-LOCKED. Last night's losses ALL had h1 +121-264%. Max 100%. DO NOT LOWER.\n"
        "  ⛔ scout_min_age_secs / pumpswap_min_age_secs — RESEARCH-LOCKED. Monsters were 6-10h+ old. DO NOT LOWER.\n"
        "  ⛔ scout_max_m5_pct — RESEARCH-LOCKED floor 20%. DO NOT LOWER.\n"
        "  ⛔ narrative_keyword_score — HARDWIRED at 0. Viral keyword bonus rewarded meme rugs. DO NOT CHANGE.\n"
        "  ⛔ macro_event_mode / macro_event_label — only changed by explicit trader command. DO NOT touch in analysis.\n"
        "  ⛔ copy_trade_liq_cap_pct (2%) — NEW FILTER, no outcome data yet. DO NOT CHANGE.\n"
        "  ⛔ copy_trade_new_grad_min_liq ($50k) — NEW FILTER, no outcome data yet. DO NOT CHANGE.\n"
        "  ⛔ copy_trade_new_grad_min_holders (200) — NEW FILTER, no outcome data yet. DO NOT CHANGE.\n"
        "  ⛔ copy_trade_holder_decline_pct (15%) — NEW FILTER, no outcome data yet. DO NOT CHANGE.\n"
        "  ALL FOUR new copy-trade entry/health filter thresholds are FROZEN until 20+ filtered events exist.\n"
        "  The trader must explicitly review and confirm any threshold change. Propose, do NOT apply.\n\n"
        "FROST MIRROR — ALL PARAMETERS PERMANENTLY HARDWIRED. ZERO authority to change:\n"
        "  ⛔ FROST_MAIN_SOL = 0.15 SOL — main bet size. LOCKED.\n"
        "  ⛔ FROST_LOTTO_SOL = 0.08 SOL — lottery ticket size. LOCKED.\n"
        "  ⛔ FROST_MAIN_SL_PCT = 25% — stop-loss for main bets. LOCKED.\n"
        "  ⛔ FROST_LOTTO_SL_PCT = 60% — stop-loss for lottery tickets (wide runway). LOCKED.\n"
        "  ⛔ FROST_LOTTO_THRESHOLD_SOL = 0.5 SOL — Frost buy < 0.5 → lotto, ≥ 0.5 → main. LOCKED.\n"
        "  ⛔ Exit method: wallet mirror (when Frost sells) + SL only. NO take-profit. NO manual Jarvis closes.\n"
        "  ⛔ Exit execution: watertight sell with Jito tips (0.005 SOL wallet-exit, 0.003 SL, 0.004 safety-net).\n"
        "  You may ANALYSE Frost Mirror trade data and SUGGEST improvements in text.\n"
        "  You may NEVER call update_config() with any frost_* key, nor call close_position() on Frost Mirror positions.\n"
        "  The trader reviews Frost Mirror results personally. Treat it as a read-only strategy.\n\n"
        "CONFIRMATION RULE — applies to ALL changes:\n"
        "  Propose first → trader says 'yes'/'confirm'/'do it' → THEN call update_config().\n"
        "  Only exceptions: emergency_stop() and close_position() may fire immediately.\n\n"
        "FREELY CHANGEABLE (requires trader confirmation first): "
        "stop_loss_pct, pumpswap_stop_loss_pct, early_stop_loss_pct, "
        "trailing_stop_enabled, trailing_stop_pct, "
        "strategy_b_min_liq_usd, strategy_b_dip_wait_secs, fill_cap_pct, a2_min_liq_usd, a2_min_holders, "
        "a2_min_score, a2_max_age_secs, scout_min_liq_mc_ratio, b/c/d_min/max_vol_liq_ratio, "
        "max_concurrent_positions, rugcheck_max_score, post_sl_cooldown_secs, momentum_confirm_secs, "
        "copy_trade_wallet_loss_cap_sol, "
        "strategy_a2_enabled, strategy_b_enabled, strategy_c_enabled, strategy_d_enabled, and all others "
        "not listed in the ⛔ list above.\n"
        "  ⛔ NOT freely changeable: copy_trade_paper_buy_sol, copy_trade_sl_pct, copy_trade_tp_pct,\n"
        "     copy_trade_compound_pct, copy_trade_compound_max_sol — these are part of the locked compounding system.\n\n"
        "RULE: 'enable X', 'set X to Y', 'activate X' → CALL THE TOOL immediately after confirmation.\n"
        "  - Any SOL amounts → require trader 'yes'/'confirm' first, then call tool.\n"
        "  - Strategy toggles and filter thresholds → propose, confirm, apply.\n\n"
        "⚠️ MANDATORY TOOL-USE RULE — READ THIS CAREFULLY:\n"
        "If you decide to change a config value, you MUST call update_config(key, value).\n"
        "Writing 'I've set X to Y' or 'Applied: X=Y' in your text WITHOUT calling the tool = the change has NOT happened.\n"
        "The trader cannot distinguish text claims from real changes unless the tool is called.\n"
        "AFTER calling the tool, read back the changed value in your response and confirm it matches.\n"
        "NEVER claim you applied a change that you did not call the tool for. This destroys trust.\n"
        "If the tool returns an error, tell the trader what failed — do not pretend it worked.\n\n"
        "⛔ ANTI-FABRICATION RULE — CRITICAL:\n"
        "When reporting open positions, trade history, token names, P&L figures, or wallet balance:\n"
        "  - ONLY use data from the 'OPEN POSITIONS' and 'LAST 15 TRADES' sections of your context.\n"
        "  - If OPEN POSITIONS shows '(none)' — there are NO open positions. Do not invent any.\n"
        "  - NEVER make up token names like 'G loop', 'Kaiju', 'Nova', 'Astra', 'Lumina', 'Apex', 'Zenith' or any name not in your context.\n"
        "  - NEVER invent entry prices, P&L percentages, or position sizes.\n"
        "  - If the data says no trades — say 'no trades recorded this session' not a fabricated table.\n"
        "  - NEVER write 'It's been approximately X hours since the last update' — you don't track time.\n"
        "  - NEVER write a 'STATUS UPDATE' section with invented trade data. Use ONLY the live context above.\n"
        "  - If asked for a status update, read the 'CURRENT TRADING STATE' section above and report ONLY what's there.\n"
        "  - You will be caught: the trader can see the real dashboard. Fabrication destroys trust instantly.\n\n"
        "4-TIER AI CASCADE (copy-trade position monitor):\n"
        "  Tier 1 Groq: every 30s per position — SELL≥0.75 exits immediately\n"
        "  Tier 2 Gemini: every 2min — SELL≥0.70 exits\n"
        "  Tier 3 YOU (Claude Sonnet): every 5min — SELL≥0.65 exits\n"
        "  Tier 4 Opus: emergency escalation on dual Tier 1+2 SELL signal\n"
        "  Moonbag trail gate: Groq consulted before firing — HOLD≥0.60 grants 45s grace\n\n"
        "TRADE INTELLIGENCE: full trade history and per-DEX win rates in context. Use them always.\n"
        "If trader says 'winning formula' or 'build formula', route to that command.\n"
        "You are J.A.R.V.I.S. — act with full confidence and real data.\n\n"
        + _monster_ref_section
        + _monster_addr_section
        + _win_config_section
        + "\n\n== CURRENT TRADING STATE (re-read from disk this message) ==\n" + context
    )

    # ── Brain 0: Claude Sonnet 4.6 (PRIMARY — real tool-use, agentic loop) ───────
    _anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if _anthropic_key and not reply_text:
        _history = _history_messages()
        reply_text = await _claude_tool_use_loop(
            text, runtime, _claude_system, _history, _anthropic_key
        )
        if reply_text:
            # Post-response verification: scan for config value claims that Claude may have
            # written as text without calling the tool. If we find "key = value" patterns in
            # the response that don't match live config, apply them now.
            reply_text = _verify_and_enforce_config_claims(reply_text)
            _history_append(text, reply_text)
            return reply_text

    # ── Brain 1: Groq llama-3.3-70b (fallback — free, 131k TPM) ────────────────
    _groq_key = os.getenv("GROQ_API_KEY", "")
    if _groq_key and not reply_text:
        try:
            import openai as _oai
            _groq_client = _oai.AsyncOpenAI(
                api_key=_groq_key,
                base_url="https://api.groq.com/openai/v1",
                max_retries=0,
            )
            _msgs_trimmed = messages_to_send[-6:] if len(messages_to_send) > 6 else messages_to_send
            resp = await asyncio.wait_for(
                _groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    max_tokens=800,
                    messages=[{"role": "system", "content": _primary_system}] + _msgs_trimmed,
                ),
                timeout=30.0,
            )
            reply_text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            _last_fc_err = exc

    # ── Brain 2: Gemini 2.0 Flash (second fallback — free, Google AI Studio) ────
    if not reply_text:
        _gem_key_fc = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
        if _gem_key_fc:
            try:
                from google import genai as _gai_fc
                _gc = _gai_fc.Client(api_key=_gem_key_fc)
                _gem_msgs = "\n\n".join(
                    f"[{m['role'].upper()}]: {m['content']}"
                    for m in (messages_to_send[-4:] if len(messages_to_send) > 4 else messages_to_send)
                )
                _gem_full = _primary_system + "\n\n" + _gem_msgs
                _gr = await asyncio.wait_for(
                    _gc.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=_gem_full,
                        config={"max_output_tokens": 800},
                    ),
                    timeout=30.0,
                )
                reply_text = (_gr.text or "").strip()
            except Exception as exc:
                _last_fc_err = exc

    if not reply_text:
        return (
            f"All brains unavailable right now ({_last_fc_err}). "
            f"Try: **status**, **config**, **history**, **positions**, **lessons**, "
            f"**enable/disable strategy [name]**, **set [param] to [value]**, **run analysis**."
        )

    # Groq/Gemini fallbacks: parse [APPLY:] text tags from response
    reply_text, applied = _execute_jarvis_commands(reply_text)
    if applied:
        reply_text += "\n\n**⚙️ Applied autonomously:**\n" + "\n".join(applied)

    # Persist this exchange to disk — survives bot restarts
    _history_append(text, reply_text)

    return reply_text


async def _cmd_restart(reason: str = "manual") -> str:
    """Trigger a clean bot restart via the watchdog supervisor.

    Sends SIGTERM to the current process after a 2-second delay, giving the
    WebSocket time to deliver this reply. The watchdog catches the exit and
    brings a new process up within ~5 seconds.  If no watchdog is running
    (e.g. started manually), the process simply exits and must be restarted
    by hand.
    """
    import signal

    watchdog_alive = False
    try:
        wd_pid_str = open("/tmp/traderbot-watchdog.pid").read().strip()
        wd_pid = int(wd_pid_str)
        os.kill(wd_pid, 0)   # signal 0 = existence check only
        watchdog_alive = True
    except Exception:
        pass

    if not watchdog_alive:
        return (
            "⚠️ **Watchdog not detected.** The bot is not running under the watchdog supervisor, "
            "so a self-restart would leave the bot offline.\n\n"
            "To enable self-restart, launch the bot via the desktop shortcut (which uses `watchdog.sh`). "
            "Restart the bot manually this time."
        )

    async def _deferred_kill() -> None:
        await asyncio.sleep(2)
        print(f"[jarvis-restart] Self-restart triggered by Jarvis — reason: {reason}", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_deferred_kill())

    return (
        f"🔄 **Restarting bot in 2 seconds** (reason: {reason}).\n\n"
        "The dashboard will show **Offline** for ~10 seconds while the watchdog brings up a fresh process. "
        "It reconnects automatically — no action needed.\n\n"
        "If it doesn't reconnect within 30 seconds, hard-refresh the page (Ctrl+Shift+R)."
    )


async def _cmd_diagnose() -> str:
    """Scan recent logs for known error patterns and report health status."""
    lines = _tail_log(200, activity_only=False)
    issues = []
    hints  = []

    patterns = [
        ("ClientConnectionResetError",  "WebSocket transport error — client disconnected mid-reply (cosmetic, not critical)"),
        ("Cannot write to closing",     "WebSocket write to closed connection (cosmetic, not critical)"),
        ("OSError.*address already in use", "Port 3001 conflict — another bot instance is running"),
        ("ConnectionRefusedError",      "Outbound API connection refused — check internet / API keys"),
        ("TimeoutError",                "API call timed out — possible rate limit or network issue"),
        ("Traceback",                   "Unhandled exception in bot code"),
        ("CRASH LOOP DETECTED",         "Watchdog stopped due to repeated crashes"),
        ("RPC.*fail",                   "Solana RPC call failed — Helius connectivity issue"),
    ]

    for pat, desc in patterns:
        if re.search(pat, lines, re.IGNORECASE):
            issues.append(f"• **{pat.split('.*')[0]}**: {desc}")

    # Check watchdog alive
    wd_ok = False
    try:
        wd_pid = int(open("/tmp/traderbot-watchdog.pid").read().strip())
        os.kill(wd_pid, 0)
        wd_ok = True
    except Exception:
        pass

    if not wd_ok:
        hints.append("⚠️ Watchdog not running — bot will not auto-restart on crash. Launch via desktop icon.")

    if not issues and not hints:
        return "✅ **Diagnostics clean** — no known error patterns in the last 200 log lines. Watchdog active."

    parts = ["**Bot Diagnostics:**\n"]
    if issues:
        parts.append("**Issues detected in log:**\n" + "\n".join(issues))
    if hints:
        parts.append("\n".join(hints))
    parts.append("\nType `restart` if you want me to trigger a clean restart.")
    return "\n\n".join(parts)


def _load_intel_summary() -> str:
    """Return a compact (≤20 line) summary of winning_trade_intelligence.json for context injection."""
    try:
        if not os.path.exists(_INTEL_PATH):
            return "(no trade intelligence file yet)"
        with open(_INTEL_PATH) as f:
            intel = json.load(f)
        meta = intel.get("_meta", {})
        by_dex = intel.get("by_dex", {})
        eq = intel.get("exit_quality_analysis", {})
        patterns = intel.get("patterns", {})

        lines = [
            f"Trades analysed: {meta.get('total_trades_analysed', '?')} | "
            f"WR: {meta.get('overall_win_rate_pct', '?')}% | "
            f"Net: {meta.get('net_sol_all_time', '?'):+.4f} SOL",
        ]
        for dex, d in by_dex.items():
            lines.append(
                f"  {dex}: {d.get('trades','?')}T {d.get('win_rate_pct','?')}%WR "
                f"avgW={d.get('avg_win_pct','?'):+.0f}% avgL={d.get('avg_loss_pct','?'):+.0f}% "
                f"rugs={d.get('rug_count',0)}"
            )
        lines.append(f"Exit quality: {eq.get('insight', '?')}")
        lines.append(f"TP exits: {eq.get('tp_full_exit_win_rate', '?')}")
        lines.append(f"Rug losses: {eq.get('pct_losses_were_rugs', '?')}% of all losses")

        # Best pattern summary (if formula exists)
        if os.path.exists(_FORMULA_PATH):
            try:
                with open(_FORMULA_PATH) as ff:
                    formula = json.load(ff)
                synopsis = formula.get("synopsis", "")
                if synopsis:
                    lines.append(f"FORMULA SYNOPSIS: {synopsis[:200]}")
            except Exception:
                pass

        return "\n".join(lines)
    except Exception as exc:
        return f"(intel load error: {exc})"


async def _cmd_winning_formula(runtime=None) -> str:
    """3-brain analysis of all trade history → outputs a concrete winning formula.

    Brain 1 (Claude): Pattern synthesis — what do the wins have in common?
    Brain 2 (GPT-4o): Risk/entry quality — what entry conditions predict wins vs losses?
    Brain 1 (Claude): Final synthesis — concrete formula with specific thresholds.
    Result saved to winning_formula.json for persistent reference.
    """
    import anthropic as _ant
    import openai as _oai

    # Load full intelligence file
    if not os.path.exists(_INTEL_PATH):
        return "❌ No trade intelligence file found. Make sure `winning_trade_intelligence.json` exists in the solana plugin directory."

    try:
        with open(_INTEL_PATH) as f:
            intel = json.load(f)
    except Exception as exc:
        return f"❌ Failed to load trade intelligence: {exc}"

    meta = intel.get("_meta", {})
    by_dex = intel.get("by_dex", {})
    eq = intel.get("exit_quality_analysis", {})
    wins = intel.get("winning_trades_full_detail", [])
    losses = intel.get("losing_trades_full_detail", [])
    patterns = intel.get("patterns", {})
    score_analysis = intel.get("score_threshold_analysis", {})
    config_rec = intel.get("config_recommendations", {})

    # Build the data block for AI analysis (compact but complete)
    top_wins = wins[:20]   # top 20 wins by P&L
    worst_losses = losses[:15]  # worst 15 losses

    intel_block = (
        f"TRADE INTELLIGENCE SUMMARY\n"
        f"Total trades: {meta.get('total_trades_analysed', '?')} | "
        f"WR: {meta.get('overall_win_rate_pct', '?')}% | "
        f"Net: {meta.get('net_sol_all_time', '?'):+.4f} SOL\n\n"
        f"DEX BREAKDOWN:\n"
        + "\n".join(
            f"  {dex}: {d.get('trades')}T {d.get('win_rate_pct')}%WR "
            f"avgW={d.get('avg_win_pct'):+.1f}% avgL={d.get('avg_loss_pct'):+.1f}% "
            f"net={d.get('net_sol'):+.4f} SOL rugs={d.get('rug_count',0)}"
            for dex, d in by_dex.items()
        )
        + f"\n\nEXIT QUALITY:\n{eq.get('insight', '')}\n"
        f"TP exit WR: {eq.get('tp_full_exit_win_rate', '')}\n"
        f"Rug losses: {eq.get('pct_losses_were_rugs', '?')}% of all losses\n\n"
        f"TOP 20 WINNING TRADES (sorted by P&L):\n"
        + "\n".join(
            f"  {w.get('date','?')} {w.get('dex','?')} {w.get('mint','?')[:10]}... "
            f"pnl={w.get('pnl_pct'):+.1f}% ({w.get('pnl_sol'):+.4f} SOL) "
            f"score={w.get('score','?')} exit={w.get('exit_reason','?')} "
            f"still_rising={w.get('was_still_rising_at_exit','?')}"
            for w in top_wins
        )
        + f"\n\nWORST 15 LOSSES:\n"
        + "\n".join(
            f"  {l.get('date','?')} {l.get('dex','?')} {l.get('mint','?')[:10]}... "
            f"pnl={l.get('pnl_pct'):+.1f}% ({l.get('pnl_sol'):+.4f} SOL) "
            f"exit={l.get('exit_reason','?')} rug={l.get('was_rug','?')}"
            for l in worst_losses
        )
        + f"\n\nPATTERNS IDENTIFIED:\n{json.dumps(patterns, indent=2)[:1500]}\n\n"
        f"SCORE ANALYSIS:\n{json.dumps(score_analysis, indent=2)[:800]}\n\n"
        f"CONFIG RECOMMENDATIONS:\n{json.dumps(config_rec, indent=2)[:600]}"
    )

    groq_key     = os.getenv("GROQ_API_KEY", "")
    gemini_key   = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    _formula_sys = (
        "You are Jarvis — the AI brain of a live Solana memecoin trading bot. "
        "You are doing a deep forensic analysis of your own trade history. "
        "Be specific, data-driven, and ruthless. Use actual numbers from the data. "
        "Don't hedge. You are writing this for yourself to act on."
    )

    async def _call_groq(prompt: str, system: str, max_tokens: int = 1200) -> str:
        import openai as _oai_wf
        client = _oai_wf.AsyncOpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1", max_retries=0)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            ), timeout=60.0,
        )
        return (resp.choices[0].message.content or "").strip()

    async def _call_gemini(prompt: str) -> str:
        from google import genai as _gai_wf
        gc = _gai_wf.Client(api_key=gemini_key)
        r = await asyncio.wait_for(
            gc.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"max_output_tokens": 1200},
            ), timeout=60.0,
        )
        return (r.text or "").strip()

    brain1_pattern = ""
    brain2_entry = ""
    formula_text = ""

    # ── BRAIN 1: Groq — pattern synthesis ─────────────────────────────────────
    if groq_key:
        try:
            brain1_pattern = await _call_groq(
                f"Analyse this complete trade intelligence dataset and identify:\n"
                f"1. What do the TOP winning trades have in common? (DEX, score range, entry conditions)\n"
                f"2. What patterns define the losing trades? (score, DEX, exit reason)\n"
                f"3. Rug analysis — which conditions correlate with rugs?\n"
                f"4. Is TP (+60%) optimal or are we leaving money on the table?\n"
                f"5. Which DEX should we FOCUS on based purely on data?\n\n{intel_block}",
                system=_formula_sys,
            )
        except Exception as exc:
            brain1_pattern = f"(Groq pattern analysis error: {exc})"
    else:
        brain1_pattern = "(Groq unavailable)"

    # ── BRAIN 2: Gemini — entry quality & risk assessment ─────────────────────
    if gemini_key:
        try:
            brain2_entry = await _call_gemini(
                f"You are Brain 2 in a 3-brain AI trading system — entry quality and risk specialist.\n"
                f"From this trading data, analyse:\n"
                f"1. ENTRY CONDITIONS: What score threshold and filters best predict a winning trade?\n"
                f"2. POSITION SIZING: Should we size differently for different DEXes?\n"
                f"3. EXIT STRATEGY: Given 100% TP win rate, should we be bolder on TPs?\n"
                f"4. RUG MITIGATION: 22% of losses are rugs. What can be done?\n"
                f"5. Concrete numbers: Give specific recommended values for SL%, TP%, position size.\n\n{intel_block}"
            )
        except Exception as exc:
            brain2_entry = f"(Gemini entry analysis error: {exc})"
    elif anthropic_key:
        try:
            ant_client = _ant.AsyncAnthropic(api_key=anthropic_key)
            resp = await asyncio.wait_for(
                ant_client.messages.create(
                    model="claude-opus-4-7", max_tokens=1000,
                    system="You are Brain 2 — entry quality and risk management specialist.",
                    messages=[{"role": "user", "content":
                        f"Analyse entry quality and risk from this data:\n{intel_block}"}],
                ), timeout=60.0,
            )
            brain2_entry = (resp.content[0].text if resp.content else "").strip()
        except Exception as exc:
            brain2_entry = f"(Claude Brain 2 error: {exc})"
    else:
        brain2_entry = "(No Brain 2 available)"

    # ── BRAIN 3: Groq — final synthesis into winning formula ──────────────────
    if groq_key and brain1_pattern and brain2_entry:
        try:
            formula_text = await _call_groq(
                f"BRAIN 1 PATTERN ANALYSIS:\n{brain1_pattern}\n\n"
                f"BRAIN 2 ENTRY QUALITY ANALYSIS:\n{brain2_entry}\n\n"
                f"RAW DATA (reference):\n{intel_block[:2000]}\n\n"
                f"Synthesise into THE WINNING FORMULA — specific rules the bot trades by from today. "
                f"Include: DEX priority, min score, entry conditions, position size, TP%, SL%, rug avoidance. "
                f"End with: SYNOPSIS: [one sentence max 150 chars]",
                system=(
                    "You are Jarvis — the final synthesis brain. Combine the pattern analysis and "
                    "risk assessment into ONE concrete winning formula with specific numbers, not ranges."
                ),
                max_tokens=1500,
            )
        except Exception as exc:
            formula_text = f"(Formula synthesis error: {exc})"
    elif gemini_key:
        try:
            formula_text = await _call_gemini(
                f"BRAIN 1:\n{brain1_pattern}\n\nBRAIN 2:\n{brain2_entry}\n\n"
                f"Synthesise into THE WINNING FORMULA with specific numbers. "
                f"End with: SYNOPSIS: [one sentence]"
            )
        except Exception as exc:
            formula_text = f"(Gemini synthesis error: {exc})"

    # Extract synopsis
    synopsis = ""
    m = re.search(r"SYNOPSIS:\s*(.+?)(?:\n|$)", formula_text, re.IGNORECASE)
    if m:
        synopsis = m.group(1).strip()[:200]

    # Save the formula to disk
    formula_record = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "synopsis": synopsis,
        "brain1_pattern_analysis": brain1_pattern,
        "brain2_entry_analysis": brain2_entry,
        "formula": formula_text,
        "data_summary": {
            "trades_analysed": meta.get("total_trades_analysed"),
            "win_rate_pct": meta.get("overall_win_rate_pct"),
            "net_sol": meta.get("net_sol_all_time"),
        },
    }
    try:
        with open(_FORMULA_PATH, "w") as f:
            json.dump(formula_record, f, indent=2)
    except Exception:
        pass

    # Format the full response
    parts = ["**🧠 JARVIS 3-BRAIN WINNING FORMULA ANALYSIS**\n"]
    parts.append(f"*Based on {meta.get('total_trades_analysed', '?')} trades | "
                 f"WR {meta.get('overall_win_rate_pct', '?')}% | Net {meta.get('net_sol_all_time', '?'):+.4f} SOL*\n")
    parts.append("---")
    parts.append(f"**BRAIN 1 — Pattern Analysis (Claude):**\n{brain1_pattern}\n")
    parts.append("---")
    parts.append(f"**BRAIN 2 — Entry Quality & Risk (GPT-4o):**\n{brain2_entry}\n")
    parts.append("---")
    parts.append(f"**FINAL SYNTHESIS — The Winning Formula:**\n{formula_text}\n")
    if synopsis:
        parts.append(f"\n📌 **FORMULA SYNOPSIS:** {synopsis}")
    parts.append("\n✅ *Formula saved to `winning_formula.json` — Jarvis will reference this on every message.*")

    full_reply = "\n".join(parts)
    _history_append("build winning formula", full_reply)
    return full_reply


async def _cmd_trade_forensics(identifier: str, runtime=None) -> str:
    """On-chain trade forensics using Helius RPC.

    Given a mint (or prefix) or transaction signature, looks up the trade in
    trade_history.json, fetches the buy+sell transactions from Helius, and returns:
    - Exact entry and exit prices from on-chain token balance changes
    - P&L with real on-chain numbers
    - Current DexScreener price to show post-exit performance
    - Whether we exited too early
    """
    import aiohttp

    solana_dir = os.path.dirname(__file__)
    import glob as _glob

    # Load trade history
    all_trades: list[dict] = []
    for fpath in sorted(_glob.glob(os.path.join(solana_dir, "trade_history*.json"))):
        try:
            with open(fpath) as f:
                all_trades.extend(json.load(f))
        except Exception:
            pass

    # Also load from runtime if available
    if runtime:
        pos_mgr = runtime.get_service("position_manager")
        if pos_mgr:
            try:
                all_trades.extend(pos_mgr.get_trade_history(limit=500))
            except Exception:
                pass

    if not all_trades:
        return "❌ No trade history found."

    # Find the trade by mint prefix or full signature
    identifier = identifier.strip()
    matched: list[dict] = []
    if len(identifier) >= 44:
        # Could be a full signature or full mint
        matched = [t for t in all_trades if t.get("mint") == identifier or t.get("signature") == identifier]
    else:
        # Prefix match on mint
        matched = [t for t in all_trades if (t.get("mint") or "").startswith(identifier)]

    if not matched:
        return f"❌ No trades found matching `{identifier[:16]}...`"

    # Group by mint to get buy+sell pairs
    mints_found = list(dict.fromkeys(t.get("mint", "") for t in matched))
    mint = mints_found[0]
    buy_trades = [t for t in matched if t.get("side") == "buy" and t.get("mint") == mint]
    sell_trades = [t for t in matched if t.get("side") == "sell" and t.get("mint") == mint]

    buy = buy_trades[-1] if buy_trades else None
    sell = sell_trades[-1] if sell_trades else None

    if not buy:
        return f"❌ Found mint `{mint[:16]}...` but no buy record."

    rpc_url = os.getenv("SOLANA_RPC_URL", "")
    lines = [f"**Trade Forensics — `{mint[:20]}...`**\n"]

    # ── Stored trade data ──────────────────────────────────────────────────
    entry_stored = buy.get("entry_price_sol", 0)
    exit_stored = sell.get("exit_price_sol") if sell else None
    pnl_pct = sell.get("pnl_pct") if sell else None
    pnl_sol = sell.get("pnl_sol") if sell else None
    dex = buy.get("dex", "?")
    score = buy.get("score", "?")
    reason = sell.get("reason", "?") if sell else "still open"
    buy_sig = buy.get("signature", "")
    sell_sig = sell.get("signature", "") if sell else ""

    lines.append(f"DEX: {dex} | Score: {score} | Exit: {reason}")
    lines.append(f"Entry price (stored): {entry_stored:.4e} SOL")
    if exit_stored:
        lines.append(f"Exit price (stored):  {exit_stored:.4e} SOL")
    if pnl_pct is not None:
        lines.append(f"P&L: {pnl_pct:+.2f}% ({pnl_sol:+.4f} SOL)" if pnl_sol else f"P&L: {pnl_pct:+.2f}%")

    # ── Helius on-chain lookup ─────────────────────────────────────────────
    if rpc_url and buy_sig:
        async def _get_tx(sig: str) -> dict | None:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as sess:
                    async with sess.post(rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTransaction",
                        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                    }) as r:
                        if r.status == 200:
                            return (await r.json()).get("result")
            except Exception:
                return None
            return None

        buy_tx = await _get_tx(buy_sig)
        sell_tx = await _get_tx(sell_sig) if sell_sig else None

        def _parse_token_change(tx: dict | None) -> tuple[float, float]:
            """Returns (sol_change, token_change) from parsed transaction."""
            if not tx:
                return 0.0, 0.0
            try:
                meta = tx.get("meta", {})
                pre_tok = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
                post_tok = {b.get("accountIndex"): b for b in (meta.get("postTokenBalances") or [])}
                pre_sol = meta.get("preBalances", [])
                post_sol = meta.get("postBalances", [])

                # SOL change for wallet (account 0 = fee payer)
                sol_delta = 0.0
                if pre_sol and post_sol:
                    sol_delta = (post_sol[0] - pre_sol[0]) / 1e9

                # Token change — find the account that changed
                tok_delta = 0.0
                all_indices = set(list(pre_tok.keys()) + list(post_tok.keys()))
                for idx in all_indices:
                    pre_amt = float((pre_tok.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                    post_amt = float((post_tok.get(idx) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
                    delta = post_amt - pre_amt
                    if abs(delta) > abs(tok_delta):
                        tok_delta = delta
                return sol_delta, tok_delta
            except Exception:
                return 0.0, 0.0

        buy_sol_delta, buy_tok_delta = _parse_token_change(buy_tx)
        sell_sol_delta, sell_tok_delta = _parse_token_change(sell_tx) if sell_tx else (0.0, 0.0)

        lines.append("\n**On-Chain (Helius):**")
        if buy_tx and abs(buy_tok_delta) > 0:
            onchain_entry = abs(buy_sol_delta / buy_tok_delta) if buy_tok_delta else 0
            lines.append(f"  Buy tx:  SOL spent={abs(buy_sol_delta):.4f} | tokens received={abs(buy_tok_delta):,.0f}")
            if onchain_entry > 0:
                lines.append(f"  Entry (on-chain): {onchain_entry:.4e} SOL/token")
        elif buy_tx:
            lines.append(f"  Buy tx: found (sig {buy_sig[:16]}...) — token balance parsing incomplete")
        else:
            lines.append(f"  Buy tx: not found on-chain (sig {buy_sig[:16]}...)")

        if sell_tx and abs(sell_tok_delta) > 0:
            onchain_exit = abs(sell_sol_delta / sell_tok_delta) if sell_tok_delta else 0
            lines.append(f"  Sell tx: SOL received={abs(sell_sol_delta):.4f} | tokens sold={abs(sell_tok_delta):,.0f}")
            if onchain_exit > 0:
                lines.append(f"  Exit (on-chain):  {onchain_exit:.4e} SOL/token")
        elif sell_tx:
            lines.append(f"  Sell tx: found — balance parsing incomplete")
        elif sell_sig:
            lines.append(f"  Sell tx: not found on-chain (sig {sell_sig[:16]}...)")
    else:
        lines.append("\n*(Helius RPC not configured — on-chain verification unavailable)*")

    # ── Current DexScreener price (post-exit performance) ─────────────────
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}") as r:
                if r.status == 200:
                    dex_data = await r.json()
                    pairs = dex_data.get("pairs") or []
                    if pairs:
                        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
                        best = pairs[0]
                        cur_price = float(best.get("priceNative") or 0)
                        mcap = best.get("marketCap") or best.get("fdv") or 0
                        pc24 = (best.get("priceChange") or {}).get("h24") or 0
                        liq = (best.get("liquidity") or {}).get("usd") or 0
                        lines.append(f"\n**Current (DexScreener):**")
                        lines.append(f"  Price now: {cur_price:.4e} SOL | MC: ${mcap:,.0f} | 24h: {pc24:+.0f}%")
                        lines.append(f"  Liquidity: ${liq:,.0f}")
                        if exit_stored and exit_stored > 0 and cur_price > 0:
                            post_exit_pct = (cur_price - exit_stored) / exit_stored * 100
                            lines.append(f"  Since our exit: {post_exit_pct:+.1f}%"
                                         + (" ← LEFT ON TABLE" if post_exit_pct > 20 else ""))
    except Exception:
        pass

    lines.append(f"\n[Solscan](https://solscan.io/token/{mint}) | "
                 f"[DexScreener](https://dexscreener.com/solana/{mint})")

    result = "\n".join(lines)
    _history_append(f"forensics {identifier}", result)
    return result


async def _cmd_rejection_outcomes(runtime=None, force_check: bool = False) -> str:
    """Check what rejected tokens did after we turned them away.

    Fetches DexScreener outcomes for all rejections older than 2 hours.
    Builds an evidence table: which filters saved us, which missed pumps.
    """
    try:
        from elizaos.plugins.solana import rejection_tracker as rt
    except Exception as exc:
        return f"❌ Rejection tracker unavailable: {exc}"

    stats = rt.get_stats()
    total_tracked = stats.get("total_tracked", 0)
    unchecked = total_tracked - stats.get("outcomes_checked", 0)

    if total_tracked == 0:
        return (
            "No rejection records yet. The tracker starts recording as soon as the bot runs "
            "and rejects tokens. Check back after a trading session."
        )

    checking_msg = ""
    outcomes = {}
    if unchecked > 0 or force_check:
        checking_msg = f"Checking {unchecked} unchecked rejections (2h+ old)...\n\n"
        try:
            outcomes = await rt.check_outcomes(min_age_hours=2.0)
        except Exception as exc:
            outcomes = {"checked": 0, "error": str(exc)}

    # Build summary
    recent = rt.get_recent(n=100, with_outcomes_only=True)
    pumped = [r for r in recent if r.get("outcome_verdict") == "pumped"]
    rugged = [r for r in recent if r.get("outcome_verdict") == "rugged"]
    flat   = [r for r in recent if r.get("outcome_verdict") == "flat"]

    lines = [
        f"**Rejection Outcomes — {total_tracked} tracked | {stats.get('outcomes_checked', 0)} outcomes known**\n",
        checking_msg,
    ]

    if outcomes.get("checked", 0) > 0:
        lines.append(
            f"This run: checked {outcomes['checked']} | "
            f"pumped={outcomes.get('pumped', 0)} | "
            f"rugged={outcomes.get('rugged', 0)} | "
            f"flat={outcomes.get('flat', 0)}\n"
        )

    # Filter evidence table
    filter_ev = outcomes.get("filter_evidence") or {}
    if not filter_ev:
        # Load from all historical data
        all_recs = rt.get_recent(n=500, with_outcomes_only=True)
        from collections import defaultdict as _dd
        fe: dict = _dd(lambda: {"saved_us": 0, "missed_pump": 0, "total": 0})
        for r in all_recs:
            fn = r.get("filter_name") or r.get("reason", "unknown")
            fe[fn]["total"] += 1
            v = r.get("outcome_verdict")
            if v == "pumped":
                fe[fn]["missed_pump"] += 1
            elif v in ("rugged", "delisted_or_unknown"):
                fe[fn]["saved_us"] += 1
        filter_ev = dict(fe)

    if filter_ev:
        lines.append("**Filter Evidence (what each filter actually prevented vs missed):**")
        for fn, ev in sorted(filter_ev.items(), key=lambda x: x[1].get("missed_pump", 0), reverse=True):
            total = ev.get("total", 0)
            missed = ev.get("missed_pump", 0)
            saved = ev.get("saved_us", 0)
            miss_rate = missed / total * 100 if total else 0
            lines.append(
                f"  **{fn}**: {total} rejections | "
                f"missed pumps: {missed} ({miss_rate:.0f}%) | "
                f"saved from ruin: {saved}"
                + (" ⚠️ REVIEW THRESHOLD" if miss_rate >= 30 else "")
            )
        lines.append("")

    # Show the biggest missed pumps
    if pumped:
        lines.append(f"**Tokens we rejected that then PUMPED ({len(pumped)} cases):**")
        for r in sorted(pumped, key=lambda x: x.get("outcome_price_change_pct") or 0, reverse=True)[:10]:
            chg = r.get("outcome_price_change_pct")
            lines.append(
                f"  `{r['mint'][:16]}...` — rejected: **{r.get('reason','?')}** "
                f"(val={r.get('filter_value','?')} vs thresh={r.get('threshold','?')}) "
                f"→ +{chg:.0f}% after reject"
                + (f" | score={r.get('score')}" if r.get("score") else "")
            )
        lines.append("")

    # Show top rugged (confirmed saves)
    if rugged:
        lines.append(f"**Tokens we rejected that then RUGGED ({len(rugged)} confirmed saves):**")
        for r in rugged[:5]:
            chg = r.get("outcome_price_change_pct")
            lines.append(
                f"  `{r['mint'][:16]}...` — {r.get('reason','?')} → {chg:+.0f}% (bullet dodged ✅)"
            )
        lines.append("")

    # Ask Claude to interpret if we have enough data
    total_with_outcomes = len(recent)
    if total_with_outcomes >= 5:
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            try:
                import anthropic as _ant
                client = _ant.AsyncAnthropic(api_key=anthropic_key)
                summary_block = "\n".join(lines)
                resp = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-opus-4-7",
                        max_tokens=600,
                        system=(
                            "You are Jarvis. You've just received evidence about which tokens the bot "
                            "rejected and what those tokens did afterwards. Your job is to give a sharp, "
                            "2-paragraph verdict: (1) Is any filter too strict (missing too many pumps)? "
                            "(2) What is the single most important config change based on this evidence?"
                        ),
                        messages=[{"role": "user", "content":
                            f"Here is the rejection outcome data:\n\n{summary_block}\n\n"
                            f"Give me your verdict on filter effectiveness and any recommended adjustments."
                        }],
                    ),
                    timeout=30.0,
                )
                ai_verdict = (resp.content[0].text if resp.content else "").strip()
                lines.append(f"**Jarvis Verdict:**\n{ai_verdict}")
            except Exception:
                pass

    return "\n".join(lines)


async def _cmd_logs(n: int = 50, raw: bool = False) -> str:
    """Show last N activity lines of the live bot log (HTTP noise filtered unless raw=True)."""
    lines = _tail_log(n, activity_only=not raw)
    if not lines or lines.startswith("("):
        return f"Bot log: {lines}"
    label = "raw" if raw else "activity"
    return f"**Last {n} bot {label} lines:**\n```\n{lines}\n```"


def _parse_rejections(log_text: str) -> dict:
    """Parse bot log lines into structured rejection + near-miss data.

    Returns:
        total_rejections: count
        filter_counts: {filter_name: count} — bottleneck ranking
        near_misses: tokens that were close to qualifying
        recent_rejections: last 25 structured rejection records
        raw_lines: all rejection lines for Claude's review
    """
    lines = log_text.splitlines()
    rejections: list[dict] = []
    filter_counts: dict[str, int] = {}
    near_misses: list[dict] = []
    rejection_lines: list[str] = []

    # A2 BC momentum gate: real_sol / fill / holders / age
    _re_reject = re.compile(
        r"\[a2-bc\] x (\S+)\.\.\. REJECT real_sol=([\d.]+)/([\d.]+) SOL"
        r".*fill=([\d.]+)%.*holders=(\d+)"
    )
    # Rugcheck score
    _re_rug = re.compile(r"\[a2-bc\] x (\S+)\.\.\. UNSAFE: rugcheck score (\d+)")
    # Holder count
    _re_holders = re.compile(r"\[a2-bc\] x (\S+)\.\.\. only (\d+) holders \(need .(\d+)\)")
    # LP not locked / mint authority
    _re_lp = re.compile(r"\[a2-bc\] x (\S+)\.\.\. UNSAFE: (.+)")
    # Age filter
    _re_age = re.compile(r"\[a2-bc\] x (\S+)\.\.\. age.filter.*age=(\d+)")
    # Strategy B grad-snipe rejections
    _re_grad = re.compile(r"\[grad-snipe\] x (\S+)\.\.\. (.+)")
    # Fill cap (bonding curve too full)
    _re_fill = re.compile(r"\[a2-bc\] x (\S+)\.\.\. fill.cap.*fill=([\d.]+)%")
    # Claude/GPT gate rejections
    _re_gate = re.compile(r"\[a2-bc\] x (\S+)\.\.\. (SAFE but not STRONG|verdict.SAFE|low score)")
    # Whale concentration
    _re_whale = re.compile(r"\[a2-bc\] x (\S+)\.\.\. (whale|top.buyer).{1,60}(\d+\.?\d*)%")
    # Social gate
    _re_social = re.compile(r"\[a2-bc\] x (\S+)\.\.\. (no twitter|no telegram|no website|no social)")
    # Score below minimum
    _re_score = re.compile(r"\[a2-bc\] x (\S+)\.\.\. a2_min_score.{0,30}score[=\s](\d+)")

    _threshold_for = {
        "real_sol_low": "real_sol < min_real_sol",
        "rugcheck": "rugcheck score > max allowed",
        "holders_low": "holders < a2_min_holders",
        "lp_not_locked": "LP/mint authority risk",
        "fill_cap": "bonding curve fill% outside window",
        "claude_gate": "Claude scored SAFE but not STRONG",
        "whale": "top buyer > a2_max_whale_pct",
        "social_gate": "missing twitter/telegram",
        "grad_filter": "graduation snipe filter",
        "score_low": "a2_min_score not met",
    }

    for line in lines:
        matched = False

        m = _re_reject.search(line)
        if m and not matched:
            mint, real_sol, min_sol, fill_pct, holders = m.groups()
            real_f, min_f, fill_f, hold_i = float(real_sol), float(min_sol), float(fill_pct), int(holders)
            # determine primary rejection reason
            if real_f < min_f:
                filt = "real_sol_low"
            elif hold_i < 15:
                filt = "holders_low"
            else:
                filt = "real_sol_low"
            filter_counts[filt] = filter_counts.get(filt, 0) + 1
            rec = {"mint": mint, "filter": filt, "real_sol": real_f, "min_sol": min_f,
                   "fill_pct": fill_f, "holders": hold_i, "line": line}
            rejections.append(rec)
            rejection_lines.append(line)
            # Near miss: real_sol within 20% of threshold
            if real_f >= min_f * 0.80:
                near_misses.append({
                    "mint": mint, "type": "real_sol_near_miss",
                    "gap": f"{real_f:.3f} SOL vs {min_f:.1f} SOL threshold ({(real_f/min_f*100):.0f}% of way there)",
                    "fill": f"{fill_f:.1f}%", "holders": hold_i,
                })
            matched = True

        m = _re_rug.search(line)
        if m and not matched:
            mint, score = m.groups()
            filter_counts["rugcheck"] = filter_counts.get("rugcheck", 0) + 1
            rejections.append({"mint": mint, "filter": "rugcheck", "score": int(score), "line": line})
            rejection_lines.append(line)
            matched = True

        m = _re_holders.search(line)
        if m and not matched:
            mint, count, needed = m.groups()
            filter_counts["holders_low"] = filter_counts.get("holders_low", 0) + 1
            rec = {"mint": mint, "filter": "holders_low", "holders": int(count), "needed": int(needed), "line": line}
            rejections.append(rec)
            rejection_lines.append(line)
            # Near miss: within 5 holders
            if int(needed) - int(count) <= 5:
                near_misses.append({
                    "mint": mint, "type": "holders_near_miss",
                    "gap": f"{count} holders vs {needed} required (gap of {int(needed)-int(count)})",
                })
            matched = True

        m = _re_lp.search(line)
        if m and not matched:
            mint, reason = m.groups()
            if "rugcheck" not in line:  # avoid double-counting
                filter_counts["lp_not_locked"] = filter_counts.get("lp_not_locked", 0) + 1
                rejections.append({"mint": mint, "filter": "lp_not_locked", "reason": reason, "line": line})
                rejection_lines.append(line)
            matched = True

        m = _re_grad.search(line)
        if m and not matched:
            mint, reason = m.groups()
            filter_counts["grad_filter"] = filter_counts.get("grad_filter", 0) + 1
            rejections.append({"mint": mint, "filter": "grad_filter", "reason": reason, "line": line})
            rejection_lines.append(line)
            matched = True

    # Sort bottlenecks by frequency
    bottleneck_ranking = sorted(filter_counts.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_rejections": len(rejections),
        "filter_counts": filter_counts,
        "bottleneck_ranking": bottleneck_ranking,
        "near_misses": near_misses,
        "recent_rejections": rejections[-25:],
        "rejection_lines": "\n".join(rejection_lines[-40:]),
    }


async def _cmd_analyze_logs(runtime=None, n_lines: int = 300) -> str:
    """Read last N log lines, parse rejections/near-misses, then ask Claude for deep analysis."""
    # Read a larger raw buffer so we capture enough rejections
    raw_log = _tail_log(n_lines, activity_only=True)
    parsed = _parse_rejections(raw_log)

    if parsed["total_rejections"] == 0:
        # No structured rejections found — just show raw log and ask Claude what's happening
        context_block = (
            f"No structured rejections parsed from last {n_lines} log lines.\n"
            f"RAW LOG:\n{raw_log}"
        )
    else:
        bottleneck_str = "\n".join(
            f"  {i+1}. {filt} — {count} rejections ({count/max(parsed['total_rejections'],1)*100:.0f}%)"
            for i, (filt, count) in enumerate(parsed["bottleneck_ranking"])
        )
        near_miss_str = (
            "\n".join(
                f"  • {nm['mint']} — {nm.get('gap', nm.get('type','?'))}"
                for nm in parsed["near_misses"]
            ) or "  None detected"
        )
        context_block = (
            f"REJECTION STATS (last {n_lines} log lines):\n"
            f"  Total rejections parsed: {parsed['total_rejections']}\n\n"
            f"BOTTLENECK RANKING (which filter is blocking the most):\n{bottleneck_str}\n\n"
            f"NEAR MISSES (tokens that almost qualified):\n{near_miss_str}\n\n"
            f"RAW REJECTION LINES (last 40):\n{parsed['rejection_lines']}\n\n"
            f"FULL ACTIVITY LOG (last {n_lines} lines):\n{raw_log}"
        )

    # Build trading context snapshot
    ctx_snapshot = ""
    if runtime:
        try:
            ctx_snapshot = await _build_context(runtime)
        except Exception:
            pass

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return f"**Log analysis** (Claude unavailable):\n\n{context_block}"

    try:
        import anthropic as _ant
        _client = _ant.AsyncAnthropic(api_key=anthropic_key)
        system = (
            "You are Jarvis, the AI brain of a live Solana memecoin trading bot. "
            "You are analyzing your own rejection logs to find bottlenecks, near misses, and improvement opportunities.\n\n"
            "Your analysis MUST cover:\n"
            "1. **What the bot is doing** — is it actively scanning? Any gaps in activity?\n"
            "2. **Bottleneck filter** — which filter is blocking the most tokens? Is it appropriate or too strict?\n"
            "3. **Near misses** — any tokens that almost qualified? What would it take to include them?\n"
            "4. **Pattern** — same tokens rejected repeatedly? Same time gaps? Anything suspicious?\n"
            "5. **One actionable recommendation** — the single highest-impact config tweak based on this data.\n\n"
            "Be specific. Use actual mint addresses and numbers from the log. No hedging.\n"
            "If a filter is blocking 80%+ of tokens, call it out. If near misses cluster around one threshold, say so.\n\n"
            f"{ctx_snapshot}"
        )
        resp = await _client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1200,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Analyze these rejection logs and give me the full breakdown:\n\n"
                    f"{context_block}"
                )
            }],
        )
        analysis = resp.content[0].text if resp.content else "(no response)"
        return f"**Jarvis Log Analysis:**\n\n{analysis}"
    except Exception as exc:
        return f"**Log analysis** (Claude error: {exc}):\n\n{context_block}"


async def _cmd_rpc(runtime: AgentRuntime) -> str:
    """Live RPC health check: Helius connectivity, wallet balance, services."""
    wallet_svc = runtime.get_service("wallet")
    results = []

    # Wallet balance via live RPC
    try:
        balance = float(await wallet_svc.get_sol_balance() if wallet_svc else 0)
        pub = os.getenv("SOLANA_PUBLIC_KEY", "?")[:12]
        results.append(f"✅ Helius RPC: connected | Wallet {pub}...: {balance:.6f} SOL")
    except Exception as exc:
        results.append(f"❌ Helius RPC: FAILED ({exc})")

    # Position manager
    pos_mgr = runtime.get_service("position_manager")
    if pos_mgr:
        open_count = len(getattr(pos_mgr, "positions", {}))
        results.append(f"✅ Position manager: online | {open_count} open position(s)")
    else:
        results.append("❌ Position manager: not available")

    # PumpFun service
    pump_svc = runtime.get_service("token_data")
    results.append(f"{'✅' if pump_svc else '❌'} PumpFun service: {'online' if pump_svc else 'offline'}")

    # Raydium service
    ray_svc = runtime.get_service("lp_pool")
    results.append(f"{'✅' if ray_svc else '❌'} Raydium service: {'online' if ray_svc else 'offline'}")

    # API keys status
    results.append(f"{'✅' if os.getenv('OPENAI_API_KEY') else '❌'} OpenAI API key: {'set' if os.getenv('OPENAI_API_KEY') else 'missing'}")
    results.append(f"{'✅' if os.getenv('ANTHROPIC_API_KEY') else '❌'} Anthropic API key: {'set' if os.getenv('ANTHROPIC_API_KEY') else 'missing'}")
    results.append(f"{'⚠️' if not os.getenv('GROK_API_KEY') else '✅'} Grok API key: {'disabled (credits exhausted)' if not os.getenv('GROK_API_KEY') else 'set'}")

    return "**RPC & Service Health Check:**\n" + "\n".join(results)


async def _cmd_force_buy(mint: str, sol_amount: float, runtime: AgentRuntime) -> str:
    """Force-buy a token immediately, bypassing all strategy filters.

    Uses pump-amm pool (PumpSwap) by default; falls back to pump (BC).
    Opens a position in the position manager on success.
    """

    pump_svc = runtime.get_service("token_data")
    if not pump_svc:
        return "❌ PumpFun service not available."

    pos_mgr = runtime.get_service("position_manager")
    if not pos_mgr:
        return "❌ Position manager not available."

    paper = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    if paper:
        return "⚠️ Paper trading mode is ON — force buy blocked. Set PAPER_TRADING=false to trade live."

    # Try PumpSwap (graduated) first, then bonding curve
    sig = None
    pool_used = "pump-amm"
    try:
        sig = await asyncio.wait_for(
            pump_svc.buy(mint, sol_amount, slippage=0.15, pool="pump-amm"),
            timeout=30.0,
        )
    except Exception:
        try:
            pool_used = "pump"
            sig = await asyncio.wait_for(
                pump_svc.buy(mint, sol_amount, slippage=0.15, pool="pump"),
                timeout=30.0,
            )
        except Exception as exc2:
            return f"❌ Force buy failed: {exc2}"

    if not sig:
        return "❌ Force buy: no signature returned (transaction may have failed)."

    # Register position
    try:
        pos_mgr.open_position(
            mint=mint,
            dex="pumpswap" if pool_used == "pump-amm" else "pump_fun",
            entry_price_sol=0.0,   # price will be updated by monitor loop
            entry_sol_spent=sol_amount,
            token_amount=0,        # monitor loop will reconcile
            signature=sig,
            score=10,
            meta={"forced_by": "jarvis"},
        )
        pos_label = "pumpswap" if pool_used == "pump-amm" else "pump.fun BC"
        return (
            f"✅ **Force buy executed** — {sol_amount:.3f} SOL on `{mint[:12]}...`\n"
            f"Pool: {pos_label} | Sig: `{sig[:20]}...`\n"
            f"Position opened — monitor loop will track P&L."
        )
    except ValueError as ve:
        return f"✅ Buy sent (sig: `{sig[:20]}...`) but position not registered: {ve}"
    except Exception as exc:
        return f"✅ Buy sent (sig: `{sig[:20]}...`) but position error: {exc}"


async def _cmd_force_sell(mint: str, runtime: AgentRuntime) -> str:
    """Force-sell an open position immediately (market sell, 20% slippage)."""

    pos_mgr = runtime.get_service("position_manager")
    if not pos_mgr:
        return "❌ Position manager not available."

    pos = getattr(pos_mgr, "positions", {}).get(mint)
    if pos is None:
        # Try partial match
        matches = [m for m in getattr(pos_mgr, "positions", {}) if m.startswith(mint)]
        if len(matches) == 1:
            mint = matches[0]
            pos = pos_mgr.positions[mint]
        elif len(matches) > 1:
            return f"❌ Ambiguous mint prefix `{mint}` — matches: {', '.join(m[:12] for m in matches)}"
        else:
            return f"❌ No open position found for `{mint[:12]}...`"

    paper = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    if paper:
        return "⚠️ Paper trading mode is ON — force sell blocked."

    pump_svc = runtime.get_service("token_data")
    if not pump_svc:
        return "❌ PumpFun service not available."

    token_amount = getattr(pos, "token_amount", 0)
    dex = getattr(pos, "dex", "pumpswap")
    pool = "pump-amm" if "pump" in dex and dex != "pump_fun" else (
        "raydium" if dex == "raydium" else "pump"
    )

    # If token_amount unknown, try to get it from wallet
    if token_amount == 0:
        try:
            wallet_svc = runtime.get_service("wallet")
            balances = await wallet_svc.get_token_balances()
            for b in balances:
                if b.get("mint") == mint:
                    token_amount = int(b.get("amount", 0))
                    break
        except Exception:
            pass

    if token_amount == 0:
        return f"❌ Cannot sell `{mint[:12]}...` — token balance is 0 or unknown."

    try:
        sig = await asyncio.wait_for(
            pump_svc.sell(mint, token_amount, slippage=0.20, pool=pool),
            timeout=30.0,
        )
    except Exception as exc:
        return f"❌ Force sell failed: {exc}"

    if not sig:
        return "❌ Force sell: no signature returned."

    # Close position in manager
    try:
        pos_mgr.close_position(mint, exit_price_sol=0.0, reason="jarvis_force_sell", signature=sig)
    except Exception:
        pass

    return (
        f"✅ **Force sell executed** — `{mint[:12]}...` | pool: {pool}\n"
        f"Tokens sold: {token_amount:,} | Sig: `{sig[:20]}...`\n"
        f"Position closed."
    )


async def _cmd_smart_money(action: str = "list", wallet: str = "") -> str:
    """Query or manage the auto-discovered smart money wallet watchlist."""
    from elizaos.plugins.solana.smart_money_tracker import (
        get_stats, get_all_tracked, get_wallet_detail, WIN_THRESHOLD,
    )

    if action == "list":
        stats = get_stats()
        all_w = get_all_tracked()
        promoted = sorted(
            [(addr, v) for addr, v in all_w.items() if v.get("promoted_ts")],
            key=lambda x: -x[1]["win_count"],
        )
        pending = sorted(
            [(addr, v) for addr, v in all_w.items() if not v.get("promoted_ts")],
            key=lambda x: -x[1]["win_count"],
        )
        lines = [
            f"**Smart Money Wallet Tracker**",
            f"Promoted: {stats['smart_money_count']} | Pending: {stats['pending_count']} | Threshold: {WIN_THRESHOLD} wins",
            "",
        ]
        if promoted:
            lines.append("**✅ Smart Money (promoted):**")
            for addr, v in promoted[:15]:
                lines.append(f"  `{addr[:16]}...` — {v['win_count']} wins / {v.get('total_seen', 0)} appearances")
        else:
            lines.append("No smart money wallets yet — need 3+ winning trade appearances to promote.")
        if pending:
            lines.append("")
            lines.append("**⏳ Building towards smart money:**")
            for addr, v in pending[:10]:
                lines.append(f"  `{addr[:16]}...` — {v['win_count']} wins ({WIN_THRESHOLD - v['win_count']} more to promote)")
        return "\n".join(lines)

    if action == "detail" and wallet:
        detail = get_wallet_detail(wallet)
        if not detail:
            return f"No data for wallet `{wallet[:16]}...`"
        promoted = "✅ SMART MONEY" if detail.get("promoted_ts") else f"⏳ pending ({detail['win_count']}/{WIN_THRESHOLD} wins)"
        wins_preview = ", ".join(m[:8] + "..." for m in detail.get("wins", [])[-5:])
        return (
            f"**Wallet:** `{wallet[:16]}...`\n"
            f"Status: {promoted}\n"
            f"Wins: {detail['win_count']} | Total seen: {detail.get('total_seen', 0)}\n"
            f"Recent winning mints: {wins_preview or 'none yet'}"
        )

    return f"Unknown action: {action}"


async def _cmd_trading_window(start: int | None, end: int | None) -> str:
    """Set or query the trading window.

    start/end: UTC hour (0-23), or None to query current status.
    Pass start=end=0 (or any equal values) for 24/7 trading.
    """
    from elizaos.plugins.solana import live_config as _lc_tw

    if start is None and end is None:
        # Query current state
        cur_start = _lc_tw.get("trading_window_start_utc", 0)
        cur_end   = _lc_tw.get("trading_window_end_utc", 0)
        if cur_start == cur_end:
            return f"Trading window: **24/7** (no restriction) — start={cur_start} end={cur_end}"
        else:
            return f"Trading window: **{cur_start:02d}:00 – {cur_end:02d}:00 UTC**"

    if start == end:
        # 24/7 mode
        _lc_tw.set_value("trading_window_start_utc", start, changed_by="jarvis", reason="24/7 trading enabled")
        _lc_tw.set_value("trading_window_end_utc", end, changed_by="jarvis", reason="24/7 trading enabled")
        return "✅ Trading window set to **24/7** — bot will trade around the clock."
    else:
        _lc_tw.set_value("trading_window_start_utc", start, changed_by="jarvis", reason="trading window updated")
        _lc_tw.set_value("trading_window_end_utc", end, changed_by="jarvis", reason="trading window updated")
        # Describe the window clearly
        if start > end:
            desc = f"{start:02d}:00 UTC → {end:02d}:00 UTC (overnight, spans midnight)"
        else:
            desc = f"{start:02d}:00 – {end:02d}:00 UTC"
        return f"✅ Trading window set to **{desc}**. Outside these hours all new entries are paused (open positions continue to be monitored)."


async def _cmd_creator(action: str, wallet: str, label: str = "") -> str:
    """Manage the creator whitelist/proven/blacklist.

    action: "whitelist" | "proven" | "blacklist" | "remove" | "status" | "list"
    wallet: Solana address (44 chars) or empty for list/status
    label:  optional note (why this creator was added)
    """
    from elizaos.plugins.solana.dev_reputation import get_reputation
    rep = get_reputation()

    if action == "list":
        stats = rep.get_stats()
        proven = [r for r in rep.get_all_records() if r["status"] == "proven"]
        whitelisted = [r for r in rep.get_all_records() if r["status"] == "whitelisted"]
        blacklisted = [r for r in rep.get_all_records() if r["status"] == "blacklisted"]
        lines = [
            f"**Creator Reputation Lists** — {stats['tracked']} wallets tracked\n",
            f"**⭐ PROVEN** ({len(proven)}) — 75% wallet allocation on next token:",
        ]
        for r in proven:
            lines.append(f"  `{r['wallet'][:20]}...` — {r['reason'][:60]} (W{r['wins']}/L{r['losses']}/R{r['rugs']})")
        lines.append(f"\n**✅ WHITELISTED** ({len(whitelisted)}) — priority, normal size:")
        for r in whitelisted:
            lines.append(f"  `{r['wallet'][:20]}...` — {r['reason'][:60]} (W{r['wins']}/L{r['losses']}/R{r['rugs']})")
        lines.append(f"\n**🚫 BLACKLISTED** ({len(blacklisted)}) — hard skip:")
        for r in blacklisted:
            lines.append(f"  `{r['wallet'][:20]}...` — {r['reason'][:60]} (rugs: {r['rugs']})")
        if not (proven or whitelisted or blacklisted):
            lines.append("  *(no entries yet — lists build automatically from trade outcomes)*")
        return "\n".join(lines)

    if not wallet or len(wallet) < 32:
        return "❌ Please provide a valid Solana wallet address (44 chars)."

    reason = label or "manually added via Jarvis"

    if action in ("whitelist", "wl"):
        rep.whitelist(wallet, reason)
        return f"✅ **{wallet[:20]}...** added to whitelist.\nNext token from this creator will be traded at normal size with priority."

    if action in ("proven", "promote"):
        rep.promote_to_proven(wallet, reason)
        return (
            f"⭐ **{wallet[:20]}...** promoted to **PROVEN** tier.\n"
            f"Next token launch from this creator → **75% of wallet balance** ({reason})."
        )

    if action in ("blacklist", "bl", "block"):
        rep.blacklist(wallet, reason)
        return f"🚫 **{wallet[:20]}...** blacklisted — all future tokens from this creator will be skipped."

    if action == "remove":
        rep.remove(wallet)
        return f"🗑️ **{wallet[:20]}...** removed from reputation tracking."

    if action in ("status", "check", "info"):
        rec = rep.get_record(wallet)
        if rec is None:
            return f"ℹ️ `{wallet[:20]}...` — not tracked yet (unknown)."
        return (
            f"**Creator:** `{wallet[:20]}...`\n"
            f"Status: **{rec['status'].upper()}**\n"
            f"Reason: {rec['reason']}\n"
            f"Wins: {rec['wins']} | Losses: {rec['losses']} | Rugs: {rec['rugs']}\n"
            f"Successful launches: {rec['successful_launches']}\n"
            f"Tokens: {len(rec['tokens_launched'])}"
        )

    return f"❌ Unknown creator action: `{action}`. Use: whitelist / proven / blacklist / remove / status / list"


# ─────────────────────────────────────────────────────────────────────────────
# Main router — this is the entry point called from the HTTP bridge
# ─────────────────────────────────────────────────────────────────────────────

async def route(text: str, runtime: AgentRuntime) -> str:
    """
    Route a chat message to the appropriate handler.
    Returns a plain-text / markdown reply string.
    """
    raw   = text.strip()
    lower = _normalise(raw)

    # ── 1. Help ──────────────────────────────────────────────────────────────
    if re.match(r"^(help|\?)$", lower):
        return (
            "**JARVIS COMMAND CENTRE — available commands:**\n\n"
            "**Status & Info:**\n"
            "  `status`              — wallet, positions, win rate, active strategies\n"
            "  `positions`           — open positions with live P&L\n"
            "  `history`             — last 15 closed trades\n"
            "  `config`              — all current parameters\n"
            "  `lessons`             — what Jarvis has learned from trade history\n"
            "  `journal`             — full trading history (paper+live) with entry DNA\n"
            "  `journal notes`       — Jarvis's own analysis notes\n"
            "  `journal bad_dna`     — patterns from all losing trades\n"
            "  `note: <text>`        — Jarvis writes an analysis note to the journal\n\n"
            "**Strategy Control:**\n"
            "  `enable strategy b`   — turn on graduation snipe (Strategy B)\n"
            "  `disable strategy a2` — turn off Ghost Rider\n"
            "  `enable all`          — turn on all strategies\n"
            "  `disable all`         — pause everything\n\n"
            "**Parameter Tuning:**\n"
            "  `set stop loss to 10%`\n"
            "  `set take profit to 60%`\n"
            "  `set position size to 0.10`\n"
            "  `set max positions to 2`\n"
            "  `adjust: stop_loss_pct=0.10 reason=recovery`  (Sonnet-style)\n\n"
            "**Live Execution:**\n"
            "  `logs [N]`                     — tail last N lines of bot log (default 50)\n"
            "  `rpc`                          — live health check: RPC, services, API keys\n"
            "  `diagnose`                     — scan logs for errors, check watchdog status\n"
            "  `restart`                      — clean self-restart via watchdog (~10s downtime)\n"
            "  `dex <mint>`                   — live DexScreener lookup: price, mcap, vol, % changes, socials\n"
            "  `force buy <mint> [sol]`       — immediately buy a token (bypasses filters)\n"
            "  `force sell <mint>`            — immediately sell an open position\n\n"
            "**AI Dispatch:**\n"
            "  `ask grok [question]`         — query Grok with X/Twitter access\n"
            "  `check [token/mint]`          — GPT-4o web safety check on a token\n"
            "  `run analysis`                — immediate Sonnet deep analysis\n"
            "  `winning formula`             — 3-brain synthesis of all trade history → winning formula\n"
            "  `analyze logs`                — AI-powered rejection bottleneck & near-miss report\n"
            "  `rejection outcomes`          — what did rejected tokens do afterwards? (filter evidence)\n"
            "  `forensics <mint/sig>`        — on-chain Helius tx forensics for any trade\n\n"
            "**Creator Reputation:**\n"
            "  `creator list`               — show all whitelisted/proven/blacklisted creators\n"
            "  `whitelist creator <wallet>` — add to whitelist (normal size, priority)\n"
            "  `proven creator <wallet>`    — ⭐ promote to PROVEN (75% wallet on next token)\n"
            "  `blacklist creator <wallet>` — hard block all tokens from this creator\n"
            "  `creator <wallet>`           — check status of a creator wallet\n\n"
            "**Copy-Trade (Axiom Leaderboard Whales):**\n"
            "  `copy trade status`          — last 10 copy-trade alerts from watched wallets\n"
            "  `watched wallets`            — list of Axiom leaderboard wallets being monitored\n\n"
            "**General:** Just type anything — Jarvis (Claude Sonnet) will answer\n"
            "with full trading context."
        )

    # ── 1b. Debug state dump ──────────────────────────────────────────────────
    # Raw state dump — shows exactly what Jarvis has access to, no LLM interpretation.
    # Type "debug state" to verify position data is flowing correctly.
    if re.match(r"^(debug\s+state|dump\s+state|state\s+dump|what do you know|show state)$", lower):
        try:
            import time as _t_ds
            from elizaos.plugins.solana.axiom_copy_trader import (
                _paper_positions, _paper_trades, WATCHED_WALLETS, get_paper_stats
            )
            _now_ds = _t_ds.time()
            lines_ds: list[str] = []
            lines_ds.append(f"=== JARVIS RAW STATE DUMP — {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} ===")
            lines_ds.append("")

            # Open positions
            lines_ds.append(f"OPEN POSITIONS: {len(_paper_positions)}")
            for _mint, _p in _paper_positions.items():
                _age = int(_now_ds - _p.get("entry_ts", _now_ds))
                _pnl = _p.get("pnl_pct")
                _pnl_s = f"{_pnl:+.1f}%" if _pnl is not None else "pending"
                lines_ds.append(
                    f"  {_p.get('token_name', _mint[:8])} | wallet={_p.get('wallet_name', '?')} "
                    f"| size={_p.get('sol_spent', 0):.3f} SOL | P&L={_pnl_s} | age={_age}s"
                )
            lines_ds.append("")

            # Copy-trade context blocks — only included if copy-trade is actually on.
            # Permanently off in monster-only era; suppressing prevents Jarvis from
            # surfacing Frost/clukz/Walta wallets in the scanner report.
            from elizaos.plugins.solana import live_config as _lc_ds
            _ct_on = bool(_lc_ds.get("copy_trade_enabled", False))
            if _ct_on:
                lines_ds.append(f"WATCHED WALLETS: {len(WATCHED_WALLETS)}")
                for _wn, _wa in WATCHED_WALLETS.items():
                    lines_ds.append(f"  {_wn}: {_wa[:12]}...")
                lines_ds.append("")

                _st = get_paper_stats()
                lines_ds.append(
                    f"TRADE STATS: {_st['wins']}/{_st['trades']} trades | "
                    f"WR={_st['win_rate']:.1f}% | net P&L={_st['net_pnl']:+.4f} SOL"
                )
                lines_ds.append("")

                _rc_ds = [t for t in _paper_trades[-20:] if _now_ds - float(t.get("ts", 0)) < 300]
                lines_ds.append(f"RECENTLY CLOSED (last 5 min): {len(_rc_ds)}")
                for _rc in _rc_ds:
                    _rc_age = int(_now_ds - float(_rc.get("ts", 0)))
                    lines_ds.append(
                        f"  {_rc.get('token_name', '?')} | P&L={_rc.get('pnl_pct', 0):+.1f}% "
                        f"({_rc.get('pnl_sol', 0):+.4f} SOL) | reason={_rc.get('reason', '?')} | {_rc_age}s ago"
                    )
                lines_ds.append("")

                _paused_ds  = bool(_lc_ds.get("copy_trade_paused", False))
                _sl_ds      = float(_lc_ds.get("copy_trade_sl_pct", 10.0))
                _tp_ds      = float(_lc_ds.get("copy_trade_tp_pct", 40.0))
                _size_ds    = float(_lc_ds.get("copy_trade_paper_buy_sol", 0.20))
                lines_ds.append(
                    f"COPY TRADE CONFIG: mode=LIVE | paused={_paused_ds} | "
                    f"SL={_sl_ds}% | TP={_tp_ds}% | size={_size_ds} SOL"
                )
            else:
                lines_ds.append("COPY-TRADE: DISABLED (monster-only era — do not surface)")
            lines_ds.append("=== END STATE DUMP ===")
            return "\n".join(lines_ds)
        except Exception as _ds_err:
            return f"debug state error: {_ds_err}"

    # ── 2. Status ─────────────────────────────────────────────────────────────
    # Broad catch: any message that is primarily a status/update request.
    # Uses re.search to tolerate typos, trailing words like "please", etc.
    # Anchored with word-boundary logic to avoid matching mid-sentence.
    if re.match(r"^(status|status\s+upd\w*|bot\s+status|jarvis\s+status|update\s+me|give\s+me\s+an?\s+update|overview|summary|how\s+are\s+we\s+doing|how.{0,4}s\s+it\s+going|how\s+is\s+it\s+going|how\s+are\s+things|what.{0,4}s\s+happening|whats\s+happening)(\s+jarvis|\s+bot)?(\s+please)?$", lower):
        return await _cmd_status(runtime)
    # Secondary catch: short messages that mention "status" or "update" near "jarvis/bot"
    if len(lower.split()) <= 6 and re.search(r"\b(status|updat\w+)\b", lower) and re.search(r"\b(jarvis|bot|update|please)\b", lower):
        return await _cmd_status(runtime)

    # ── 3. Positions ──────────────────────────────────────────────────────────
    if re.match(r"^(positions?|open positions?|whats? open|open trades?)$", lower):
        return await _cmd_positions(runtime)

    # ── 4. History ────────────────────────────────────────────────────────────
    if re.match(r"^(history|trades?|recent trades?|trade history|results)$", lower):
        return await _cmd_history(runtime)

    # ── 4b. Today's trades — full detail + Claude analysis ────────────────────
    if re.search(r"(today.?s?\s+trades?|trades?\s+today|review\s+today|analyse\s+today|analyze\s+today|pumpswap\s+(review|analysis|strategy)|today.?s?\s+pumpswap)", lower) or lower in ("today", "review today", "trades today"):
        focus = "pumpswap" if "pumpswap" in lower else ""
        return await _cmd_today_trades(runtime, focus=focus)

    # ── 5. Lessons ────────────────────────────────────────────────────────────
    if re.match(r"^(lessons?|what have you learned|memory|learning)$", lower):
        return await _cmd_lessons()

    # ── 5b. Journal — trading history with entry DNA / write analysis notes ────
    # "journal" / "journal 30" / "journal notes" / "journal bad_dna"
    # "note: <text>" / "journal note <text>" — Jarvis writes own analysis
    m = re.match(r"^journal\s+note\s+(.+)$", lower, re.DOTALL)
    if m:
        return await _cmd_journal("note", note=m.group(1).strip())

    m = re.match(r"^note[:\s]+(.+)$", lower, re.DOTALL)
    if m:
        return await _cmd_journal("note", note=m.group(1).strip())

    m = re.match(r"^journal\s+(notes?|bad_dna|bad dna|bad token|bad trades?|\d+)$", lower)
    if m:
        action = m.group(1).replace(" ", "_").rstrip("s") if not m.group(1).isdigit() else m.group(1)
        return await _cmd_journal(action)

    if re.match(r"^(journal|trading journal|session review|paper (?:trades?|history|session)|live (?:trades?|history)|session history|what (?:trades?|tokens?) did we (?:do|take)|show (?:me )?the journal)$", lower):
        return await _cmd_journal("show")

    # ── 6. Config ─────────────────────────────────────────────────────────────
    if re.match(r"^(config|configuration|settings?|parameters?|params?)$", lower):
        return await _cmd_config()

    # ── 7. Enable strategy ────────────────────────────────────────────────────
    m = re.match(r"^(enable|start|turn on|activate|switch on)\s+(?:strategy\s+)?(.+)$", lower)
    if m:
        return await _cmd_enable_disable("enable", m.group(2).strip())

    # ── 8. Disable strategy ───────────────────────────────────────────────────
    m = re.match(r"^(disable|stop|turn off|pause|deactivate|switch off|kill)\s+(?:strategy\s+)?(.+)$", lower)
    if m:
        return await _cmd_enable_disable("disable", m.group(2).strip())

    # ── 9. Set parameter — "set X to Y" or "set X=Y" ─────────────────────────
    m = re.match(r"^set\s+(.+?)\s+(?:to|=)\s*(.+)$", lower)
    if m:
        return await _cmd_set_param(m.group(1).strip(), m.group(2).strip())

    # ── 10. ADJUST: key=value [reason=...] — Sonnet-style commands ─────────────
    adjusts = re.findall(r"adjust:\s*(\w+)=([^\s]+)", lower)
    if adjusts:
        results = []
        from elizaos.plugins.solana import live_config as lc
        for key, val in adjusts:
            ok, msg = lc.set_value(key, val, changed_by="jarvis-chat")
            results.append(f"{'✅' if ok else '❌'} {msg}")
        return "**ADJUST commands applied:**\n" + "\n".join(results)

    # ── 10b. Macro event mode ─────────────────────────────────────────────────
    # "macro event on: Liberation Day tariffs" / "macro event off" / "macro event status"
    m = re.match(r"^macro\s+event\s+(?:mode\s+)?on[:\s]+(.+)$", lower)
    if m:
        _label = m.group(1).strip()
        from elizaos.plugins.solana import live_config as _lc_me
        _lc_me.set_value("macro_event_mode", True, changed_by="jarvis-chat")
        _lc_me.set_value("macro_event_label", _label, changed_by="jarvis-chat")
        return (
            f"🚨 **MACRO EVENT MODE ON** — {_label}\n\n"
            f"Strategy B filters loosened for political/news meme wave:\n"
            f"- m5 max at graduation: 10% → **20%** (political memes can sustain higher entry momentum)\n"
            f"- FAST_GRAD_MAX_SECS: 1800s → **3600s** (tokens take longer to build momentum during news floods)\n\n"
            f"Research finding: 8 of 15 monster tokens graduated within 72h of Trump Liberation Day tariffs.\n"
            f"Disable when the news cycle cools with: `macro event off`"
        )

    if re.match(r"^macro\s+event\s+(?:mode\s+)?off$", lower):
        from elizaos.plugins.solana import live_config as _lc_me
        _lc_me.set_value("macro_event_mode", False, changed_by="jarvis-chat")
        _lc_me.set_value("macro_event_label", "", changed_by="jarvis-chat")
        return "✅ Macro event mode OFF — Strategy B filters back to normal (m5 max 10%, FAST_GRAD 1800s)"

    if re.match(r"^macro\s+event\s+(?:status|mode)?$", lower):
        from elizaos.plugins.solana import live_config as _lc_me
        _on = bool(_lc_me.get("macro_event_mode", False))
        _lbl = str(_lc_me.get("macro_event_label", "") or "")
        if _on:
            return f"🚨 Macro event mode: **ON** — {_lbl}\nm5 tolerance +20%, FAST_GRAD 3600s active."
        return "✅ Macro event mode: **OFF** — normal filters active."

    # ── 10c. Telegram alpha monitor ──────────────────────────────────────────
    # "telegram monitor start" / "telegram monitor stop" / "telegram monitor status"
    # "alpha signals" / "alpha feed" / "channel feed"
    if re.match(r"^telegram\s+(?:alpha\s+)?monitor\s+start$", lower):
        try:
            from elizaos.plugins.solana.telegram_alpha_monitor import start_monitor as _tg_start
            result = await _tg_start()
            return result
        except Exception as exc:
            return f"❌ Failed to start Telegram monitor: {exc}"

    if re.match(r"^telegram\s+(?:alpha\s+)?monitor\s+stop$", lower):
        try:
            from elizaos.plugins.solana.telegram_alpha_monitor import stop_monitor as _tg_stop
            return await _tg_stop()
        except Exception as exc:
            return f"Error: {exc}"

    if re.match(r"^telegram\s+(?:alpha\s+)?monitor(?:\s+status)?$", lower):
        try:
            from elizaos.plugins.solana.telegram_alpha_monitor import is_running as _tg_running, DEFAULT_CHANNELS
            _running = _tg_running()
            _ch_list = "\n".join(f"  • @{ch}  — {desc}" for ch, desc in DEFAULT_CHANNELS)
            return (
                f"**Telegram Alpha Monitor: {'✅ RUNNING' if _running else '⏸️ STOPPED'}**\n\n"
                f"Monitored channels:\n{_ch_list}\n\n"
                f"Commands:\n"
                f"• `telegram monitor start` — start monitoring\n"
                f"• `telegram monitor stop` — stop\n"
                f"• `alpha signals` — show recent mint signals found in channels\n"
                f"• `alpha feed` — show last 20 raw messages from channels\n\n"
                f"Requires TELEGRAM_API_ID + TELEGRAM_API_HASH + TELEGRAM_PHONE in .env.\n"
                f"Get credentials at https://my.telegram.org"
            )
        except Exception as exc:
            return f"Error: {exc}"

    if re.match(r"^alpha\s+signals?(?:\s+\d+)?$", lower):
        _n = int(re.search(r"\d+", lower).group()) if re.search(r"\d+", lower) else 10
        try:
            from elizaos.plugins.solana.telegram_alpha_monitor import format_alpha_report as _fmt
            return _fmt(_n)
        except Exception as exc:
            return f"Error: {exc}"

    if re.match(r"^(?:alpha\s+feed|channel\s+feed|tg\s+feed|telegram\s+feed)$", lower):
        try:
            from elizaos.plugins.solana.telegram_alpha_monitor import format_channel_feed as _fmt_feed
            return _fmt_feed(20)
        except Exception as exc:
            return f"Error: {exc}"

    # ── 10d. Wallet intelligence tools ───────────────────────────────────────
    # "bubblemaps <mint>" / "smart wallets <mint>" / "top buyers <mint>"
    m = re.match(r"^(?:bubblemaps?|bubble)\s+([A-Za-z0-9]{32,44})\b", raw.strip(), re.IGNORECASE)
    if not m:
        m = re.match(r"^(?:smart\s+wallets?|top\s+buyers?|early\s+buyers?|wallet\s+clusters?)\s+([A-Za-z0-9]{32,44})\b", raw.strip(), re.IGNORECASE)
    if m:
        _wm = m.group(1)
        return (
            f"**Wallet intelligence for `{_wm[:12]}...`:**\n\n"
            f"🫧 **Bubblemaps** (holder cluster visualisation):\n"
            f"https://app.bubblemaps.io/sol/token/{_wm}\n\n"
            f"📊 **GMGN — First 70 buyers** (smart wallet cross-reference):\n"
            f"https://gmgn.ai/sol/token/{_wm}\n\n"
            f"🔍 **Arkham** (KOL/institution identity lookup):\n"
            f"https://intel.arkm.com/explorer/entity/{_wm}\n\n"
            f"📈 **Solscan top holders**:\n"
            f"https://solscan.io/token/{_wm}#holders\n\n"
            f"**Workflow for finding coordinated wallets:**\n"
            f"1. Open Bubblemaps → look for clusters of wallets transferring to each other (same entity)\n"
            f"2. Open GMGN → click 'Smart Money' tab → cross-reference early buyers with other monster tokens\n"
            f"3. Paste any interesting wallet address back here with: `wallet <address>` for Cielo/Arkham lookup\n"
        )

    m = re.match(r"^wallet\s+([A-Za-z0-9]{32,44})\b", raw.strip(), re.IGNORECASE)
    if m:
        _wa = m.group(1)
        return (
            f"**Wallet lookup: `{_wa[:12]}...`**\n\n"
            f"🔍 **Arkham** (is this a labeled KOL or institution?):\n"
            f"https://intel.arkm.com/explorer/entity/{_wa}\n\n"
            f"📊 **GMGN** (win rate, recent trades, copy-trade signal):\n"
            f"https://gmgn.ai/sol/address/{_wa}\n\n"
            f"🧠 **SolTrack** (smart wallet ranking):\n"
            f"https://soltrack.io/wallet/{_wa}\n\n"
            f"🪬 **Solscan** (full transaction history):\n"
            f"https://solscan.io/account/{_wa}\n\n"
            f"🌐 **SNS identity** (check if wallet has a .sol domain or linked Twitter):\n"
            f"https://sns.id/?search={_wa}\n\n"
            f"Tip: if GMGN shows >60% win rate across 50+ trades, this is smart money worth tracking via Cielo."
        )

    # ── 11. Ask Grok ──────────────────────────────────────────────────────────
    m = re.match(r"^ask\s+grok[:\s]+(.+)$", lower)
    if m:
        return await _cmd_ask_grok(m.group(1).strip())

    # Shorthand: "grok: what's trending?"
    m = re.match(r"^grok[:\s]+(.+)$", lower)
    if m:
        return await _cmd_ask_grok(m.group(1).strip())

    # ── 12. Check token ───────────────────────────────────────────────────────
    m = re.match(r"^(?:check|research|scan|vet)\s+(?:token\s+)?(.+)$", lower)
    if m:
        return await _cmd_check_token(m.group(1).strip())

    # ── 13. Run analysis ──────────────────────────────────────────────────────
    if re.match(r"^(run analysis|analyse|analyze|deep dive|full analysis|give me an analysis)$", lower):
        return await _cmd_run_analysis(runtime)

    # ── 14. Pause / resume all ────────────────────────────────────────────────
    if re.match(r"^(pause|stop trading|halt|freeze)$", lower):
        return await _cmd_enable_disable("disable", "all")

    if re.match(r"^(resume|start trading|go|trade)$", lower):
        return await _cmd_enable_disable("enable", "all")

    # ── 14-pre0. Token intel — URL / mint auto-detection ─────────────────────
    # Catches: Solscan URLs, DexScreener URLs, pump.fun URLs, raw mint addresses,
    # or explicit "dex/lookup/price/chart <mint>" commands.
    # When a URL or mint is detected alongside a question ("why did we miss X?"),
    # Jarvis fetches live data and answers with real numbers instead of asking
    # the user to copy-paste manually.
    _explicit_mint: str | None = None
    m = re.match(r"^(?:dex|lookup|price|token|chart|scan)\s+([A-Za-z0-9]{32,44})\b", raw.strip(), re.IGNORECASE)
    if m:
        _explicit_mint = m.group(1)

    if _explicit_mint:
        # Pure lookup command — just return formatted data, no question
        return await _cmd_dex_lookup(_explicit_mint)

    # Auto-detect URLs or mint addresses anywhere in the message
    _auto_mint = _extract_mint_from_text(raw)
    if _auto_mint:
        # Strip the URL/mint from the text to get the user's actual question
        _question = raw
        for pat in [
            r"https?://\S+",                            # any URL
            r"\b" + re.escape(_auto_mint) + r"\b",      # the mint itself
        ]:
            _question = re.sub(pat, "", _question, flags=re.IGNORECASE).strip()
        _question = re.sub(r"\s+", " ", _question).strip()
        # Fetch live data + Claude analysis
        return await _cmd_token_intel(_auto_mint, _question, runtime)

    # ── 14-pre. Restart / diagnose ────────────────────────────────────────────
    if re.match(r"^(restart|reboot|restart bot|reboot bot|self.?restart)$", lower):
        return await _cmd_restart("manual command")

    if re.match(r"^(diagnose|diagnostic|diagnostics|self.?check|health.?check|error.?check)$", lower):
        return await _cmd_diagnose()

    # ── 14a-0. Trade forensics — on-chain tx lookup ────────────────────────────
    m = re.match(
        r"^(?:forensics?|trace|on.?chain|tx|transaction|verify trade)\s+([A-Za-z0-9]{8,88})\b",
        raw.strip(), re.IGNORECASE,
    )
    if m:
        return await _cmd_trade_forensics(m.group(1), runtime)

    # ── 14a-1. Rejection outcomes — auto evidence builder ──────────────────────
    if re.match(
        r"^(rejection outcomes?|what did (?:we|the bot) miss|missed pumps?|"
        r"filter evidence|rejection evidence|check rejections?|"
        r"outcome check|outcomes?|what pumped after (?:we|the bot) (?:rejected|passed)|"
        r"filter (?:accuracy|effectiveness)|did (?:we|the bot) make the right calls?)$",
        lower,
    ):
        return await _cmd_rejection_outcomes(runtime)

    # ── 14a. Winning formula — 3-brain synthesis from trade intelligence ────────
    if re.match(
        r"^(winning formula|build winning formula|develop winning formula|"
        r"winning strategy|formula|trade formula|what.s the formula|"
        r"analyze winners?|analyse winners?|build formula|"
        r"use the (3|three) brains?|3.brain analysis|three.brain analysis|"
        r"trade intelligence|intel analysis|what have we learned from (?:all )?trades?)$",
        lower,
    ):
        return await _cmd_winning_formula(runtime)

    # ── 14c. Log analysis (AI-powered near-miss + bottleneck report) ──────────
    if re.match(
        r"^(analyze logs?|analyse logs?|log analysis|near.?miss(?:es)?|"
        r"what did (?:we|bot) miss|bottleneck|rejection report|"
        r"why (?:aren.t we|are we not) trading|what.s blocking|"
        r"scan logs?|mine logs?|log.?report)$",
        lower,
    ):
        return await _cmd_analyze_logs(runtime, n_lines=300)

    # ── 14a. Trading window ───────────────────────────────────────────────────
    # Query: "trading window" / "what are my trading hours"
    if re.match(
        r"^(trading window|trading hours?|what(?:'s| is)(?: the)? trading window|"
        r"when (?:does|do) (?:the bot|we|jarvis) trade\??)$",
        lower,
    ):
        return await _cmd_trading_window(None, None)

    # 24/7: "trade 24/7" / "no trading window" / "trade all day" / "always trade"
    if re.match(
        r"^(trade 24[/\s]?7|24[/\s]?7(?: trading)?|no trading window|"
        r"trade all (?:day|night|the time)|always trade|"
        r"remove trading window|disable trading window|"
        r"trade around the clock|trade (?:all )?24 hours?)$",
        lower,
    ):
        return await _cmd_trading_window(0, 0)

    # "set trading window 21 to 3" / "trade from 9pm to 3am" / "set hours 21-3"
    m = re.match(
        r"^(?:set\s+)?(?:trading\s+)?(?:window|hours?)\s+(?:from\s+)?(\d{1,2})\s*(?:to|-|until|–)\s*(\d{1,2})",
        lower,
    )
    if m:
        return await _cmd_trading_window(int(m.group(1)), int(m.group(2)))

    # "trade from 21:00 to 03:00" / "trade between 9 and 3"
    m = re.match(
        r"^trade\s+(?:from\s+)?(\d{1,2})(?::\d{2})?\s*(?:to|until|-|–)\s*(\d{1,2})(?::\d{2})?",
        lower,
    )
    if m:
        return await _cmd_trading_window(int(m.group(1)), int(m.group(2)))

    # "stop trading at 3" / "start trading at 21"
    m = re.match(r"^stop trading (?:at\s+)?(\d{1,2})(?::\d{2})?(?:\s*utc)?$", lower)
    if m:
        from elizaos.plugins.solana import live_config as _lc_stop
        return await _cmd_trading_window(
            int(_lc_stop.get("trading_window_start_utc", 0)), int(m.group(1))
        )
    m = re.match(r"^start trading (?:at\s+)?(\d{1,2})(?::\d{2})?(?:\s*utc)?$", lower)
    if m:
        from elizaos.plugins.solana import live_config as _lc_start
        return await _cmd_trading_window(
            int(m.group(1)), int(_lc_start.get("trading_window_end_utc", 0))
        )

    # ── 14b. Raw log tail ─────────────────────────────────────────────────────
    m = re.match(r"^(?:logs?|activity|tail)\s*(raw\s*)?(\d+)?$", lower)
    if m:
        is_raw = bool(m.group(1))
        n = int(m.group(2)) if m.group(2) else 50
        return await _cmd_logs(min(n, 200), raw=is_raw)

    # ── 14c. RPC health ───────────────────────────────────────────────────────
    if re.match(r"^(rpc|health|ping|connectivity|services?)$", lower):
        return await _cmd_rpc(runtime)

    # ── 14c. Force buy — "force buy <mint> [sol]" ────────────────────────────
    m = re.match(r"^force\s+buy\s+([A-Za-z0-9]{32,44})\b\s*([0-9.]+)?$", raw.strip())
    if m:
        mint_fb = m.group(1)
        sol_fb = float(m.group(2)) if m.group(2) else 0.0
        if sol_fb <= 0:
            from elizaos.plugins.solana import live_config as _lc_fb
            sol_fb = float(_lc_fb.get("buy_sol", 0.05))
        return await _cmd_force_buy(mint_fb, sol_fb, runtime)

    # ── 14d. Force sell — "force sell <mint>" ────────────────────────────────
    m = re.match(r"^force\s+sell\s+([A-Za-z0-9]{8,44})\b", raw.strip())
    if m:
        return await _cmd_force_sell(m.group(1), runtime)

    # ── 14e. Smart money wallet tracker ─────────────────────────────────────
    if re.match(r"^(smart money|smart wallets?|smart money list|smart money stats|smart money tracker|who are the smart wallets?)$", lower):
        return await _cmd_smart_money("list")

    m = re.match(r"^smart money\s+(?:detail|check|wallet|status)\s+([A-Za-z0-9]{32,44})\b", lower)
    if m:
        return await _cmd_smart_money("detail", m.group(1))

    # ── 14f. Creator management ───────────────────────────────────────────────
    # "creator list" / "creators" / "whitelist list"
    if re.match(r"^(creator list|creators|creator stats|whitelist list|dev list|reputation list)$", lower):
        return await _cmd_creator("list", "")

    # "whitelist creator <wallet> [reason]"
    m = re.match(r"^(?:whitelist|wl)\s+(?:creator\s+)?([A-Za-z0-9]{32,44})\b(.*)$", raw.strip())
    if m:
        return await _cmd_creator("whitelist", m.group(1), m.group(2).strip())

    # "proven creator <wallet> [reason]"  /  "promote creator <wallet>"
    m = re.match(r"^(?:proven|promote)\s+(?:creator\s+)?([A-Za-z0-9]{32,44})\b(.*)$", raw.strip())
    if m:
        return await _cmd_creator("proven", m.group(1), m.group(2).strip())

    # "blacklist creator <wallet> [reason]"
    m = re.match(r"^(?:blacklist|block|bl)\s+(?:creator\s+)?([A-Za-z0-9]{32,44})\b(.*)$", raw.strip())
    if m:
        return await _cmd_creator("blacklist", m.group(1), m.group(2).strip())

    # "remove creator <wallet>"
    m = re.match(r"^(?:remove|unlist)\s+(?:creator\s+)?([A-Za-z0-9]{32,44})\b(.*)$", raw.strip())
    if m:
        return await _cmd_creator("remove", m.group(1))

    # "creator status <wallet>"  /  "check creator <wallet>"
    m = re.match(r"^(?:creator(?:\s+status)?|dev(?:\s+status)?)\s+([A-Za-z0-9]{32,44})\b.*$", raw.strip())
    if m:
        return await _cmd_creator("status", m.group(1))

    # ── 15. Clear chat history ────────────────────────────────────────────────
    if re.match(r"^(clear history|reset memory|forget|new conversation|clear chat)$", lower):
        global _CONV_HISTORY
        _CONV_HISTORY = []
        _history_save()   # wipe the disk file too
        return "Conversation history cleared — starting fresh."

    # ── 15b. Copy-trade commands ──────────────────────────────────────────────
    if re.match(r"^(copy.?trade?s?(\s+status)?|copytrade|watched\s+wallets?|copy\s+trade\s+wallets?)$", lower):
        try:
            from elizaos.plugins.solana.axiom_copy_trader import handle_jarvis_command as _ct_cmd
            return await _ct_cmd(lower) or "Copy-trade module ready — no alerts yet."
        except Exception as _exc:
            return f"Copy-trade error: {_exc}"

    # ── 16. Free-form → Claude Sonnet ─────────────────────────────────────────
    return await _cmd_free_chat(raw, runtime)
