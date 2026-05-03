"""Pull deep on-chain + DexScreener data for candidate monster tokens and emit a JSON report."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
HELIUS_DAS = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

TOKENS = [
    ("BiBMG42BrmvJE5gY5UCXLURroGR6tBC1gfcYbZuEpump", "unc_asteroid"),
    ("BnXWvsVZYgBxTUDyDqHZjvFbQGvEZeipY4ZdmqCbpump", "token_2"),
    ("5YX8Bsgm4AENU8SbsjMEeZssDcvnT5sx96mxgf69pump", "token_3"),
    ("Sgei8pavjJLWVrYUp9PwNxK6hmP5TphQd2HR4Yxpump", "token_4"),
    ("CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump", "token_5"),
    ("3TYgKwkE2Y3rxdw9osLRSpxpXmSC1C1oo19W9KHspump", "BULL"),
]


async def rpc(session: aiohttp.ClientSession, method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
        return await r.json()


async def get_asset(session: aiohttp.ClientSession, mint: str) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}}
    async with session.post(HELIUS_DAS, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
        return (await r.json()).get("result", {}) or {}


async def get_creation_info(session: aiohttp.ClientSession, mint: str) -> dict:
    """Paginate signatures back to the earliest; fetch creation tx for deployer + block time."""
    all_sigs = []
    before = None
    for _ in range(40):  # up to ~40,000 sigs
        params = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        res = await rpc(session, "getSignaturesForAddress", params)
        sigs = res.get("result", []) or []
        if not sigs:
            break
        all_sigs.extend(sigs)
        if len(sigs) < 1000:
            break
        before = sigs[-1]["signature"]
    if not all_sigs:
        return {"error": "no_signatures"}
    earliest = all_sigs[-1]
    total_sigs = len(all_sigs)
    tx_res = await rpc(
        session,
        "getTransaction",
        [earliest["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    tx = tx_res.get("result") or {}
    deployer = None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        deployer = keys[0]["pubkey"] if keys else None
    except Exception:
        pass
    return {
        "creation_sig": earliest["signature"],
        "creation_block_time": earliest.get("blockTime"),
        "creation_slot": earliest.get("slot"),
        "deployer": deployer,
        "total_sigs_in_sample": total_sigs,
    }


async def get_supply(session: aiohttp.ClientSession, mint: str) -> dict:
    res = await rpc(session, "getTokenSupply", [mint])
    return (res.get("result") or {}).get("value", {}) or {}


async def get_largest_holders(session: aiohttp.ClientSession, mint: str) -> list:
    res = await rpc(session, "getTokenLargestAccounts", [mint])
    return (res.get("result") or {}).get("value", []) or []


async def dexscreener(session: aiohttp.ClientSession, mint: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return {}
            return await r.json()
    except Exception:
        return {}


async def analyze(mint: str, label: str) -> dict:
    out: dict = {"label": label, "mint": mint}
    async with aiohttp.ClientSession() as session:
        # Run requests concurrently
        asset_task = asyncio.create_task(get_asset(session, mint))
        creation_task = asyncio.create_task(get_creation_info(session, mint))
        supply_task = asyncio.create_task(get_supply(session, mint))
        holders_task = asyncio.create_task(get_largest_holders(session, mint))
        dex_task = asyncio.create_task(dexscreener(session, mint))

        asset = await asset_task
        creation = await creation_task
        supply = await supply_task
        largest = await holders_task
        dex = await dex_task

        # Metadata + socials from DAS asset
        content = (asset or {}).get("content") or {}
        metadata = content.get("metadata") or {}
        links = content.get("links") or {}
        json_uri = content.get("json_uri")
        out["metadata"] = {
            "name": metadata.get("name"),
            "symbol": metadata.get("symbol"),
            "description": metadata.get("description"),
            "token_standard": metadata.get("token_standard"),
        }
        out["socials"] = {
            "external_url": links.get("external_url"),
            "image": links.get("image"),
            "json_uri": json_uri,
        }
        # Helius sometimes bundles in grouping / additional socials
        for k in ("twitter", "telegram", "website"):
            if k in metadata:
                out["socials"][k] = metadata[k]

        # Supply
        out["supply"] = {
            "amount": supply.get("amount"),
            "decimals": supply.get("decimals"),
            "ui_amount": supply.get("uiAmount"),
        }

        # Creation
        out["creation"] = creation

        # Top holders
        total_ui = float(supply.get("uiAmount") or 0) or 1.0
        top_holders = []
        for a in largest[:10]:
            ui_amt = float((a.get("uiAmount") or 0))
            top_holders.append(
                {
                    "address": a.get("address"),
                    "ui_amount": ui_amt,
                    "pct_of_supply": round((ui_amt / total_ui) * 100, 3) if total_ui else None,
                }
            )
        out["top10_holders"] = top_holders
        top1_pct = top_holders[0]["pct_of_supply"] if top_holders else None
        top5_pct = sum((h["pct_of_supply"] or 0) for h in top_holders[:5]) if top_holders else None
        out["holder_concentration"] = {
            "top1_pct": top1_pct,
            "top5_pct": round(top5_pct, 3) if top5_pct is not None else None,
            "sample_size": len(top_holders),
        }

        # DexScreener pairs
        pairs = (dex or {}).get("pairs") or []
        pairs_sorted = sorted(
            pairs,
            key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0)),
            reverse=True,
        )
        dex_summary = []
        for p in pairs_sorted[:6]:
            dex_summary.append(
                {
                    "dex": p.get("dexId"),
                    "pair_address": p.get("pairAddress"),
                    "created_at_ms": p.get("pairCreatedAt"),
                    "price_usd": p.get("priceUsd"),
                    "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                    "fdv": p.get("fdv"),
                    "mc": p.get("marketCap"),
                    "vol_h24": (p.get("volume") or {}).get("h24"),
                    "vol_h6": (p.get("volume") or {}).get("h6"),
                    "vol_h1": (p.get("volume") or {}).get("h1"),
                    "txns_h24": (p.get("txns") or {}).get("h24"),
                    "txns_h1": (p.get("txns") or {}).get("h1"),
                    "price_change": p.get("priceChange"),
                    "socials": p.get("info", {}).get("socials") if p.get("info") else None,
                    "websites": p.get("info", {}).get("websites") if p.get("info") else None,
                }
            )
        out["dex_pairs_top"] = dex_summary

        # Quick derived summary
        primary = dex_summary[0] if dex_summary else {}
        out["snapshot"] = {
            "primary_dex": primary.get("dex"),
            "primary_liq_usd": primary.get("liquidity_usd"),
            "primary_mc": primary.get("mc"),
            "primary_price_usd": primary.get("price_usd"),
            "h24_change_pct": (primary.get("price_change") or {}).get("h24"),
            "h6_change_pct": (primary.get("price_change") or {}).get("h6"),
            "h1_change_pct": (primary.get("price_change") or {}).get("h1"),
            "m5_change_pct": (primary.get("price_change") or {}).get("m5"),
            "pair_created_at_ms": primary.get("created_at_ms"),
        }

    return out


async def main() -> None:
    results = []
    for mint, label in TOKENS:
        print(f"[*] analyzing {label} = {mint}", file=sys.stderr)
        try:
            r = await analyze(mint, label)
        except Exception as e:
            r = {"mint": mint, "label": label, "error": repr(e)}
        results.append(r)
        await asyncio.sleep(0.6)  # polite pacing

    out_path = Path(__file__).parent / "monster_candidates_deep_dive.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"[+] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
