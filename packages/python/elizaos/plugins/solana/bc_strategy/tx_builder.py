"""Custom pump.fun bonding-curve transaction builder + Sender submitter — STUB.

The post-graduation monster lane uses PumpPortal's `trade-local` API which
abstracts away tx construction. The BC lane needs end-to-end control so we
can splice in a Sender tip-account transfer for full dual-routing priority.

What this module owns when implemented:
  - build_bc_buy_ix(mint, sol_amount, max_sol_cost) → solders.Instruction
      Uses PUMP_FUN_BUY_DISCRIMINATOR + the 17-account layout pump.fun v2
      requires (per-mint PDAs derivable from program seeds). Token-2022
      paths must derive ATAs through ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL.
  - build_bc_sell_ix(mint, token_amount, min_sol_received) → Instruction
      Symmetric to buy. PUMP_FUN_SELL_DISCRIMINATOR.
  - assemble_tx(ixs, payer, recent_blockhash, jito_tip, sender_tip) → VersionedTransaction
      Includes:
        * ComputeBudget set_compute_unit_price(microlamports)  — Jito tip
        * SystemProgram.transfer(payer, jito_tip_account, jito_tip_lamports)
        * SystemProgram.transfer(payer, sender_tip_account_random, sender_tip_lamports)
        * <user instructions>
      Sender's tip-transfer is the key new piece vs the monster lane —
      gets us full priority on the staked-validator broadcast leg.
  - submit_bc_tx(signed_bytes) → sig
      POSTs to BOTH SOLANA_RPC_URL and SOLANA_SENDER_URL in parallel,
      same as SolanaRpcClient.send_transaction does today. The Sender
      tip transfer in the tx unlocks Sender's full priority handling.

References:
  - constants.PUMP_FUN_BUY_DISCRIMINATOR / PUMP_FUN_SELL_DISCRIMINATOR
  - constants.PUMP_FUN_FEE_RECIPIENT, RENT_SYSVAR, SYSTEM_PROGRAM
  - constants.ASSOCIATED_TOKEN_PROGRAM, COMPUTE_BUDGET_PROGRAM
  - bc_strategy.config.BC_SENDER_TIP_ACCOUNTS — pick one at random per-tx
"""

from __future__ import annotations

from typing import Any


async def build_and_submit_buy(mint: str, sol_amount: float) -> str:
    raise NotImplementedError(
        "BC tx builder stub. See module docstring for the implementation "
        "blueprint. Discriminators and program ID constants are already in "
        "elizaos.plugins.solana.constants."
    )


async def build_and_submit_sell(mint: str, token_amount: int) -> str:
    raise NotImplementedError("BC sell tx builder stub")


def pick_sender_tip_account() -> str:
    """Pick one of the 11 Sender tip accounts at random. Distributing
    across accounts avoids any single account becoming a hotspot during
    high-volume periods."""
    import random

    from elizaos.plugins.solana.bc_strategy.config import BC_SENDER_TIP_ACCOUNTS
    return random.choice(BC_SENDER_TIP_ACCOUNTS)
