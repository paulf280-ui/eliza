"""Launches provider — recent pump.fun token launches from the monitor buffer."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from elizaos.types import Provider, ProviderResult

from elizaos.plugins.solana.services.token_monitor import TOKEN_MONITOR_SERVICE_TYPE

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime, Memory, State


def _time_ago(ts: float) -> str:
    elapsed = time.time() - ts
    if elapsed < 60:
        return f"{int(elapsed)}s ago"
    if elapsed < 3600:
        return f"{int(elapsed / 60)}m ago"
    return f"{int(elapsed / 3600)}h ago"


async def _get_recent_launches(
    runtime: IAgentRuntime, _message: Memory, _state: State | None = None
) -> ProviderResult:
    from elizaos.plugins.solana.services.token_monitor import TokenLaunchMonitorService

    monitor_svc = runtime.get_service(TOKEN_MONITOR_SERVICE_TYPE)
    if not isinstance(monitor_svc, TokenLaunchMonitorService):
        return ProviderResult(
            text="Token launch monitor service not available.",
            values={},
            data={},
        )

    launches = monitor_svc.get_recent_launches(10)

    if not launches:
        return ProviderResult(
            text="# Recent pump.fun Launches\nNo launches detected yet.",
            values={"count": 0},
            data={"launches": []},
        )

    lines = ["# Recent pump.fun Launches (last detected)"]
    for launch in launches:
        mint = launch.get("mint", "?")
        ts = launch.get("timestamp", 0.0)
        price_sol = launch.get("price_sol", 0.0)
        progress = launch.get("progress_pct", 0.0)
        age = _time_ago(ts) if ts else "unknown"
        lines.append(
            f"- {mint[:8]}...  launched {age}, "
            f"price {price_sol:.8f} SOL, {progress:.1f}% bonded"
        )

    return ProviderResult(
        text="\n".join(lines),
        values={"count": len(launches)},
        data={"launches": launches},
    )


launches_provider = Provider(
    name="pump_fun_launches",
    description="Recent pump.fun token launches detected via real-time WebSocket monitoring.",
    get=_get_recent_launches,
    dynamic=True,
)
