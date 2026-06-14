"""deployer_index.py — persistent, accumulating deployer-DNA database.

Every time we resolve a token's deployer (bot entry, map scan, API call), we
write the verdict here. Over time this becomes our own growing index of every
deployer we've ever seen — like the competitor's "32k deployers, 1.3k serial
ruggers", except built for free as a byproduct of usage.

Two payoffs:
  1. Instant flag — if a brand-new token's creator is already in the index as a
     SERIAL_RUGGER, we know before it even has holders.
  2. A sellable stat — "N deployers indexed, M serial ruggers caught."

We keep the WORST/most-complete view of each deployer (max launches & dead
count ever seen), so the picture only sharpens over time.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DB_PATH = Path(__file__).parent / "deployer_index.db"
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS deployers (
                creator         TEXT PRIMARY KEY,
                verdict         TEXT,
                tokens_launched INTEGER,
                dead            INTEGER,
                sampled         INTEGER,
                dead_pct        REAL,
                first_seen      REAL,
                last_seen       REAL,
                last_mint       TEXT,
                seen_count      INTEGER
            )
        """)
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_dep_verdict ON deployers(verdict)")
        _conn.commit()
    return _conn


def save_deployer(report: dict, mint: str = "") -> None:
    """Upsert a deployer report. Keeps the most-complete view ever seen."""
    creator = (report or {}).get("creator")
    if not creator:
        return
    now = time.time()
    try:
        c = _db()
        row = c.execute("SELECT tokens_launched, dead, sampled, first_seen, seen_count "
                        "FROM deployers WHERE creator=?", (creator,)).fetchone()
        launched = int(report.get("tokens_launched") or 0)
        dead     = int(report.get("dead") or 0)
        sampled  = int(report.get("sampled") or 0)
        if row:
            # Keep the maximum knowledge we've ever had about this deployer
            launched = max(launched, int(row[0] or 0))
            dead     = max(dead, int(row[1] or 0))
            sampled  = max(sampled, int(row[2] or 0))
            first_seen = row[3] or now
            seen_count = int(row[4] or 0) + 1
        else:
            first_seen = now
            seen_count = 1
        dead_pct = round(dead / sampled * 100, 1) if sampled else 0.0
        c.execute("""
            INSERT OR REPLACE INTO deployers
              (creator, verdict, tokens_launched, dead, sampled, dead_pct,
               first_seen, last_seen, last_mint, seen_count)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (creator, report.get("verdict"), launched, dead, sampled, dead_pct,
              first_seen, now, mint or report.get("last_mint") or "", seen_count))
        c.commit()
    except Exception as e:
        print(f"[deployer-index] save failed: {e}")


def get_deployer(creator: str) -> dict | None:
    """Return the indexed record for a creator, or None."""
    if not creator:
        return None
    try:
        c = _db()
        r = c.execute("SELECT creator, verdict, tokens_launched, dead, sampled, "
                      "dead_pct, first_seen, last_seen, seen_count "
                      "FROM deployers WHERE creator=?", (creator,)).fetchone()
        if not r:
            return None
        return {"creator": r[0], "creator_short": r[0][:6] + "…" + r[0][-4:],
                "verdict": r[1], "tokens_launched": r[2], "dead": r[3],
                "sampled": r[4], "dead_pct": r[5], "first_seen": r[6],
                "last_seen": r[7], "seen_count": r[8]}
    except Exception:
        return None


def is_known_rugger(creator: str) -> dict | None:
    """Fast path — return the record only if this creator is a known rugger."""
    rec = get_deployer(creator)
    if rec and rec.get("verdict") in ("SERIAL_RUGGER", "POOR_TRACK_RECORD"):
        return rec
    return None


def stats() -> dict:
    """Sellable index stats."""
    try:
        c = _db()
        total = c.execute("SELECT COUNT(*) FROM deployers").fetchone()[0]
        serial = c.execute("SELECT COUNT(*) FROM deployers WHERE verdict='SERIAL_RUGGER'").fetchone()[0]
        poor = c.execute("SELECT COUNT(*) FROM deployers WHERE verdict='POOR_TRACK_RECORD'").fetchone()[0]
        prolific = c.execute("SELECT COUNT(*) FROM deployers WHERE tokens_launched>=10").fetchone()[0]
        worst = c.execute("SELECT creator, tokens_launched, dead, sampled, dead_pct "
                          "FROM deployers WHERE sampled>0 ORDER BY dead DESC, dead_pct DESC LIMIT 10").fetchall()
        return {
            "deployers_indexed": total,
            "serial_ruggers":    serial,
            "poor_track_record": poor,
            "prolific_10plus":   prolific,
            "worst_offenders": [
                {"creator_short": w[0][:6] + "…" + w[0][-4:], "launched": w[1],
                 "dead": w[2], "sampled": w[3], "dead_pct": w[4]} for w in worst
            ],
        }
    except Exception:
        return {"deployers_indexed": 0}
