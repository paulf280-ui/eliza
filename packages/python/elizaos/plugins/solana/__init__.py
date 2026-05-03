"""Solana plugin for elizaOS — on-chain data, trading, and launch monitoring."""

from __future__ import annotations

from elizaos.types import Plugin

from .actions import buy_token_action, get_price_action, sell_token_action
from .providers import launches_provider, market_provider, wallet_provider
from .services import (
    MonsterStrategyService,
    PositionManagerService,
    PumpFunService,
    RaydiumService,
    SocialMonitorService,
    SolanaWalletService,
    TokenLaunchMonitorService,
)


def create_solana_plugin() -> Plugin:
    """Create a configured Solana plugin instance."""
    return Plugin(
        name="solana",
        description=(
            "Solana blockchain integration: wallet management, pump.fun bonding curve "
            "trading, Jupiter/Raydium DEX swaps, and real-time token launch monitoring."
        ),
        services=[
            SolanaWalletService,
            PumpFunService,
            RaydiumService,
            TokenLaunchMonitorService,
            SocialMonitorService,
            PositionManagerService,
            MonsterStrategyService,
        ],
        providers=[wallet_provider, market_provider, launches_provider],
        actions=[buy_token_action, sell_token_action, get_price_action],
    )


__all__ = [
    "create_solana_plugin",
    "SolanaWalletService",
    "PumpFunService",
    "RaydiumService",
    "TokenLaunchMonitorService",
    "SocialMonitorService",
    "PositionManagerService",
    "MonsterStrategyService",
    "wallet_provider",
    "market_provider",
    "launches_provider",
    "buy_token_action",
    "sell_token_action",
    "get_price_action",
]
