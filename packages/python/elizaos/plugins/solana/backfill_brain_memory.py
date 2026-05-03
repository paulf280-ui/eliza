"""backfill_brain_memory.py — one-off seed of historical context into all 3 brains.

Reads `copy_trade_paper_trades.json` (119 closed trades as of 2026-04-18) and
writes a `historical_summary` block + distilled `learned_patterns` into each of
the 3 brain memory files so Groq / Gemini / Claude wake up knowing:

- Overall historical WR and net SOL
- Best- and worst-performing copied wallets
- Most common exit reasons
- Hold-time distribution (win vs loss)

This does NOT flood `recent_decisions` (capped at 60) — it adds a new
`historical_summary` field that `brain_memory_as_prompt` surfaces in the
system prompt block.

Run:
    .venv_py/bin/python -m elizaos.plugins.solana.backfill_brain_memory
"""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter, defaultdict

from elizaos.plugins.solana.brain_memory import (
    BRAIN_FILES,
    load_brain_memory,
    save_brain_memory,
)

_DIR = os.path.dirname(__file__)
_TRADES_PATH = os.path.join(_DIR, "copy_trade_paper_trades.json")


def _compute_summary(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("pnl_sol") is not None]
    if not closed:
        return {}
    wins = [t for t in closed if (t.get("pnl_sol") or 0) > 0]

    by_wallet: dict[str, list] = defaultdict(list)
    for t in closed:
        w = (t.get("wallet") or "").strip() or "unknown"
        by_wallet[w].append(t)
    wallet_stats = {}
    for w, recs in by_wallet.items():
        w_wins = sum(1 for r in recs if (r.get("pnl_sol") or 0) > 0)
        wallet_stats[w] = {
            "trades":  len(recs),
            "wr":      round(w_wins / len(recs) * 100, 1),
            "net_sol": round(sum(r.get("pnl_sol") or 0 for r in recs), 3),
        }

    by_reason = Counter((t.get("reason") or "unknown").split(":")[0].lower() for t in closed)

    hold_buckets = {"<2min": 0, "2-5min": 0, "5-15min": 0, "15-60min": 0, ">60min": 0}
    win_hold_buckets = dict(hold_buckets)
    for t in closed:
        h = t.get("hold_mins") or 0
        b = "<2min" if h < 2 else "2-5min" if h < 5 else "5-15min" if h < 15 else "15-60min" if h < 60 else ">60min"
        hold_buckets[b] += 1
        if (t.get("pnl_sol") or 0) > 0:
            win_hold_buckets[b] += 1

    pnls = [float(t.get("pnl_pct") or 0) for t in closed]
    return {
        "total_trades":       len(closed),
        "wins":               len(wins),
        "losses":             len(closed) - len(wins),
        "win_rate_pct":       round(len(wins) / len(closed) * 100, 1),
        "net_sol":            round(sum(t.get("pnl_sol") or 0 for t in closed), 3),
        "median_pnl_pct":     round(statistics.median(pnls), 1),
        "best_pnl_pct":       round(max(pnls), 1),
        "worst_pnl_pct":      round(min(pnls), 1),
        "by_wallet":          dict(sorted(wallet_stats.items(), key=lambda kv: -kv[1]["net_sol"])),
        "by_exit_reason":     dict(by_reason.most_common(10)),
        "hold_bucket_counts": hold_buckets,
        "hold_bucket_wins":   win_hold_buckets,
        "first_trade":        closed[0].get("dt"),
        "last_trade":         closed[-1].get("dt"),
    }


def _derive_patterns(summary: dict) -> list[str]:
    """Distil key patterns the brains should know from 119 closed trades."""
    patterns: list[str] = []
    wr = summary.get("win_rate_pct", 0)
    net = summary.get("net_sol", 0)
    patterns.append(f"Historical baseline: {summary['total_trades']} trades, WR={wr}%, net={net:+.3f} SOL.")

    by_wallet = summary.get("by_wallet", {})
    # Top 3 by net (profitable)
    top = [(w, s) for w, s in by_wallet.items() if s["net_sol"] > 0][:3]
    if top:
        top_str = ", ".join(f"{w}({s['wr']}%WR {s['net_sol']:+.2f}SOL)" for w, s in top)
        patterns.append(f"Best copied wallets: {top_str}")
    # Worst 3 by net
    worst = sorted(by_wallet.items(), key=lambda kv: kv[1]["net_sol"])[:3]
    if worst:
        worst_str = ", ".join(f"{w}({s['wr']}%WR {s['net_sol']:+.2f}SOL)" for w, s in worst)
        patterns.append(f"Worst copied wallets (blacklisted): {worst_str}")

    by_reason = summary.get("by_exit_reason", {})
    if "wallet_exit" in by_reason:
        we_pct = round(by_reason["wallet_exit"] / summary["total_trades"] * 100, 0)
        patterns.append(f"Wallet mirror-exit drives {we_pct:.0f}% of closes — follow the whale out of the position fast.")

    sl20 = by_reason.get("stop_loss_20pct", 0)
    sl10 = by_reason.get("stop_loss_10pct", 0)
    if sl20 and sl10:
        patterns.append(f"SL tightened 20%→10% after analysis: 20% SL avg exit -33% (catastrophic); 10% SL is correct. Never raise SL.")

    tp_wins = by_reason.get("tp_15pct", 0) + by_reason.get("take_profit_42pct", 0) + by_reason.get("take_profit_trail_142pct", 0) + by_reason.get("take_profit_trail_18pct", 0)
    if tp_wins and summary["total_trades"]:
        tp_pct = round(tp_wins / summary["total_trades"] * 100, 0)
        patterns.append(f"Only {tp_pct:.0f}% of trades hit TP — don't hold waiting for TP, exit on whale signal or at breakeven+.")

    hb = summary.get("hold_bucket_counts", {})
    wb = summary.get("hold_bucket_wins", {})
    if hb:
        # Most profitable hold window
        best_bucket = max((b for b in hb if hb[b]), key=lambda b: wb.get(b, 0) / max(hb[b], 1))
        wr_best = round(wb.get(best_bucket, 0) / max(hb[best_bucket], 1) * 100, 0)
        patterns.append(f"Best hold bucket: {best_bucket} (WR={wr_best:.0f}%). Trades >60min have 0% WR — cut dead positions.")

    return patterns


def backfill() -> dict:
    if not os.path.exists(_TRADES_PATH):
        print(f"[backfill] No trades file at {_TRADES_PATH}")
        return {}

    trades = json.load(open(_TRADES_PATH))
    summary = _compute_summary(trades)
    if not summary:
        print("[backfill] no closed trades — nothing to backfill")
        return {}
    patterns = _derive_patterns(summary)

    updated = 0
    for brain in BRAIN_FILES:
        mem = load_brain_memory(brain)
        mem["historical_summary"] = summary
        # Shared baseline goes into shared_context — NOT into learned_patterns.
        # learned_patterns is now per-brain (derived from each brain's own history
        # in memory_refresh_loop). Overwriting it here would re-homogenise them.
        mem["shared_context"] = patterns
        save_brain_memory(brain, mem)
        updated += 1

    print(f"[backfill] ✓ wrote historical_summary + {len(patterns)} patterns to {updated} brain files")
    print(f"  {summary['total_trades']} trades | WR={summary['win_rate_pct']}% | net={summary['net_sol']:+.3f} SOL")
    for p in patterns:
        print(f"    • {p}")
    return summary


if __name__ == "__main__":
    backfill()
