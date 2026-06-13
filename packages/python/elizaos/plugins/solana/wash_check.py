"""wash_check.py — fake-volume / wash-trading detection.

Cabals that can't hide their holder map instead fake momentum: a handful of
wallets pass tokens back and forth to inflate volume and force the token onto
"trending" lists. This reconstructs the pool's swaps (same Helius Enhanced
history the cohort analysis uses) and flags two tells:

  • Round-trip wallets — wallets that both buy AND sell repeatedly while their
    net position barely changes (classic wash: volume without accumulation).
  • Maker concentration — a large share of volume from very few wallets.

Returns a 0-100 wash score, the % of volume that's circular, and the worst
offenders with their trade counts. Lazy / on-demand.
"""

from __future__ import annotations

import time

import aiohttp

from elizaos.plugins.solana.cohort_pnl import (
    _fetch_pool_swaps, _get_pool_address, _wallet_legs,
)

_ROUNDTRIP_MIN_TRADES = 4      # ≥4 swaps to count as a repeat trader
_NET_FLAT_RATIO       = 0.30   # |bought-sold| / max < this = position barely moved


async def get_wash_analysis(
    session: aiohttp.ClientSession, mint: str, swaps: list[dict] | None = None
) -> dict:
    empty = {"wash_score": 0, "wash_volume_pct": 0.0, "top5_maker_pct": 0.0,
             "unique_makers": 0, "swaps_analyzed": 0, "offenders": [],
             "computed_at": time.time()}
    if swaps is None:
        pool = await _get_pool_address(session, mint)
        if not pool:
            return empty
        swaps = await _fetch_pool_swaps(session, pool)
    if not swaps:
        return empty

    w: dict[str, dict] = {}
    total_vol = 0.0
    for tx in swaps:
        trader, tok, sol = _wallet_legs(tx, mint)
        if not trader or tok == 0:
            continue
        vol = abs(sol)
        a = w.setdefault(trader, {"vol": 0.0, "bought": 0.0, "sold": 0.0, "trades": 0})
        a["vol"]    += vol
        a["trades"] += 1
        if tok > 0:
            a["bought"] += tok
        else:
            a["sold"] += -tok
        total_vol += vol

    if total_vol <= 0 or not w:
        return {**empty, "swaps_analyzed": len(swaps)}

    # Round-trip (wash) wallets
    wash_vol = 0.0
    offenders: list[dict] = []
    for addr, a in w.items():
        both = a["bought"] > 0 and a["sold"] > 0
        net_ratio = abs(a["bought"] - a["sold"]) / max(a["bought"], a["sold"], 1)
        if both and a["trades"] >= _ROUNDTRIP_MIN_TRADES and net_ratio < _NET_FLAT_RATIO:
            wash_vol += a["vol"]
            offenders.append({
                "wallet":     addr[:6] + "…" + addr[-4:],
                "wallet_full": addr,
                "trades":     a["trades"],
                "vol_pct":    round(a["vol"] / total_vol * 100, 1),
            })
    offenders.sort(key=lambda o: -o["vol_pct"])

    vols = sorted((a["vol"] for a in w.values()), reverse=True)
    top5_pct = round(sum(vols[:5]) / total_vol * 100, 1)
    wash_pct = round(wash_vol / total_vol * 100, 1)
    # Score: circular volume dominates, maker concentration adds a little
    wash_score = round(min(100.0, wash_pct * 0.8 + max(0.0, top5_pct - 40) * 0.5))

    return {
        "wash_score":      wash_score,
        "wash_volume_pct": wash_pct,
        "top5_maker_pct":  top5_pct,
        "unique_makers":   len(w),
        "swaps_analyzed":  len(swaps),
        "offenders":       offenders[:5],
        "computed_at":     time.time(),
    }
