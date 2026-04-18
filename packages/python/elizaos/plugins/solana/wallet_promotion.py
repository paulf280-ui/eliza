"""wallet_promotion.py — analyse copy_trade_signal_log to surface promotable wallets.

Background
──────────
WATCHED_WALLETS in axiom_copy_trader.py currently has 2 open slots after the
2026-04-18 rotation (rambo removed). The signal log contains a long tail of
wallets that showed up as `sold_by_whale` ticks (and sometimes `entered`) but
were never added to the watched set because attribution and volume weren't
obvious at the time.

This module reads `copy_trade_signal_log.json` and `copy_trade_paper_trades.json`
and produces per-wallet aggregates so Jarvis / the dashboard can surface
promotion candidates. It never mutates WATCHED_WALLETS at runtime — the
blacklist of known bad actors (Bundler_EeX, Gang_0SOL, Cented, etc.) lives in
axiom_copy_trader.py and must be honoured by a human review before any wallet
is added to the watched set.

Usage
─────
    from elizaos.plugins.solana.wallet_promotion import build_report
    report = build_report()
    # report = [{wallet, trades, wr_pct, net_sol, median_pnl_pct, last_ts, verdict}, ...]

    # Write to disk for dashboard / Jarvis to read:
    from elizaos.plugins.solana.wallet_promotion import write_report
    write_report()
"""
from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

_DIR = os.path.dirname(__file__)
_TRADES_PATH    = os.path.join(_DIR, "copy_trade_paper_trades.json")
_SIGNAL_PATH    = os.path.join(_DIR, "copy_trade_signal_log.json")
_REPORT_PATH    = os.path.join(_DIR, "wallet_promotion_report.json")

# Wallets we never want to re-consider even if they light up.
# Mirrors the comments at the top of axiom_copy_trader.py.
BLACKLIST: set[str] = {
    "Bundler_EeX",
    "Gang_0SOL",
    "Cented",
    "Whale_CyaE",   # Cented alias
    "Schoen",
    "Axiom_W1",
    # clukz un-blacklisted 2026-04-18 — 20 trades +0.397 SOL, re-promoted to WATCHED
    "Goyim",
    "CookDoc",
    "huvey",
    "MONOgirl",
    "Razr",
    "rambo",        # removed 2026-04-18 — negative expectancy
}

# Minimum sample size before we'll recommend a promotion.
_MIN_TRADES = 3
# Minimum win-rate for a PROMOTE verdict.
_MIN_WR_PCT = 50.0
# Minimum net SOL. Wallets that are coin-flip but net-negative don't get promoted.
_MIN_NET_SOL = 0.0


def _safe_load(path: str) -> list[dict]:
    try:
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def build_report() -> list[dict[str, Any]]:
    """Aggregate per-wallet P&L from paper trade history. Returns a ranked list.

    Each record:
      wallet:           short alias ("Frost", "Wallet_3BLj")
      trades:           total closed trades
      wr_pct:           win rate (wins / trades)
      net_sol:          sum of pnl_sol
      median_pnl_pct:   median of pnl_pct
      last_ts:          most recent trade ts
      verdict:          "PROMOTE" | "HOLD" | "BLACKLIST" | "WATCH"
    """
    trades = _safe_load(_TRADES_PATH)
    if not trades:
        return []

    by_wallet: dict[str, list[dict]] = {}
    for t in trades:
        w = (t.get("wallet") or "").strip()
        if not w:
            continue
        by_wallet.setdefault(w, []).append(t)

    rows: list[dict[str, Any]] = []
    for wallet, recs in by_wallet.items():
        closed = [r for r in recs if r.get("pnl_sol") is not None]
        if not closed:
            continue
        pnls_sol = [float(r.get("pnl_sol") or 0) for r in closed]
        pnls_pct = [float(r.get("pnl_pct") or 0) for r in closed]
        wins = sum(1 for p in pnls_sol if p > 0)
        wr = round(wins / len(closed) * 100, 1)
        net = round(sum(pnls_sol), 4)
        med = round(statistics.median(pnls_pct) if pnls_pct else 0.0, 1)
        last_ts = max((float(r.get("ts") or 0) for r in closed), default=0.0)

        # Verdict
        if wallet in BLACKLIST:
            verdict = "BLACKLIST"
        elif len(closed) < _MIN_TRADES:
            verdict = "WATCH"
        elif wr >= _MIN_WR_PCT and net > _MIN_NET_SOL:
            verdict = "PROMOTE"
        else:
            verdict = "HOLD"

        rows.append({
            "wallet":         wallet,
            "trades":         len(closed),
            "wr_pct":         wr,
            "net_sol":        net,
            "median_pnl_pct": med,
            "last_ts":        last_ts,
            "verdict":        verdict,
        })

    # Rank: PROMOTE by net_sol desc, then WATCH by trades desc, HOLD by wr desc, BLACKLIST last
    order_map = {"PROMOTE": 0, "WATCH": 1, "HOLD": 2, "BLACKLIST": 3}
    rows.sort(key=lambda r: (order_map[r["verdict"]], -r["net_sol"], -r["wr_pct"]))
    return rows


def write_report() -> dict[str, Any]:
    """Build and persist the report. Returns the summary payload."""
    rows = build_report()
    payload = {
        "generated_ts": time.time(),
        "total_wallets": len(rows),
        "promote_count":   sum(1 for r in rows if r["verdict"] == "PROMOTE"),
        "blacklist_count": sum(1 for r in rows if r["verdict"] == "BLACKLIST"),
        "rows": rows,
    }
    try:
        tmp = _REPORT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, _REPORT_PATH)
    except Exception as e:
        print(f"[wallet-promotion] write failed: {e}")
    return payload


if __name__ == "__main__":
    p = write_report()
    print(f"wallet_promotion: {p['total_wallets']} wallets | "
          f"{p['promote_count']} PROMOTE | {p['blacklist_count']} BLACKLIST")
    for r in p["rows"][:10]:
        print(
            f"  {r['verdict']:>10}  {r['wallet']:<20}  "
            f"{r['trades']:>3}t  wr={r['wr_pct']:>5.1f}%  "
            f"net={r['net_sol']:+.4f} SOL  med={r['median_pnl_pct']:+.1f}%"
        )
