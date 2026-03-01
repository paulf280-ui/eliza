"""Solana JSON-RPC and WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class SolanaRpcClient:
    """Single shared aiohttp session for all Solana RPC calls."""

    def __init__(self, rpc_url: str, ws_url: str | None = None) -> None:
        self._rpc_url = rpc_url
        self._ws_url = ws_url
        self._session: aiohttp.ClientSession | None = None
        self._req_id = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        async with session.post(self._rpc_url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error {data['error']['code']}: {data['error']['message']}")
        return data.get("result")

    async def get_balance(self, pubkey: str) -> int:
        """Returns balance in lamports."""
        result = await self._rpc("getBalance", [pubkey, {"commitment": "confirmed"}])
        return result.get("value", 0) if isinstance(result, dict) else result

    async def get_account_info(
        self, pubkey: str, encoding: str = "base64"
    ) -> dict[str, Any] | None:
        result = await self._rpc(
            "getAccountInfo", [pubkey, {"encoding": encoding, "commitment": "confirmed"}]
        )
        return result.get("value") if isinstance(result, dict) else None

    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict[str, Any] | None]:
        result = await self._rpc(
            "getMultipleAccounts",
            [pubkeys, {"encoding": encoding, "commitment": "confirmed"}],
        )
        return result.get("value", []) if isinstance(result, dict) else []

    async def get_program_accounts(
        self,
        program_id: str,
        filters: list[dict[str, Any]] | None = None,
        encoding: str = "base64",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"encoding": encoding, "commitment": "confirmed"}
        if filters:
            params["filters"] = filters
        return await self._rpc("getProgramAccounts", [program_id, params]) or []

    async def get_token_accounts_by_owner(
        self,
        owner: str,
        mint: str | None = None,
        program_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if mint:
            token_filter: dict[str, Any] = {"mint": mint}
        else:
            from elizaos.plugins.solana.constants import TOKEN_PROGRAM

            token_filter = {"programId": program_id or TOKEN_PROGRAM}
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [owner, token_filter, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        return result.get("value", []) if isinstance(result, dict) else []

    async def get_latest_blockhash(self) -> tuple[str, int]:
        """Returns (blockhash_str, last_valid_slot)."""
        result = await self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = result.get("value", {}) if isinstance(result, dict) else {}
        return value.get("blockhash", ""), value.get("lastValidBlockHeight", 0)

    async def send_transaction(
        self, tx_bytes: bytes | str, encoding: str = "base64"
    ) -> str:
        """Returns transaction signature."""
        if isinstance(tx_bytes, bytes):
            import base64

            tx_data = base64.b64encode(tx_bytes).decode()
        else:
            tx_data = tx_bytes
        result = await self._rpc(
            "sendTransaction",
            [tx_data, {"encoding": encoding, "preflightCommitment": "confirmed"}],
        )
        return str(result)

    async def simulate_transaction(
        self, tx_bytes: bytes | str, encoding: str = "base64"
    ) -> dict[str, Any]:
        if isinstance(tx_bytes, bytes):
            import base64

            tx_data = base64.b64encode(tx_bytes).decode()
        else:
            tx_data = tx_bytes
        result = await self._rpc(
            "simulateTransaction",
            [tx_data, {"encoding": encoding, "commitment": "confirmed"}],
        )
        return result.get("value", {}) if isinstance(result, dict) else {}

    async def subscribe_logs(
        self,
        program_id: str,
        callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Opens a WebSocket subscription and calls callback for each log notification.

        Runs until cancelled. Does NOT reconnect — callers should wrap in a retry loop.
        """
        if not self._ws_url:
            raise ValueError("WebSocket URL not configured (SOLANA_WS_URL missing)")

        session = await self._get_session()
        sub_id: int | None = None

        async with session.ws_connect(self._ws_url) as ws:
            subscribe_msg = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [program_id]},
                        {"commitment": "confirmed"},
                    ],
                }
            )
            await ws.send_str(subscribe_msg)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if "result" in data and sub_id is None:
                        sub_id = data["result"]
                    elif data.get("method") == "logsNotification":
                        log_data = data.get("params", {}).get("result", {})
                        await callback(log_data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
