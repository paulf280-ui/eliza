"""
trading_journal_writer.py
─────────────────────────
Writes every closed trade to a daily JSON journal so Jarvis can learn from
both paper and live sessions.  Files live in trading_journal/ alongside this
module.

Files written:
  trading_journal/YYYY-MM-DD_paper.json   — paper trades
  trading_journal/YYYY-MM-DD_live.json    — live trades
  trading_journal/analysis_notes.json     — Jarvis's own written analysis
  trading_journal/bad_token_dna.json      — patterns from losing trades
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

_JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "trading_journal")


def _journal_path(is_paper: bool, utc_date: str | None = None) -> str:
    if utc_date is None:
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = "paper" if is_paper else "live"
    return os.path.join(_JOURNAL_DIR, f"{utc_date}_{suffix}.json")


def record_trade(trade: dict[str, Any]) -> None:
    """Append a closed trade record to today's journal file.

    Called from position_manager.close_position() after every exit.
    The trade dict must have been enriched with entry DNA fields.
    """
    is_paper = bool(trade.get("paper_trade", False))
    path = _journal_path(is_paper)

    try:
        os.makedirs(_JOURNAL_DIR, exist_ok=True)
        existing: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        # Build a focused entry — only what Jarvis needs to learn from
        entry = {
            "timestamp": trade.get("exit_utc") or datetime.now(timezone.utc).isoformat(),
            "mint": trade.get("mint", ""),
            "symbol": trade.get("token_symbol") or trade.get("token_name") or "?",
            "dex": trade.get("dex", "?"),
            "outcome": trade.get("outcome", "?"),
            "pnl_pct": trade.get("pnl_pct"),
            "pnl_sol": trade.get("pnl_sol"),
            "reason": trade.get("reason", "?"),
            "hold_secs": trade.get("hold_secs"),
            "peak_pnl_pct": trade.get("peak_pnl_pct"),
            "score": trade.get("score"),
            # ── Entry DNA ──────────────────────────────────────────────────────
            "liq_usd_at_entry": trade.get("liq_usd_at_entry"),
            "vol_liq_ratio_at_entry": trade.get("vol_liq_ratio_at_entry"),
            "age_hours_at_entry": round(
                (trade.get("time_since_launch_secs") or 0) / 3600, 2
            ) if trade.get("time_since_launch_secs") else None,
            "h1_pct_at_entry": trade.get("h1_pct_at_entry"),
            "m5_pct_at_entry": trade.get("m5_pct_at_entry"),
            "buy_ratio_at_entry": trade.get("buy_ratio_at_entry"),
            "has_socials": trade.get("has_socials"),
            "score_reasons": trade.get("score_reasons", []),
        }

        existing.append(entry)

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

    except Exception as exc:
        print(f"[journal] Failed to write trade: {exc}")


def get_today_summary(is_paper: bool) -> dict:
    """Return a summary dict of today's journal for Jarvis context injection."""
    path = _journal_path(is_paper)
    if not os.path.exists(path):
        return {"trades": 0, "note": "No trades today yet"}

    try:
        with open(path) as f:
            trades = json.load(f)
        if not isinstance(trades, list) or not trades:
            return {"trades": 0, "note": "No trades today yet"}

        sells = [t for t in trades if t.get("outcome") in ("win", "loss", "rug")]
        wins = [t for t in sells if t.get("outcome") == "win"]
        losses = [t for t in sells if t.get("outcome") == "loss"]
        rugs = [t for t in sells if t.get("outcome") == "rug"]

        avg_win = sum(t.get("pnl_pct") or 0 for t in wins) / max(len(wins), 1)
        avg_loss = sum(t.get("pnl_pct") or 0 for t in losses) / max(len(losses), 1)
        net_sol = sum(t.get("pnl_sol") or 0 for t in sells)

        return {
            "trades": len(sells),
            "wins": len(wins),
            "losses": len(losses),
            "rugs": len(rugs),
            "win_rate": round(len(wins) / max(len(sells), 1) * 100, 1),
            "avg_win_pct": round(avg_win, 1),
            "avg_loss_pct": round(avg_loss, 1),
            "net_sol": round(net_sol, 4),
        }
    except Exception:
        return {"trades": 0, "note": "Error reading journal"}


def get_recent_trades_with_dna(is_paper: bool, limit: int = 20, days_back: int = 3) -> list[dict]:
    """Return the last `limit` closed trades with full entry DNA, across the last N days."""
    from datetime import timedelta
    results: list[dict] = []
    today = datetime.now(timezone.utc)

    for d in range(days_back):
        date_str = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        path = _journal_path(is_paper, date_str)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                day_trades = json.load(f)
            if isinstance(day_trades, list):
                results = day_trades + results  # prepend older days
        except Exception:
            pass

    # Most recent last → return last `limit`
    return results[-limit:]


def write_jarvis_note(note: str, category: str = "general") -> str:
    """Jarvis calls this to write his own analysis notes to the journal.

    Parameters
    ----------
    note: The analysis text Jarvis wants to persist.
    category: Tag for the note (e.g. 'pattern', 'config_change', 'market', 'lesson').

    Returns a confirmation string.
    """
    path = os.path.join(_JOURNAL_DIR, "analysis_notes.json")
    try:
        os.makedirs(_JOURNAL_DIR, exist_ok=True)
        existing: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "note": note,
        }
        existing.append(entry)

        # Keep last 200 notes
        if len(existing) > 200:
            existing = existing[-200:]

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        return f"Note saved to trading_journal/analysis_notes.json [{category}]"
    except Exception as exc:
        return f"Failed to save note: {exc}"


def get_jarvis_notes(limit: int = 20) -> list[dict]:
    """Return the most recent Jarvis analysis notes."""
    path = os.path.join(_JOURNAL_DIR, "analysis_notes.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            notes = json.load(f)
        if isinstance(notes, list):
            return notes[-limit:]
    except Exception:
        pass
    return []


def update_bad_token_dna(trade: dict[str, Any]) -> None:
    """Append losing/rug trade entry DNA to bad_token_dna.json for pattern analysis."""
    if trade.get("outcome") not in ("loss", "rug"):
        return
    path = os.path.join(_JOURNAL_DIR, "bad_token_dna.json")
    try:
        os.makedirs(_JOURNAL_DIR, exist_ok=True)
        existing: list[dict] = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []

        dna = {
            "timestamp": trade.get("exit_utc") or datetime.now(timezone.utc).isoformat(),
            "symbol": trade.get("token_symbol") or "?",
            "dex": trade.get("dex", "?"),
            "outcome": trade.get("outcome", "?"),
            "pnl_pct": trade.get("pnl_pct"),
            "reason": trade.get("reason", "?"),
            "liq_usd": trade.get("liq_usd_at_entry"),
            "vol_liq": trade.get("vol_liq_ratio_at_entry"),
            "age_h": round((trade.get("time_since_launch_secs") or 0) / 3600, 2) if trade.get("time_since_launch_secs") else None,
            "h1_pct": trade.get("h1_pct_at_entry"),
            "m5_pct": trade.get("m5_pct_at_entry"),
            "buy_ratio": trade.get("buy_ratio_at_entry"),
            "has_socials": trade.get("has_socials"),
            "score": trade.get("score"),
        }
        existing.append(dna)

        # Keep last 500 bad trades
        if len(existing) > 500:
            existing = existing[-500:]

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass
