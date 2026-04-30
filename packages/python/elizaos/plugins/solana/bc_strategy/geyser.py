"""Yellowstone Geyser gRPC subscriber — STUB.

The pre-graduation lane lives or dies on data freshness. DexScreener REST
polling lags real on-chain state by 30-90 seconds; that's terminal for any
strategy entering on bonding-curve progress. This module subscribes to
Helius's Yellowstone Geyser gRPC stream and emits per-mint events into
state.BcMintState as they happen.

What this module owns when implemented:
  - gRPC connection management (reconnect on drop, heartbeat)
  - Filter subscription for the pump.fun program (BC_PUMP_FUN_PROGRAM)
  - Decoding pump.fun BC `buy` and `sell` instructions:
      * buy event → append WalletBuy to state.buys, update progress_pct
      * sell event → increment sells_count_60s
  - Decoding pump.fun `BondingCurveCreate` event for launch_ts / launch_slot
  - Detecting block-0 bundles (≥2 buys from same deployer in the launch slot)
  - Backpressure handling so a slow tick doesn't queue up unbounded events

What's NOT in this module:
  - Trading decisions (gates.py owns that)
  - Position management (loop.py + exits.py)
  - Tx submission (tx_builder.py)

Recommended Python dep: `grpcio` + `grpcio-tools` for the proto bindings.
Helius publishes their .proto definitions at the Yellowstone repo. Vendor
the generated Python stubs into this directory to avoid runtime codegen.
"""

from __future__ import annotations

from elizaos.plugins.solana.bc_strategy.config import BC_GEYSER_GRPC_URL


async def run_geyser_loop() -> None:
    """Long-lived coroutine, one per process. Awaits gRPC events and
    mutates state in place. Started by loop.py when BC strategy enabled."""
    raise NotImplementedError(
        "BC Geyser subscriber not yet built. When ready: subscribe to "
        f"{BC_GEYSER_GRPC_URL or '<unset>'} with a filter on the pump.fun "
        "program, decode BondingCurveCreate / buy / sell events, and update "
        "elizaos.plugins.solana.bc_strategy.state via get_or_create / drop."
    )
