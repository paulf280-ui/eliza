"""
brain_memory.py — Persistent per-brain memory for Groq, Gemini, and Claude.

Each brain has its own JSON file that persists across restarts:
  groq_brain_memory.json   — Groq llama-3.3-70b  (speed brain, 30s decisions)
  gemini_brain_memory.json — Gemini 2.0-flash     (analysis brain, 2min decisions)
  claude_brain_memory.json — Claude Sonnet/Opus   (depth brain, 5min decisions)

Every 120 seconds a refresh loop:
  1. Snapshots current open positions + live config
  2. Writes to all 3 memory files
  3. Each brain reads from its own file at the START of every decision call

This means every brain wakes up knowing:
  - The current config (SL, TP, trade size, active strategies)
  - Every open position with live P&L
  - Its own last 50 decisions and their outcomes
  - Its own accuracy stats (correct holds, missed rugs, premature exits)
  - Its top learned patterns
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

_DIR = os.path.dirname(__file__)

BRAIN_FILES: dict[str, str] = {
    "groq":   os.path.join(_DIR, "groq_brain_memory.json"),
    "gemini": os.path.join(_DIR, "gemini_brain_memory.json"),
    "claude": os.path.join(_DIR, "claude_brain_memory.json"),
}

BRAIN_ROLES: dict[str, str] = {
    "groq": (
        "GROQ SPEED BRAIN (llama-3.3-70b). You fire every 30 seconds. "
        "Your job: fast gut-check, flag rugs immediately, never overthink. "
        "SELL with confidence≥0.75 triggers immediate exit. You are first responder."
    ),
    "gemini": (
        "GEMINI ANALYSIS BRAIN (gemini-2.5-flash-lite). You fire every 2 minutes. "
        "Your job: confirm or override Groq's call with deeper pattern analysis. "
        "Check holder trends, liquidity shifts, momentum direction. SELL confidence≥0.70 exits."
    ),
    "claude": (
        "CLAUDE DEPTH BRAIN (claude-opus-4-7) — the quant meme-coin trader. "
        "You fire every 5 minutes. Your job: strategic review with full context, "
        "feeding off every trade outcome and token-makeup signal we've accumulated. "
        "You make data-driven calls — accumulation vs distribution, winner-profile vs "
        "loser-profile — and your decisions carry the most weight. SELL confidence≥0.65 exits."
    ),
}

# In-memory cache to avoid re-reading files on every call
_cache: dict[str, dict] = {}
_cache_ts: dict[str, float] = {}
_CACHE_TTL = 30.0

# Track whether the 120s loop is already running
_refresh_loop_running = False


def _default_memory(brain: str) -> dict:
    return {
        "brain": brain,
        "role": BRAIN_ROLES[brain],
        "created": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "config_snapshot": {},
        "open_positions": [],
        "recent_decisions": [],
        "session_stats": {
            "total_calls": 0,
            "holds_recommended": 0,
            "exits_recommended": 0,
            "correct_hold_then_tp": 0,
            "premature_exit_missed_tp": 0,
            "correct_exit_before_sl": 0,
            "missed_rug_held_too_long": 0,
        },
        "learned_patterns": [],
        "meteora_lessons": [],   # recorded outcomes from Meteora positions with signal state
        "last_refresh_ts": 0,
    }


def load_brain_memory(brain: str) -> dict:
    """Load brain memory file. Returns default structure if file missing. Cached 30s."""
    now = time.time()
    if brain in _cache and now - _cache_ts.get(brain, 0) < _CACHE_TTL:
        return _cache[brain]
    path = BRAIN_FILES.get(brain)
    if not path:
        return _default_memory(brain)
    try:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            _cache[brain] = data
            _cache_ts[brain] = now
            return data
    except Exception:
        pass
    mem = _default_memory(brain)
    _cache[brain] = mem
    _cache_ts[brain] = now
    return mem


def save_brain_memory(brain: str, data: dict) -> None:
    """Write brain memory to disk atomically and refresh cache."""
    path = BRAIN_FILES.get(brain)
    if not path:
        return
    try:
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["last_refresh_ts"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        _cache[brain] = data
        _cache_ts[brain] = time.time()
    except Exception as e:
        print(f"[brain-memory] Save failed for {brain}: {e}")


def record_decision(
    brain: str,
    mint: str,
    token_name: str,
    action: str,
    reason: str,
    pnl_pct: float | None,
    confidence: float | None = None,
    candle_shape: str | None = None,
) -> None:
    """Record a brain decision to its memory file.

    action: HOLD | SELL | WATCH | RUNNER | RUG_RISK | ENTRY_OK | ENTRY_WARN
    candle_shape: compact Jarvis sign descriptor, e.g. "S1=✅(4.2x) S2=✅ shape=🟢🟢🔴🟢🟢"
    Called immediately after each AI decision is parsed.
    """
    mem = load_brain_memory(brain)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mint": mint[:12],
        "token": token_name[:20] if token_name else mint[:8],
        "action": action,
        "reason": (reason or "")[:120],
        "pnl_pct_at_decision": round(pnl_pct, 1) if pnl_pct is not None else None,
        "confidence": round(confidence, 2) if confidence is not None else None,
        "outcome": "PENDING",
        "final_pnl_pct": None,
        "candle_shape": candle_shape,
    }
    decisions: list = mem.get("recent_decisions", [])
    decisions.append(entry)
    mem["recent_decisions"] = decisions[-60:]  # keep last 60

    # Candle observation log — builds the pattern library over time.
    # Each entry records what the 1m chart looked like when Groq made this
    # call + the outcome once the trade closes. The pattern derivation step
    # reads these to compute win/loss rates per signal combination.
    if candle_shape:
        obs: list = mem.setdefault("candle_observations", [])
        obs.append({
            "ts":            entry["ts"],
            "mint":          mint[:12],
            "token":         entry["token"],
            "candle_shape":  candle_shape,
            "action":        action,
            "pnl_at_obs":    entry["pnl_pct_at_decision"],
            "outcome":       "PENDING",
            "final_pnl_pct": None,
        })
        mem["candle_observations"] = obs[-50:]  # keep last 50 observations

    stats: dict = mem.setdefault("session_stats", {})
    stats["total_calls"] = stats.get("total_calls", 0) + 1
    if action in ("HOLD", "RUNNER", "WATCH"):
        stats["holds_recommended"] = stats.get("holds_recommended", 0) + 1
    elif action in ("SELL", "EXIT"):
        stats["exits_recommended"] = stats.get("exits_recommended", 0) + 1

    _cache.pop(brain, None)
    save_brain_memory(brain, mem)


def record_decision_all_brains(
    mint: str,
    token_name: str,
    action: str,
    reason: str,
    pnl_pct: float | None,
    source: str = "rule_engine",
) -> None:
    """Record the same decision to every brain's memory.

    Used when a hard rule fires (SL/TP/peak/stagnant/wallet_exit). The rule
    acted on behalf of all brains, so every brain should see the outcome.
    source tags the decision origin so brains can distinguish AI calls from
    rule-engine events when reading their own history.
    """
    for brain in ("groq", "gemini", "claude"):
        mem = load_brain_memory(brain)
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mint": mint[:12],
            "token": token_name[:20] if token_name else mint[:8],
            "action": action,
            "reason": (reason or "")[:120],
            "pnl_pct_at_decision": round(pnl_pct, 1) if pnl_pct is not None else None,
            "confidence": None,
            "source": source,
            "outcome": "PENDING",
            "final_pnl_pct": None,
        }
        decisions: list = mem.get("recent_decisions", [])
        decisions.append(entry)
        mem["recent_decisions"] = decisions[-60:]
        _cache.pop(brain, None)
        save_brain_memory(brain, mem)


def resolve_outcome_all_brains(mint: str, outcome: str, final_pnl: float) -> None:
    """Resolve the latest PENDING decision for `mint` across all 3 brain files."""
    for brain in ("groq", "gemini", "claude"):
        resolve_outcome(brain, mint, outcome, final_pnl)


def resolve_outcome(brain: str, mint: str, outcome: str, final_pnl: float) -> None:
    """Mark the most recent PENDING decision for this mint with its actual outcome.

    outcome: TP_HIT | SL_HIT | WALLET_EXIT | MANUAL | TIMEOUT
    Called when a position closes.
    """
    mem = load_brain_memory(brain)
    decisions: list = mem.get("recent_decisions", [])
    for i in range(len(decisions) - 1, -1, -1):
        d = decisions[i]
        if d.get("mint") == mint[:12] and d.get("outcome") == "PENDING":
            d["outcome"] = outcome
            d["final_pnl_pct"] = round(final_pnl, 1)
            # Update accuracy counters
            stats: dict = mem.setdefault("session_stats", {})
            action = d.get("action", "")
            if action in ("HOLD", "RUNNER", "WATCH") and outcome == "TP_HIT":
                stats["correct_hold_then_tp"] = stats.get("correct_hold_then_tp", 0) + 1
            elif action in ("HOLD", "RUNNER", "WATCH") and outcome == "SL_HIT":
                stats["missed_rug_held_too_long"] = stats.get("missed_rug_held_too_long", 0) + 1
            elif action in ("SELL", "EXIT") and final_pnl > 5:
                stats["premature_exit_missed_tp"] = stats.get("premature_exit_missed_tp", 0) + 1
            elif action in ("SELL", "EXIT") and final_pnl <= 0:
                stats["correct_exit_before_sl"] = stats.get("correct_exit_before_sl", 0) + 1
            break
    mem["recent_decisions"] = decisions

    # Resolve candle observations for this mint too
    outcome_tag = "WIN" if final_pnl > 0 else "LOSS"
    obs_list: list = mem.get("candle_observations", [])
    for o in obs_list:
        if o.get("mint") == mint[:12] and o.get("outcome") == "PENDING":
            o["outcome"]       = outcome_tag
            o["final_pnl_pct"] = round(final_pnl, 1)
    mem["candle_observations"] = obs_list

    _cache.pop(brain, None)
    save_brain_memory(brain, mem)


def record_meteora_exit(
    brain: str,
    mint: str,
    token_name: str,
    liq_trend: str,       # "growing" | "stable" | "draining"
    holder_trend: str,    # "growing" | "stable" | "declining"
    pnl_pct: float,
    exit_reason: str,
    liq_chg_pct: float | None = None,
    holder_delta: int | None = None,
) -> None:
    """Record a Meteora position exit with the signal state that drove the decision.

    Builds pattern memory: brains see what signals were present when positions closed
    well vs poorly, and learn to make better calls over time.
    """
    mem = load_brain_memory(brain)
    lessons: list = mem.setdefault("meteora_lessons", [])
    outcome_tag = "WIN" if pnl_pct > 0 else "LOSS"
    lesson = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "token":        token_name,
        "mint":         mint[:12],
        "outcome":      outcome_tag,
        "pnl_pct":      round(pnl_pct, 1),
        "exit_reason":  exit_reason,
        "liq_trend":    liq_trend,
        "holder_trend": holder_trend,
        "liq_chg_pct":  round(liq_chg_pct, 1) if liq_chg_pct is not None else None,
        "holder_delta": holder_delta,
    }
    lessons.append(lesson)
    # Keep last 30 Meteora lessons per brain
    mem["meteora_lessons"] = lessons[-30:]
    _cache.pop(brain, None)
    save_brain_memory(brain, mem)


def claude_recently_holding(mint: str, max_age_secs: int = 600) -> tuple[bool, float]:
    """Did Claude's MOST RECENT decision on this mint say HOLD within the last
    max_age_secs? Returns (is_holding, confidence). Used by strategy_e_monster's
    brain gate as a veto on lower-tier SELL signals — if the depth tier is
    actively holding, don't let Groq override.

    Returns False if Claude's last decision on this mint was anything other than
    HOLD (SELL, WATCH, or none), or if no recent decision exists.
    """
    try:
        mem = load_brain_memory("claude")
        decisions = mem.get("recent_decisions", []) or []
        now = datetime.now(timezone.utc)
        # Walk newest-first — recent_decisions is appended chronologically.
        for d in reversed(decisions):
            if (d.get("mint") or "")[:12] != mint[:12]:
                continue
            try:
                ts_raw = d.get("ts", "")
                # ISO format with timezone; tolerate Z suffix
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                age = (now - ts).total_seconds()
                if age > max_age_secs:
                    return False, 0.0
                if d.get("action") == "HOLD":
                    return True, float(d.get("confidence") or 0.0)
                # Most recent decision was SELL or WATCH — no veto
                return False, 0.0
            except Exception:
                continue
        return False, 0.0
    except Exception:
        return False, 0.0


def brain_memory_as_prompt(brain: str) -> str:
    """Format this brain's memory as a compact system-prompt block (~600 chars).

    Injected at the START of every brain call so it wakes up with full context.
    """
    mem = load_brain_memory(brain)
    stats = mem.get("session_stats", {})
    decisions = mem.get("recent_decisions", [])
    config = mem.get("config_snapshot", {})
    positions = mem.get("open_positions", [])
    patterns = mem.get("learned_patterns", [])

    total = stats.get("total_calls", 0)
    lines = [
        f"\n\n=== YOUR PERSISTENT MEMORY ({brain.upper()} BRAIN) ===",
        f"Role: {BRAIN_ROLES[brain]}",
    ]

    # Config snapshot
    if config:
        strats = config.get("strategies_active", [])
        wallets = config.get("watched_wallets", [])
        lines.append(
            f"Bot config: SL={config.get('sl_pct','?')}%  TP={config.get('tp_pct','?')}%  "
            f"size={config.get('trade_size_sol','?')}SOL  max_pos={config.get('max_positions','?')}  "
            f"strategies={strats}  wallets={wallets}"
        )

    # Open positions
    if positions:
        pstrs = []
        for p in positions[:4]:
            pnl = p.get("pnl_pct")
            age = p.get("age_mins", "?")
            pstrs.append(f"{p.get('token','?')}({('+' if pnl and pnl>0 else '')}{pnl:.0f}% {age}min)" if isinstance(pnl, (int, float)) else f"{p.get('token','?')}(?%)")
        lines.append(f"Open now: {', '.join(pstrs)}")
    else:
        lines.append("Open now: none")

    # Session accuracy
    if total > 0:
        tp_ok = stats.get("correct_hold_then_tp", 0)
        rug_miss = stats.get("missed_rug_held_too_long", 0)
        premature = stats.get("premature_exit_missed_tp", 0)
        lines.append(
            f"Your accuracy this session ({total} calls): "
            f"correct_hold→TP={tp_ok}  missed_rugs={rug_miss}  premature_exits={premature}"
        )

    # Last 5 resolved decisions
    resolved = [d for d in decisions[-10:] if d.get("outcome") not in ("PENDING", None)][-5:]
    if resolved:
        lines.append("Your recent outcomes:")
        for d in resolved:
            lines.append(
                f"  {d.get('token','?')}: {d.get('action','?')} @ "
                f"{d.get('pnl_pct_at_decision','?')}%  →  "
                f"{d.get('outcome','?')} ({d.get('final_pnl_pct','?')}%)"
            )

    # Pending (open) decisions
    pending = [d for d in decisions[-10:] if d.get("outcome") == "PENDING"]
    if pending:
        lines.append("Still open (your last call):")
        for d in pending[-3:]:
            lines.append(
                f"  {d.get('token','?')}: last said {d.get('action','?')} "
                f"@ {d.get('pnl_pct_at_decision','?')}%  conf={d.get('confidence','?')}"
            )

    # Brain-specific learned patterns (YOUR own track record, not shared)
    if patterns:
        lines.append("YOUR independent patterns:")
        for p in patterns[:6]:
            lines.append(f"  - {p}")

    # Shared historical baseline (same across all brains — cross-brain context)
    shared = mem.get("shared_context") or []
    if shared:
        lines.append("Shared historical baseline (across all brains):")
        for s in shared[:3]:
            lines.append(f"  - {s}")

    # Candle pattern memory — what you've learned from 1m candle signals
    candle_obs = [o for o in (mem.get("candle_observations") or [])
                  if o.get("outcome") in ("WIN", "LOSS")]
    if len(candle_obs) >= 3:
        combo_wins:   dict[str, int] = {}
        combo_totals: dict[str, int] = {}
        for o in candle_obs:
            shape = o.get("candle_shape", "")
            s1 = "S1=✅" in shape
            s2 = "S2=✅" in shape
            key = f"S1={'✅' if s1 else '❌'}+S2={'✅' if s2 else '❌'}"
            combo_totals[key] = combo_totals.get(key, 0) + 1
            if o.get("outcome") == "WIN":
                combo_wins[key] = combo_wins.get(key, 0) + 1
        parts = []
        for k, total in sorted(combo_totals.items(), key=lambda kv: -kv[1]):
            wins = combo_wins.get(k, 0)
            parts.append(f"{k}→{wins}/{total}W")
        lines.append(f"1m candle pattern memory ({len(candle_obs)} obs): {' | '.join(parts)}")

    # Historical summary (from backfill — 119 closed trades as of 2026-04-18)
    hist = mem.get("historical_summary") or {}
    if hist.get("total_trades"):
        lines.append(
            f"Historical: {hist['total_trades']} trades, "
            f"WR={hist.get('win_rate_pct','?')}%, net={hist.get('net_sol','?'):+.2f} SOL "
            f"(median {hist.get('median_pnl_pct','?'):+.1f}%, best {hist.get('best_pnl_pct','?'):+.1f}%, worst {hist.get('worst_pnl_pct','?'):+.1f}%)"
        )
        # Top 3 profitable wallets
        top_w = [(w, s) for w, s in (hist.get("by_wallet") or {}).items() if s.get("net_sol", 0) > 0][:3]
        if top_w:
            lines.append("Profitable wallets copied: " + ", ".join(
                f"{w}({s['wr']}%WR {s['net_sol']:+.2f}SOL)" for w, s in top_w
            ))

    # Peer summary — what the OTHER two brains recently decided
    peers: dict = mem.get("peer_summary", {}) or {}
    peer_lines: list[str] = []
    for peer_brain, peer_decisions in peers.items():
        for d in (peer_decisions or [])[-2:]:
            peer_lines.append(
                f"  {peer_brain.upper()}: {d.get('token','?')} "
                f"{d.get('action','?')} ({d.get('reason','')[:40]}) "
                f"→ {d.get('outcome','?')}"
            )
    if peer_lines:
        lines.append("Peer brains' recent calls:")
        lines.extend(peer_lines[:5])

    # Meteora-specific lessons — what signals led to wins vs losses
    meteora_lessons: list = mem.get("meteora_lessons", [])
    if meteora_lessons:
        wins  = [l for l in meteora_lessons if l.get("outcome") == "WIN"]
        losses = [l for l in meteora_lessons if l.get("outcome") == "LOSS"]
        lines.append(f"Meteora trade memory ({len(meteora_lessons)} trades, {len(wins)}W/{len(losses)}L):")
        # Summarise what signals correlated with wins vs losses
        win_liq_growing  = sum(1 for l in wins   if l.get("liq_trend") == "growing")
        loss_liq_drain   = sum(1 for l in losses if l.get("liq_trend") == "draining")
        win_hold_growing = sum(1 for l in wins   if l.get("holder_trend") == "growing")
        if wins:
            lines.append(f"  WIN pattern: liq_growing={win_liq_growing}/{len(wins)} holder_growing={win_hold_growing}/{len(wins)}")
        if losses:
            lines.append(f"  LOSS pattern: liq_draining={loss_liq_drain}/{len(losses)}")
        # Show last 3 outcomes
        for l in meteora_lessons[-3:]:
            lines.append(
                f"  {l.get('token','?')}: {l.get('outcome','?')} {l.get('pnl_pct','?'):+.1f}% "
                f"liq={l.get('liq_trend','?')} holders={l.get('holder_trend','?')} → {l.get('exit_reason','?')}"
            )

    # Scout-rejection stats — teaches the brain how tight each scout's filter
    # is. If a scout is over-rejecting (false-positive-heavy), brains should
    # ease up on aggressive SELL calls; if tight, trust the scout picks more.
    try:
        from elizaos.plugins.solana import rejection_tracker as _rt
        _rej_block = _rt.get_brain_summary(lookback_hours=48.0)
        if _rej_block:
            lines.append(_rej_block)
    except Exception:
        pass

    lines.append("=== END MEMORY ===")
    return "\n".join(lines)


def _build_config_snapshot(lc: dict) -> dict:
    """Build a compact config snapshot — reads directly from live_config module for accuracy."""
    try:
        from elizaos.plugins.solana import live_config as _lc_snap
        _g = _lc_snap.get
    except Exception:
        _g = lc.get  # type: ignore[assignment]

    strats = []
    for suffix in ("b", "c", "d", "e"):
        if _g(f"strategy_{suffix}_enabled", False):
            strats.append(suffix.upper())
    if _g("copy_trade_enabled", False):
        strats.append("copy_trade")

    wallets = list(lc.get("watched_wallets_map", {}).keys())

    return {
        "sl_pct":           _g("copy_trade_sl_pct", _g("stop_loss_pct", "?")),
        "tp_pct":           _g("copy_trade_tp_pct", "?"),
        "trade_size_sol":   _g("copy_trade_paper_buy_sol") or _g("strategy_b_buy_sol", "?"),
        "max_positions":    _g("max_concurrent_positions", "?"),
        "strategies_active": strats,
        "watched_wallets":  wallets,
        "live_mode":        not _g("paper_trading", True),
        "snapshot_ts":      datetime.now(timezone.utc).isoformat(),
    }


def _build_position_snapshot(active_monitors: dict, paper_positions: dict) -> list:
    """Build compact position list from active monitors + paper positions."""
    now = time.time()
    positions = []
    seen: set[str] = set()

    # From trade monitor (has live P&L from AI cascade)
    for mint, mon in active_monitors.items():
        seen.add(mint)
        pos = paper_positions.get(mint, {})
        entry_ts = pos.get("entry_ts", now)
        entry_price = pos.get("entry_price", 0)
        current_price = pos.get("current_price")
        pnl_pct = None
        if entry_price and current_price:
            pnl_pct = round((current_price / entry_price - 1) * 100, 1)
        positions.append({
            "mint":     mint[:12],
            "token":    pos.get("token_name", mint[:8])[:20],
            "dex":      pos.get("dex", "?"),
            "pnl_pct":  pnl_pct,
            "age_mins": round((now - entry_ts) / 60, 1),
            "strategy": pos.get("strategy", "copy_trade"),
            "wallet":   pos.get("wallet", ""),
        })

    # Any paper positions not in active monitors
    for mint, pos in paper_positions.items():
        if mint in seen:
            continue
        entry_ts = pos.get("entry_ts", now)
        entry_price = pos.get("entry_price", 0)
        current_price = pos.get("current_price")
        pnl_pct = None
        if entry_price and current_price:
            pnl_pct = round((current_price / entry_price - 1) * 100, 1)
        positions.append({
            "mint":     mint[:12],
            "token":    pos.get("token_name", mint[:8])[:20],
            "dex":      pos.get("dex", "?"),
            "pnl_pct":  pnl_pct,
            "age_mins": round((now - entry_ts) / 60, 1),
            "strategy": "copy_trade",
            "wallet":   pos.get("wallet", ""),
        })

    return positions


async def memory_refresh_loop() -> None:
    """120-second loop: refresh all 3 brain memory files with current world state.

    Reads live positions + config and writes to groq/gemini/claude_brain_memory.json.
    Starts automatically when first spawn_monitor fires — only one instance runs.
    Guard (_refresh_loop_running) is set by ensure_memory_loop_started() before task creation.
    """
    print("[brain-memory] 🧠 Brain memory refresh loop started — updating every 120s")

    while True:
        try:
            await asyncio.sleep(120)

            # Import here to avoid circular dependency
            from elizaos.plugins.solana.trade_monitor import _active_monitors
            from elizaos.plugins.solana import live_config as _lc
            try:
                from elizaos.plugins.solana.axiom_copy_trader import _paper_positions
            except Exception:
                _paper_positions = {}

            # live_config stores its dict as _config (not _cfg)
            lc_dict = dict(_lc._config) if hasattr(_lc, "_config") else {}
            # Inject watched wallets from copy trader
            try:
                from elizaos.plugins.solana.axiom_copy_trader import WATCHED_WALLETS
                lc_dict["watched_wallets_map"] = WATCHED_WALLETS
            except Exception:
                pass
            config_snap = _build_config_snapshot(lc_dict)
            pos_snap = _build_position_snapshot(_active_monitors, _paper_positions)

            # Shared historical baseline (same for every brain — it's the same trade
            # data). Goes into shared_context, NOT learned_patterns.
            _shared = _load_winning_patterns()

            # Build a per-brain peer summary: last 5 decisions from the OTHER brains.
            # This gives each brain "consciousness" of what its siblings recently saw,
            # so a SELL call from Groq can inform Gemini's next deeper review.
            _brain_recent: dict[str, list] = {}
            for brain in ("groq", "gemini", "claude"):
                mem_r = load_brain_memory(brain)
                _brain_recent[brain] = (mem_r.get("recent_decisions") or [])[-5:]

            updated = 0
            for brain in ("groq", "gemini", "claude"):
                mem = load_brain_memory(brain)
                mem["config_snapshot"] = config_snap
                mem["open_positions"]  = pos_snap
                # Shared historical baseline (same across brains by design).
                if _shared:
                    mem["shared_context"] = _shared[:6]
                # Brain-specific learned_patterns derived from THIS brain's own
                # decision history — so Groq learns Groq's biases, Gemini learns
                # Gemini's, Claude learns Claude's. No longer identical across brains.
                mem["learned_patterns"] = _derive_brain_specific_patterns(brain, mem)
                # peer_summary = recent decisions from the OTHER two brains
                peers = {b: _brain_recent[b] for b in ("groq", "gemini", "claude") if b != brain}
                mem["peer_summary"] = peers
                _cache.pop(brain, None)
                save_brain_memory(brain, mem)
                updated += 1

            pos_count = len(pos_snap)
            print(
                f"[brain-memory] ✓ {updated}/3 brain memories refreshed — "
                f"{pos_count} open position(s) | "
                f"config: SL={config_snap.get('sl_pct')}% TP={config_snap.get('tp_pct')}%"
            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[brain-memory] Refresh error: {e}")


def _derive_brain_specific_patterns(brain: str, mem: dict) -> list[str]:
    """Build patterns unique to THIS brain from its own decision history.

    Each brain learns from its own track record: HOLD/SELL accuracy, best
    pnl_pct buckets for correct calls, confidence calibration, and recurring
    failure modes. The output is distinct per brain — Groq's patterns reflect
    Groq's biases, not a shared corpus.
    """
    decisions: list = mem.get("recent_decisions") or []
    # Only AI-made decisions — rule-engine events (SL/TP/wallet_exit) are shared
    ai_calls = [
        d for d in decisions
        if (d.get("source") or "ai") != "rule_engine"
        and d.get("outcome") not in ("PENDING", None)
    ]
    patterns: list[str] = []

    # Brain self-identity — reminds each model who it is independently.
    role_tag = {"groq": "SPEED", "gemini": "ANALYSIS", "claude": "DEPTH"}.get(brain, brain.upper())
    patterns.append(
        f"YOU ARE {brain.upper()} BRAIN ({role_tag}). Your patterns below come from YOUR "
        f"own track record — not a shared corpus. Bring your independent view."
    )

    if len(ai_calls) < 3:
        # Cold start — seed with role-appropriate prior guidance
        seed = {
            "groq":   "Cold start: you haven't resolved 3+ calls yet. Lean toward SELL confidence≥0.80 "
                      "on any rug signal (liq drain, mass holder drop). Your value is catching rugs in <30s.",
            "gemini": "Cold start: you haven't resolved 3+ calls yet. Your edge is confirming Groq's SELL "
                      "or overriding with pattern context. Demand holder+liq alignment before HOLD.",
            "claude": "Cold start: you haven't resolved 3+ calls yet. Your edge is strategic depth — "
                      "only HOLD when accumulation (buys>sells, holders growing, liq growing) is clear.",
        }
        patterns.append(seed.get(brain, "Cold start — build your track record."))
        return patterns

    # Accuracy by action
    holds = [d for d in ai_calls if d.get("action") in ("HOLD", "RUNNER", "WATCH")]
    sells = [d for d in ai_calls if d.get("action") in ("SELL", "EXIT")]
    hold_wins = [d for d in holds if d.get("outcome") == "TP_HIT" or (d.get("final_pnl_pct") or 0) > 0]
    sell_saves = [d for d in sells if (d.get("final_pnl_pct") or 0) <= 0]  # saved us from a loss
    premature_sells = [d for d in sells if (d.get("final_pnl_pct") or 0) > 5]  # exited a winner

    if holds:
        hold_acc = round(len(hold_wins) / len(holds) * 100, 0)
        patterns.append(
            f"Your HOLD calls: {len(hold_wins)}/{len(holds)} ended profitable ({hold_acc:.0f}% acc). "
            + ("Your HOLDs are working — keep conviction." if hold_acc >= 50 else
               "Your HOLDs under-perform — demand stronger signals before HOLD.")
        )
    if sells:
        sell_acc = round(len(sell_saves) / len(sells) * 100, 0)
        premature_rate = round(len(premature_sells) / len(sells) * 100, 0)
        patterns.append(
            f"Your SELL calls: {len(sell_saves)}/{len(sells)} avoided a loss ({sell_acc:.0f}% acc), "
            f"but {len(premature_sells)}/{len(sells)} ({premature_rate:.0f}%) exited a winner too early."
        )

    # pnl_pct bucket where this brain's calls land best
    win_by_bucket: dict[str, list[float]] = {}
    for d in ai_calls:
        p = d.get("pnl_pct_at_decision")
        f = d.get("final_pnl_pct")
        if p is None or f is None:
            continue
        if p < -10:
            bucket = "crash(<-10%)"
        elif p < 0:
            bucket = "early_red(-10→0%)"
        elif p < 10:
            bucket = "mild_green(0→10%)"
        elif p < 30:
            bucket = "run(10→30%)"
        else:
            bucket = "moonbag(>30%)"
        win_by_bucket.setdefault(bucket, []).append(f)

    for bucket, finals in sorted(win_by_bucket.items(), key=lambda kv: -sum(kv[1])):
        if len(finals) >= 2:
            avg_f = round(sum(finals) / len(finals), 1)
            patterns.append(
                f"At pnl={bucket}: your calls finished avg {avg_f:+.1f}% on {len(finals)} trades."
            )
            if len(patterns) >= 5:
                break

    # Recent mistake — surface one concrete failure so the brain can reason about it
    recent_misses = [
        d for d in ai_calls[-15:]
        if d.get("action") in ("HOLD", "RUNNER")
        and (d.get("final_pnl_pct") or 0) < -5
    ]
    if recent_misses:
        m = recent_misses[-1]
        patterns.append(
            f"Recent miss: you said {m.get('action')} on {m.get('token','?')} at "
            f"{m.get('pnl_pct_at_decision','?')}% — it closed {m.get('final_pnl_pct','?')}%. "
            "Consider what signal you overweighted."
        )

    # ── Candle pattern stats from resolved candle_observations ───────────────
    # Groups by S1/S2 combo and reports win rate per combo so Groq builds
    # a quantitative understanding of which candle signals matter most.
    obs_resolved = [
        o for o in (mem.get("candle_observations") or [])
        if o.get("outcome") in ("WIN", "LOSS")
    ]
    if len(obs_resolved) >= 5:
        from collections import Counter as _Counter
        combo_wins:   dict[str, int] = {}
        combo_totals: dict[str, int] = {}
        for o in obs_resolved:
            shape = o.get("candle_shape", "")
            s1 = "S1=✅" in shape
            s2 = "S2=✅" in shape
            key = f"S1={'✅' if s1 else '❌'} S2={'✅' if s2 else '❌'}"
            combo_totals[key] = combo_totals.get(key, 0) + 1
            if o.get("outcome") == "WIN":
                combo_wins[key] = combo_wins.get(key, 0) + 1
        candle_lines = [f"Candle signal stats ({len(obs_resolved)} resolved observations):"]
        for combo, total in sorted(combo_totals.items(), key=lambda kv: -kv[1]):
            wins = combo_wins.get(combo, 0)
            wr   = round(wins / total * 100)
            candle_lines.append(f"  {combo} → {wins}/{total} wins ({wr}%)")
        patterns.append(" | ".join(candle_lines))

    return patterns[:8]


def _load_winning_patterns() -> list[str]:
    """Load top patterns from winning_patterns.json for injection into brain memory."""
    try:
        path = os.path.join(_DIR, "winning_patterns.json")
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            patterns = data.get("patterns", [])
            verdict = data.get("meteora_verdict", "")
            if verdict:
                patterns = [f"METEORA: {verdict}"] + patterns
            return patterns[:8]
    except Exception:
        pass
    return []


def ensure_memory_loop_started() -> None:
    """Idempotent — safe to call multiple times. Only starts one loop."""
    global _refresh_loop_running
    if _refresh_loop_running:
        return
    _refresh_loop_running = True   # set BEFORE creating task to block concurrent calls
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(memory_refresh_loop())
    except Exception as e:
        _refresh_loop_running = False  # reset on failure so next call can retry
        print(f"[brain-memory] Failed to start refresh loop: {e}")
