"""TokenLaunchMonitorService — real-time pump.fun launch monitoring via WebSocket."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from elizaos.types import Service

from elizaos.plugins.solana.constants import PUMP_FUN_PROGRAM_ID

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime

# Custom service type not in ServiceTypeRegistry
TOKEN_MONITOR_SERVICE_TYPE = "token_monitor"


class TokenLaunchMonitorService(Service):
    service_type = TOKEN_MONITOR_SERVICE_TYPE

    @property
    def capability_description(self) -> str:
        return "Real-time pump.fun token launch monitoring via WebSocket log subscription."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._launches: deque[dict[str, Any]] = deque(maxlen=100)
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> TokenLaunchMonitorService:
        service = cls()
        service._runtime = runtime
        service._task = asyncio.create_task(service._ws_loop())
        runtime.logger.info(
            "TokenLaunchMonitorService started — listening for pump.fun launches",
            src="service:token_monitor",
            agentId=str(runtime.agent_id),
        )
        return service

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ---------------------------------------------------------------- public

    def get_recent_launches(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest-first list of recent launches (up to limit)."""
        items = list(self._launches)
        items.reverse()  # newest first
        return items[:limit]

    # ---------------------------------------------------- background WS loop

    async def _ws_loop(self) -> None:
        """Reconnecting WebSocket loop — exponential backoff on disconnect."""
        from elizaos.types import ServiceTypeRegistry

        backoff = 1.0
        max_backoff = 30.0

        while True:
            try:
                wallet_svc = (
                    self._runtime.get_service(ServiceTypeRegistry.WALLET)  # type: ignore[union-attr]
                    if self._runtime
                    else None
                )
                if wallet_svc is None:
                    # Wait for wallet service to be ready
                    await asyncio.sleep(5)
                    continue

                rpc = wallet_svc.rpc  # type: ignore[attr-defined]
                await rpc.subscribe_logs(PUMP_FUN_PROGRAM_ID, self._handle_log)
                # If subscribe_logs returns, reset backoff
                backoff = 1.0

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"TokenLaunchMonitorService WS error: {exc} — reconnecting in {backoff}s",
                        src="service:token_monitor",
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _handle_log(self, log_data: dict[str, Any]) -> None:
        """Process a log notification from the pump.fun program."""
        logs: list[str] = []
        value = log_data.get("value", log_data)
        if isinstance(value, dict):
            logs = value.get("logs", [])

        # Look for the Create instruction signature in the logs
        is_create = any(
            "InitializeMint2" in line or "Program log: Instruction: Create" in line
            for line in logs
        )
        if not is_create:
            return

        # Try to extract the mint address from the transaction accounts
        # The signature comes from the transaction context
        signature = value.get("signature", "")
        accounts = value.get("accountKeys", [])

        # On pump.fun, the mint is typically the 2nd account (index 1)
        mint = ""
        if len(accounts) > 1:
            mint = accounts[1] if isinstance(accounts[1], str) else ""

        if not mint:
            return

        # Fetch bonding curve data for initial price
        bonding_data: dict[str, Any] = {}
        try:
            from elizaos.types import ServiceTypeRegistry

            pump_svc = (
                self._runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)  # type: ignore[union-attr]
                if self._runtime
                else None
            )
            if pump_svc is not None and hasattr(pump_svc, "get_bonding_curve"):
                bonding_data = await pump_svc.get_bonding_curve(mint)  # type: ignore[attr-defined]
        except Exception:
            pass

        launch_entry: dict[str, Any] = {
            "mint": mint,
            "signature": signature,
            "timestamp": time.time(),
            "price_sol": bonding_data.get("price_sol", 0.0),
            "progress_pct": bonding_data.get("progress_pct", 0.0),
            "complete": bonding_data.get("complete", False),
        }
        self._launches.append(launch_entry)

        if self._runtime:
            try:
                await self._runtime.emit_event("token_launch", launch_entry)
            except Exception:
                pass
