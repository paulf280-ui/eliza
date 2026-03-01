"""Raydium/Jupiter service — price feed, quotes, and swaps via Jupiter aggregator."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import aiohttp

from elizaos.types import Service, ServiceTypeRegistry

from elizaos.plugins.solana.constants import (
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

    # --------------------------------------------------------------- public

    async def get_price(self, mint: str, vs_token: str = "USDC") -> float:
        """Return price of mint in vs_token (USDC or SOL)."""
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

    async def swap(
        self,
        token_in: str,
        token_out: str,
        amount_in: float,
        slippage: float = 0.01,
    ) -> str:
        """Execute a Jupiter swap. Returns transaction signature."""
        wallet_svc = self._wallet_service()
        user_pubkey = wallet_svc.get_public_key()

        # Determine input decimals (assume SOL=9, others=6 if unknown)
        decimals = 9 if token_in == WSOL_MINT else 6
        amount_lamports = int(amount_in * (10**decimals))

        quote = await self.get_quote(token_in, token_out, amount_lamports, slippage)

        session = await self._get_session()
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
        }
        async with session.post(JUPITER_SWAP_API, json=swap_payload) as resp:
            resp.raise_for_status()
            swap_data = await resp.json()

        # Deserialize and sign the versioned transaction
        tx_b64 = swap_data.get("swapTransaction", "")
        tx_bytes = base64.b64decode(tx_b64)

        from solders.transaction import VersionedTransaction

        vtx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign: re-serialize with our keypair
        keypair = wallet_svc.get_keypair()
        if keypair is None:
            raise ValueError("SOLANA_PRIVATE_KEY not configured")

        vtx.sign([keypair])
        signed_bytes = bytes(vtx)
        return await wallet_svc.rpc.send_transaction(signed_bytes)

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
