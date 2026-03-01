"""BUY_TOKEN action — buy a Solana token via pump.fun or Jupiter/Raydium."""

from __future__ import annotations

import re
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
    "BUY",
    "PURCHASE_TOKEN",
    "SWAP_TO_TOKEN",
    "BUY_CRYPTO",
]

_DESCRIPTION = (
    "Buy a Solana token using SOL. "
    "Parameters: mint (required) — token mint address; "
    "sol_amount (required) — SOL to spend; "
    "dex (optional, default 'auto') — 'pump_fun' | 'raydium' | 'auto'; "
    "slippage (optional, default 0.01) — fraction, e.g. 0.01 = 1%."
)

_MINT_RE = re.compile(r"\b([A-Za-z0-9]{32,44})\b")
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*sol", re.IGNORECASE)


def _extract_params(text: str) -> tuple[str, float, str, float]:
    """Rough extraction of mint, sol_amount, dex, slippage from free text."""
    mint = ""
    for m in _MINT_RE.finditer(text):
        candidate = m.group(1)
        # Skip common English words accidentally matched
        if len(candidate) >= 32 and candidate.isalnum():
            mint = candidate
            break

    sol_amount = 0.0
    m2 = _AMOUNT_RE.search(text)
    if m2:
        sol_amount = float(m2.group(1))

    dex = "auto"
    if "pump" in text.lower():
        dex = "pump_fun"
    elif "raydium" in text.lower() or "jupiter" in text.lower():
        dex = "raydium"

    slippage = 0.01
    slip_m = re.search(r"slippage[:\s]+(\d+(?:\.\d+)?)\s*%?", text, re.IGNORECASE)
    if slip_m:
        val = float(slip_m.group(1))
        slippage = val / 100 if val > 1 else val

    return mint, sol_amount, dex, slippage


async def _validate(
    runtime: IAgentRuntime, message: Memory, state: State | None = None
) -> bool:
    from elizaos.plugins.solana.services.wallet import SolanaWalletService

    wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
    if not isinstance(wallet_svc, SolanaWalletService):
        return False
    return not wallet_svc.is_read_only()


async def _handler(
    runtime: IAgentRuntime,
    message: Memory,
    state: State | None = None,
    options: HandlerOptions | None = None,
    callback: HandlerCallback | None = None,
    responses: list[Memory] | None = None,
) -> ActionResult:
    from elizaos.plugins.solana.constants import WSOL_MINT
    from elizaos.plugins.solana.services.pump_fun import PumpFunService
    from elizaos.plugins.solana.services.raydium import RaydiumService

    text = (message.content.text if message.content else "") or ""
    mint, sol_amount, dex, slippage = _extract_params(text)

    if not mint:
        msg = "Please provide a token mint address to buy."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=msg, success=False)

    if sol_amount <= 0:
        msg = "Please specify a valid SOL amount to spend (e.g. '0.01 SOL')."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=msg, success=False)

    pump_svc = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)

    signature = ""
    route_used = ""

    try:
        if dex == "auto":
            # Check if still on pump.fun bonding curve
            if isinstance(pump_svc, PumpFunService):
                bc = await pump_svc.get_bonding_curve(mint)
                use_pump = bool(bc) and not bc.get("complete", True)
            else:
                use_pump = False

            if use_pump and isinstance(pump_svc, PumpFunService):
                signature = await pump_svc.buy(mint, sol_amount, slippage)
                route_used = "pump.fun"
            elif isinstance(raydium_svc, RaydiumService):
                signature = await raydium_svc.swap(WSOL_MINT, mint, sol_amount, slippage)
                route_used = "Jupiter/Raydium"
            else:
                return ActionResult(text="No DEX service available.", success=False)

        elif dex == "pump_fun" and isinstance(pump_svc, PumpFunService):
            signature = await pump_svc.buy(mint, sol_amount, slippage)
            route_used = "pump.fun"

        elif isinstance(raydium_svc, RaydiumService):
            signature = await raydium_svc.swap(WSOL_MINT, mint, sol_amount, slippage)
            route_used = "Jupiter/Raydium"
        else:
            return ActionResult(text="Requested DEX service not available.", success=False)

    except Exception as exc:
        error_msg = f"Buy transaction failed: {exc}"
        if callback:
            from elizaos.types import Content
            await callback(Content(text=error_msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=error_msg, success=False)

    result_text = (
        f"Bought {mint[:8]}... using {sol_amount} SOL via {route_used}.\n"
        f"Transaction: {signature}"
    )
    if callback:
        from elizaos.types import Content
        await callback(Content(text=result_text, actions=["BUY_TOKEN"]))

    return ActionResult(
        text=result_text,
        values={
            "signature": signature,
            "mint": mint,
            "sol_spent": sol_amount,
            "route": route_used,
        },
        success=True,
    )


buy_token_action = Action(
    name="BUY_TOKEN",
    description=_DESCRIPTION,
    similes=_SIMILES,
    validate=_validate,
    handler=_handler,
    examples=[],
)
