"""PumpFun service — bonding-curve reads and buy/sell transactions."""

from __future__ import annotations

import os
import struct
from typing import TYPE_CHECKING, Any

from elizaos.types import Service, ServiceTypeRegistry

from elizaos.plugins.solana.constants import (
    BONDING_CURVE_LAYOUT,
    BONDING_CURVE_LAYOUT_OFFSET,
    PUMP_FUN_PROGRAM_ID,
)

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime



class PumpFunService(Service):
    service_type = ServiceTypeRegistry.TOKEN_DATA

    @property
    def capability_description(self) -> str:
        return "Pump.fun bonding curve data and buy/sell transaction builder."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._helius_api_key: str = ""

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> PumpFunService:
        service = cls()
        service._runtime = runtime
        # Prefer explicit HELIUS_API_KEY; fall back to extracting it from the
        # RPC URL (which is formatted as "https://...helius-rpc.com/?api-key=<KEY>")
        key = os.getenv("HELIUS_API_KEY", "")
        if not key:
            rpc_url = os.getenv("SOLANA_RPC_URL", "")
            if "api-key=" in rpc_url:
                key = rpc_url.split("api-key=", 1)[1].split("&")[0].strip()
        service._helius_api_key = key
        runtime.logger.info(
            "PumpFunService started",
            src="service:pump_fun",
            agentId=str(runtime.agent_id),
        )
        return service

    async def stop(self) -> None:
        pass

    def _wallet_service(self) -> Any:
        from elizaos.plugins.solana.services.wallet import SolanaWalletService

        svc = self._runtime.get_service(ServiceTypeRegistry.WALLET)  # type: ignore[union-attr]
        if not isinstance(svc, SolanaWalletService):
            raise RuntimeError("SolanaWalletService not available")
        return svc

    # ---------------------------------------------------------------- reading

    async def get_bonding_curve(self, mint: str) -> dict[str, Any]:
        """Fetch and parse the pump.fun bonding curve for a token mint."""
        from solders.pubkey import Pubkey

        mint_pubkey = Pubkey.from_string(mint)
        program_id = Pubkey.from_string(PUMP_FUN_PROGRAM_ID)
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)],
            program_id,
        )

        wallet_svc = self._wallet_service()
        rpc = wallet_svc.rpc

        account = await rpc.get_account_info(str(bc_pda), encoding="base64")
        if account is None:
            return {}

        raw_data = account.get("data")
        if not raw_data:
            return {}

        import base64

        data_bytes: bytes
        if isinstance(raw_data, list):
            data_bytes = base64.b64decode(raw_data[0])
        else:
            data_bytes = base64.b64decode(raw_data)

        if len(data_bytes) < BONDING_CURVE_LAYOUT_OFFSET + struct.calcsize(BONDING_CURVE_LAYOUT):
            return {}

        (
            virtual_token_reserves,
            virtual_sol_reserves,
            real_token_reserves,
            real_sol_reserves,
            token_total_supply,
            complete_byte,
        ) = struct.unpack_from(BONDING_CURVE_LAYOUT, data_bytes, BONDING_CURVE_LAYOUT_OFFSET)

        complete = bool(complete_byte)
        lamports_per_sol = 1_000_000_000

        # Price in SOL per token
        if virtual_token_reserves > 0:
            price_sol = (virtual_sol_reserves / lamports_per_sol) / (
                virtual_token_reserves / 1_000_000
            )
        else:
            price_sol = 0.0

        # Bonding curve progress (0-100%)
        initial_supply = token_total_supply + real_token_reserves if token_total_supply else 0
        if initial_supply > 0:
            progress_pct = (1 - real_token_reserves / initial_supply) * 100
        else:
            progress_pct = 100.0 if complete else 0.0

        return {
            "mint": mint,
            "bonding_curve_address": str(bc_pda),
            "price_sol": price_sol,
            "virtual_sol": virtual_sol_reserves / lamports_per_sol,
            "virtual_tokens": virtual_token_reserves / 1_000_000,
            "real_sol": real_sol_reserves / lamports_per_sol,
            "real_tokens": real_token_reserves / 1_000_000,
            "complete": complete,
            "progress_pct": round(progress_pct, 2),
        }

    async def get_recent_launches(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch recent pump.fun launches using Helius enhanced transactions API."""
        if not self._helius_api_key:
            return []

        import aiohttp

        url = (
            f"https://api.helius.xyz/v0/addresses/{PUMP_FUN_PROGRAM_ID}/transactions"
            f"?type=CREATE&api-key={self._helius_api_key}&limit={limit}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    txs = await resp.json()
        except Exception as exc:
            if self._runtime:
                self._runtime.logger.warning(
                    f"PumpFunService.get_recent_launches failed: {exc}",
                    src="service:pump_fun",
                )
            return []

        launches = []
        for tx in txs:
            token_transfers = tx.get("tokenTransfers", [])
            if not token_transfers:
                continue
            mint = token_transfers[0].get("mint", "")
            description = tx.get("description", "")
            ts = tx.get("timestamp", 0)
            launches.append(
                {
                    "mint": mint,
                    "description": description,
                    "created_at": ts,
                    "signature": tx.get("signature", ""),
                }
            )
        return launches

    # ----------------------------------------------------------------- writing

    _PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"

    async def _pumpportal_trade(
        self,
        action: str,
        mint: str,
        amount: float | int | str,
        denominated_in_sol: bool,
        slippage_pct: int,
        pool: str,
        priority_fee: float = 0.005,
    ) -> str:
        """Call pumpportal.fun to get a pre-built transaction, sign it, and submit.

        pumpportal handles all pump.fun v2 Token-2022 account derivation
        (17 complex accounts including per-mint PDAs with unknown seeds) so we
        don't have to. The returned VersionedTransaction just needs signing.
        """
        import aiohttp
        from solders.transaction import VersionedTransaction

        wallet_svc = self._wallet_service()
        keypair = wallet_svc.get_keypair()
        if keypair is None:
            raise RuntimeError("Wallet keypair not available (read-only mode)")

        payload = {
            "publicKey": wallet_svc.get_public_key(),
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": slippage_pct,
            "priorityFee": priority_fee,  # caller controls fee; default 0.005 SOL (raised from 0.002 2026-04-30 per pump.fun quant guide for landing reliability on contested blockspace)
            "pool": pool,
        }

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        ) as session:
            async with session.post(self._PUMPPORTAL_URL, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    # If bonding curve is gone (token graduated), auto-retry with pump-amm
                    if pool == "pump" and ("migrated" in text.lower() or action == "sell") and action in ("buy", "sell"):
                        # For sells on pump pool: token may have graduated → try pump-amm
                        payload["pool"] = "pump-amm"
                        async with session.post(self._PUMPPORTAL_URL, json=payload) as resp2:
                            if resp2.status != 200:
                                text2 = await resp2.text()
                                raise RuntimeError(
                                    f"pumpportal {action} failed HTTP {resp2.status}: {text2[:200]}"
                                )
                            tx_bytes = await resp2.read()
                    elif pool == "pump-amm" and action == "sell":
                        # pump-amm sell failed — try legacy "pump" pool as fallback
                        payload["pool"] = "pump"
                        async with session.post(self._PUMPPORTAL_URL, json=payload) as resp2:
                            if resp2.status != 200:
                                text2 = await resp2.text()
                                raise RuntimeError(
                                    f"pumpportal {action} (pump fallback) failed HTTP {resp2.status}: {text2[:200]}"
                                )
                            tx_bytes = await resp2.read()
                    else:
                        raise RuntimeError(
                            f"pumpportal {action} failed HTTP {resp.status}: {text[:200]}"
                        )
                else:
                    tx_bytes = await resp.read()

        vtx = VersionedTransaction.from_bytes(tx_bytes)
        signed_vtx = VersionedTransaction(vtx.message, [keypair])
        signed_bytes = bytes(signed_vtx)

        # Pre-flight simulation with current state (replaces blockhash, skips sig check).
        # Catches BondingCurveComplete (error 6005 / 0x1775) before wasting priority fees.
        if pool == "pump" and action == "buy":
            sim = await wallet_svc.rpc.simulate_transaction(signed_bytes)
            err = sim.get("err")
            if err:
                inner = err.get("InstructionError") if isinstance(err, dict) else None
                is_bc_complete = (
                    isinstance(inner, list)
                    and len(inner) > 1
                    and isinstance(inner[1], dict)
                    and inner[1].get("Custom") == 6005
                )
                if is_bc_complete:
                    # Token graduated — retry with PumpSwap (pump-amm) pool
                    payload["pool"] = "pump-amm"
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as session2:
                        async with session2.post(self._PUMPPORTAL_URL, json=payload) as resp2:
                            if resp2.status != 200:
                                text2 = await resp2.text()
                                raise RuntimeError(
                                    f"pumpportal buy (pump-amm retry) failed HTTP {resp2.status}: {text2[:200]}"
                                )
                            tx_bytes2 = await resp2.read()
                    vtx2 = VersionedTransaction.from_bytes(tx_bytes2)
                    signed_bytes = bytes(VersionedTransaction(vtx2.message, [keypair]))
                elif err:
                    raise RuntimeError(f"pumpportal {action} simulation failed: {err}")

        sig = await wallet_svc.rpc.send_transaction(signed_bytes)
        # Confirm on-chain — raises if tx failed, preventing ghost trade logging
        await wallet_svc.rpc.confirm_transaction(sig, timeout=35)
        return sig

    async def buy(
        self, mint: str, sol_amount: float, slippage: float = 0.10, pool: str = "pump"
    ) -> str:
        """Buy a token via pumpportal. Returns transaction signature.

        pool: "pump" (bonding curve), "pump-amm" (PumpSwap/graduated),
              "raydium" (Raydium AMM)
        """
        slippage_pct = max(1, int(slippage * 100))
        return await self._pumpportal_trade(
            action="buy",
            mint=mint,
            amount=sol_amount,
            denominated_in_sol=True,
            slippage_pct=slippage_pct,
            pool=pool,
        )

    async def sell(
        self, mint: str, token_amount: int, slippage: float = 0.10, pool: str = "pump"
    ) -> str:
        """Sell tokens via pumpportal. Returns transaction signature.

        pool: "pump" (bonding curve), "pump-amm" (PumpSwap/graduated),
              "raydium" (Raydium AMM)
        """
        slippage_pct = max(1, int(slippage * 100))
        return await self._pumpportal_trade(
            action="sell",
            mint=mint,
            amount=token_amount,
            denominated_in_sol=False,
            slippage_pct=slippage_pct,
            pool=pool,
        )
