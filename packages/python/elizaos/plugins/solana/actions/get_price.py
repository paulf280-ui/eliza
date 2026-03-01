"""GET_TOKEN_PRICE action — fetch price from Jupiter + pump.fun bonding curve."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elizaos.types import Action, ActionResult, ServiceTypeRegistry

if TYPE_CHECKING:
    from elizaos.types import (
        HandlerCallback,
        HandlerOptions,
        IAgentRuntime,
        Memory,
        State,
    )

_SIMILES = [
    "GET_PRICE",
    "TOKEN_PRICE",
    "CHECK_PRICE",
    "PRICE_CHECK",
    "WHAT_IS_PRICE",
]

_DESCRIPTION = (
    "Fetch the current price of a Solana token. "
    "Parameters: mint (required) — the token mint address."
)


async def _validate(
    runtime: IAgentRuntime, message: Memory, state: State | None = None
) -> bool:
    return True


async def _handler(
    runtime: IAgentRuntime,
    message: Memory,
    state: State | None = None,
    options: HandlerOptions | None = None,
    callback: HandlerCallback | None = None,
    responses: list[Memory] | None = None,
) -> ActionResult:
    from elizaos.plugins.solana.services.pump_fun import PumpFunService
    from elizaos.plugins.solana.services.raydium import RaydiumService

    # Extract mint from message text (simple heuristic)
    text = (message.content.text if message.content else "") or ""
    words = text.split()
    # Look for a base58-ish token address (32-44 chars, alphanumeric)
    mint = ""
    for word in words:
        cleaned = word.strip(".,;:")
        if 32 <= len(cleaned) <= 44 and cleaned.isalnum():
            mint = cleaned
            break

    if not mint:
        result_text = "Please provide a token mint address."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=result_text, actions=["GET_TOKEN_PRICE"]))
        return ActionResult(text=result_text, success=False)

    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)
    pump_svc = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)

    price_usd = 0.0
    price_sol = 0.0
    bonding_info: dict = {}

    if isinstance(raydium_svc, RaydiumService):
        try:
            price_usd = await raydium_svc.get_price(mint, vs_token="USDC")
            sol_price = await raydium_svc.get_price(mint, vs_token="SOL")
            price_sol = sol_price
        except Exception:
            pass

    if isinstance(pump_svc, PumpFunService):
        try:
            bonding_info = await pump_svc.get_bonding_curve(mint)
            if bonding_info and not price_sol:
                price_sol = bonding_info.get("price_sol", 0.0)
        except Exception:
            pass

    lines = [f"Token: {mint[:8]}...{mint[-4:]}"]
    if price_usd:
        lines.append(f"Price (USD): ${price_usd:.8f}")
    if price_sol:
        lines.append(f"Price (SOL): {price_sol:.8f} SOL")
    if bonding_info:
        lines.append(f"Bonding curve: {bonding_info.get('progress_pct', 0):.1f}% complete")
        lines.append(f"Migrated to DEX: {'Yes' if bonding_info.get('complete') else 'No'}")

    result_text = "\n".join(lines) if len(lines) > 1 else f"No price data found for {mint}."

    if callback:
        from elizaos.types import Content
        await callback(Content(text=result_text, actions=["GET_TOKEN_PRICE"]))

    return ActionResult(
        text=result_text,
        values={
            "mint": mint,
            "price_usd": price_usd,
            "price_sol": price_sol,
            "bonding_complete": bonding_info.get("complete", False),
        },
        success=True,
    )


get_price_action = Action(
    name="GET_TOKEN_PRICE",
    description=_DESCRIPTION,
    similes=_SIMILES,
    validate=_validate,
    handler=_handler,
    examples=[],
)
