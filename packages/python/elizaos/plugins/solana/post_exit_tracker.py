"""
post_exit_tracker.py — Track what a token does AFTER we exit it.

Every closed position is recorded. We then fetch DexScreener at
+30min, +1hr, +4hr, +24hr to see if our exit was too early.

Key questions answered:
  - Did we SL exit while it actually pumped? (bad SL)
  - Did our trailing stop fire at the right time?
  - What did the token do in the 24h after we left?

Usage:
  from elizaos.plugins.solana.post_exit_tracker import record_exit, check_due
  record_exit(mint, exit_price_sol, exit_reason, pnl_pct, dex, entry_price_sol)
  results = await check_due()  # call periodically (e.g. in Jarvis context build)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_STORE_PATH = os.path.join(os.path.dirname(__file__), "post_exit_tracker.json")
_MAX_RECORDS = 500

_store: list[dict] | None = None

_CHECKPOINTS = [
    ("30m", 30 * 60),
    ("1h",  60 * 60),
    ("4h",  4 * 60 * 60),
    ("24h", 24 * 60 * 60),
]


def _load() -> list[dict]:
    global _store
    if _store is not None:
        return _store
    try:
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH) as f:
                data = json.load(f)
            _store = data if isinstance(data, list) else []
        else:
            _store = []
    except Exception:
        _store = []
    return _store


def _save() -> None:
    global _store
    if _store is None:
        return
    try:
        if len(_store) > _MAX_RECORDS:
            _store = _store[-_MAX_RECORDS:]
        with open(_STORE_PATH, "w") as f:
            json.dump(_store, f, indent=2)
    except Exception:
        pass


def record_exit(
    mint: str,
    exit_price_sol: float,
    exit_reason: str,
    pnl_pct: float,
    dex: str = "",
    entry_price_sol: float = 0.0,
    extra: dict | None = None,
) -> None:
    """Record a trade exit for later post-exit price tracking."""
    store = _load()
    now = time.time()

    rec: dict[str, Any] = {
        "mint": mint,
        "dex": dex,
        "exit_reason": exit_reason,
        "exit_price_sol": exit_price_sol,
        "entry_price_sol": entry_price_sol,
        "pnl_pct": round(pnl_pct, 2),
        "ts": now,
        "checks": {},
    }
    # Pre-populate check slots
    for label, delay in _CHECKPOINTS:
        rec["checks"][label] = {
            "due_at": now + delay,
            "done": False,
            "price_change_pct": None,   # % change from our exit price
            "price_sol": None,
        }
    if extra:
        rec["extra"] = extra

    store.append(rec)
    _save()


async def check_due() -> dict:
    """Fetch DexScreener for any overdue post-exit price checks.

    Returns summary dict with per-checkpoint stats.
    """
    import aiohttp

    store = _load()
    now = time.time()

    # Find all mints with at least one overdue checkpoint
    mints_needing: set[str] = set()
    for rec in store:
        for label, check in rec.get("checks", {}).items():
            if not check["done"] and now >= check.get("due_at", 0):
                mints_needing.add(rec["mint"])
                break

    if not mints_needing:
        return {"checked": 0}

    prices: dict[str, float] = {}  # mint → current price_sol
    mints = list(mints_needing)
    BATCH = 30
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
        for i in range(0, len(mints), BATCH):
            batch = mints[i:i + BATCH]
            try:
                async with sess.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    # Pick highest-liq pair per mint
                    seen: dict[str, tuple[float, float]] = {}  # mint → (liq, price)
                    for p in (data.get("pairs") or []):
                        base = (p.get("baseToken") or {}).get("address", "")
                        if not base:
                            continue
                        liq = float((p.get("liquidity") or {}).get("usd") or 0)
                        price = float(p.get("priceNative") or 0)
                        if price > 0 and (base not in seen or liq > seen[base][0]):
                            seen[base] = (liq, price)
                    for base, (_, price) in seen.items():
                        prices[base] = price
            except Exception:
                pass

    updated = 0
    for rec in store:
        mint = rec["mint"]
        if mint not in prices:
            continue
        current_price = prices[mint]
        exit_price = rec.get("exit_price_sol", 0.0) or 0.0
        for label, check in rec.get("checks", {}).items():
            if not check["done"] and now >= check.get("due_at", 0):
                check["done"] = True
                check["price_sol"] = current_price
                if exit_price > 0 and current_price > 0:
                    check["price_change_pct"] = round(
                        (current_price - exit_price) / exit_price * 100, 1
                    )
                updated += 1

    if updated:
        _save()

    return {"checked": len(mints_needing), "checkpoints_updated": updated}


def get_recent(n: int = 50, exit_reason: str | None = None) -> list[dict]:
    """Return n most recent exit records, optionally filtered by reason."""
    store = _load()
    recs = list(reversed(store[-n * 3:]))  # oversample then filter
    if exit_reason:
        recs = [r for r in recs if r.get("exit_reason") == exit_reason]
    return recs[:n]


def get_sl_analysis() -> dict:
    """Compare SL exits vs what the token did after — quantifies overexit rate."""
    store = _load()
    sl_exits = [r for r in store if r.get("exit_reason") in ("stop_loss", "early_stop_loss", "trailing_stop_loss")]
    pumped_after_sl = 0
    flat_after_sl = 0
    rugged_after_sl = 0
    for r in sl_exits:
        chg_1h = (r.get("checks") or {}).get("1h", {}).get("price_change_pct")
        if chg_1h is None:
            continue
        if chg_1h >= 20:
            pumped_after_sl += 1
        elif chg_1h <= -20:
            rugged_after_sl += 1
        else:
            flat_after_sl += 1
    return {
        "total_sl_exits": len(sl_exits),
        "pumped_after_sl_1h": pumped_after_sl,
        "flat_after_sl_1h": flat_after_sl,
        "rugged_after_sl_1h": rugged_after_sl,
        "overexit_rate_pct": round(pumped_after_sl / len(sl_exits) * 100, 1) if sl_exits else 0,
    }
