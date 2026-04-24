"""holder_guard.smart_money — count overlap between a new token's early
buyers and our curated smart-money roster.

The roster is produced by `build_smart_money_list.py` and persisted to
`smart_money_list.json`. At scout time, we pull the new token's most
recent ~first-N buyers via Helius enhanced transactions and count how
many of them appear in the roster. ≥3 overlap = high conviction signal.

Loaded once, cached in-memory. Call `refresh()` after the offline build
script runs to pick up the new roster without a bot restart.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiohttp

_BASE = Path(__file__).parent.parent
_LIST_FILE = _BASE / "smart_money_list.json"

_PAGE_SIZE_DEFAULT = 100
_FETCH_TIMEOUT_SECS = 6

_smart_set: set[str] = set()
_smart_loaded_ts: float = 0.0
_LOAD_TTL_SECS = 300    # reload from disk every 5 min so offline rebuilds are picked up


def _load() -> set[str]:
    global _smart_set, _smart_loaded_ts
    now = time.time()
    if _smart_set and (now - _smart_loaded_ts) < _LOAD_TTL_SECS:
        return _smart_set
    try:
        if _LIST_FILE.exists():
            data = json.loads(_LIST_FILE.read_text())
            _smart_set = set((data.get("wallets") or {}).keys())
        else:
            _smart_set = set()
    except Exception:
        _smart_set = set()
    _smart_loaded_ts = now
    return _smart_set


def refresh() -> None:
    """Force a reload on next access — call after offline build script."""
    global _smart_loaded_ts
    _smart_loaded_ts = 0.0


def roster_size() -> int:
    return len(_load())


async def count_overlap(
    session: aiohttp.ClientSession,
    pool_address: str,
    mint: str,
    first_n: int = 200,
) -> tuple[int, list[str]] | tuple[None, list]:
    """Return (overlap_count, matched_wallets) for the first-N buyers of a mint.

    Uses Helius enhanced transactions on the pool address. Returns (None, [])
    when the lookup fails (treat as no-data, don't block the scout).
    """
    roster = _load()
    if not roster:
        return None, []

    key = os.getenv("HELIUS_API_KEY", "")
    if not key:
        return None, []

    buyers: list[str] = []
    before: str | None = None
    pages = 0
    max_pages = 3  # keep scout-time lookup cheap — 300 txs is plenty

    while pages < max_pages and len(buyers) < first_n:
        params = f"?api-key={key}&limit={_PAGE_SIZE_DEFAULT}"
        if before:
            params += f"&before={before}"
        url = f"https://api.helius.xyz/v0/addresses/{pool_address}/transactions{params}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECS)) as r:
                if r.status != 200:
                    break
                page = await r.json()
        except Exception:
            break
        if not page:
            break
        for tx in page:
            for tt in (tx.get("tokenTransfers") or []):
                if tt.get("mint") != mint:
                    continue
                buyer = tt.get("toUserAccount")
                if not buyer:
                    continue
                try:
                    if float(tt.get("tokenAmount") or 0) <= 0:
                        continue
                except (ValueError, TypeError):
                    continue
                if buyer not in buyers:
                    buyers.append(buyer)
                    if len(buyers) >= first_n:
                        break
                break   # one buyer per tx
            if len(buyers) >= first_n:
                break
        pages += 1
        if len(page) < _PAGE_SIZE_DEFAULT:
            break
        before = page[-1].get("signature")
        if not before:
            break

    matched = [b for b in buyers if b in roster]
    return len(matched), matched


async def count_for_mint(
    session: aiohttp.ClientSession,
    mint: str,
    first_n: int = 200,
) -> int | None:
    """Convenience wrapper — resolves pool via DexScreener then counts overlap.

    Returns None when the roster is empty, Helius is unavailable, or the pool
    can't be resolved. Scouts treat None as "no data — don't gate on this".
    """
    if not _load():
        return None
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    best = None
    best_liq = 0.0
    for p in (data.get("pairs") or []):
        if p.get("dexId") not in ("pump-amm", "pumpswap", "pumpfun"):
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq > best_liq:
            best_liq = liq
            best = p.get("pairAddress")
    if not best:
        return None

    count, _ = await count_overlap(session, best, mint, first_n=first_n)
    return count
