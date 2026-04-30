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

    def __init__(
        self,
        rpc_url: str,
        ws_url: str | None = None,
        sender_url: str | None = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._ws_url = ws_url
        # Optional dual-submission target for sendTransaction. When set, every
        # tx submission fires at this URL in parallel with the primary RPC,
        # taking whichever signature returns first. Used to broadcast through
        # Helius Sender (sender.helius-rpc.com/fast) which dual-routes to
        # staked validators + Jito for faster inclusion under congestion.
        # Other RPC methods (getBalance, getTransaction, etc) ignore this.
        self._sender_url = sender_url
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

    async def _rpc_at(self, url: str, method: str, params: list[Any]) -> Any:
        """Like _rpc but targets an explicit URL. Used for Sender dual-submit."""
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        async with session.post(url, json=payload) as resp:
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
            "getAccountInfo", [pubkey, {"encoding": encoding, "commitment": "processed"}]
        )
        return result.get("value") if isinstance(result, dict) else None

    async def get_multiple_accounts(
        self, pubkeys: list[str], encoding: str = "base64"
    ) -> list[dict[str, Any] | None]:
        result = await self._rpc(
            "getMultipleAccounts",
            [pubkeys, {"encoding": encoding, "commitment": "processed"}],
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

    async def get_token_balance(self, owner: str, mint: str) -> float:
        """Return the UI token balance for a specific mint in `owner`'s wallet.

        Returns 0.0 if the token account doesn't exist or has zero balance.
        Used for post-sell phantom sell detection: if sell reported success but
        balance is still >0 the tx was a phantom and we need to retry.
        """
        accounts = await self.get_token_accounts_by_owner(owner, mint=mint)
        for acc in accounts:
            info = (acc.get("account") or {}).get("data", {}).get("parsed", {}).get("info", {})
            ui = info.get("tokenAmount", {}).get("uiAmount") or 0
            if ui and ui > 0:
                return float(ui)
        return 0.0

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
        self, tx_bytes: bytes | str, encoding: str = "base64", skip_preflight: bool = True
    ) -> str:
        """Submit a signed transaction. Returns transaction signature.

        skip_preflight=True (default): bypasses simulation so we always attempt
        submission even for state-sensitive txs (e.g. bonding curve buys where
        simulation might use slightly stale state). Failed txs still cost priority
        fee but don't consume significant funds.

        When _sender_url is set, the same payload is fired at the primary RPC
        AND the Sender endpoint in parallel; whichever returns a valid sig
        first wins. Same tx submitted twice yields the same sig (idempotent),
        so a duplicate is harmless. Sender broadcasts to staked-validator
        connections + Jito which can land 1-3 slots earlier under congestion.
        """
        if isinstance(tx_bytes, bytes):
            import base64

            tx_data = base64.b64encode(tx_bytes).decode()
        else:
            tx_data = tx_bytes
        params = [tx_data, {
            "encoding": encoding,
            "skipPreflight": skip_preflight,
            "preflightCommitment": "processed",  # fastest — skip heavy confirmed simulation
            "maxRetries": 0,  # fire-and-forget; bot polls confirmation itself — avoids RPC retry overhead
        }]
        if self._sender_url:
            # Fan-out to primary + Sender. Take first non-error result.
            primary = asyncio.create_task(self._rpc("sendTransaction", params))
            sender  = asyncio.create_task(self._rpc_at(self._sender_url, "sendTransaction", params))
            done, pending = await asyncio.wait(
                {primary, sender}, return_when=asyncio.FIRST_COMPLETED
            )
            sig: str | None = None
            first_err: Exception | None = None
            for t in done:
                try:
                    sig = str(t.result())
                    break
                except Exception as exc:
                    first_err = exc
            if sig is None:
                # First-completed task failed; await the other.
                for t in pending:
                    try:
                        sig = str(await t)
                        break
                    except Exception as exc:
                        first_err = exc
                if sig is None:
                    raise first_err or RuntimeError("Both RPC and Sender failed")
            else:
                # Cancel pending; loser's tx will land on its own if it does.
                for t in pending:
                    t.cancel()
            return sig
        result = await self._rpc("sendTransaction", params)
        return str(result)

    async def simulate_transaction(
        self,
        tx_bytes: bytes | str,
        encoding: str = "base64",
        sig_verify: bool = False,
        replace_recent_blockhash: bool = True,
    ) -> dict[str, Any]:
        if isinstance(tx_bytes, bytes):
            import base64

            tx_data = base64.b64encode(tx_bytes).decode()
        else:
            tx_data = tx_bytes
        result = await self._rpc(
            "simulateTransaction",
            [tx_data, {
                "encoding": encoding,
                "commitment": "confirmed",
                "sigVerify": sig_verify,
                "replaceRecentBlockhash": replace_recent_blockhash,
            }],
        )
        return result.get("value", {}) if isinstance(result, dict) else {}

    async def get_transaction(
        self, signature: str, encoding: str = "jsonParsed", commitment: str = "confirmed"
    ) -> dict[str, Any] | None:
        """Fetch a confirmed transaction by its signature."""
        result = await self._rpc(
            "getTransaction",
            [signature, {"encoding": encoding, "maxSupportedTransactionVersion": 0, "commitment": commitment}],
        )
        return result  # already the tx object (or None if not found)

    async def confirm_transaction(
        self, signature: str, timeout: float = 20.0, poll_interval: float = 0.75
    ) -> None:
        """Poll until a submitted transaction is confirmed on-chain.

        Raises RuntimeError if the transaction is not seen within `timeout` seconds,
        or if it lands but carries an on-chain error (e.g. slippage exceeded, bad account).

        Call this immediately after send_transaction so callers know whether the swap
        actually succeeded before logging a trade or updating position state.
        """
        import asyncio as _asyncio

        deadline = _asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - _asyncio.get_event_loop().time()
            if remaining <= 0:
                raise RuntimeError(
                    f"Transaction {signature[:20]}... not confirmed within {timeout:.0f}s (dropped or very slow)"
                )
            try:
                tx = await self.get_transaction(signature, encoding="json")
            except Exception:
                tx = None

            if tx is not None:
                err = (tx.get("meta") or {}).get("err")
                if err is not None:
                    raise RuntimeError(
                        f"Transaction {signature[:20]}... failed on-chain: {err}"
                    )
                return  # confirmed with no error

            await _asyncio.sleep(poll_interval)

    async def get_token_largest_accounts(self, mint: str) -> list[dict]:
        """Return up to 20 largest token accounts for a mint.

        Each entry: {"address": str, "amount": str, "decimals": int, "uiAmount": float}
        The bonding curve contract is typically the #1 holder until graduation.
        Use the count of non-zero entries as a lower-bound on distinct holder count.
        """
        result = await self._rpc(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
        if isinstance(result, dict):
            return result.get("value", []) or []
        return []

    async def get_recent_prioritization_fees(self) -> int:
        """Return a competitive compute unit price (microlamports/CU).

        Queries the last 150 blocks, takes the 75th-percentile fee, and
        applies a 1.5× safety multiplier.  Falls back to 50,000 µlamports
        (≈ 0.01 SOL / tx at 200k CU) if the RPC call fails.
        """
        try:
            fees: list[dict] = await self._rpc("getRecentPrioritizationFees", []) or []
            values = [
                int(f.get("prioritizationFee", 0))
                for f in fees
                if int(f.get("prioritizationFee", 0)) > 0
            ]
            if not values:
                return 50_000
            values.sort()
            p75 = values[min(int(len(values) * 0.75), len(values) - 1)]
            return min(int(p75 * 1.5), 2_000_000)  # hard cap at 2 M µlamports
        except Exception:
            return 50_000  # safe fallback

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

        async with session.ws_connect(self._ws_url, heartbeat=30) as ws:
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
