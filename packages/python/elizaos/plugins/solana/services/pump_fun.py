"""PumpFun service — bonding-curve reads and buy/sell transactions."""

from __future__ import annotations

import os
import struct
from typing import TYPE_CHECKING, Any

from elizaos.types import Service, ServiceTypeRegistry

from elizaos.plugins.solana.constants import (
    ASSOCIATED_TOKEN_PROGRAM,
    BONDING_CURVE_LAYOUT,
    BONDING_CURVE_LAYOUT_OFFSET,
    COMPUTE_BUDGET_PROGRAM,
    PUMP_FUN_BUY_DISCRIMINATOR,
    PUMP_FUN_FEE_RECIPIENT,
    PUMP_FUN_PROGRAM_ID,
    PUMP_FUN_SELL_DISCRIMINATOR,
    RENT_SYSVAR,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
)

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime


def _get_ata(owner_str: str, mint_str: str) -> Any:
    """Derive Associated Token Account address."""
    from solders.pubkey import Pubkey

    owner = Pubkey.from_string(owner_str)
    mint = Pubkey.from_string(mint_str)
    token_prog = Pubkey.from_string(TOKEN_PROGRAM)
    ata, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_prog), bytes(mint)],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM),
    )
    return ata


def _set_compute_unit_limit(units: int) -> Any:
    """Build ComputeBudget SetComputeUnitLimit instruction."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        program_id=Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
        accounts=[],
        data=bytes([0x02]) + struct.pack("<I", units),
    )


def _set_compute_unit_price(microlamports: int) -> Any:
    """Build ComputeBudget SetComputeUnitPrice instruction."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        program_id=Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
        accounts=[],
        data=bytes([0x03]) + struct.pack("<Q", microlamports),
    )


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
        service._helius_api_key = os.getenv("HELIUS_API_KEY", "")
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

    async def buy(self, mint: str, sol_amount: float, slippage: float = 0.01) -> str:
        """Buy a token on the pump.fun bonding curve. Returns transaction signature."""
        from solders.instruction import AccountMeta, Instruction
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        wallet_svc = self._wallet_service()
        user_pubkey = Pubkey.from_string(wallet_svc.get_public_key())
        mint_pubkey = Pubkey.from_string(mint)
        program_id = Pubkey.from_string(PUMP_FUN_PROGRAM_ID)

        # Derive PDAs
        global_pda, _ = Pubkey.find_program_address([b"global"], program_id)
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)], program_id
        )
        event_auth_pda, _ = Pubkey.find_program_address([b"__event_authority"], program_id)

        # Derive ATAs
        assoc_bc = _get_ata(str(bc_pda), mint)
        assoc_user = _get_ata(str(user_pubkey), mint)

        # Fetch bonding curve to compute token amount
        bc_data = await self.get_bonding_curve(mint)
        if not bc_data:
            raise RuntimeError(f"Could not fetch bonding curve for {mint}")

        lamports_per_sol = 1_000_000_000
        sol_lamports = int(sol_amount * lamports_per_sol)
        max_sol_lamports = int(sol_lamports * (1 + slippage))

        # Estimate token amount from virtual reserves
        vt = bc_data["virtual_tokens"] * 1_000_000  # back to raw
        vs = bc_data["virtual_sol"] * lamports_per_sol  # back to lamports
        if vs + sol_lamports > 0:
            token_amount = int(vt * sol_lamports / (vs + sol_lamports))
        else:
            token_amount = 0
        token_amount = int(token_amount * (1 - slippage))  # apply slippage

        # Build instruction data
        data = PUMP_FUN_BUY_DISCRIMINATOR + struct.pack("<Q", token_amount) + struct.pack("<Q", max_sol_lamports)

        accounts = [
            AccountMeta(pubkey=global_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(PUMP_FUN_FEE_RECIPIENT), is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bc_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=assoc_bc, is_signer=False, is_writable=True),
            AccountMeta(pubkey=assoc_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(SYSTEM_PROGRAM), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(TOKEN_PROGRAM), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(RENT_SYSVAR), is_signer=False, is_writable=False),
            AccountMeta(pubkey=event_auth_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=program_id, is_signer=False, is_writable=False),
        ]

        buy_ix = Instruction(program_id=program_id, data=bytes(data), accounts=accounts)
        cu_limit_ix = _set_compute_unit_limit(200_000)
        cu_price_ix = _set_compute_unit_price(1_000_000)

        tx = Transaction.new_with_payer([cu_limit_ix, cu_price_ix, buy_ix], user_pubkey)
        return await wallet_svc.sign_and_send(tx)

    async def sell(self, mint: str, token_amount: int, slippage: float = 0.01) -> str:
        """Sell tokens on the pump.fun bonding curve. Returns transaction signature."""
        from solders.instruction import AccountMeta, Instruction
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        wallet_svc = self._wallet_service()
        user_pubkey = Pubkey.from_string(wallet_svc.get_public_key())
        mint_pubkey = Pubkey.from_string(mint)
        program_id = Pubkey.from_string(PUMP_FUN_PROGRAM_ID)

        # Derive PDAs
        global_pda, _ = Pubkey.find_program_address([b"global"], program_id)
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)], program_id
        )
        event_auth_pda, _ = Pubkey.find_program_address([b"__event_authority"], program_id)

        # Derive ATAs
        assoc_bc = _get_ata(str(bc_pda), mint)
        assoc_user = _get_ata(str(user_pubkey), mint)

        # Compute min SOL output from bonding curve
        bc_data = await self.get_bonding_curve(mint)
        lamports_per_sol = 1_000_000_000
        if bc_data:
            vt = bc_data["virtual_tokens"] * 1_000_000
            vs = bc_data["virtual_sol"] * lamports_per_sol
            if vt - token_amount > 0:
                sol_out = int(vs * token_amount / vt)
            else:
                sol_out = 0
            min_sol_lamports = int(sol_out * (1 - slippage))
        else:
            min_sol_lamports = 0

        data = PUMP_FUN_SELL_DISCRIMINATOR + struct.pack("<Q", token_amount) + struct.pack("<Q", min_sol_lamports)

        accounts = [
            AccountMeta(pubkey=global_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(PUMP_FUN_FEE_RECIPIENT), is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=bc_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=assoc_bc, is_signer=False, is_writable=True),
            AccountMeta(pubkey=assoc_user, is_signer=False, is_writable=True),
            AccountMeta(pubkey=user_pubkey, is_signer=True, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string(SYSTEM_PROGRAM), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(TOKEN_PROGRAM), is_signer=False, is_writable=False),
            AccountMeta(pubkey=event_auth_pda, is_signer=False, is_writable=False),
            AccountMeta(pubkey=program_id, is_signer=False, is_writable=False),
        ]

        sell_ix = Instruction(program_id=program_id, data=bytes(data), accounts=accounts)
        cu_limit_ix = _set_compute_unit_limit(200_000)
        cu_price_ix = _set_compute_unit_price(1_000_000)

        tx = Transaction.new_with_payer([cu_limit_ix, cu_price_ix, sell_ix], user_pubkey)
        return await wallet_svc.sign_and_send(tx)
