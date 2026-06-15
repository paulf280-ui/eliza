"""alpha_engine.py — PRIVATE smart-money accumulator (bot-only, not public).

The inverse of Cabal-Hunter: Cabal-Hunter finds coordination to AVOID; this finds
wallets that keep showing up early on tokens that GRADUATE and run, so we can
eventually enter small beside them.

BOUNDARY (do not violate):
  * This is private/selfish — no public endpoints, nothing customer-visible.
  * It reads the shared cabal_cache.db READ-ONLY (mode=ro) so it can never lock,
    slow, or corrupt anything the public Cabal-Hunter tool depends on.
  * It writes ONLY to its own private store (alpha_engine.db).
  * Token feed piggybacks on tokens the bot/tool already scanned — it makes NO
    extra on-chain/Helius calls (only DexScreener for outcomes), so it adds no
    load to the bot's RPC budget.

What it does on a loop (default every 15 min):
  1. collect_new()  — snapshot early holders of any newly-seen mint, PERMANENTLY,
     before the 8h cache eviction throws them away.
  2. poll_outcomes() — re-check each tracked token on DexScreener: liquidity,
     mcap, price, graduation, and PEAK multiple over our first-seen baseline.

After a few weeks of accumulation, run alpha_analyze.py to score wallets.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
ALPHA_DB = os.environ.get("ALPHA_DB", str(BASE / "alpha_engine.db"))
CACHE_DB = os.environ.get("CABAL_CACHE_DB", str(BASE / "cabal_cache.db"))

POLL_INTERVAL_SECS = int(os.environ.get("ALPHA_POLL_SECS", "900"))   # 15 min
OUTCOME_STALE_SECS = int(os.environ.get("ALPHA_OUTCOME_STALE", "840"))  # re-poll if older than 14 min
PUMP_GRAD_MCAP = 60_000.0   # pump.fun graduates ~$69k mcap; 60k catches the cross
DEAD_LIQ_USD = 2_000.0      # below this = effectively dead
DEAD_GIVEUP_SECS = 3 * 86400  # stop polling tokens dead for >3 days
# NOTE: "on an AMM" (pumpswap/raydium dexId) is NOT graduation — pump tokens route
# through PumpSwap even while tiny. Graduation = a real MCAP milestone. The true
# win signal is peak_multiple over our first-seen baseline, tracked over time.

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

log = logging.getLogger("alpha_engine")


# ── store ──────────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    db = sqlite3.connect(ALPHA_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            mint TEXT PRIMARY KEY,
            token_name TEXT,
            first_seen_ts REAL,
            pair_created_ts REAL,
            cabal_score REAL,
            wallets_checked INTEGER,
            source TEXT
        );
        CREATE TABLE IF NOT EXISTS holdings (
            mint TEXT,
            wallet TEXT,
            rank INTEGER,
            pct REAL,
            label TEXT,
            snapshot_ts REAL,
            PRIMARY KEY (mint, wallet)
        );
        CREATE INDEX IF NOT EXISTS idx_holdings_wallet ON holdings(wallet);
        CREATE TABLE IF NOT EXISTS outcomes (
            mint TEXT PRIMARY KEY,
            first_check_ts REAL,
            last_check_ts REAL,
            checks INTEGER DEFAULT 0,
            first_price_usd REAL,
            first_mcap_usd REAL,
            cur_liq_usd REAL,
            cur_mcap_usd REAL,
            cur_price_usd REAL,
            max_mcap_usd REAL,
            max_price_usd REAL,
            peak_multiple REAL,
            dex_id TEXT,
            graduated INTEGER DEFAULT 0,
            status TEXT
        );
        -- price PATH per poll — needed to derive SL/TP later: how deep winners
        -- dip before they run (max adverse excursion) and time-to-peak. This
        -- CANNOT be reconstructed after the fact, so capture it as it happens.
        CREATE TABLE IF NOT EXISTS price_path (
            mint TEXT, ts REAL, price_usd REAL, mcap_usd REAL, liq_usd REAL
        );
        CREATE INDEX IF NOT EXISTS idx_path_mint ON price_path(mint, ts);
        -- Cluster MASTER/funder wallets — the operator who funds a coordinated push
        -- (often doesn't hold tokens directly, so not in `holdings`). Build their
        -- reputation across tokens: a funder whose pushes keep running = follow them
        -- into fresh launches. Capture now; can't backfill once the cache evicts.
        CREATE TABLE IF NOT EXISTS cluster_masters (
            mint TEXT,
            master TEXT,
            cluster_type TEXT,
            wallet_count INTEGER,
            combined_pct REAL,
            snapshot_ts REAL,
            PRIMARY KEY (mint, master)
        );
        CREATE INDEX IF NOT EXISTS idx_masters_master ON cluster_masters(master);
        """
    )
    db.commit()
    return db


