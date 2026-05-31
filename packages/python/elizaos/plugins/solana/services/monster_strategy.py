"""MonsterStrategyService — orchestrates Strategy E (monster token trading).

Starts four concurrent background tasks:
  1. cluster_confirm_scout_loop    (monster_signals)
  2. serial_deployer_sniper_loop   (monster_signals)
  3. lifecycle_scout_loop          (monster_signals)
  4. monitor_positions_loop        (strategy_e_monster)
  5. monster_social_monitor_loop   (monster_social_monitor) — opt-in

Gated by MONSTER_STRATEGY_ENABLED=true at the top level. Each sub-scout has
its own env flag. All defaults disabled / paper-only so this code is SAFE
to merge without surprising the live bot.

The service runs COMPLETELY INDEPENDENTLY from the copy-trade strategy
(axiom_copy_trader). It has its own position pool, own state files, and
explicitly does NOT consult traded_mints.json.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, ClassVar

import aiohttp

from elizaos.types import Service

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime


def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


MONSTER_STRATEGY_ENABLED = _env_on("MONSTER_STRATEGY_ENABLED", "false")


class MonsterStrategyService(Service):
    """Background orchestration for Strategy E."""

    service_type: ClassVar[str] = "monster_strategy"

    @property
    def capability_description(self) -> str:
        return (
            "Strategy E (monster tokens): cluster-confirm + serial-deployer + "
            "lifecycle entry scouts, event-driven exits, separate slot pool "
            "from copy-trade."
        )

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._session: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task] = []

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> MonsterStrategyService:
        svc = cls()
        svc._runtime = runtime
        if not MONSTER_STRATEGY_ENABLED:
            runtime.logger.info(
                "MonsterStrategyService not started (MONSTER_STRATEGY_ENABLED=false)",
                src="service:monster_strategy",
            )
            return svc
        svc._session = aiohttp.ClientSession()

        # Defer imports so the module loads cleanly when the strategy is off.
        from elizaos.plugins.solana import monster_signals, monster_social_monitor
        from elizaos.plugins.solana import strategy_e_monster as monster

        import os as _os_ms
        _ca_enabled      = _os_ms.getenv("CREATOR_ALPHA_ENABLED",          "true").lower()  not in ("false","0","no")
        _serial_enabled  = _os_ms.getenv("SERIAL_DEPLOYER_ENABLED",        "true").lower()  not in ("false","0","no")
        _cluster_enabled = _os_ms.getenv("MONSTER_CLUSTER_CONFIRM_ENABLED","false").lower() not in ("false","0","no")
        _breakout_enabled= _os_ms.getenv("MONSTER_BREAKOUT_ENABLED",       "false").lower() not in ("false","0","no")
        _jarvis_enabled  = _os_ms.getenv("MONSTER_JARVIS_ENABLED",         "false").lower() not in ("false","0","no")

        svc._tasks = [
            asyncio.create_task(
                monster.monitor_positions_loop(runtime, svc._session),
                name="monster_position_monitor",
            ),
            asyncio.create_task(
                monster_signals.lifecycle_scout_loop(runtime, svc._session),
                name="monster_lifecycle",
            ),
        ]
        # DexScreener backup poll — catches tokens missed by Helius webhooks
        svc._tasks.append(asyncio.create_task(
            monster_signals.dexscreener_backup_poll_loop(svc._session),
            name="monster_ds_backup",
        ))
        if _cluster_enabled:
            svc._tasks.append(asyncio.create_task(
                monster_signals.cluster_confirm_scout_loop(runtime, svc._session),
                name="monster_cluster_confirm",
            ))
        if _serial_enabled:
            svc._tasks.append(asyncio.create_task(
                monster_signals.serial_deployer_sniper_loop(runtime, svc._session),
                name="monster_serial_deployer",
            ))
        if _breakout_enabled:
            svc._tasks.append(asyncio.create_task(
                monster_signals.breakout_candle_scout_loop(runtime, svc._session),
                name="monster_breakout",
            ))
        if _jarvis_enabled:
            svc._tasks.append(asyncio.create_task(
                monster_signals.jarvis_scout_loop(runtime, svc._session),
                name="monster_jarvis",
            ))
        if _ca_enabled:
            svc._tasks.append(asyncio.create_task(
                monster_signals.creator_alpha_scout_loop(runtime, svc._session),
                name="monster_creator_alpha",
            ))
        svc._tasks.append(asyncio.create_task(
            monster_social_monitor.monster_social_monitor_loop(runtime, svc._session),
            name="monster_social_monitor",
        ))
        mode = "PAPER" if monster.MONSTER_PAPER_ONLY else "LIVE"
        runtime.logger.info(
            f"MonsterStrategyService started — mode={mode}, "
            f"cluster={monster_signals.CLUSTER_CONFIRM_ENABLED}, "
            f"serial={monster_signals.SERIAL_DEPLOYER_ENABLED}, "
            f"lifecycle={monster_signals.LIFECYCLE_SCOUT_ENABLED}, "
            f"breakout={monster_signals.BREAKOUT_SCOUT_ENABLED}, "
            f"jarvis={monster_signals.JARVIS_SCOUT_ENABLED}, "
            f"social={monster_social_monitor.SOCIAL_ENABLED}",
            src="service:monster_strategy",
        )
        return svc

    async def stop(self) -> None:
        for t in self._tasks:
            if t and not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self._session and not self._session.closed:
            await self._session.close()
