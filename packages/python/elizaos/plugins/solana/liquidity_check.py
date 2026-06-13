"""liquidity_check.py — price-impact / execution-physics simulator.

A token can have clean holders and a flawless dev and still wreck you on exit if
the pool is thin. This estimates how far a market SELL moves the price, so a
trader (or a bot's take-profit logic) knows whether the pool will actually
absorb the size they plan to dump.

Constant-product (x*y=k) model on the live pool. One side of the pool ≈ half
the reported USD liquidity; selling `X` USD of token pushes price down by
roughly X / (side + X). Approximate by design — labelled as an estimate.
"""

from __future__ import annotations

import time

import aiohttp

_SELL_SIZES_SOL = [1, 5, 10, 25]


async def get_liquidity_sim(session: aiohttp.ClientSession, mint: str) -> dict:
    empty = {"liquidity_usd": 0.0, "price_usd": 0.0, "sol_price": 0.0,
             "lp_burned": False, "sells": [], "computed_at": time.time()}
    try:
        async with session.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            pairs = await r.json() if r.status == 200 else []
    except Exception:
        return empty
    if not isinstance(pairs, list) or not pairs:
        return empty

    # Highest-liquidity pair
    p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    liq_usd      = float((p.get("liquidity") or {}).get("usd") or 0)
    price_usd    = float(p.get("priceUsd") or 0)
    price_native = float(p.get("priceNative") or 0)
    dex_id       = str(p.get("dexId") or "").lower()
    lp_burned    = "pump" in dex_id    # pump-amm LP is burned at graduation
    sol_price    = (price_usd / price_native) if price_native > 0 else 0.0

    sells = []
    if liq_usd > 0 and sol_price > 0:
        side = liq_usd / 2.0
        for n in _SELL_SIZES_SOL:
            usd = n * sol_price
            impact = round(usd / (side + usd) * 100, 1)
            sells.append({"sol": n, "usd": round(usd), "impact_pct": impact})

    return {
        "liquidity_usd":  round(liq_usd),
        "price_usd":      price_usd,
        "sol_price":      round(sol_price, 2),
        "lp_burned":      lp_burned,
        "dex":            p.get("dexId"),
        "sells":          sells,
        "computed_at":    time.time(),
    }
