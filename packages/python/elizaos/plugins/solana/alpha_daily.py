"""alpha_daily.py — daily digest + snapshot for the private alpha engine.

Run read-only any time for a digest; run with --snapshot (once/day via cron) to
persist the day's wallet leaderboard so we can watch the list STABILISE over time.
The whole point: a wallet that recurs on winners across MANY days is real smart
money; a one-day flash is luck. Persistence (days_on_board) is the truth filter.

Writes only to its own private alpha_engine.db (its own snapshot tables).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
ALPHA_DB = os.environ.get("ALPHA_DB", str(BASE / "alpha_engine.db"))

MIN_TOKENS = int(os.environ.get("ALPHA_MIN_TOKENS", "2"))     # thin early; raise later
WIN_PEAK_MULT = float(os.environ.get("ALPHA_WIN_PEAK", "2.0"))


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(ALPHA_DB, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_summary (
            day TEXT PRIMARY KEY, ts REAL,
            tokens INT, wallets INT, recurring INT,
            graduated INT, runners INT, base_win_rate REAL,
            qualified_wallets INT, lift REAL
        );
        CREATE TABLE IF NOT EXISTS wallet_daily (
            day TEXT, wallet TEXT, tokens INT, wins INT, win_rate REAL, lift REAL,
            PRIMARY KEY (day, wallet)
        );
        CREATE INDEX IF NOT EXISTS idx_wd_wallet ON wallet_daily(wallet);
        CREATE TABLE IF NOT EXISTS graduated_seen (
            mint TEXT PRIMARY KEY, first_grad_day TEXT
        );
        """
    )
    db.commit()
    return db


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _compute(db: sqlite3.Connection):
    rows = db.execute(
        "SELECT t.mint, COALESCE(o.graduated,0), COALESCE(o.peak_multiple,0), "
        "COALESCE(o.cur_mcap_usd,0), COALESCE(o.max_mcap_usd,0) "
        "FROM tokens t LEFT JOIN outcomes o ON o.mint=t.mint"
    ).fetchall()
    win = {m: (bool(g) or pk >= WIN_PEAK_MULT) for m, g, pk, cm, mm in rows}
    runners = sum(1 for _, _, _, cm, _ in rows if cm >= 100_000)
    graduated = sum(1 for _, g, _, _, _ in rows if g)

    wallet_tokens = defaultdict(set)
    for mint, wallet in db.execute(
        "SELECT mint, wallet FROM holdings WHERE label IS NULL"
    ).fetchall():
        if mint in win:
            wallet_tokens[wallet].add(mint)
    return rows, win, wallet_tokens, runners, graduated


def _leaderboard(win, wallet_tokens, base):
    board = []
    for w, ms in wallet_tokens.items():
        if len(ms) < MIN_TOKENS:
            continue
        wins = sum(1 for m in ms if win[m])
        rate = wins / len(ms)
        lift = (rate / base) if base else 0
        board.append((w, len(ms), wins, rate, lift))
    board.sort(key=lambda x: (-x[4], -x[1]))
    return board


def snapshot(db: sqlite3.Connection, day: str):
    rows, win, wallet_tokens, runners, graduated = _compute(db)
    n = len(rows)
    wins = sum(win.values())
    base = wins / n if n else 0
    board = _leaderboard(win, wallet_tokens, base)
    qtot = sum(t for _, t, _, _, _ in board)
    qhit = sum(wn for _, _, wn, _, _ in board)
    agg_lift = ((qhit / qtot) / base) if (qtot and base) else 0

    db.execute(
        "INSERT OR REPLACE INTO daily_summary VALUES (?,?,?,?,?,?,?,?,?,?)",
        (day, time.time(), n, len(wallet_tokens),
         sum(1 for ms in wallet_tokens.values() if len(ms) >= 2),
         graduated, runners, base, len(board), agg_lift),
    )
    db.execute("DELETE FROM wallet_daily WHERE day=?", (day,))
    db.executemany(
        "INSERT INTO wallet_daily VALUES (?,?,?,?,?,?)",
        [(day, w, t, wn, r, l) for w, t, wn, r, l in board],
    )
    # record first day each token is observed graduated
    for m, g, *_ in rows:
        if g:
            db.execute(
                "INSERT OR IGNORE INTO graduated_seen (mint, first_grad_day) VALUES (?,?)",
                (m, day),
            )
    db.commit()


