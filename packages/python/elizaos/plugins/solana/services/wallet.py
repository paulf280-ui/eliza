"""Solana wallet service — keypair management, balance queries, transaction signing."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from elizaos.types import Service, ServiceTypeRegistry

from elizaos.plugins.solana.rpc import SolanaRpcClient

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime


class SolanaWalletService(Service):
    service_type = ServiceTypeRegistry.WALLET

    @property
    def capability_description(self) -> str:
        return "Solana wallet service for balance queries and transaction signing."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._rpc: SolanaRpcClient | None = None
        self._keypair: Any | None = None  # solders.keypair.Keypair
        self._public_key: str = ""
        self._read_only: bool = True

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> SolanaWalletService:
        service = cls()
        service._runtime = runtime

        rpc_url = os.getenv("SOLANA_RPC_URL", "")
        ws_url = os.getenv("SOLANA_WS_URL", "")
        # Helius Sender — dual-routes sendTransaction to staked validators +
        # Jito for faster inclusion. Other RPC methods stay on the primary.
        sender_url = os.getenv("SOLANA_SENDER_URL", "") or None
        service._rpc = SolanaRpcClient(
            rpc_url=rpc_url,
            ws_url=ws_url or None,
            sender_url=sender_url,
        )
        if sender_url:
            print(f"[wallet] Sender dual-submit enabled → {sender_url}")

        raw_key = os.getenv("SOLANA_PRIVATE_KEY", "")
        if raw_key:
            try:
                import base58
                from solders.keypair import Keypair

                key_bytes = base58.b58decode(raw_key)
                service._keypair = Keypair.from_bytes(key_bytes)
                service._public_key = str(service._keypair.pubkey())
                service._read_only = False
                runtime.logger.info(
                    f"SolanaWalletService: loaded keypair pubkey={service._public_key}",
                    src="service:solana_wallet",
                )
            except Exception as exc:
                runtime.logger.warning(
                    f"SolanaWalletService: failed to load SOLANA_PRIVATE_KEY ({exc}), "
                    "starting in read-only mode",
                    src="service:solana_wallet",
                )
        else:
            # Fall back to public-key-only mode
            service._public_key = os.getenv("SOLANA_PUBLIC_KEY", "")
            runtime.logger.info(
                "SolanaWalletService: no SOLANA_PRIVATE_KEY — read-only mode "
                f"pubkey={service._public_key or '<unset>'}",
                src="service:solana_wallet",
            )

        return service

    async def stop(self) -> None:
        if self._rpc:
            await self._rpc.close()
        self._keypair = None

    # ------------------------------------------------------------------ public

    def get_public_key(self) -> str:
        return self._public_key

    def get_keypair(self) -> Any:
        """Returns solders.keypair.Keypair (for internal use by other services)."""
        return self._keypair

    def is_read_only(self) -> bool:
        return self._read_only

    @property
    def rpc(self) -> SolanaRpcClient:
        if self._rpc is None:
            raise RuntimeError("SolanaWalletService not started")
        return self._rpc

    async def get_sol_balance(self) -> float:
        if not self._public_key:
            return 0.0
        lamports = await self.rpc.get_balance(self._public_key)
        return lamports / 1_000_000_000

    async def get_token_balances(self) -> list[dict[str, Any]]:
        if not self._public_key:
            return []
        from elizaos.plugins.solana.constants import TOKEN_PROGRAM_2022
        import asyncio as _asyncio

        # Query both SPL Token and Token-2022 programs (pump.fun v2 uses Token-2022)
        accounts_spl, accounts_t22 = await _asyncio.gather(
            self.rpc.get_token_accounts_by_owner(self._public_key),
            self.rpc.get_token_accounts_by_owner(self._public_key, program_id=TOKEN_PROGRAM_2022),
        )
        result = []
        for acc in accounts_spl + accounts_t22:
            info = (
                acc.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            token_amount = info.get("tokenAmount", {})
            amount = token_amount.get("uiAmount", 0) or 0
            if amount > 0:
                result.append(
                    {
                        "mint": info.get("mint", ""),
                        "amount": amount,
                        "decimals": token_amount.get("decimals", 0),
                        "raw_amount": int(token_amount.get("amount", 0)),
                    }
                )
        return result

    async def sign_and_send(self, tx: Any) -> str:
        """Sign and send a solders Transaction. Returns signature string."""
        if self._read_only or self._keypair is None:
            raise ValueError(
                "SOLANA_PRIVATE_KEY not configured — wallet is in read-only mode"
            )
        blockhash_str, _ = await self.rpc.get_latest_blockhash()
        from solders.hash import Hash

        blockhash = Hash.from_string(blockhash_str)
        tx.sign([self._keypair], blockhash)
        tx_bytes = bytes(tx)
        return await self.rpc.send_transaction(tx_bytes)
