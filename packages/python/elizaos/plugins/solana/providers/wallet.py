"""Wallet provider — SOL balance and token holdings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elizaos.types import Provider, ProviderResult, ServiceTypeRegistry

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime, Memory, State


async def _get_wallet_info(
    runtime: IAgentRuntime, _message: Memory, _state: State | None = None
) -> ProviderResult:
    from elizaos.plugins.solana.services.wallet import SolanaWalletService

    svc = runtime.get_service(ServiceTypeRegistry.WALLET)
    if not isinstance(svc, SolanaWalletService):
        return ProviderResult(
            text="Solana wallet service not available.",
            values={},
            data={},
        )

    pubkey = svc.get_public_key()
    if not pubkey:
        return ProviderResult(
            text="No Solana wallet configured (SOLANA_PUBLIC_KEY or SOLANA_PRIVATE_KEY not set).",
            values={},
            data={},
        )

    try:
        sol_balance = await svc.get_sol_balance()
        token_balances = await svc.get_token_balances()
        read_only = svc.is_read_only()

        mode_note = " (read-only)" if read_only else ""
        lines = [
            "# Wallet" + mode_note,
            f"Address: {pubkey}",
            f"SOL Balance: {sol_balance:.6f} SOL",
        ]

        if token_balances:
            lines.append(f"Token Holdings: {len(token_balances)} token(s)")
            for tok in sorted(token_balances, key=lambda t: t["amount"], reverse=True)[:10]:
                lines.append(
                    f"  - {tok['mint']}  {tok['amount']:.4f} (decimals: {tok['decimals']})"
                )
        else:
            lines.append("Token Holdings: none")

        text = "\n".join(lines)
        return ProviderResult(
            text=text,
            values={
                "address": pubkey,
                "sol_balance": sol_balance,
                "token_count": len(token_balances),
                "read_only": read_only,
            },
            data={"tokens": token_balances},
        )
    except Exception as exc:
        return ProviderResult(
            text=f"Error fetching wallet info: {exc}",
            values={},
            data={},
        )


wallet_provider = Provider(
    name="solana_wallet",
    description=(
        "Current Solana wallet address, SOL balance, and SPL token holdings "
        "from the on-chain wallet."
    ),
    get=_get_wallet_info,
    dynamic=True,
)
