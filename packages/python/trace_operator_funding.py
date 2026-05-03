#!/usr/bin/env python3
"""Trace which operator wallet funded each golden dev wallet.

This walks backwards through each dev wallet's transaction history to find
the first significant SOL transfer IN from an operator wallet.
"""

import json
import os
import asyncio
import aiohttp
from typing import Optional

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Dev wallets to trace
DEV_WALLETS = {
    "4euyaqBe45J5dniAcNARERUo7B1wG96riG21sEpNndfU": "Milkers + MOG",
    "E2RtHD8NNict752mA6yDakZXn6utsVscRWJcumNBBNZs": "Trump Coin",
    "985wHVZXTkZKb9Lg5r8dQSexje3pzzKeSGs7UqPaDho9": "CAMINO",
    "55i6Vo1BNz3gd2QLBqVm41G9TTVoUymRs73wgu6F7Lag": "PAP",
}

# Known operators
OPERATORS = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "MEGA OPERATOR",
    "DwCp9GZw3ueoXPykHSPUkRZEwcTVbJH2i9Sf1cXYicWf": "mid-tier",
    "G5u7s6CNufkAvseFTpBbBk2qYg17hdRC1i2jasEgukBs": "DWOGE",
    "E9vf42zJXFv8Ljop1cG68NAxLDat4ZEGEWDLfJVX38GF": "proven",
    "8S4o7RitQkJ587waWwtyPF18Yyayehobt41QUjLjzi4Z": "FOFAR",
    "BY4StcU9Y2BpgH8quZzorg31EGE4L1rjomN8FNsCBEcx": "CCP",
}

async def get_signatures(session: aiohttp.ClientSession, wallet: str, limit: int = 100) -> list[dict]:
    """Fetch signatures for a wallet."""
    try:
        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": limit}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                res = await r.json()
                return res.get("result", [])
    except Exception as e:
        print(f"  Error fetching sigs for {wallet[:20]}...: {e}")
    return []

async def get_transaction(session: aiohttp.ClientSession, sig: str) -> Optional[dict]:
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
        print(f"  Error fetching tx {sig[:20]}...: {e}")
    return None

async def find_funding_source(session: aiohttp.ClientSession, dev_wallet: str, token_name: str) -> Optional[dict]:
    """Walk backwards through dev wallet history to find first SOL transfer IN from operator."""
    print(f"\n{'='*80}")
    print(f"Tracing {token_name} dev wallet: {dev_wallet}")
    print(f"{'='*80}")

    sigs = await get_signatures(session, dev_wallet, limit=100)
    print(f"Found {len(sigs)} signatures")

    if not sigs:
        print("  ✗ No signatures found")
        return None

    # Walk backwards through transactions
    for i, sig_entry in enumerate(sigs):
        sig = sig_entry.get("signature")
        if not sig:
            continue

        tx = await get_transaction(session, sig)
        if not tx:
            continue

        # Look for transfers from system program (SOL transfers)
        meta = tx.get("meta") or {}
        post_balances = meta.get("postBalances") or []
        pre_balances = meta.get("preBalances") or []

        # Check each account in the transaction
        message = tx.get("transaction", {}).get("message", {})
        accounts = message.get("accountKeys", [])

        # Look for net SOL increase to dev_wallet (funding)
        for j, account in enumerate(accounts):
            acc_addr = account.get("pubkey") if isinstance(account, dict) else account

            if acc_addr == dev_wallet:
                # Found the wallet in this tx
                if j < len(post_balances) and j < len(pre_balances):
                    pre_bal = pre_balances[j]
                    post_bal = post_balances[j]
                    delta = post_bal - pre_bal

                    # Positive delta = received SOL
                    if delta > 100_000_000:  # > 0.1 SOL
                        # Find the source
                        instructions = message.get("instructions", [])

                        # Look for transfer instructions from system program
                        for instr in instructions:
                            if instr.get("program") == "system" or instr.get("programId") == SYSTEM_PROGRAM:
                                parsed = instr.get("parsed", {})
                                if parsed.get("type") == "transfer":
                                    source = parsed.get("info", {}).get("source")
                                    amount = parsed.get("info", {}).get("lamports")

                                    if source and amount and amount > 100_000_000:
                                        is_operator = source in OPERATORS
                                        operator_name = OPERATORS.get(source, "UNKNOWN")

                                        print(f"  ✓ FOUND FUNDING:")
                                        print(f"    Source: {source}")
                                        print(f"    Amount: {amount / 1e9:.2f} SOL")
                                        print(f"    Is Operator: {is_operator}")
                                        if is_operator:
                                            print(f"    Operator Name: {operator_name}")
                                        print(f"    TX: {sig}")

                                        return {
                                            "dev_wallet": dev_wallet,
                                            "token_name": token_name,
                                            "source": source,
                                            "amount_lamports": amount,
                                            "is_operator": is_operator,
                                            "operator_name": operator_name if is_operator else None,
                                            "signature": sig,
                                        }

    print(f"  ✗ Could not find funding source in first {len(sigs)} txs")
    return None

async def main():
    """Trace all golden dev wallets."""
    async with aiohttp.ClientSession() as session:
        results = []

        for dev_wallet, token_name in DEV_WALLETS.items():
            result = await find_funding_source(session, dev_wallet, token_name)
            if result:
                results.append(result)
            await asyncio.sleep(0.5)  # Rate limit

        # Summary
        print(f"\n\n{'='*80}")
        print("SUMMARY — OPERATOR → DEV WALLET MAPPING")
        print(f"{'='*80}\n")

        for result in results:
            if result["is_operator"]:
                print(f"✓ {result['token_name']:20} → {result['operator_name']}")
                print(f"  Dev: {result['dev_wallet']}")
                print(f"  Source: {result['source']}")
                print()

        # Print as JSON for playbook update
        print(f"\n{'='*80}")
        print("PLAYBOOK STRUCTURE")
        print(f"{'='*80}\n")
        print(json.dumps({
            "funding_map": {r["dev_wallet"]: r["source"] for r in results if r["is_operator"]},
            "full_results": results,
        }, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
