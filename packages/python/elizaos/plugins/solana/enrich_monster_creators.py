"""Deep enrichment for every mint in monster_addresses.json.

For each monster we collect:
  - creator wallet (fee-payer of the very first signature for the mint)
  - pump.fun metadata URI + the JSON body (contains TG/Discord/Twitter/Reddit links the
    creator pinned at mint time — pre-DexScreener)
  - earliest buyers (first 15 distinct signers after creator) — coordination cluster probe
  - siblings deployed by the same creator via Helius DAS searchAssets(authority=creator)
  - graduation/Meteora pool timestamps already present in the file are passed through

Output: monster_creator_intel.json — keyed by mint, plus an aggregate creator index.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# pump.fun global state / known system accounts we want to IGNORE when computing
# "early buyers" (the creator, pump.fun program, associated token authority, etc.)
IGNORE_ACCOUNTS = {
    PUMP_FUN_PROGRAM,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token Program
    "11111111111111111111111111111111",             # System Program
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token
}


async def rpc(session: aiohttp.ClientSession, method: str, params, timeout: int = 30) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(RPC, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json()


async def get_earliest_signatures(session, mint, limit=60):
    """Walk backwards from the latest sig to find the EARLIEST N signatures for this mint."""
    all_sigs = []
    before = None
    for _ in range(40):
        params = [mint, {"limit": 1000, **({"before": before} if before else {})}]
        res = await rpc(session, "getSignaturesForAddress", params)
        sigs = (res.get("result") or [])
        if not sigs:
            break
        all_sigs.extend(sigs)
        if len(sigs) < 1000:
            break
        before = sigs[-1]["signature"]
    if not all_sigs:
        return []
    # Signatures come newest-first, so the LAST N are the oldest
    return all_sigs[-limit:]


async def get_tx(session, sig):
    res = await rpc(
        session,
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    return res.get("result") or {}


async def get_asset(session, mint):
    res = await rpc(session, "getAsset", {"id": mint})
    return res.get("result") or {}


async def search_by_creator(session, creator):
    """Helius DAS searchAssets by creatorAddress — returns all assets this wallet has created."""
    res = await rpc(
        session,
        "searchAssets",
        {
            "creatorAddress": creator,
            "onlyVerified": False,
            "tokenType": "fungible",
            "page": 1,
            "limit": 1000,
        },
        timeout=60,
    )
    r = res.get("result") or {}
    return r.get("items") or []


async def fetch_metadata_json(session, url):
    if not url:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return {"error": f"http_{r.status}"}
            ctype = r.headers.get("content-type", "")
            if "json" in ctype or url.endswith(".json") or "ipfs" in url:
                return await r.json(content_type=None)
            return {"raw": (await r.text())[:600]}
    except Exception as e:
        return {"error": repr(e)}


async def enrich_mint(session, mint: str, known_creator: str | None):
    out: dict = {"mint": mint}

    # 1) DAS metadata — JSON URI, symbol, links
    asset = await get_asset(session, mint)
    content = (asset or {}).get("content") or {}
    out["json_uri"] = content.get("json_uri")
    out["symbol"] = (content.get("metadata") or {}).get("symbol")
    out["name"] = (content.get("metadata") or {}).get("name")
    out["description"] = (content.get("metadata") or {}).get("description")
    out["creators_field"] = asset.get("creators")  # sometimes explicit
    out["authorities"] = asset.get("authorities")

    # 2) Fetch the pump.fun JSON body — this is where original Telegram/Discord/Reddit live
    uri = out["json_uri"]
    out["json_body"] = await fetch_metadata_json(session, uri) if uri else None

    # 3) Earliest signatures — creator + earliest buyers
    sigs = await get_earliest_signatures(session, mint, limit=25)
    earliest_txs = []
    for s in sigs:
        tx = await get_tx(session, s["signature"])
        if not tx:
            continue
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            payer = keys[0]["pubkey"] if keys else None
        except Exception:
            payer = None
        earliest_txs.append({
            "sig": s["signature"],
            "block_time": s.get("blockTime"),
            "slot": s.get("slot"),
            "payer": payer,
        })
    out["earliest_txs"] = earliest_txs

    # Creator = fee-payer of the first tx if not supplied
    creator = known_creator
    if not creator and earliest_txs:
        creator = earliest_txs[0].get("payer")
    out["creator_wallet"] = creator

    # 4) Early-buyer cluster — distinct payers from earliest_txs after the creator
    distinct_payers = []
    seen = set()
    for tx in earliest_txs:
        p = tx.get("payer")
        if not p or p in IGNORE_ACCOUNTS or p == creator:
            continue
        if p in seen:
            continue
        seen.add(p)
        distinct_payers.append(p)
        if len(distinct_payers) >= 15:
            break
    out["early_buyers"] = distinct_payers

    return out


async def creator_sibling_scan(session, creators: list[str]):
    """For each creator, list all tokens they've deployed via Helius DAS."""
    results = {}
    for c in creators:
        if not c:
            continue
        print(f"[*] sibling scan: {c}", file=sys.stderr)
        items = await search_by_creator(session, c)
        siblings = []
        for it in items:
            if it.get("interface") != "FungibleToken" and it.get("interface") != "FungibleAsset":
                continue
            cnt = (it.get("content") or {})
            md = cnt.get("metadata") or {}
            siblings.append({
                "mint": it.get("id"),
                "symbol": md.get("symbol"),
                "name": md.get("name"),
            })
        results[c] = {"count": len(siblings), "siblings": siblings[:50]}
        await asyncio.sleep(0.3)
    return results


async def main():
    base = Path("/home/paulf/eliza/packages/python/elizaos/plugins/solana")
    data = json.loads((base / "monster_addresses.json").read_text())
    monsters = data["monsters"]

    mint_intel: dict[str, dict] = {}
    creators_found: list[str] = []

    async with aiohttp.ClientSession() as session:
        for i, m in enumerate(monsters):
            mint = m.get("mint")
            if not mint:
                continue
            known = m.get("creator_wallet")
            print(f"[*] {i+1}/{len(monsters)} enriching {m.get('symbol')} ({mint})", file=sys.stderr)
            try:
                e = await enrich_mint(session, mint, known)
            except Exception as ex:
                e = {"mint": mint, "error": repr(ex)}
            mint_intel[mint] = e
            if e.get("creator_wallet") and e["creator_wallet"] not in creators_found:
                creators_found.append(e["creator_wallet"])
            await asyncio.sleep(0.3)

        sibling_index = await creator_sibling_scan(session, creators_found)

    # Early-buyer cross-reference: which wallets appear in multiple monsters?
    wallet_hits = Counter()
    wallet_to_monsters: dict[str, list[str]] = defaultdict(list)
    for mint, e in mint_intel.items():
        sym = e.get("symbol") or e.get("name") or mint[:8]
        for w in e.get("early_buyers") or []:
            wallet_hits[w] += 1
            wallet_to_monsters[w].append(sym)
    coord_cluster = {
        w: {"monster_count": c, "monsters": wallet_to_monsters[w]}
        for w, c in wallet_hits.most_common()
        if c >= 2
    }

    out = {
        "generated_utc": None,
        "monster_count": len(monsters),
        "creators_unique": len(creators_found),
        "mint_intel": mint_intel,
        "creator_siblings": sibling_index,
        "coordination_cluster_multi_monster_buyers": coord_cluster,
    }
    (base / "monster_creator_intel.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"[+] wrote {base / 'monster_creator_intel.json'}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
