"""Market price provider — Jupiter prices for held tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elizaos.types import Provider, ProviderResult, ServiceTypeRegistry

from elizaos.plugins.solana.constants import WSOL_MINT

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime, Memory, State


async def _get_market_prices(
    runtime: IAgentRuntime, _message: Memory, _state: State | None = None
) -> ProviderResult:
    from elizaos.plugins.solana.services.raydium import RaydiumService
    from elizaos.plugins.solana.services.wallet import SolanaWalletService

    wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)

    if not isinstance(wallet_svc, SolanaWalletService) or not isinstance(
        raydium_svc, RaydiumService
    ):
        return ProviderResult(
            text="Market price service not available.",
            values={},
            data={},
        )

    try:
        # SOL price
        sol_price = await raydium_svc.get_price(WSOL_MINT, vs_token="USDC")

        lines = ["# Market Prices"]
        prices: dict[str, float] = {"SOL": sol_price}
        lines.append(f"SOL: ${sol_price:.4f}")

        # Prices for held tokens
        token_balances = await wallet_svc.get_token_balances()
        for tok in token_balances[:10]:
            mint = tok["mint"]
            try:
                price = await raydium_svc.get_price(mint, vs_token="USDC")
                if price > 0:
                    prices[mint] = price
                    value_usd = price * tok["amount"]
                    lines.append(
                        f"{mint[:8]}...: ${price:.8f}  "
                        f"(holdings value: ${value_usd:.4f})"
                    )
            except Exception:
                pass

        return ProviderResult(
            text="\n".join(lines),
            values={"sol_price_usd": sol_price, "token_count": len(prices) - 1},
            data={"prices": prices},
        )
    except Exception as exc:
        return ProviderResult(
            text=f"Error fetching market prices: {exc}",
            values={},
            data={},
        )


market_provider = Provider(
    name="solana_market",
    description="Jupiter price feed for SOL and held SPL tokens (USD values).",
    get=_get_market_prices,
    dynamic=True,
)
