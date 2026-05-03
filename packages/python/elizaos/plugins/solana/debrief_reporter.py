"""
debrief_reporter.py — Multi-brain debrief report after every 2 completed trades.

Generates a structured intelligence briefing showing what each brain (Groq,
Gemini, Claude Sonnet, Claude Opus) did during each trade, whether the exit
was correct, rolling performance stats, and learning engine status.

The report is pushed to the J.A.R.V.I.S. dashboard chat and saved as a text
file in packages/python/elizaos/plugins/solana/reports/.

Usage:
  from elizaos.plugins.solana.debrief_reporter import record_completed_trade
  record_completed_trade(close_record, monitor_log)

Called from axiom_copy_trader._close_paper_position after trade is recorded.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

_DIR = os.path.dirname(__file__)
_REPORTS_DIR = os.path.join(_DIR, "reports")
_PAPER_TRADES_PATH = os.path.join(_DIR, "copy_trade_paper_trades.json")

# ── State ─────────────────────────────────────────────────────────────────────
_pending_trades: list[dict] = []   # buffer — fires report when len == 2
_total_trade_count: int    = 0     # total closed trades this session


def record_completed_trade(close_record: dict, monitor_log: dict | None) -> None:
    """Call after every closed copy-trade position.

    close_record: the dict appended to _paper_trades in _close_paper_position
    monitor_log:  result of trade_monitor.consume_decision_log(mint), may be None
    """
    global _pending_trades, _total_trade_count
    _total_trade_count += 1

    entry = {
        "trade_num":     _total_trade_count,
        "close_record":  close_record,
        "monitor_log":   monitor_log,
    }
    _pending_trades.append(entry)

    if len(_pending_trades) >= 2:
        _fire_report(list(_pending_trades))
        _pending_trades.clear()


# ── Report generation ─────────────────────────────────────────────────────────

def _fire_report(trades: list[dict]) -> None:
    """Build and dispatch the debrief report."""
    import asyncio

    nums = [t["trade_num"] for t in trades]
    report = _build_report(trades, nums)

    # Ensure reports directory exists
    os.makedirs(_REPORTS_DIR, exist_ok=True)
    fname = os.path.join(_REPORTS_DIR, f"debrief_{nums[0]}-{nums[-1]}_{int(time.time())}.txt")
    try:
        with open(fname, "w") as f:
            f.write(report)
    except Exception:
        pass

    # Push to Jarvis dashboard chat
    async def _push():
        try:
            from elizaos.plugins.solana.dashboard_api import push_system_alert
            await push_system_alert(report)
        except Exception:
            pass
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert
            # Telegram has a 4096-char limit — send a summary only
            summary = _build_telegram_summary(trades, nums)
            await send_alert(summary)
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_push())
    except Exception:
        pass

    print(f"[debrief] 📊 Report fired for trades #{nums[0]}-#{nums[-1]}")


def _build_report(trades: list[dict], nums: list[int]) -> str:
    lines = [
        "━" * 44,
        f"🧠 DEBRIEF REPORT — Trades #{nums[0]}-#{nums[-1]}",
        "━" * 44,
        "",
    ]

    for i, entry in enumerate(trades):
        cr  = entry["close_record"]
        ml  = entry["monitor_log"]
        num = entry["trade_num"]

        lines += _build_trade_section(num, cr, ml)
        if i < len(trades) - 1:
            lines.append("")

    lines += ["", "━" * 44]
    lines += _build_rolling_section()
    lines += ["", "━" * 44]
    lines += _build_learning_section()
    lines += ["", "━" * 44]

    return "\n".join(lines)


def _build_trade_section(num: int, cr: dict, ml: dict | None) -> list[str]:
    tok    = cr.get("token_name", "?")[:20]
    pnl_p  = float(cr.get("pnl_pct", 0))
    pnl_s  = float(cr.get("pnl_sol", 0))
    hold   = float(cr.get("hold_mins", 0))
    reason = cr.get("reason", "?")
    result_emoji = "✅" if pnl_s >= 0 else "❌"
    pnl_sign = "+" if pnl_p >= 0 else ""

    lines = [
        f"TRADE #{num}: {tok}",
        f"  Result: {result_emoji} {pnl_sign}{pnl_p:.1f}% | {pnl_sign}{pnl_s:.4f} SOL | Hold: {hold:.0f}min",
        f"  Exit: {_format_reason(reason)}",
        "",
    ]

    if ml:
        groq   = ml.get("groq", {})
        gemini = ml.get("gemini", {})
        claude = ml.get("claude", {})
        opus   = ml.get("opus", {})

        # Groq
        g_total = groq.get("total_calls", 0)
        g_alert = groq.get("alert_count", 0)
        g_watch = groq.get("watch_count", 0)
        g_norm  = groq.get("hold_count", 0)
        g_alert_reason = groq.get("last_alert_reason") or "—"
        lines.append(f"  🔴 GROQ (Sentinel): {g_total} checks — "
                     f"{g_norm} normal, {g_watch} watch, {g_alert} alert")
        if g_alert > 0:
            lines.append(f"     ⚡ Alert: \"{g_alert_reason[:80]}\"")

        # Gemini
        gem_total = gemini.get("total_calls", 0)
        gem_reason = gemini.get("last_reason") or "—"
        if gem_total > 0:
            lines.append(f"  🟡 GEMINI (Analyst): {gem_total} check(s). "
                         f"Last: {gemini.get('last_action','?')} "
                         f"({gemini.get('last_confidence', 0)*100:.0f}% conf)")
            lines.append(f"     \"{gem_reason[:80]}\"")
        else:
            lines.append(f"  🟡 GEMINI (Analyst): Not triggered")

        # Claude Sonnet
        c_total   = claude.get("total_calls", 0)
        c_reached = c_total > 0
        c_action  = claude.get("last_action")
        c_conf    = claude.get("last_confidence")
        c_reason  = claude.get("last_reason") or "—"
        if c_reached:
            lines.append(f"  🔵 CLAUDE (Strategist): {c_total} call(s). "
                         f"Action: {c_action} ({(c_conf or 0)*100:.0f}% conf)")
            lines.append(f"     \"{c_reason[:80]}\"")
        else:
            lines.append(f"  🔵 CLAUDE (Strategist): NOT CALLED ⚠️")
            lines.append(f"     {_claude_not_reached_reason(cr, hold)}")

        # Opus
        o_total  = opus.get("total_calls", 0)
        o_action = opus.get("last_action")
        if o_total > 0:
            lines.append(f"  🔴 OPUS (CRO): Triggered. Action: {o_action}")
            if opus.get("last_reason"):
                lines.append(f"     \"{opus['last_reason'][:80]}\"")
        else:
            lines.append(f"  🔴 OPUS (CRO): Not triggered")

    else:
        lines.append("  ⚠️  TradeMonitor was not active for this trade")
        lines.append(f"  ℹ️  {_claude_not_reached_reason(cr, hold)}")

    # Key insight
    lines.append("")
    lines.append(f"  💡 {_key_insight(cr, ml, hold)}")

    # Post-exit result (from learning engine if available)
    post = _get_post_exit_data(cr.get("mint", ""))
    if post:
        lines.append(f"  {post}")

    return lines


def _format_reason(r: str) -> str:
    if "wallet_exit" in r:   return f"Whale exit ({r.split(':')[-1].strip() if ':' in r else r})"
    if "stop_loss_safety" in r: return "Safety-net SL (monitor failed to exit)"
    if "stop_loss" in r:     return "Stop Loss"
    if "take_profit" in r:   return "Take Profit"
    if "ai_groq" in r:       return "AI exit — Groq"
    if "ai_gemini" in r:     return "AI exit — Gemini"
    if "ai_sonnet" in r:     return "AI exit — Claude Sonnet"
    if "ai_opus" in r:       return "AI exit — Claude Opus"
    if "atr_trail" in r:     return "ATR Trailing Stop"
    if "emergency_bsr" in r: return "Emergency — mass exit signal (BSR)"
    if "emergency_liq" in r: return "Emergency — liquidity rug"
    if "moonbag_trail" in r: return "Moonbag trail stop"
    if "stagnant" in r:      return "Stagnant low-MC exit"
    if "max_hold" in r:      return "Max hold time"
    return r.replace("_", " ")


def _claude_not_reached_reason(cr: dict, hold_mins: float) -> str:
    reason = cr.get("reason", "")
    if "wallet_exit" in reason:
        return "Copy-trade whale exit — bot mirrors immediately. Correct behaviour, no AI needed."
    if "stop_loss_safety" in reason:
        return "Safety-net SL fired. The AI monitor may have crashed — check logs."
    if "stop_loss" in reason:
        return f"Static SL fired at {hold_mins:.0f}min. Claude's first review was due at 5min."
    if hold_mins < 3:
        return f"Trade lasted {hold_mins:.0f}min — too short for the 5-min Claude cycle."
    return "Claude was not triggered during this trade."


def _key_insight(cr: dict, ml: dict | None, hold_mins: float) -> str:
    reason = cr.get("reason", "")
    pnl    = float(cr.get("pnl_pct", 0))

    if "wallet_exit" in reason:
        peak = float(cr.get("peak_pnl_pct", 0))
        lag  = cr.get("entry_lag_secs")
        lag_s = f" | Entry lag: {lag:.0f}s" if lag else ""
        return f"Whale exit copied correctly. Peak was +{peak:.0f}% before they sold{lag_s}."

    if ml:
        groq_alerts = ml.get("groq", {}).get("alert_count", 0)
        claude_action = ml.get("claude", {}).get("last_action")
        if "stop_loss" in reason and groq_alerts > 0:
            return (f"SL triggered with {groq_alerts} Groq alert(s) beforehand — "
                    f"on-chain signals confirmed the exit direction.")
        if claude_action == "SELL":
            return "Claude recommended SELL — AI cascade drove this exit correctly."
        if claude_action == "HOLD" and pnl < 0:
            return "Claude said HOLD but price fell to SL — consider tightening AI thresholds."
        if not ml.get("claude", {}).get("total_calls", 0):
            return (f"Claude not reached in {hold_mins:.0f}min hold. "
                    f"For trades < 5min, Groq and emergency stops are the primary protection.")

    if pnl > 20:
        return f"Strong +{pnl:.0f}% exit. TP mechanism working well."
    if pnl < -15:
        return f"Significant loss at {pnl:.0f}%. Review whether AI should have exited earlier."
    return f"Standard {'win' if pnl >= 0 else 'loss'} at {pnl:+.1f}%."


def _get_post_exit_data(mint: str) -> str | None:
    """Try to find post-exit price data from the learning engine."""
    try:
        from elizaos.plugins.solana.learning_engine import _load_outcomes
        outcomes = _load_outcomes()
        for rec in reversed(outcomes):
            if rec.get("mint") == mint:
                p30 = rec.get("price_30m_pct")
                early = rec.get("exit_was_early")
                if p30 is not None:
                    direction = f"{p30:+.1f}% after 30min"
                    verdict = "EXIT CORRECT ✅" if not early else f"EXITED TOO EARLY ⚠️ (missed {p30:.0f}%)"
                    return f"  Post-exit: {direction} — {verdict}"
                break
    except Exception:
        pass
    return None


def _build_rolling_section() -> list[str]:
    """Rolling performance stats from the last 10 closed trades."""
    try:
        if not os.path.exists(_PAPER_TRADES_PATH):
            return ["📊 ROLLING PERFORMANCE: No trade history yet"]
        with open(_PAPER_TRADES_PATH) as f:
            all_trades = json.load(f)
    except Exception:
        return ["📊 ROLLING PERFORMANCE: Could not load trade history"]

    recent = all_trades[-10:] if len(all_trades) >= 10 else all_trades
    total  = len(all_trades)

    recent_wins   = [t for t in recent  if t.get("pnl_sol", 0) > 0]
    all_wins      = [t for t in all_trades if t.get("pnl_sol", 0) > 0]
    all_losses    = [t for t in all_trades if t.get("pnl_sol", 0) <= 0]
    recent_losses = [t for t in recent  if t.get("pnl_sol", 0) <= 0]

    wr_recent = len(recent_wins) / len(recent) * 100 if recent else 0
    wr_all    = len(all_wins) / total * 100 if total else 0
    avg_win   = (sum(t.get("pnl_pct", 0) for t in all_wins)   / len(all_wins))   if all_wins   else 0
    avg_loss  = (sum(t.get("pnl_pct", 0) for t in all_losses) / len(all_losses)) if all_losses else 0

    # Brain activity stats from learning engine
    brain_stats = _compute_brain_stats()

    # AI-driven exits count
    ai_exits = sum(
        1 for t in recent
        if any(t.get("reason", "").startswith(x) for x in ("ai_", "atr_trail", "emergency"))
    )
    safety_exits = sum(1 for t in recent if "safety_net" in t.get("reason", ""))

    lines = [
        "📊 ROLLING PERFORMANCE",
        "",
        f"  Win Rate (last {len(recent)}): {wr_recent:.0f}%  |  Overall: {wr_all:.0f}% ({len(all_wins)}/{total})",
        f"  Avg Win: +{avg_win:.1f}%  |  Avg Loss: {avg_loss:.1f}%",
        f"  AI-driven exits (last {len(recent)}): {ai_exits}  |  Safety-net SL: {safety_exits}",
        "",
        "  Brain Activity (last 10 trades):",
        f"    Groq calls:   {brain_stats['groq_calls']}",
        f"    Gemini calls: {brain_stats['gemini_calls']}",
        f"    Claude calls: {brain_stats['claude_calls']}",
        f"    Opus calls:   {brain_stats['opus_calls']}",
        f"    Claude reached: {brain_stats['claude_pct']:.0f}% of trades",
    ]

    concerns = _identify_concerns(recent, brain_stats, safety_exits)
    if concerns:
        lines.append("")
        lines.append("  ⚠️  CONCERNS:")
        for c in concerns:
            lines.append(f"    • {c}")
    else:
        lines.append("")
        lines.append("  ✅ No concerns identified")

    return lines


def _compute_brain_stats() -> dict:
    """Estimate brain activity from recently closed monitor logs."""
    # Pull from the last 10 decision logs in the learning outcomes
    groq_calls = gemini_calls = claude_calls = opus_calls = 0
    claude_reached = 0
    n = 0

    try:
        from elizaos.plugins.solana.learning_engine import _load_outcomes
        outcomes = _load_outcomes()
        # We can only count from the monitor logs stored with outcomes
        # For now, use totals from all active + recently closed logs
        from elizaos.plugins.solana import trade_monitor as _tm
        recent_logs = list(_tm._closed_logs.values())[-10:]
        n = len(recent_logs)
        for log in recent_logs:
            groq_calls   += log.get("groq", {}).get("total_calls", 0)
            gemini_calls += log.get("gemini", {}).get("total_calls", 0)
            claude_calls += log.get("claude", {}).get("total_calls", 0)
            opus_calls   += log.get("opus", {}).get("total_calls", 0)
            if log.get("claude", {}).get("total_calls", 0) > 0:
                claude_reached += 1
    except Exception:
        pass

    return {
        "groq_calls":   groq_calls,
        "gemini_calls": gemini_calls,
        "claude_calls": claude_calls,
        "opus_calls":   opus_calls,
        "claude_pct":   (claude_reached / n * 100) if n > 0 else 0,
        "n_logs":       n,
    }


def _identify_concerns(recent: list[dict], brain_stats: dict, safety_exits: int) -> list[str]:
    concerns = []

    # Claude not being reached often enough
    if brain_stats["n_logs"] >= 5 and brain_stats["claude_pct"] < 40:
        concerns.append(
            f"Claude only reached on {brain_stats['claude_pct']:.0f}% of trades. "
            "Most exits fire before the 5-min cycle. This is expected for fast copy-trade positions — "
            "Groq's 30s checks are the primary AI layer for short trades."
        )

    # Safety-net SL firing (monitor failures)
    if safety_exits > 0:
        concerns.append(
            f"Safety-net SL fired {safety_exits}x (last 10 trades). "
            "TradeMonitor may be failing to exit — check logs for monitor errors."
        )

    # Win rate below 30%
    recent_wins = [t for t in recent if t.get("pnl_sol", 0) > 0]
    if len(recent) >= 8 and len(recent_wins) / len(recent) < 0.30:
        wr = len(recent_wins) / len(recent) * 100
        concerns.append(
            f"Win rate at {wr:.0f}% over last {len(recent)} trades. "
            "Consider reviewing wallet selection or entry filters."
        )

    return concerns


def _build_learning_section() -> list[str]:
    """Learning engine status."""
    total = 0
    try:
        if os.path.exists(_PAPER_TRADES_PATH):
            with open(_PAPER_TRADES_PATH) as f:
                total = len(json.load(f))
    except Exception:
        pass

    trades_until = 50 - (total % 50) if total > 0 else 50

    rules_count  = 0
    last_session = "Never"
    try:
        patterns_path = os.path.join(_DIR, "winning_patterns.json")
        if os.path.exists(patterns_path):
            with open(patterns_path) as f:
                d = json.load(f)
            rules_count  = len(d.get("patterns", []))
            ts = d.get("analysis_ts")
            if ts:
                dt = datetime.utcfromtimestamp(float(ts))
                last_session = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    pending = 0
    try:
        from elizaos.plugins.solana.learning_engine import _pending_checks
        pending = len(_pending_checks)
    except Exception:
        pass

    return [
        "🧠 LEARNING STATUS",
        "",
        f"  Trades logged:           {total}",
        f"  Until next Opus session: {trades_until} trades",
        f"  Learned rules active:    {rules_count}",
        f"  Last Opus session:       {last_session}",
        f"  Post-exit checks pending: {pending}",
    ]


def _build_telegram_summary(trades: list[dict], nums: list[int]) -> str:
    """Compact version for Telegram (< 500 chars)."""
    parts = [f"🧠 <b>Debrief #{nums[0]}-{nums[-1]}</b>"]
    for entry in trades:
        cr = entry["close_record"]
        ml = entry["monitor_log"]
        p = float(cr.get("pnl_pct", 0))
        s = float(cr.get("pnl_sol", 0))
        emoji = "✅" if s >= 0 else "❌"
        brains = ""
        if ml:
            g = ml.get("groq", {}).get("total_calls", 0)
            c = ml.get("claude", {}).get("total_calls", 0)
            brains = f" [G:{g} C:{c}]"
        parts.append(f"{emoji} {cr.get('token_name','?')[:12]} {p:+.1f}%{brains}")
    return "\n".join(parts)
