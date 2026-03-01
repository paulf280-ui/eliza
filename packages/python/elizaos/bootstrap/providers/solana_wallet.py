import os
import time

import aiohttp

from elizaos.types import Provider, ProviderResult


async def get_solana_balance(runtime, memory, state=None):
    SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
    SOLANA_PUBLIC_KEY = os.getenv("SOLANA_PUBLIC_KEY")

    start_time = time.time()

    if not SOLANA_RPC_URL or not SOLANA_PUBLIC_KEY:
        return ProviderResult(
            text="Solana wallet configuration is missing (SOLANA_RPC_URL or SOLANA_PUBLIC_KEY not set).",
            values={},
            data={},
        )

    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [SOLANA_PUBLIC_KEY]}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(SOLANA_RPC_URL, json=payload) as resp:
                resp.raise_for_status()
                json_resp = await resp.json()
        result = json_resp.get("result", {})
        lamports = result.get("value", 0)
        sol = lamports / 1_000_000_000
        elapsed = time.time() - start_time

        text = f"SOL Balance: {sol:.6f} SOL\nWallet Address: {SOLANA_PUBLIC_KEY}"
        runtime.logger.debug(
            f"[solana_wallet_provider] Balance fetched in {elapsed:.2f}s: {sol:.6f} SOL",
            src="provider:solana_wallet",
        )
        return ProviderResult(
            text=text,
            values={"sol": sol, "lamports": lamports, "address": SOLANA_PUBLIC_KEY},
            data=result,
        )
    except TimeoutError:
        elapsed = time.time() - start_time
        return ProviderResult(
            text=f"Timeout fetching Solana balance after {elapsed:.2f}s.",
            values={},
            data={},
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ProviderResult(
            text=f"Error fetching Solana balance: {e} (elapsed: {elapsed:.2f}s)",
            values={},
            data={},
        )


solana_wallet_provider = Provider(
    name="solana_wallet",
    description="Fetches the current SOL balance and address for the configured Solana wallet.",
    get=get_solana_balance,
    dynamic=True,
)