def digest(db: sqlite3.Connection, day: str):
    cur = db.execute("SELECT * FROM daily_summary WHERE day=?", (day,)).fetchone()
    cols = [d[0] for d in db.execute("SELECT * FROM daily_summary WHERE day=?", (day,)).description]
    if not cur:
        print("no snapshot for today yet — run with --snapshot"); return
    s = dict(zip(cols, cur))
    prev = db.execute(
        "SELECT tokens, graduated, qualified_wallets FROM daily_summary WHERE day<? ORDER BY day DESC LIMIT 1",
        (day,),
    ).fetchone()
    dt = dg = dq = None
    if prev:
        dt, dg, dq = s["tokens"] - prev[0], s["graduated"] - prev[1], s["qualified_wallets"] - prev[2]

    def delta(x):
        return "" if x is None else f"  ({'+' if x >= 0 else ''}{x} vs yest)"

    print(f"\n════ ALPHA ENGINE — {day} ════")
    print(f"tokens tracked : {s['tokens']}{delta(dt)}")
    print(f"graduated      : {s['graduated']}{delta(dg)}   runners(mcap≥100k): {s['runners']}")
    print(f"base win rate  : {s['base_win_rate']:.1%}   (win = graduated or peak ≥ {WIN_PEAK_MULT}x)")
    print(f"qualified wallets (≥{MIN_TOKENS} tokens): {s['qualified_wallets']}{delta(dq)}")
    print(f"aggregate lift : {s['lift']:.2f}x  (>1 = qualified wallets beat random)")

    # tokens that first showed graduated today
    newg = db.execute("SELECT mint FROM graduated_seen WHERE first_grad_day=?", (day,)).fetchall()
    print(f"\n— graduations first seen today: {len(newg)}")
    for (m,) in newg[:8]:
        print(f"    {m}")

    # leaderboard WITH persistence (days_on_board) — the truth filter
    print(f"\n— TOP WALLETS today (persistence = days this wallet has made the board)")
    board = db.execute(
        "SELECT wallet, tokens, wins, win_rate, lift FROM wallet_daily WHERE day=? "
        "ORDER BY lift DESC, tokens DESC LIMIT 25", (day,)
    ).fetchall()
    for w, t, wn, r, l in board:
        days_on = db.execute(
            "SELECT COUNT(DISTINCT day) FROM wallet_daily WHERE wallet=?", (w,)
        ).fetchone()[0]
        star = " ⭐" if days_on >= 3 else ""
        print(f"    {w[:6]}..{w[-4:]}  {wn}/{t} won ({r:.0%})  lift {l:.2f}x  · {days_on}d on board{star}")

    # who is MOST persistent across all days (the emerging core list)
    print(f"\n— MOST PERSISTENT wallets across all days (the list to watch)")
    persistent = db.execute(
        "SELECT wallet, COUNT(DISTINCT day) d, AVG(win_rate) ar, MAX(tokens) mt "
        "FROM wallet_daily GROUP BY wallet HAVING d>=2 ORDER BY d DESC, ar DESC LIMIT 20"
    ).fetchall()
    if not persistent:
        print("    (none yet — needs ≥2 days of snapshots; check back tomorrow)")
    for w, d, ar, mt in persistent:
        print(f"    {w[:6]}..{w[-4:]}  on board {d} days  avg win {ar:.0%}  up to {mt} tokens")

    # MASTER/FUNDER wallets — the operators funding coordinated pushes. A funder
    # whose tokens keep graduating/running is one to FOLLOW into fresh launches.
    print(f"\n— MASTER/FUNDER wallets (funded ≥2 tokens, ranked by wins)")
    try:
        masters = db.execute(
            """SELECT cm.master, COUNT(DISTINCT cm.mint) tokens,
                      SUM(CASE WHEN o.graduated=1 OR COALESCE(o.peak_multiple,0)>=? THEN 1 ELSE 0 END) wins,
                      MAX(cm.combined_pct) max_pct
               FROM cluster_masters cm LEFT JOIN outcomes o ON o.mint=cm.mint
               GROUP BY cm.master HAVING tokens>=2 ORDER BY wins DESC, tokens DESC LIMIT 15""",
            (WIN_PEAK_MULT,),
        ).fetchall()
        if not masters:
            print("    (none recurring yet — funders need to appear on ≥2 scanned tokens)")
        for m, t, wn, mp in masters:
            print(f"    {m[:6]}..{m[-4:]}  funded {t} tokens, {wn or 0} won  (up to {mp:.0f}% supply/push)")
    except Exception as e:
        print(f"    (master table not ready: {e})")

    # OPERATORS — deployer + the wallet that funded the deployer. The snipe thesis:
    # an operator whose tokens KEEP hitting 30K is one to follow into fresh launches,
    # even if the tokens ultimately rug (we exit at the swing). 30K = the target.
    REACHED = 30000.0
    for role, col in [("deployer_funder", "MASTER FUNDERS (funded the deployer)"),
                      ("deployer", "DEPLOYERS (the operator wallet)")]:
        print(f"\n— {col} — funded ≥2 tokens, ranked by how many hit 30K")
        try:
            rows = db.execute(
                f"""SELECT op.{role}, COUNT(DISTINCT op.mint) tokens,
                       SUM(CASE WHEN COALESCE(oc.max_mcap_usd,0)>=? THEN 1 ELSE 0 END) hit30k,
                       MAX(op.deployer_verdict) verdict
                    FROM token_operators op LEFT JOIN outcomes oc ON oc.mint=op.mint
                    WHERE op.{role} IS NOT NULL
                    GROUP BY op.{role} HAVING tokens>=2
                    ORDER BY hit30k DESC, tokens DESC LIMIT 15""",
                (REACHED,),
            ).fetchall()
            if not rows:
                print("    (none recurring yet — needs the same operator on ≥2 scanned tokens)")
            for w, t, hit, vd in rows:
                tag = "⛔serial" if vd == "SERIAL_RUGGER" else ("⚠poor" if vd == "POOR_TRACK_RECORD" else "")
                print(f"    {w[:6]}..{w[-4:]}  {t} tokens, {hit or 0} hit 30K  {tag}")
        except Exception as e:
            print(f"    (operators table not ready: {e})")
    print()


def main():
    db = _db()
    day = _today()
    if "--snapshot" in sys.argv:
        snapshot(db, day)
    elif not db.execute("SELECT 1 FROM daily_summary WHERE day=?", (day,)).fetchone():
        snapshot(db, day)  # auto-snapshot if none yet today, so digest always works
    digest(db, day)
    db.close()


if __name__ == "__main__":
    main()
