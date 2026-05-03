"""TokenLaunchMonitorService — real-time pump.fun launch monitoring via WebSocket.

Primary:  Helius LaserStream (wss://atlas-mainnet.helius-rpc.com) — transactionSubscribe
          provides full parsed transaction data, so no extra getTransaction call is needed.
          Detects new token launches with sub-second latency.

Fallback: Standard Solana logsSubscribe — used if LaserStream is unavailable.
          Requires a follow-up getTransaction call (adds 0-6s delay).
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp

from elizaos.types import Service

from elizaos.plugins.solana.constants import PUMP_FUN_PROGRAM_ID

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime

# Custom service type not in ServiceTypeRegistry
TOKEN_MONITOR_SERVICE_TYPE = "token_monitor"

# Helius LaserStream endpoint — lower latency than standard Helius RPC WebSocket
_LASERSTREAM_ENDPOINT = "wss://atlas-mainnet.helius-rpc.com"


class TokenLaunchMonitorService(Service):
    service_type = TOKEN_MONITOR_SERVICE_TYPE

    @property
    def capability_description(self) -> str:
        return "Real-time pump.fun token launch monitoring via Helius LaserStream WebSocket."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._launches: deque[dict[str, Any]] = deque(maxlen=100)
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> TokenLaunchMonitorService:
        service = cls()
        service._runtime = runtime
        # LaserStream as primary; _ws_loop is the fallback inside _laserstream_loop
        service._task = asyncio.create_task(service._laserstream_loop())
        runtime.logger.info(
            "TokenLaunchMonitorService started — Helius LaserStream primary, logsSubscribe fallback",
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

    # ------------------------------------------------ LaserStream primary loop

    async def _laserstream_loop(self) -> None:
        """Reconnecting Helius LaserStream loop.

        Subscribes to pump.fun program transactions via transactionSubscribe.
        Full parsed account keys are available immediately — no follow-up
        getTransaction call needed, saving 0-6 seconds per launch detection.

        Falls back to standard logsSubscribe (_ws_loop) if the API key is
        missing or LaserStream fails to connect after initial retries.
        """
        api_key = os.getenv("HELIUS_API_KEY", "")
        if not api_key:
            if self._runtime:
                self._runtime.logger.warning(
                    "HELIUS_API_KEY not set — falling back to standard logsSubscribe",
                    src="service:token_monitor",
                )
            await self._ws_loop()
            return

        url = f"{_LASERSTREAM_ENDPOINT}/?api-key={api_key}"
        subscribe_msg = _json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {
                    "failed": False,
                    "mentionsAccountOrProgram": PUMP_FUN_PROGRAM_ID,
                },
                {
                    "commitment": "processed",
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "showRewards": False,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        })

        backoff = 1.0
        max_backoff = 30.0
        connect_attempts = 0

        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        url,
                        heartbeat=30,
                        timeout=aiohttp.ClientWSTimeout(ws_close=15.0),
                    ) as ws:
                        await ws.send_str(subscribe_msg)
                        connect_attempts = 0
                        backoff = 1.0
                        if self._runtime:
                            self._runtime.logger.info(
                                "LaserStream connected — streaming pump.fun transactions in real-time",
                                src="service:token_monitor",
                            )

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = _json.loads(msg.data)
                                    if data.get("method") == "transactionNotification":
                                        asyncio.create_task(
                                            self._handle_laserstream(data)
                                        )
                                except Exception:
                                    pass
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connect_attempts += 1
                if self._runtime:
                    self._runtime.logger.warning(
                        f"LaserStream error (attempt {connect_attempts}): {exc} — retrying in {backoff}s",
                        src="service:token_monitor",
                    )

            # After 3 failed attempts fall back to standard logsSubscribe
            if connect_attempts >= 3:
                if self._runtime:
                    self._runtime.logger.warning(
                        "LaserStream unreachable after 3 attempts — falling back to standard logsSubscribe",
                        src="service:token_monitor",
                    )
                await self._ws_loop()
                return

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _handle_laserstream(self, data: dict[str, Any]) -> None:
        """Process a LaserStream transactionNotification.

        Full parsed transaction data is available immediately — extract mint and
        creator wallet directly without a follow-up getTransaction call.
        """
        result = data.get("params", {}).get("result", {})
        signature = result.get("signature", "")
        if not signature:
            return

        tx_envelope = result.get("transaction", {})
        meta = tx_envelope.get("meta", {})

        # Skip failed transactions
        if meta.get("err") is not None:
            return

        log_messages: list[str] = meta.get("logMessages", [])
        is_create = any("Instruction: Create" in line for line in log_messages)
        if not is_create:
            return

        # Account keys — available immediately in the LaserStream payload
        tx_inner = tx_envelope.get("transaction", {})
        msg_data = tx_inner.get("message", {})
        account_keys = msg_data.get("accountKeys", [])

        all_keys: list[str] = []
        for a in account_keys:
            if isinstance(a, dict):
                all_keys.append(a.get("pubkey", ""))
            else:
                all_keys.append(str(a))

        # On pump.fun Create: accounts[0] = creator wallet, accounts[1] = new token mint
        creator_wallet = all_keys[0] if len(all_keys) > 0 else ""
        mint = all_keys[1] if len(all_keys) > 1 else ""

        if not mint or len(mint) < 32:
            return

        await self._process_launch(mint, creator_wallet, signature)

    # ---------------------------------------- Standard logsSubscribe fallback

    async def _ws_loop(self) -> None:
        """Reconnecting logsSubscribe fallback loop — used when LaserStream is unavailable."""
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
                    await asyncio.sleep(5)
                    continue

                rpc = wallet_svc.rpc  # type: ignore[attr-defined]
                await rpc.subscribe_logs(PUMP_FUN_PROGRAM_ID, self._handle_log)
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
        """Process a logsSubscribe notification — requires follow-up getTransaction call."""
        logs: list[str] = []
        value = log_data.get("value", log_data)
        if isinstance(value, dict):
            logs = value.get("logs", [])

        # NOTE: "Instruction: Withdraw" on the pump.fun program is NOT the PumpSwap
        # migration instruction. Graduation detection is handled by the pump.fun
        # API polling loop in graduation_snipe_loop (run_traderbot.py).
        is_create = any("Instruction: Create" in line for line in logs)
        if not is_create:
            return

        signature = value.get("signature", "")
        if not signature:
            return

        # logsSubscribe does NOT include accountKeys — fetch the transaction.
        # The RPC indexer lags the WebSocket notification by a few seconds, so we retry.
        mint = ""
        creator_wallet = ""
        try:
            wallet_svc = (
                self._runtime.get_service("wallet")  # type: ignore[union-attr]
                if self._runtime else None
            )
            if wallet_svc is not None and hasattr(wallet_svc, "rpc"):
                rpc = wallet_svc.rpc  # type: ignore[attr-defined]
                tx = None
                for attempt in range(3):
                    if attempt > 0:
                        await asyncio.sleep(2.0)
                    tx = await rpc.get_transaction(signature)
                    if tx is not None:
                        break
                if tx:
                    accounts = (
                        tx.get("transaction", {})
                        .get("message", {})
                        .get("accountKeys", [])
                    )
                    all_keys = [
                        (a.get("pubkey", "") if isinstance(a, dict) else str(a))
                        for a in accounts
                    ]
                    if len(all_keys) > 0:
                        creator_wallet = all_keys[0]
                    if len(all_keys) > 1:
                        mint = all_keys[1]
        except Exception:
            pass

        if not mint or len(mint) < 32:
            return

        await self._process_launch(mint, creator_wallet, signature)

    # -------------------------------------------------- common launch handler

    async def _process_launch(
        self,
        mint: str,
        creator_wallet: str,
        signature: str,
    ) -> None:
        """Fetch bonding curve + social metadata, emit token_launch event."""
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

        # Fetch token social metadata via on-chain Token-2022 inline metadata + IPFS.
        # pump.fun tokens use Token-2022 with an inline metadataPointer extension that
        # stores name/symbol/URI directly on the mint account. The URI points to an
        # IPFS JSON object that contains website, twitter, and telegram fields supplied
        # by the creator at deployment time.
        pf_meta: dict[str, Any] = {}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6.0)
            ) as _sess_tm:
                # Step 1: resolve URI from on-chain Token-2022 metadata
                _metadata_uri = ""
                try:
                    wallet_svc_tm = (
                        self._runtime.get_service("wallet")  # type: ignore[union-attr]
                        if self._runtime else None
                    )
                    if wallet_svc_tm is not None and hasattr(wallet_svc_tm, "rpc"):
                        _rpc_tm = wallet_svc_tm.rpc  # type: ignore[attr-defined]
                        _acct = await _rpc_tm.get_account_info(mint, encoding="jsonParsed")
                        if _acct:
                            _exts = (
                                _acct.get("data", {})
                                .get("parsed", {})
                                .get("info", {})
                                .get("extensions", [])
                            )
                            for _ext in _exts:
                                if _ext.get("extension") == "tokenMetadata":
                                    _uri = _ext.get("state", {}).get("uri", "")
                                    if _uri:
                                        _metadata_uri = _uri
                                        pf_meta["name"]   = _ext.get("state", {}).get("name", "")
                                        pf_meta["symbol"] = _ext.get("state", {}).get("symbol", "")
                                    break
                except Exception:
                    pass

                # Step 2: fetch the IPFS metadata JSON for website/twitter/telegram
                if _metadata_uri:
                    _ipfs_cid = (
                        _metadata_uri.split("/ipfs/")[-1].split("?")[0]
                        if "/ipfs/" in _metadata_uri
                        else ""
                    )
                    _ipfs_urls = (
                        [f"https://ipfs.io/ipfs/{_ipfs_cid}"]
                        if _ipfs_cid
                        else [_metadata_uri]
                    )
                    for _ipfs_url in _ipfs_urls:
                        try:
                            async with _sess_tm.get(
                                _ipfs_url,
                                headers={"Accept": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=4.0),
                            ) as _rj:
                                if _rj.status == 200:
                                    _jmeta = await _rj.json(content_type=None)
                                    pf_meta.setdefault("name",        _jmeta.get("name", ""))
                                    pf_meta.setdefault("symbol",      _jmeta.get("symbol", ""))
                                    pf_meta["description"] = _jmeta.get("description", "")
                                    pf_meta["website"]     = _jmeta.get("website", "")
                                    pf_meta["twitter"]     = _jmeta.get("twitter", "")
                                    pf_meta["telegram"]    = _jmeta.get("telegram", "")
                                    break
                        except Exception:
                            continue
        except Exception:
            pass

        launch_entry: dict[str, Any] = {
            "mint": mint,
            "creator_wallet": creator_wallet,
            "signature": signature,
            "timestamp": time.time(),
            "price_sol": bonding_data.get("price_sol", 0.0),
            "progress_pct": bonding_data.get("progress_pct", 0.0),
            "complete": bonding_data.get("complete", False),
            "_source": "laserstream",
            "_age_known": True,
            # Pump.fun creator-supplied social metadata
            "pf_name": pf_meta.get("name", ""),
            "pf_symbol": pf_meta.get("symbol", ""),
            "pf_description": pf_meta.get("description", ""),
            "pf_website": pf_meta.get("website", ""),
            "pf_twitter": pf_meta.get("twitter", ""),
            "pf_telegram": pf_meta.get("telegram", ""),
        }
        self._launches.append(launch_entry)

        if self._runtime:
            try:
                await self._runtime.emit_event("token_launch", launch_entry)
            except Exception:
                pass
