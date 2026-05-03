"""SELL_TOKEN action — sell a Solana token via pump.fun or Jupiter/Raydium."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")

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
    "SELL",
    "SELL_CRYPTO",
    "DUMP_TOKEN",
    "SWAP_FROM_TOKEN",
]

_DESCRIPTION = (
    "Sell a Solana token for SOL. "
    "Parameters: mint (required) — token mint address; "
    "percentage (optional, default 100) — % of holdings to sell; "
    "dex (optional, default 'auto') — 'pump_fun' | 'raydium' | 'auto'; "
    "slippage (optional, default 0.01)."
)

_MINT_RE = re.compile(r"\b([A-Za-z0-9]{32,44})\b")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)


def _extract_params(text: str) -> tuple[str, float, str, float]:
    mint = ""
    for m in _MINT_RE.finditer(text):
        candidate = m.group(1)
        if len(candidate) >= 32 and candidate.isalnum():
            mint = candidate
            break

    percentage = 100.0
    m2 = _PCT_RE.search(text)
    if m2:
        percentage = float(m2.group(1))

    dex = "auto"
    if "pump" in text.lower():
        dex = "pump_fun"
    elif "raydium" in text.lower() or "jupiter" in text.lower():
        dex = "raydium"

    slippage = 0.10  # 10% default — playbook §2.4 recommends 10-15% for sell exits
    slip_m = re.search(r"slippage[:\s]+(\d+(?:\.\d+)?)\s*%?", text, re.IGNORECASE)
    if slip_m:
        val = float(slip_m.group(1))
        slippage = val / 100 if val > 1 else val

    return mint, percentage, dex, slippage


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
    from elizaos.plugins.solana.services.wallet import SolanaWalletService

    text = (message.content.text if message.content else "") or ""
    mint, percentage, dex, slippage = _extract_params(text)

    if not mint:
        msg = "Please provide a token mint address to sell."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["SELL_TOKEN"]))
        return ActionResult(text=msg, success=False)

    wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
    pump_svc = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)
    pos_mgr = runtime.get_service("position_manager")

    # Get token balance to determine sell amount
    token_amount_raw = 0
    if isinstance(wallet_svc, SolanaWalletService):
        balances = await wallet_svc.get_token_balances()
        for tok in balances:
            if tok["mint"] == mint:
                token_amount_raw = int(tok["raw_amount"] * percentage / 100)
                break

    if token_amount_raw <= 0:
        msg = f"No holdings found for {mint[:8]}... (or zero balance)."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["SELL_TOKEN"]))
        return ActionResult(text=msg, success=False)

    signature = ""
    route_used = ""

    try:
        if PAPER_TRADING:
            import uuid as _uuid
            # Paper trading: simulate sell with real price data, no actual transaction
            signature = f"PAPER_{_uuid.uuid4().hex[:12].upper()}"
            route_used = "paper_trade"
        elif isinstance(raydium_svc, RaydiumService):
            # All sells go through Jupiter — aggregates PumpSwap, Raydium, Meteora
            if isinstance(wallet_svc, SolanaWalletService):
                balances = await wallet_svc.get_token_balances()
                decimals = next(
                    (t["decimals"] for t in balances if t["mint"] == mint), 6
                )
            else:
                decimals = 6
            amount_float = token_amount_raw / (10**decimals)
            last_exc = None
            for slip_bps in (3000, 5000, 8000):
                try:
                    signature = await raydium_svc.swap_jupiter_sell(
                        mint, amount_float, token_decimals=decimals, slippage_bps=slip_bps
                    )
                    route_used = f"Jupiter ({slip_bps//100}% slip)"
                    last_exc = None
                    break
                except Exception as _e:
                    last_exc = _e
            if last_exc is not None:
                raise last_exc
        else:
            return ActionResult(text="No DEX service available.", success=False)

    except Exception as exc:
        error_msg = f"Sell transaction failed: {exc}"
        if callback:
            from elizaos.types import Content
            await callback(Content(text=error_msg, actions=["SELL_TOKEN"]))
        return ActionResult(text=error_msg, success=False)

    # Notify position manager so it can close/update position tracking
    if pos_mgr is not None and mint in getattr(pos_mgr, "positions", {}):
        try:
            if percentage >= 100:
                # Full close — get current exit price from bonding curve or use 0
                exit_price = 0.0
                if isinstance(pump_svc, PumpFunService):
                    bc = await pump_svc.get_bonding_curve(mint)
                    if bc:
                        exit_price = bc.get("price_sol", 0.0)
                pos_mgr.close_position(mint, exit_price, "manual_sell", signature)
            else:
                # Partial sell — notify with fraction
                exit_price = 0.0
                if isinstance(pump_svc, PumpFunService):
                    bc = await pump_svc.get_bonding_curve(mint)
                    if bc:
                        exit_price = bc.get("price_sol", 0.0)
                pos_mgr.notify_partial_sell(mint, percentage / 100, exit_price, signature)
        except Exception:
            pass  # position manager notification is non-fatal

    result_text = (
        f"Sold {percentage}% of {mint[:8]}... holdings via {route_used}.\n"
        f"Transaction: {signature}"
    )
    if callback:
        from elizaos.types import Content
        await callback(Content(text=result_text, actions=["SELL_TOKEN"]))

    return ActionResult(
        text=result_text,
        values={
            "signature": signature,
            "mint": mint,
            "percentage_sold": percentage,
            "route": route_used,
        },
        success=True,
    )


sell_token_action = Action(
    name="SELL_TOKEN",
    description=_DESCRIPTION,
    similes=_SIMILES,
    validate=_validate,
    handler=_handler,
    examples=[],
)
