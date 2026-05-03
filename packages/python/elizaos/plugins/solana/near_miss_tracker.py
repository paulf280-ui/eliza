"""
near_miss_tracker.py — Track tokens that nearly passed all gates.

A "near miss" is a token that passed 7+ gates but was rejected before entry.
We check DexScreener at 1hr and 2hr to see if it pumped — if it did, our
gates are too strict at that point in the funnel.

Gates (10 total per strategy):
  Strategy B (grad-snipe): freshness, pool_found, liq_floor, not_overbought,
                            not_declining, vol_liq_max, vol_liq_min, buy_ratio,
                            rugcheck, jarvis_quality
  Strategy C/D (raydium):  not_blacklisted, price_available, rugcheck,
                            vol_liq_max, vol_liq_min, buy_ratio, score, age,
                            jarvis_quality, gpt_quality

Usage:
  from elizaos.plugins.solana.near_miss_tracker import maybe_record, check_outcomes
  maybe_record(mint, gates_passed=["freshness","pool_found",...], failed_gate="jarvis_quality",
               strategy="grad", score=8, extra={...})
  results = await check_outcomes(min_age_hours=1.0)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_STORE_PATH = os.path.join(os.path.dirname(__file__), "near_misses.json")
_MAX_RECORDS = 300
_MIN_GATES_FOR_NEAR_MISS = 7  # passed 7+ of 10 gates = near miss

_store: list[dict] | None = None


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


def maybe_record(
    mint: str,
    gates_passed: list[str],
    failed_gate: str,
    *,
    strategy: str = "",
    score: int | None = None,
    extra: dict | None = None,
) -> bool:
    """Record a near-miss if the token passed enough gates.

    Returns True if recorded (was a near miss), False if too few gates passed.
    """
    if len(gates_passed) < _MIN_GATES_FOR_NEAR_MISS:
        return False

    store = _load()

    # Dedup: don't record same mint+failed_gate within 30 minutes
    cutoff = time.time() - 1800
    for r in reversed(store):
        if r["mint"] == mint and r["failed_gate"] == failed_gate and r.get("ts", 0) > cutoff:
            return False

    rec: dict[str, Any] = {
        "mint": mint,
        "strategy": strategy,
        "gates_passed": gates_passed,
        "gates_passed_count": len(gates_passed),
        "failed_gate": failed_gate,
        "ts": time.time(),
        # 1hr check
        "check_1h_due_at": time.time() + 3600,
        "check_1h_done": False,
        "check_1h_price_change_pct": None,
        # 2hr check
        "check_2h_due_at": time.time() + 7200,
        "check_2h_done": False,
        "check_2h_price_change_pct": None,
        "outcome": None,
    }
    if score is not None:
        rec["score"] = score
    if extra:
        rec["extra"] = extra

    store.append(rec)
    _save()
    return True


async def check_outcomes() -> dict:
    """Fetch DexScreener for any overdue 1hr or 2hr price checks.

    Returns summary: {checked, pumped, rugged, flat, records}
    """
    import aiohttp

    store = _load()
    now = time.time()

    # Collect mints needing a check
    to_check_1h = [r for r in store if not r["check_1h_done"] and now >= r.get("check_1h_due_at", 0)]
    to_check_2h = [r for r in store if not r["check_2h_done"] and now >= r.get("check_2h_due_at", 0)]
    all_needing_check = {r["mint"]: r for r in (to_check_1h + to_check_2h)}

    if not all_needing_check:
        return {"checked": 0, "pumped": 0, "rugged": 0, "flat": 0, "records": []}

    prices: dict[str, float | None] = {}
    mints = list(all_needing_check.keys())
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
                    for p in (data.get("pairs") or []):
                        base = (p.get("baseToken") or {}).get("address", "")
                        if not base or base in prices:
                            continue
                        pc = p.get("priceChange") or {}
                        chg = pc.get("h1") or pc.get("h6") or pc.get("h24")
                        prices[base] = float(chg) if chg is not None else None
            except Exception:
                pass

    pumped = rugged = flat = unknown = 0
    updated: list[dict] = []

    for mint, rec in all_needing_check.items():
        chg = prices.get(mint)
        if mint in [r["mint"] for r in to_check_1h] and not rec["check_1h_done"]:
            rec["check_1h_done"] = True
            rec["check_1h_price_change_pct"] = chg
        if mint in [r["mint"] for r in to_check_2h] and not rec["check_2h_done"]:
            rec["check_2h_done"] = True
            rec["check_2h_price_change_pct"] = chg

        # Set final outcome when both checks done (or use 2h as definitive)
        if rec["check_2h_done"] and rec["outcome"] is None:
            final_chg = rec.get("check_2h_price_change_pct")
            if final_chg is None:
                rec["outcome"] = "unknown"
                unknown += 1
            elif final_chg >= 50:
                rec["outcome"] = "pumped"
                pumped += 1
            elif final_chg <= -50:
                rec["outcome"] = "rugged"
                rugged += 1
            else:
                rec["outcome"] = "flat"
                flat += 1
        updated.append(dict(rec))

    _save()
    return {
        "checked": len(all_needing_check),
        "pumped": pumped,
        "rugged": rugged,
        "flat": flat,
        "unknown": unknown,
        "records": sorted(updated, key=lambda r: -(r.get("gates_passed_count") or 0)),
    }


def get_recent(n: int = 30) -> list[dict]:
    store = _load()
    return list(reversed(store[-n:]))


def get_stats() -> dict:
    store = _load()
    total = len(store)
    with_outcome = [r for r in store if r.get("outcome")]
    pumped = sum(1 for r in with_outcome if r["outcome"] == "pumped")
    from collections import Counter
    failed_gate_counts = Counter(r.get("failed_gate", "?") for r in store)
    return {
        "total_near_misses": total,
        "with_outcome": len(with_outcome),
        "pumped_after_reject": pumped,
        "top_failed_gates": dict(failed_gate_counts.most_common(8)),
    }