def _cache_ro() -> sqlite3.Connection | None:
    """Open the shared cache strictly read-only — never lock/modify it."""
    try:
        return sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, timeout=10)
    except Exception as e:
        log.warning("cache open failed: %s", e)
        return None


# ── 1. collect: snapshot early holders before the cache evicts them ─────────────
def collect_new(db: sqlite3.Connection) -> int:
    cache = _cache_ro()
    if cache is None:
        return 0
    try:
        rows = cache.execute(
            "SELECT mint, token_name, cabal_score, holders_json, "
            "pair_created_ts, wallets_checked, clusters_json FROM cabal_cache"
        ).fetchall()
    except Exception as e:
        log.warning("cache read failed: %s", e)
        return 0
    finally:
        cache.close()

    known = {r[0] for r in db.execute("SELECT mint FROM tokens").fetchall()}
    now = time.time()
    added = 0
    masters_added = 0
    for mint, name, score, hjson, pair_ts, wchecked, cjson in rows:
        if mint in known:
            continue
        holders = json.loads(hjson or "[]")
        # early candidate buyers = non-LP holders; keep label so analysis can filter CEX/infra
        snaps = [
            (mint, h["address"], h.get("rank"), float(h.get("pct") or 0),
             h.get("label"), now)
            for h in holders
            if h.get("address") and not h.get("is_lp")
        ]
        if not snaps:
            continue
        db.execute(
            "INSERT OR IGNORE INTO tokens (mint, token_name, first_seen_ts, "
            "pair_created_ts, cabal_score, wallets_checked, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (mint, name, now, pair_ts, score, wchecked, "cache"),
        )
        db.executemany(
            "INSERT OR IGNORE INTO holdings (mint, wallet, rank, pct, label, snapshot_ts) "
            "VALUES (?,?,?,?,?,?)",
            snaps,
        )
        # Cluster master/funder wallets — only 'funding' clusters carry a real wallet
        # in master_full (time_sync/coordinated_exit use a "slot N" placeholder).
        for c in json.loads(cjson or "[]"):
            master = (c.get("master_full") or "").strip()
            if c.get("type") == "funding" and 32 <= len(master) <= 44:
                db.execute(
                    "INSERT OR IGNORE INTO cluster_masters "
                    "(mint, master, cluster_type, wallet_count, combined_pct, snapshot_ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (mint, master, c.get("type"), c.get("wallet_count"),
                     float(c.get("combined_pct") or 0), now),
                )
                masters_added += 1
        added += 1
    db.commit()
    if added:
        log.info("collected %d new tokens (holders + %d cluster masters)", added, masters_added)
    return added


# ── 2. outcomes: poll DexScreener for graduation + peak multiple ────────────────
def _dexscreener(mints: list[str]) -> dict[str, dict]:
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(mints)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    out: dict[str, dict] = {}
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        log.warning("dexscreener err: %s", e)
        return out
    for p in (data.get("pairs") or []):
        m = (p.get("baseToken") or {}).get("address")
        if not m:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        prev = out.get(m)
        if prev is None or liq > prev["liq"]:
            out[m] = {
                "liq": liq,
                "mcap": float(p.get("marketCap") or p.get("fdv") or 0),
                "price": float(p.get("priceUsd") or 0),
                "dex": (p.get("dexId") or "").lower(),
            }
    return out


def _due_mints(db: sqlite3.Connection) -> list[str]:
    now = time.time()
    rows = db.execute(
        """
        SELECT t.mint FROM tokens t
        LEFT JOIN outcomes o ON o.mint = t.mint
        WHERE o.mint IS NULL
           OR ( (? - o.last_check_ts) > ?
                AND NOT (o.status='DIED' AND (? - t.first_seen_ts) > ?) )
        """,
        (now, OUTCOME_STALE_SECS, now, DEAD_GIVEUP_SECS),
    ).fetchall()
    return [r[0] for r in rows]


