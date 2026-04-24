"""add_monsters.py — enrich + append new monster mints to monster_addresses.json.

Reads mints from the NEW_MINTS list below. For each:
  - Fetch DexScreener for symbol, name, liq, mc, h24 change, dex
  - Fetch Helius getAsset for creator wallet
  - Skip if mint already present in monsters list (idempotent)
  - Append an entry with date_added = today + source="manual_add"

After this, re-run build_smart_money_list.py to incorporate new monsters
into the roster.

Usage on AWS:
  HELIUS_API_KEY=<key> python3 add_monsters.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

BASE = Path(__file__).parent
FILE = BASE / "monster_addresses.json"

NEW_MINTS = [
    "5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2",
    "9AvytnUKsLxPxFHFqS6VLxaxt5p6BhYNr53SD2Chpump",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "Hh3oTaqDCKKfdBgsQEvxp9sUwyNf8x9qmKqEMLBWpump",
    "zGh48JtNHVBb5evgoZLXwgPD2Qu4MhkWdJLGDAupump",
    "NV2RYH954cTJ3ckFUpvfqaQXU4ARqqDH3562nFSpump",
]


def _helius_url() -> str:
    key = os.environ.get("HELIUS_API_KEY", "")
    return (
        f"https://mainnet.helius-rpc.com/?api-key={key}"
        if key else "https://api.mainnet-beta.solana.com"
    )


async def fetch_dex(session: aiohttp.ClientSession, mint: str) -> dict | None:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    # Pick highest-liquidity pair
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    return {
        "symbol":  (best.get("baseToken") or {}).get("symbol") or mint[:8],
        "name":    (best.get("baseToken") or {}).get("name") or mint[:8],
        "dex":     best.get("dexId") or "unknown",
        "liq_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "mc_usd":  float(best.get("marketCap") or best.get("fdv") or 0),
        "h24":     float((best.get("priceChange") or {}).get("h24") or 0),
        "h6":      float((best.get("priceChange") or {}).get("h6") or 0),
        "h1":      float((best.get("priceChange") or {}).get("h1") or 0),
        "vol_24h": float((best.get("volume") or {}).get("h24") or 0),
        "age_hours": (
            round((time.time() - float(best.get("pairCreatedAt") or 0) / 1000.0) / 3600, 1)
            if best.get("pairCreatedAt") else None
        ),
        "pair_address": best.get("pairAddress"),
    }


async def fetch_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    try:
        async with session.post(
            _helius_url(),
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": [mint]},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None
    creators = ((data.get("result") or {}).get("creators")) or []
    if not creators:
        return None
    for c in creators:
        if c.get("verified"):
            return c.get("address")
    return creators[0].get("address")


async def main() -> None:
    data = json.loads(FILE.read_text())
    monsters = data.get("monsters") or []
    existing = {m.get("mint") for m in monsters if m.get("mint")}

    today = time.strftime("%Y-%m-%d")
    added: list[dict] = []
    skipped: list[str] = []

    async with aiohttp.ClientSession() as session:
        for mint in NEW_MINTS:
            if mint in existing:
                skipped.append(f"{mint[:12]}... already present")
                continue
            dex, creator = await asyncio.gather(
                fetch_dex(session, mint), fetch_creator(session, mint)
            )
            if not dex:
                skipped.append(f"{mint[:12]}... no DexScreener data")
                continue

            entry = {
                "symbol":             dex["symbol"],
                "name":               dex["name"],
                "mint":               mint,
                "dex":                dex["dex"],
                "date_spotted":       today,
                "date_added":         today,
                "source":             "manual_add",
                "h1_gain_pct_at_check":  dex["h1"],
                "h6_gain_pct":           dex["h6"],
                "h24_gain_pct":          dex["h24"],
                "liq_usd_at_check":      round(dex["liq_usd"]),
                "vol_24h_usd":           round(dex["vol_24h"]),
                "fdv_at_check":          round(dex["mc_usd"]),
                "age_hours_at_check":    dex["age_hours"],
                "creator_wallet":        creator,
                "pair_address":          dex["pair_address"],
                "solscan":               f"https://solscan.io/token/{mint}",
            }
            monsters.append(entry)
            added.append(entry)
            print(f"[+] {dex['symbol']} ({mint[:12]}) — creator={creator[:12] if creator else 'unknown'}, liq=${dex['liq_usd']:.0f}, mc=${dex['mc_usd']:.0f}, age={dex['age_hours']}h")

    if skipped:
        print("\nSkipped:")
        for s in skipped:
            print(f"  - {s}")

    # Update meta counters
    data["monsters"] = monsters
    data["_total_monsters"]      = len(monsters)
    data["_addresses_confirmed"] = sum(1 for m in monsters if m.get("mint"))
    data["_last_updated"]        = today

    FILE.write_text(json.dumps(data, indent=2))
    print(f"\n[done] added {len(added)} / skipped {len(skipped)} — total monsters now {len(monsters)}")


if __name__ == "__main__":
    asyncio.run(main())
