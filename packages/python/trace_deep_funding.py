#!/usr/bin/env python3
"""Deep trace for 4euyaqBe45J5 — walk back through ALL transactions to find first funding."""

import json
import os
import asyncio
import aiohttp

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

async def get_signatures(session: aiohttp.ClientSession, wallet: str, before: str = None) -> list[dict]:
    """Fetch signatures for a wallet, optionally before a specific sig."""
    try:
        params = [wallet, {"limit": 100}]
        if before:
            params[1]["before"] = before

        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": params,
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                res = await r.json()
                return res.get("result", [])
    except Exception as e:
        print(f"  Error: {e}")
    return []

async def get_transaction(session: aiohttp.ClientSession, sig: str) -> dict | None:
    """Fetch full transaction details."""
    try:
        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                res = await r.json()
                return res.get("result")
    except Exception as e:
        print(f"  Error: {e}")
    return None

async def deep_trace(session: aiohttp.ClientSession, dev_wallet: str):
    """Walk back through ALL transactions to find first SOL transfer IN."""
    print(f"\n{'='*80}")
    print(f"DEEP TRACE: {dev_wallet}")
    print(f"{'='*80}\n")

    all_sigs = []
    before = None
    batch_count = 0

    # Walk back through all transactions
    while True:
        batch_count += 1
        sigs = await get_signatures(session, dev_wallet, before=before)

        if not sigs:
            print(f"  Reached end after {batch_count} batches ({len(all_sigs)} total sigs)")
            break

        all_sigs.extend(sigs)
        print(f"  Batch {batch_count}: {len(sigs)} sigs (total: {len(all_sigs)})")

        # Look for funding in this batch
        for i, sig_entry in enumerate(sigs):
            sig = sig_entry.get("signature")
            if not sig:
                continue

            tx = await get_transaction(session, sig)
            if not tx:
                continue

            meta = tx.get("meta") or {}
            post_balances = meta.get("postBalances") or []
            pre_balances = meta.get("preBalances") or []

            message = tx.get("transaction", {}).get("message", {})
            accounts = message.get("accountKeys", [])

            # Look for net SOL increase
            for j, account in enumerate(accounts):
                acc_addr = account.get("pubkey") if isinstance(account, dict) else account

                if acc_addr == dev_wallet and j < len(post_balances) and j < len(pre_balances):
                    delta = post_balances[j] - pre_balances[j]

                    if delta > 100_000_000:  # > 0.1 SOL
                        instructions = message.get("instructions", [])

                        for instr in instructions:
                            parsed = instr.get("parsed", {})
                            if parsed.get("type") == "transfer":
                                source = parsed.get("info", {}).get("source")
                                amount = parsed.get("info", {}).get("lamports")

                                if source and amount and amount > 100_000_000:
                                    print(f"\n✓ FOUND FUNDING TRANSFER:")
                                    print(f"  Source: {source}")
                                    print(f"  Amount: {amount / 1e9:.2f} SOL")
                                    print(f"  Timestamp: {tx.get('blockTime')}")
                                    print(f"  TX: {sig}")
                                    print(f"  Position in history: batch {batch_count}, position {i+1}")
                                    return source

        # Stop after checking 10 batches (1000 transactions) unless we're in first batch
        if batch_count >= 10 and len(all_sigs) > 500:
            print(f"\n  Checked {batch_count} batches ({len(all_sigs)} sigs) — this is a heavy trader")
            print(f"  First SOL transfer might be the account creation itself (no external funding)")
            break

        before = sigs[-1]["signature"]
        await asyncio.sleep(0.1)

    print(f"\n  ✗ Could not find significant funding transfer in {len(all_sigs)} transactions")
    return None

async def main():
    async with aiohttp.ClientSession() as session:
        result = await deep_trace(session, "4euyaqBe45J5dniAcNARERUo7B1wG96riG21sEpNndfU")

        print(f"\n{'='*80}")
        if result:
            print(f"RESULT: {result}")
        else:
            print(f"Could not determine funding source")
            print(f"This wallet may be self-funded or the original funding is too old to retrieve")
        print(f"{'='*80}")

if __name__ == "__main__":
    asyncio.run(main())