def poll_outcomes(db: sqlite3.Connection) -> int:
    due = _due_mints(db)
    if not due:
        return 0
    now = time.time()
    updated = 0
    for i in range(0, len(due), 30):
        batch = due[i:i + 30]
        state = _dexscreener(batch)
        for mint in batch:
            s = state.get(mint, {"liq": 0, "mcap": 0, "price": 0, "dex": ""})
            row = db.execute(
                "SELECT first_check_ts, first_price_usd, first_mcap_usd, "
                "max_mcap_usd, max_price_usd, graduated FROM outcomes WHERE mint=?",
                (mint,),
            ).fetchone()
            # graduation = a real mcap milestone (sticky once crossed), not "on an AMM"
            graduated = s["mcap"] >= PUMP_GRAD_MCAP
            if row is None:
                first_price = s["price"]
                first_mcap = s["mcap"]
                max_mcap = s["mcap"]
                max_price = s["price"]
                first_ts = now
                checks = 1
                grad = 1 if graduated else 0
            else:
                first_ts, first_price, first_mcap, max_mcap, max_price, grad = row
                first_price = first_price or s["price"]
                first_mcap = first_mcap or s["mcap"]
                max_mcap = max(max_mcap or 0, s["mcap"])
                max_price = max(max_price or 0, s["price"])
                grad = 1 if (grad or graduated) else 0
                checks = None  # bumped via SQL
            peak_multiple = (max_price / first_price) if first_price else 0.0
            if s["liq"] < DEAD_LIQ_USD and not grad:
                status = "DIED"
            elif grad:
                status = "GRADUATED"
            else:
                status = "TRACKING"
            db.execute(
                """
                INSERT INTO outcomes (mint, first_check_ts, last_check_ts, checks,
                    first_price_usd, first_mcap_usd, cur_liq_usd, cur_mcap_usd,
                    cur_price_usd, max_mcap_usd, max_price_usd, peak_multiple,
                    dex_id, graduated, status)
                VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mint) DO UPDATE SET
                    last_check_ts=excluded.last_check_ts,
                    checks=outcomes.checks+1,
                    cur_liq_usd=excluded.cur_liq_usd,
                    cur_mcap_usd=excluded.cur_mcap_usd,
                    cur_price_usd=excluded.cur_price_usd,
                    max_mcap_usd=excluded.max_mcap_usd,
                    max_price_usd=excluded.max_price_usd,
                    peak_multiple=excluded.peak_multiple,
                    dex_id=excluded.dex_id,
                    graduated=excluded.graduated,
                    status=excluded.status
                """,
                (mint, first_ts, now, first_price, first_mcap, s["liq"], s["mcap"],
                 s["price"], max_mcap, max_price, peak_multiple, s["dex"], grad, status),
            )
            # record one path point per poll (only live/due tokens reach here)
            db.execute(
                "INSERT INTO price_path (mint, ts, price_usd, mcap_usd, liq_usd) "
                "VALUES (?,?,?,?,?)",
                (mint, now, s["price"], s["mcap"], s["liq"]),
            )
            updated += 1
        db.commit()
        time.sleep(1.2)  # be polite to DexScreener
    log.info("polled %d token outcomes", updated)
    return updated


def stats(db: sqlite3.Connection) -> dict:
    one = lambda q, *a: db.execute(q, a).fetchone()[0]
    return {
        "tokens": one("SELECT COUNT(*) FROM tokens"),
        "wallets": one("SELECT COUNT(DISTINCT wallet) FROM holdings"),
        "recurring_wallets": one(
            "SELECT COUNT(*) FROM (SELECT wallet FROM holdings GROUP BY wallet HAVING COUNT(DISTINCT mint)>=2)"),
        "graduated": one("SELECT COUNT(*) FROM outcomes WHERE graduated=1"),
        "died": one("SELECT COUNT(*) FROM outcomes WHERE status='DIED'"),
        "tracking": one("SELECT COUNT(*) FROM outcomes WHERE status='TRACKING'"),
        "masters": one("SELECT COUNT(DISTINCT master) FROM cluster_masters"),
        "recurring_masters": one(
            "SELECT COUNT(*) FROM (SELECT master FROM cluster_masters GROUP BY master HAVING COUNT(DISTINCT mint)>=2)"),
    }


def run_once(db: sqlite3.Connection) -> None:
    try:
        collect_new(db)
    except Exception:
        log.exception("collect_new failed")
    try:
        poll_outcomes(db)
    except Exception:
        log.exception("poll_outcomes failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [alpha] %(levelname)s %(message)s",
    )
    log.info("alpha_engine starting — db=%s cache=%s interval=%ss",
             ALPHA_DB, CACHE_DB, POLL_INTERVAL_SECS)
    db = _db()
    # snapshot immediately on boot so we grab the current cache before it evicts
    run_once(db)
    log.info("accumulation stats: %s", stats(db))
    while True:
        time.sleep(POLL_INTERVAL_SECS)
        run_once(db)


if __name__ == "__main__":
    main()
