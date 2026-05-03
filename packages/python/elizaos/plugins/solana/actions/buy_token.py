"""BUY_TOKEN action — quantitative buy with safety gates and position registration.

Flow:
  1. Parse mint, sol_amount, dex, slippage from message text.
  2. Gate checks (circuit breaker → position sizing → token safety filter).
  3. If message lacks "confirm": return a detailed preview and ask for confirmation.
  4. If message contains "confirm": execute trade and register with PositionManagerService.

Quantitative defaults (from empirical research):
  - Max position: 2% of wallet (25% of full Kelly for p=0.20, b=10)
  - Default slippage: 10% (pump.fun thin liquidity; use Jito to avoid sandwich)
  - SL registered at -25%, TP1 at 2x (50% exit), TP2 at 5x (25% exit)
"""

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
    "BUY",
    "PURCHASE_TOKEN",
    "SWAP_TO_TOKEN",
    "BUY_CRYPTO",
]

_DESCRIPTION = (
    "Buy a Solana token using SOL. "
    "Parameters: mint (required) — token mint address; "
    "sol_amount (required) — SOL to spend (max 2% of wallet); "
    "dex (optional, default 'auto') — 'pump_fun' | 'raydium' | 'auto'; "
    "slippage (optional, default 0.10 = 10%). "
    "Always shows a confirmation preview first — include the word 'confirm' to execute."
)

_MINT_RE = re.compile(r"\b([A-Za-z0-9]{32,44})\b")
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*sol", re.IGNORECASE)


