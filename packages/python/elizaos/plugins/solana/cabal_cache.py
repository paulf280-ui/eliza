"""cabal_cache.py — SQLite-backed cache for pre-indexed cluster/cabal results.

The bot scanner already runs check_holder_clusters() on every token it considers
for entry. This module persists those results so the Cabal-Hunter MCP server can
return sub-100ms responses without re-running the analysis.

Schema:
  cabal_cache (
    mint          TEXT PRIMARY KEY,
    token_name    TEXT,
    risk          TEXT,          -- CLEAN | MEDIUM | HIGH
    cabal_score   REAL,          -- 0-100
    is_controlled INTEGER,       -- boolean
    clusters_json TEXT,          -- JSON array of cluster objects
    holders_json  TEXT,          -- JSON array of holder objects (bubble map)
    wallets_checked INTEGER,
    pair_created_ts REAL,
    computed_at   REAL,          -- unix timestamp
    expires_at    REAL           -- unix timestamp (computed_at + TTL)
  )
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent
_DB_PATH = _DIR / "cabal_cache.db"
_TTL_SECS = 8 * 3600   # 8 hours — covers the active trading window

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent reads
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS cabal_cache (
                mint             TEXT PRIMARY KEY,
                token_name       TEXT,
                risk             TEXT,
                cabal_score      REAL,
                is_controlled    INTEGER,
                clusters_json    TEXT,
                holders_json     TEXT,
                wallets_checked  INTEGER,
                pair_created_ts  REAL,
                computed_at      REAL,
                expires_at       REAL
            )
        """)
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expires ON cabal_cache(expires_at)"
        )
        # Migrations — additive columns for deployer reputation + time-sync flag
        for col, decl in (("deployer_json", "TEXT"), ("time_sync", "INTEGER")):
            try:
                _conn.execute(f"ALTER TABLE cabal_cache ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn.commit()
    return _conn


def _cabal_score_from_clusters(clusters: list[dict], holders: list[dict]) -> float:
    """Compute cabal score: % of supply controlled by coordinated wallets."""
    if not clusters:
        return 0.0
    total_supply = sum(float(h.get("pct", 0)) for h in holders) if holders else 100.0
    coordinated_pct = sum(float(c.get("combined_pct", 0)) for c in clusters)
    return round(min(coordinated_pct / total_supply * 100, 100.0), 1) if total_supply > 0 else 0.0


def save_result(
    mint: str,
    token_name: str,
    cluster_result: dict,
    pair_created_ts: float = 0.0,
) -> None:
    """Persist a cluster_check / get_cluster_map result to the cache.

    Accepts either the compact check_holder_clusters() result or the full
    get_cluster_map() result (which includes the holders list).
    """
    # Degraded results (owner lookups failed mid-scan) must not be cached —
    # caching them once served 15×"LP Pool" garbage for 8 hours.
    if cluster_result.get("degraded"):
        print(f"[cabal-cache] skip caching degraded result for {mint[:8]}")
        return

    now = time.time()
    risk     = cluster_result.get("risk", "CLEAN")
    clusters = cluster_result.get("clusters") or []
    holders  = cluster_result.get("holders") or []
    checked  = cluster_result.get("wallets_checked", 0)

    # Respect the blended score when provided (includes deployer floor);
    # only recompute from clusters for legacy callers that didn't set it.
    cabal_score = cluster_result.get("cabal_score")
    if cabal_score is None:
        cabal_score = _cabal_score_from_clusters(clusters, holders)
    is_controlled = 1 if (risk == "HIGH" or cabal_score >= 35.0) else 0
    deployer  = cluster_result.get("deployer")
    time_sync = 1 if cluster_result.get("time_sync") else 0

    try:
        conn = _get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO cabal_cache
              (mint, token_name, risk, cabal_score, is_controlled,
               clusters_json, holders_json, wallets_checked,
               pair_created_ts, computed_at, expires_at,
               deployer_json, time_sync)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            mint, token_name, risk, cabal_score, is_controlled,
            json.dumps(clusters), json.dumps(holders), checked,
            pair_created_ts, now, now + _TTL_SECS,
            json.dumps(deployer) if deployer else None, time_sync,
        ))
        conn.commit()
    except Exception as e:
        print(f"[cabal-cache] write failed for {mint[:8]}: {e}")


def get_result(mint: str) -> dict | None:
    """Return a cached result if it exists and hasn't expired."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM cabal_cache WHERE mint=? AND expires_at>?",
            (mint, time.time())
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM cabal_cache LIMIT 0").description or []]
        # fallback column list if description not available
        cols = cols or [
            "mint","token_name","risk","cabal_score","is_controlled",
            "clusters_json","holders_json","wallets_checked",
            "pair_created_ts","computed_at","expires_at"
        ]
        d = dict(zip(cols, row))
        out = {
            "mint":            d["mint"],
            "token_name":      d["token_name"],
            "risk":            d["risk"],
            "cabal_score":     d["cabal_score"],
            "is_controlled":   bool(d["is_controlled"]),
            "time_sync":       bool(d.get("time_sync")),
            "deployer":        json.loads(d["deployer_json"]) if d.get("deployer_json") else None,
            "clusters":        json.loads(d["clusters_json"] or "[]"),
            "holders":         json.loads(d["holders_json"] or "[]"),
            "wallets_checked": d["wallets_checked"],
            "pair_created_ts": d["pair_created_ts"],
            "computed_at":     d["computed_at"],
            "cached":          True,
            "age_seconds":     round(time.time() - d["computed_at"]),
        }
        # Re-apply the deployer score floor on read — covers rows cached
        # before blending existed, so map/API/alerts always agree.
        try:
            from elizaos.plugins.solana.deployer_check import blend_deployer_into_score
            blend_deployer_into_score(out, out.get("deployer"))
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[cabal-cache] read failed for {mint[:8]}: {e}")
        return None


def purge_expired() -> int:
    """Remove expired entries. Called periodically to keep the DB tidy."""
    try:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM cabal_cache WHERE expires_at<?", (time.time(),))
        conn.commit()
        return cur.rowcount
    except Exception:
        return 0


def cache_size() -> int:
    """Return the number of active (non-expired) cached entries."""
    try:
        return _get_conn().execute(
            "SELECT COUNT(*) FROM cabal_cache WHERE expires_at>?", (time.time(),)
        ).fetchone()[0]
    except Exception:
        return 0
