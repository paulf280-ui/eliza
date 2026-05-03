"""Raydium/Jupiter service — price feed, quotes, and swaps via Jupiter aggregator."""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from elizaos.types import Service, ServiceTypeRegistry

from elizaos.plugins.solana.constants import (
    COINGECKO_SOL_PRICE_API,
    DEXSCREENER_TOKEN_API,
    JUPITER_PRICE_API,
    JUPITER_QUOTE_API,
    JUPITER_SWAP_API,
    USDC_MINT,
    WSOL_MINT,
)

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime


class RaydiumService(Service):
    """Jupiter-backed price/swap service (aggregates Raydium, Orca, pump.fun, etc.)."""

    service_type = ServiceTypeRegistry.LP_POOL

    @property
    def capability_description(self) -> str:
        return "Jupiter aggregator for token prices, quotes, and swaps on Solana."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._session: aiohttp.ClientSession | None = None
        self._sol_price_cache: tuple[float, float] | None = None  # (price_usd, timestamp)
        self._dex_price_cache: dict[str, tuple[float, float]] = {}  # mint → (price_sol, ts)
        # PumpSwap Helius real-time price: pool_addr → (base_vault_addr, quote_vault_addr)
        # Vault addresses are constant per pool so we cache them indefinitely.
        self._pumpswap_vault_cache: dict[str, tuple[str, str]] = {}

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> RaydiumService:
        service = cls()
        service._runtime = runtime
        runtime.logger.info(
            "RaydiumService started",
            src="service:raydium",
            agentId=str(runtime.agent_id),
        )
        return service

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    def _wallet_service(self) -> Any:
        from elizaos.plugins.solana.services.wallet import SolanaWalletService

        svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)  # type: ignore[union-attr]
        if not isinstance(svc, SolanaWalletService):
            raise RuntimeError("SolanaWalletService not available")
        return svc

    # --------------------------------------------------------------- private helpers

    async def _get_sol_price_usd(self) -> float:
        """Return SOL price in USD, cached for 60 s."""
        now = time.time()
        if self._sol_price_cache and now - self._sol_price_cache[1] < 60:
            return self._sol_price_cache[0]
        try:
            session = await self._get_session()
            async with session.get(
                COINGECKO_SOL_PRICE_API,
                params={"ids": "solana", "vs_currencies": "usd"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            price = float(data.get("solana", {}).get("usd", 0))
            if price > 0:
                self._sol_price_cache = (price, now)
            return price
        except Exception:
            return self._sol_price_cache[0] if self._sol_price_cache else 0.0

    async def _get_price_dexscreener(self, mint: str, cache_ttl: float = 30.0) -> float:
        """Return mint price in SOL via DexScreener, cached per mint.

        IMPORTANT: only uses pairs where quoteToken is native SOL (WSOL).
        Non-SOL quote pairs (e.g. token/LPPP, token/USDC, token/FINDER) have
        priceNative in non-SOL units and must be excluded — they cause phantom
        TP fires when the inflated priceNative exceeds the TP threshold.
        """
        now = time.time()
        cached = self._dex_price_cache.get(mint)
        if cached and now - cached[1] < cache_ttl:
            return cached[0]
        try:
            session = await self._get_session()
            async with session.get(f"{DEXSCREENER_TOKEN_API}/{mint}") as resp:
                resp.raise_for_status()
                data = await resp.json()
            pairs = data.get("pairs") or []

            # CRITICAL: filter to pairs where the quote token is SOL (native or wrapped).
            # priceNative = "price of base in quote". If quote != SOL, priceNative is NOT
            # the SOL price of the token. Meteora FINDER/LPPP, Pete/LPPP etc. are examples
            # where priceNative ≈ 1.5 (LPPP price) and would wrongly trigger TPs.
            sol_quote_pairs = [
                p for p in pairs
                if p.get("chainId") == "solana"
                and (
                    p.get("quoteToken", {}).get("address") == WSOL_MINT
                    or p.get("quoteToken", {}).get("symbol", "").upper() in {"SOL", "WSOL"}
                )
            ]
            if not sol_quote_pairs:
                return 0.0
            # Select pair by HIGHEST liquidity (USD) — not h24 volume.
            # After graduation, the pumpfun bonding curve pair is drained to ~$0 liquidity
            # while the active PumpSwap/Raydium pair has real liquidity. Volume-based
            # selection wrongly picks the bonding curve (which traded heavily pre-graduation)
            # and returns a stale/wrong price, causing missed TPs.
            best = max(sol_quote_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            price = float(best.get("priceNative") or 0)
            if price > 0:
                self._dex_price_cache[mint] = (price, now)
            return price
        except Exception:
            return 0.0

    async def get_pumpswap_price_helius(self, pool_addr: str) -> float:
        """Return real-time PumpSwap token price via Helius RPC (no DexScreener lag).

        Decodes the PumpSwap AMM pool account to find vault addresses, then
        fetches both vault balances in a single getMultipleAccounts call.
        Price = SOL_reserve / token_reserve  (constant-product AMM).

        Pool account layout (pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA):
          bytes 0-7:   discriminator
          byte  8:     pool_bump
          bytes 9-10:  index (u16)
          bytes 11-42: creator pubkey
          bytes 43-74: base_mint (token)
          bytes 75-106: quote_mint (WSOL)
          bytes 107-138: lp_mint
          bytes 139-170: pool_base_token_account  ← token vault
          bytes 171-202: pool_quote_token_account ← SOL vault

        Returns 0.0 on any error so callers can fall back to DexScreener.
        """
        import os as _os
        helius_url = _os.getenv("SOLANA_RPC_URL", "")
        if not helius_url or not pool_addr:
            return 0.0
        try:
            from solders.pubkey import Pubkey as _Pubkey
            session = await self._get_session()

            # ── Step 1: resolve vault addresses (cached) ──────────────────────
            if pool_addr not in self._pumpswap_vault_cache:
                import json as _json
                payload = _json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAccountInfo",
                    "params": [pool_addr, {"encoding": "base64"}],
                })
                async with session.post(
                    helius_url, data=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                raw_b64 = (
                    ((data.get("result") or {}).get("value") or {})
                    .get("data", [None])[0]
                )
                if not raw_b64:
                    return 0.0
                raw = base64.b64decode(raw_b64)
                if len(raw) < 203:
                    return 0.0
                base_vault = str(_Pubkey.from_bytes(raw[139:171]))
                quote_vault = str(_Pubkey.from_bytes(raw[171:203]))
                self._pumpswap_vault_cache[pool_addr] = (base_vault, quote_vault)

            base_vault, quote_vault = self._pumpswap_vault_cache[pool_addr]

            # ── Step 2: fetch both vault balances in one RPC call ─────────────
            import json as _json
            payload = _json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "getMultipleAccounts",
                "params": [[base_vault, quote_vault], {"encoding": "jsonParsed"}],
            })
            async with session.post(
                helius_url, data=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
            accounts = ((data.get("result") or {}).get("value")) or []
            if len(accounts) < 2 or any(a is None for a in accounts[:2]):
                return 0.0

            base_ui = float(
                (accounts[0]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount")) or 0
            )
            quote_ui = float(
                (accounts[1]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount")) or 0
            )
            if base_ui <= 0:
                return 0.0
            return quote_ui / base_ui

        except Exception:
            return 0.0

    # --------------------------------------------------------------- public

    async def get_price(self, mint: str, vs_token: str = "USDC", cache_ttl: float = 30.0) -> float:
        """Return price of mint denominated in vs_token (SOL or USDC).

        Tries DexScreener first (works from WSL2); falls back to Jupiter.
        Pass cache_ttl=3.0 for active position monitoring to get fresh prices.
        """
        # DexScreener gives price in SOL (priceNative)
        price_sol = await self._get_price_dexscreener(mint, cache_ttl=cache_ttl)
        if price_sol > 0:
            if vs_token.upper() == "SOL":
                return price_sol
            sol_usd = await self._get_sol_price_usd()
            return price_sol * sol_usd if sol_usd > 0 else 0.0

        # Fallback: Jupiter (will fail from WSL2 but works in prod)
        vs_mint = USDC_MINT if vs_token.upper() == "USDC" else WSOL_MINT
        session = await self._get_session()
        params = {"ids": mint, "vsToken": vs_mint}
        try:
            async with session.get(JUPITER_PRICE_API, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
            price_info = data.get("data", {}).get(mint, {})
            return float(price_info.get("price", 0))
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.warning(
                    f"RaydiumService.get_price failed for {mint}: {exc}",
                    src="service:raydium",
                )
            return 0.0

    async def get_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in_lamports: int,
        slippage: float = 0.01,
    ) -> dict[str, Any]:
        """Get a swap quote from Jupiter."""
        session = await self._get_session()
        slippage_bps = int(slippage * 10_000)
        params = {
            "inputMint": token_in,
            "outputMint": token_out,
            "amount": str(amount_in_lamports),
            "slippageBps": str(slippage_bps),
        }
        async with session.get(JUPITER_QUOTE_API, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    _PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"

    async def swap(
        self,
        token_in: str,
        token_out: str,
        amount_in: float,
        slippage: float = 0.01,
        pool: str = "pump-amm",
        token_decimals: int = 6,
        priority_fee: float = 0.0002,
    ) -> str:
        """Execute a swap via pumpportal. Returns transaction signature.

        pool: "pump-amm" (PumpSwap/graduated), "raydium" (Raydium AMM), "pump" (bonding curve)
        token_decimals: decimals of the non-SOL token (used to convert sell amount to raw units)
        priority_fee: Jito tip in SOL. Default 0.0002 (2% of 0.01 position) for buys.
          Callers should pass 0.001 for time-critical SL exits.
        """
        from solders.transaction import VersionedTransaction

        wallet_svc = self._wallet_service()
        keypair = wallet_svc.get_keypair()
        if keypair is None:
            raise ValueError("SOLANA_PRIVATE_KEY not configured")

        is_buy = token_in == WSOL_MINT
        slippage_pct = max(1, int(slippage * 100))

        # Use the requested priority fee. Callers are responsible for passing an
        # appropriate fee: 0.001 SOL for time-critical SL/full exits, 0.0003 SOL for
        # TP partial sells where speed is less critical but fee cost matters.
        effective_fee = priority_fee

        if is_buy:
            payload = {
                "publicKey": wallet_svc.get_public_key(),
                "action": "buy",
                "mint": token_out,
                "amount": amount_in,
                "denominatedInSol": "true",
                "slippage": slippage_pct,
                "priorityFee": effective_fee,
                "pool": pool,
            }
        else:
            # Sell: amount_in is in token float units; pumpportal wants raw integer units
            raw_amount = int(amount_in * (10**token_decimals))
            payload = {
                "publicKey": wallet_svc.get_public_key(),
                "action": "sell",
                "mint": token_in,
                "amount": raw_amount,
                "denominatedInSol": "false",
                "slippage": slippage_pct,
                "priorityFee": effective_fee,
                "pool": pool,
            }

        session = await self._get_session()
        async with session.post(self._PUMPPORTAL_URL, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"pumpportal swap failed HTTP {resp.status}: {text[:200]}"
                )
            tx_bytes = await resp.read()

        vtx = VersionedTransaction.from_bytes(tx_bytes)
        signed_vtx = VersionedTransaction(vtx.message, [keypair])
        return await wallet_svc.rpc.send_transaction(bytes(signed_vtx))

    async def swap_jupiter_buy(
        self,
        token_mint: str,
        sol_amount: float,
        slippage_bps: int = 1500,
        priority_fee: float = 0.002,
    ) -> str:
        """Buy a token with SOL via Jupiter public API.

        Jupiter quotes at execution time — eliminates stale-price overpay vs PumpPortal.
        slippage_bps: 1500 = 15% tolerance.
        Returns transaction signature on success, raises on failure.
        """
        import base64 as _b64

        from solders.transaction import VersionedTransaction

        from elizaos.plugins.solana.constants import (
            JUPITER_PUBLIC_QUOTE_API,
            JUPITER_PUBLIC_SWAP_API,
            WSOL_MINT,
        )

        wallet_svc = self._wallet_service()
        keypair = wallet_svc.get_keypair()
        if keypair is None:
            raise ValueError("SOLANA_PRIVATE_KEY not configured")

        raw_lamports = int(sol_amount * 1_000_000_000)

        session = await self._get_session()

        params = {
            "inputMint": WSOL_MINT,
            "outputMint": token_mint,
            "amount": str(raw_lamports),
            "slippageBps": str(slippage_bps),
        }
        async with session.get(JUPITER_PUBLIC_QUOTE_API, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter buy quote HTTP {resp.status}: {text[:200]}")
            quote = await resp.json()

        out_amount = quote.get("outAmount")
        if not out_amount or int(out_amount) == 0:
            raise RuntimeError(f"Jupiter buy quote zero outAmount for {token_mint[:8]}")

        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet_svc.get_public_key(),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": int(priority_fee * 1_000_000_000),
        }
        async with session.post(JUPITER_PUBLIC_SWAP_API, json=swap_payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter buy swap HTTP {resp.status}: {text[:200]}")
            swap_data = await resp.json()

        swap_tx_b64 = swap_data.get("swapTransaction")
        if not swap_tx_b64:
            raise RuntimeError("Jupiter buy swap returned no swapTransaction")

        tx_bytes = _b64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        signed_vtx = VersionedTransaction(vtx.message, [keypair])
        signed_bytes = bytes(signed_vtx)

        # Pre-flight simulation — catches slippage/account errors before paying priority fee.
        # Saves ~0.002 SOL per failed attempt (Custom 6014/6025).
        sim = await wallet_svc.rpc.simulate_transaction(signed_bytes)
        err = sim.get("err")
        if err:
            raise RuntimeError(f"Jupiter buy simulation failed (tx not submitted): {err}")

        sig = await wallet_svc.rpc.send_transaction(signed_bytes)
        # Wait for on-chain confirmation — raises if tx failed (slippage, bad account, etc.)
        # This prevents ghost positions from being logged when the swap doesn't execute.
        await wallet_svc.rpc.confirm_transaction(sig, timeout=35)
        return sig

    async def swap_jupiter_sell(
        self,
        token_mint: str,
        amount_float: float,
        token_decimals: int = 6,
        slippage_bps: int = 3000,
        priority_fee: float = 0.001,
    ) -> str:
        """Sell a token for SOL via Jupiter public API.

        Used as a fallback when PumpPortal returns HTTP 400 or the on-chain tx fails.
        Jupiter aggregates all Solana DEXes so it can sell tokens PumpPortal cannot route.

        slippage_bps: basis points — 3000 = 30% for distressed/low-liquidity tokens.
        Returns transaction signature on success, raises on failure.
        """
        import base64 as _b64

        from solders.transaction import VersionedTransaction

        from elizaos.plugins.solana.constants import (
            JUPITER_PUBLIC_QUOTE_API,
            JUPITER_PUBLIC_SWAP_API,
        )

        wallet_svc = self._wallet_service()
        keypair = wallet_svc.get_keypair()
        if keypair is None:
            raise ValueError("SOLANA_PRIVATE_KEY not configured")

        raw_amount = int(amount_float * (10 ** token_decimals))
        if raw_amount <= 0:
            raise ValueError(f"Invalid sell amount: {amount_float} tokens")

        session = await self._get_session()

        # 1. Get quote: token → SOL
        params = {
            "inputMint": token_mint,
            "outputMint": WSOL_MINT,
            "amount": str(raw_amount),
            "slippageBps": str(slippage_bps),
        }
        async with session.get(JUPITER_PUBLIC_QUOTE_API, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter quote HTTP {resp.status}: {text[:200]}")
            quote = await resp.json()

        out_amount = quote.get("outAmount")
        if not out_amount or int(out_amount) == 0:
            raise RuntimeError(f"Jupiter quote returned zero outAmount for {token_mint[:8]}")

        # 2. Build swap transaction
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet_svc.get_public_key(),
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": int(priority_fee * 1_000_000_000),
        }
        async with session.post(JUPITER_PUBLIC_SWAP_API, json=swap_payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Jupiter swap HTTP {resp.status}: {text[:200]}")
            swap_data = await resp.json()

        swap_tx_b64 = swap_data.get("swapTransaction")
        if not swap_tx_b64:
            raise RuntimeError("Jupiter swap returned no swapTransaction")

        # 3. Sign and submit
        tx_bytes = _b64.b64decode(swap_tx_b64)
        vtx = VersionedTransaction.from_bytes(tx_bytes)
        signed_vtx = VersionedTransaction(vtx.message, [keypair])
        signed_bytes = bytes(signed_vtx)

        # Pre-flight simulation — catches slippage/account errors before paying priority fee.
        # Saves ~0.002 SOL per failed attempt (Custom 6014/6025).
        sim = await wallet_svc.rpc.simulate_transaction(signed_bytes)
        err = sim.get("err")
        if err:
            raise RuntimeError(f"Jupiter sell simulation failed (tx not submitted): {err}")

        sig = await wallet_svc.rpc.send_transaction(signed_bytes)
        # Confirm on-chain — raises if tx errored (slippage, no token account, etc.)
        await wallet_svc.rpc.confirm_transaction(sig, timeout=35)
        return sig

    async def get_pool_info(self, pool_id: str) -> dict[str, Any]:
        """Fetch Raydium pool info via the Raydium public REST API."""
        session = await self._get_session()
        url = f"https://api.raydium.io/v2/ammV3/ammPools"
        try:
            async with session.get(url, params={"poolIds": pool_id}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            pools = data.get("data", [])
            return pools[0] if pools else {}
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.warning(
                    f"RaydiumService.get_pool_info failed for {pool_id}: {exc}",
                    src="service:raydium",
                )
            return {}