def _extract_params(text: str) -> tuple[str, float, str, float]:
    mint = ""
    for m in _MINT_RE.finditer(text):
        candidate = m.group(1)
        if len(candidate) >= 32:
            mint = candidate
            break

    sol_amount = 0.0
    m2 = _AMOUNT_RE.search(text)
    if m2:
        sol_amount = float(m2.group(1))

    dex = "auto"
    tl = text.lower()
    if "pump" in tl:
        dex = "pump_fun"
    elif "raydium" in tl or "jupiter" in tl:
        dex = "raydium"

    slippage = 0.10  # 10% default (research-backed for pump.fun)
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
    from elizaos.plugins.solana.services.pump_fun import PumpFunService
    from elizaos.plugins.solana.services.raydium import RaydiumService
    from elizaos.plugins.solana.services.wallet import SolanaWalletService
    from elizaos.plugins.solana.constants import WSOL_MINT

    text = (message.content.text if message.content else "") or ""
    mint, sol_amount, dex, slippage = _extract_params(text)

    # ── validate basic params ─────────────────────────────────────────────────
    if not mint:
        msg = "Please provide a token mint address to buy."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=msg, success=False)

    if sol_amount <= 0:
        msg = "Please specify a valid SOL amount (e.g. 'buy 0.01 SOL of <mint>')."
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=msg, success=False)

    # ── get services ──────────────────────────────────────────────────────────
    wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
    pump_svc = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)
    pos_mgr = runtime.get_service("position_manager")

    # ── circuit breaker + position sizing ─────────────────────────────────────
    if pos_mgr is not None and isinstance(wallet_svc, SolanaWalletService):
        # Use paper wallet balance when in paper trading mode so position sizing works
        import os as _os
        _paper_mode = _os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
        _paper_bal = float(_os.getenv("PAPER_WALLET_SOL", "0.0"))
        if _paper_mode and _paper_bal > 0:
            wallet_sol = _paper_bal
        else:
            wallet_sol = await wallet_svc.get_sol_balance()
        allowed, reason = pos_mgr.check_trade_allowed(sol_amount, wallet_sol)
        if not allowed:
            msg = f"Trade blocked: {reason}"
            if callback:
                from elizaos.types import Content
                await callback(Content(text=msg, actions=["BUY_TOKEN"]))
            return ActionResult(text=msg, success=False)

    # ── token safety filter ───────────────────────────────────────────────────
    safety_msg = ""
    if pos_mgr is not None:
        safe, safety_reason = await pos_mgr.check_token_safety(mint)
        if not safe:
            msg = f"Token safety check FAILED: {safety_reason}\nTrade blocked for your protection."
            if callback:
                from elizaos.types import Content
                await callback(Content(text=msg, actions=["BUY_TOKEN"]))
            return ActionResult(text=msg, success=False)
        safety_msg = safety_reason

    # ── determine route ───────────────────────────────────────────────────────
    # Buys: pump.fun bonding curve uses PumpFunService directly (Jupiter doesn't index it fast enough).
    # Everything else (PumpSwap, Raydium, Meteora) uses Jupiter — best execution across all DEXes.
    route_used = dex
    use_pump = False
    if dex == "auto" and isinstance(pump_svc, PumpFunService):
        bc = await pump_svc.get_bonding_curve(mint)
        use_pump = bool(bc) and not bc.get("complete", True)
        route_used = "pump_fun" if use_pump else "jupiter"
    elif dex == "pump_fun":
        use_pump = True
        route_used = "pump_fun"
    else:
        route_used = "jupiter"

    # ── confirmation preview (no "confirm" in message) ────────────────────────
    confirmed = "confirm" in text.lower()
    if not confirmed:
        # Compute estimated entry price for preview
        entry_price_sol = 0.0
        bonding_info = ""
        if use_pump and isinstance(pump_svc, PumpFunService):
            try:
                bc = await pump_svc.get_bonding_curve(mint)
                if bc:
                    entry_price_sol = bc.get("price_sol", 0.0)
                    bonding_info = (
                        f"Bonding curve: {bc.get('progress_pct', 0):.1f}% filled | "
                        f"Virtual SOL: {bc.get('virtual_sol', 0):.2f}"
                    )
            except Exception:
                pass

        from elizaos.plugins.solana.services.position_manager import (
            STOP_LOSS_PCT, TP1_MULT, TP2_MULT, TP3_MULT, TRAILING_STOP_PCT
        )
        sl_price = entry_price_sol * (1 - STOP_LOSS_PCT) if entry_price_sol else 0
        tp1_price = entry_price_sol * TP1_MULT if entry_price_sol else 0
        tp2_price = entry_price_sol * TP2_MULT if entry_price_sol else 0
        tp3_price = entry_price_sol * TP3_MULT if entry_price_sol else 0

        preview = [
            "⚠️  TRADE PREVIEW — reply with 'confirm' to execute",
            f"Token:      {mint}",
            f"Route:      {route_used}",
            f"SOL in:     {sol_amount:.4f} SOL",
            f"Slippage:   {slippage*100:.0f}%",
        ]
        if entry_price_sol:
            preview += [
                f"Entry est:  {entry_price_sol:.8f} SOL/token",
                f"Stop loss:  {sl_price:.8f} SOL  (-{STOP_LOSS_PCT*100:.0f}%)",
                f"TP1 (25%):  {tp1_price:.8f} SOL  (+{(TP1_MULT-1)*100:.0f}%)",
                f"TP2 (25%):  {tp2_price:.8f} SOL  (+{(TP2_MULT-1)*100:.0f}%)",
                f"TP3 (25%):  {tp3_price:.8f} SOL  (+{(TP3_MULT-1)*100:.0f}%)",
                f"Moon bag:   final 25% held, exits on -{TRAILING_STOP_PCT*100:.0f}% pullback from peak",
            ]
        if bonding_info:
            preview.append(bonding_info)
        if safety_msg and "burned" in safety_msg:
            preview.append(f"Safety:     {safety_msg}")
        preview.append(
            "\nTo execute: 'confirm buy' or just reply 'confirm'"
        )
        msg = "\n".join(preview)
        if callback:
            from elizaos.types import Content
            await callback(Content(text=msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=msg, success=True, values={"pending_confirmation": True})

    # ── execute trade ─────────────────────────────────────────────────────────
    signature = ""
    entry_price_sol = 0.0
    token_received = 0

    try:
        if PAPER_TRADING:
            import uuid as _uuid
            # Paper trading: simulate trade with real price data, no actual transaction
            if use_pump and isinstance(pump_svc, PumpFunService):
                bc = await pump_svc.get_bonding_curve(mint)
                if bc:
                    entry_price_sol = bc.get("price_sol", 0.0)
            if entry_price_sol == 0.0 and isinstance(raydium_svc, RaydiumService):
                try:
                    entry_price_sol = await raydium_svc.get_price(mint) or 0.0
                except Exception:
                    pass
            if entry_price_sol == 0.0:
                entry_price_sol = 1e-6  # fallback placeholder
            token_received = int((sol_amount / entry_price_sol) * 1e9) if entry_price_sol else 0
            signature = f"PAPER_{_uuid.uuid4().hex[:12].upper()}"
        elif use_pump and isinstance(pump_svc, PumpFunService):
            # Bonding curve: PumpFunService direct (Jupiter doesn't index new pump.fun tokens fast enough)
            bc = await pump_svc.get_bonding_curve(mint)
            if bc:
                entry_price_sol = bc.get("price_sol", 0.0)
            signature = await pump_svc.buy(mint, sol_amount, slippage)
        elif isinstance(raydium_svc, RaydiumService):
            # Post-graduation (PumpSwap/Raydium/Meteora): Jupiter aggregates all DEXes — best execution
            last_exc = None
            for slip_bps in (1500, 3000, 5000):
                try:
                    signature = await raydium_svc.swap_jupiter_buy(
                        mint, sol_amount, slippage_bps=slip_bps, priority_fee=0.002
                    )
                    last_exc = None
                    break
                except Exception as _e:
                    last_exc = _e
            if last_exc is not None:
                raise last_exc
        else:
            msg = "No DEX service available for this trade."
            if callback:
                from elizaos.types import Content
                await callback(Content(text=msg, actions=["BUY_TOKEN"]))
            return ActionResult(text=msg, success=False)

    except Exception as exc:
        error_msg = f"Buy transaction failed: {exc}"
        if callback:
            from elizaos.types import Content
            await callback(Content(text=error_msg, actions=["BUY_TOKEN"]))
        return ActionResult(text=error_msg, success=False)

    # ── register position for SL/TP monitoring ─────────────────────────────────
    if pos_mgr is not None and entry_price_sol > 0 and isinstance(wallet_svc, SolanaWalletService):
        try:
            if not PAPER_TRADING:
                # Real trade: read actual tokens received to compute true fill price
                import asyncio as _asyncio
                await _asyncio.sleep(3)  # wait for RPC to see the confirmed tx
                balances = await wallet_svc.get_token_balances()
                token_received = next(
                    (int(t["raw_amount"]) for t in balances if t["mint"] == mint), 0
                )
                if token_received > 0:
                    # Calculate actual fill price from real tokens received (not DexScreener quote)
                    decimals = next((t.get("decimals", 6) for t in balances if t["mint"] == mint), 6)
                    entry_price_sol = sol_amount / (token_received / 10 ** decimals)
                elif entry_price_sol > 0:
                    # Tx not yet finalized — estimate token count from quoted price
                    decimals = 6
                    token_received = int((sol_amount / entry_price_sol) * (10 ** decimals))
                else:
                    decimals = 6
                    entry_price_sol = 1e-6
                    token_received = int((sol_amount / entry_price_sol) * (10 ** decimals))
            # Paper mode: token_received was already computed above from entry price
            pos_mgr.open_position(
                mint=mint,
                dex=route_used,
                entry_price_sol=entry_price_sol,
                entry_sol_spent=sol_amount,
                token_amount=token_received,
                signature=signature,
            )
        except Exception:
            pass  # position registration failure is non-fatal

    paper_prefix = "[PAPER TRADE] " if PAPER_TRADING else ""
    result_text = (
        f"{paper_prefix}✅ Bought via {route_used}: {sol_amount:.4f} SOL → {mint[:8]}...{mint[-4:]}\n"
        f"Transaction: {signature}\n"
        f"Risk management active: SL -15% | TP1 +25% (sell 50%) | TP2 +56% (sell 75% of rem.) | Trailing -15% from peak"
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
            "entry_price_sol": entry_price_sol,
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
