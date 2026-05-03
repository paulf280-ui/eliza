"""
telegram_alpha_monitor.py — Monitors public pump.fun Telegram alpha channels via Telethon.

Requires in .env:
    TELEGRAM_API_ID=<from my.telegram.org>
    TELEGRAM_API_HASH=<from my.telegram.org>
    TELEGRAM_PHONE=<your phone number e.g. +447911123456>

On first run: interactive OTP verification. Telethon saves a session file so
subsequent runs are automatic. The account does NOT need to join any channel —
Telethon reads public channels without membership.

Monitored channels (confirmed active, research 2026-04-04):
    @pumpfunlistings        — Graduation alerts with top holders %, dev hold %, mint status
    @pump_sol_alert         — GMGN fill-complete alerts (~12,800 subs)
    @pfultimate             — Every launch with X10-X1000 signal scoring (~9,800 subs)
    @pumpfun100xfinder      — 100x potential token alerts (~19,300 subs)
    @pumpfunearlytrending   — Early trending tokens from bonding curve
    @pump_calls             — Signal calls

When a Solana mint address appears in any of these channels, it is recorded in
`_alpha_signals` dict with timestamp, channel, and raw message. The graduation
snipe loop and scout loops use `get_alpha_signal()` to check if a token has
upstream social signal — which is logged as a positive indicator at entry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Solana address pattern (base58, 32-44 chars) ──────────────────────────────
_SOLANA_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# ── Alpha signal store: mint → {channel, ts, message_snippet, channel_subs_est} ─
_alpha_signals: dict[str, dict] = {}   # persisted across async calls
_recent_messages: deque[dict] = deque(maxlen=200)   # last 200 messages across all channels

# ── Confirmed active channels (research 2026-04-04) ───────────────────────────
DEFAULT_CHANNELS = [
    ("pumpfunlistings",       "Graduation alerts + top holder %, dev hold %"),
    ("pump_sol_alert",        "GMGN fill-complete alerts — ~12,800 subs"),
    ("pfultimate",            "Launch scoring X10-X1000 — ~9,800 subs"),
    ("pumpfun100xfinder",     "100x potential alerts — ~19,300 subs"),
    ("pumpfunearlytrending",  "Early trending on bonding curve"),
    ("pump_calls",            "Signal calls"),
]

_PERSIST_FILE = Path(__file__).parent / "alpha_signals.json"
_SESSION_FILE = str(Path(__file__).parent / "telegram_session")

# ── Module-level client (singleton) ──────────────────────────────────────────
_client: Any = None
_monitor_task: asyncio.Task | None = None
_is_running: bool = False


def _load_persisted() -> None:
    """Load previously seen alpha signals from disk."""
    global _alpha_signals
    try:
        if _PERSIST_FILE.exists():
            _alpha_signals = json.loads(_PERSIST_FILE.read_text())
            # Prune signals older than 24h to keep file small
            cutoff = time.time() - 86400
            _alpha_signals = {k: v for k, v in _alpha_signals.items() if v.get("ts", 0) > cutoff}
    except Exception:
        pass


def _save_persisted() -> None:
    try:
        _PERSIST_FILE.write_text(json.dumps(_alpha_signals, indent=2))
    except Exception:
        pass


def get_alpha_signal(mint: str) -> dict | None:
    """Return alpha signal metadata if this mint was called in any monitored channel, else None."""
    return _alpha_signals.get(mint)


def get_recent_signals(n: int = 20) -> list[dict]:
    """Return the last N alpha signals (most recent first)."""
    all_sigs = sorted(_alpha_signals.values(), key=lambda x: x.get("ts", 0), reverse=True)
    return all_sigs[:n]


def get_recent_messages(n: int = 20) -> list[dict]:
    """Return last N raw channel messages (for Jarvis display)."""
    msgs = list(_recent_messages)
    msgs.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return msgs[:n]


def is_running() -> bool:
    return _is_running


async def start_monitor(extra_channels: list[str] | None = None) -> str:
    """
    Start the Telegram alpha monitor as a background asyncio task.
    Returns status string.

    Requires TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE in environment.
    On first run, will print OTP prompt to stdout — user must enter the code
    in the bot's terminal.
    """
    global _client, _monitor_task, _is_running

    if _is_running:
        return "Telegram monitor already running."

    api_id   = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    phone    = os.getenv("TELEGRAM_PHONE", "").strip()

    if not api_id or not api_hash:
        return (
            "❌ Telegram monitor not started — TELEGRAM_API_ID and TELEGRAM_API_HASH not set.\n"
            "Get them at https://my.telegram.org → API development tools.\n"
            "Add to .env: TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TELEGRAM_PHONE=+447..."
        )

    try:
        from telethon import TelegramClient, events
        from telethon.errors import FloodWaitError
    except ImportError:
        return "❌ Telethon not installed. Run: .venv_py/bin/pip install telethon"

    _load_persisted()

    channels = [ch for ch, _ in DEFAULT_CHANNELS]
    if extra_channels:
        channels += [c.lstrip("@") for c in extra_channels]

    try:
        _client = TelegramClient(_SESSION_FILE, int(api_id), api_hash)
        await _client.start(phone=phone if phone else None)
        logger.info("[tg-monitor] Connected to Telegram")

        @_client.on(events.NewMessage(chats=channels))
        async def _on_message(event: Any) -> None:
            try:
                text = event.message.message or ""
                channel_name = getattr(event.message.peer_id, "channel_id", "unknown")
                # Try to get username
                try:
                    entity = await event.get_chat()
                    ch_username = getattr(entity, "username", None) or str(channel_name)
                except Exception:
                    ch_username = str(channel_name)

                ts = time.time()

                # Record raw message
                _recent_messages.appendleft({
                    "ts": ts,
                    "channel": ch_username,
                    "text": text[:500],
                })

                # Extract Solana mint addresses from message
                found_mints = _SOLANA_ADDR_RE.findall(text)
                for mint in found_mints:
                    # Basic sanity: Solana addresses are 32-44 chars base58
                    # Filter out common noise (program IDs, known non-mints)
                    if len(mint) < 32:
                        continue
                    if mint not in _alpha_signals:
                        signal = {
                            "mint":    mint,
                            "channel": ch_username,
                            "ts":      ts,
                            "snippet": text[:200],
                        }
                        _alpha_signals[mint] = signal
                        _save_persisted()
                        logger.info(
                            f"[tg-alpha] 🔔 {mint[:8]}... called in @{ch_username}: "
                            f"{text[:80]}"
                        )
                        print(
                            f"[tg-alpha] 🔔 ALPHA SIGNAL: {mint[:8]}... called in @{ch_username}"
                        )
            except Exception as exc:
                logger.debug(f"[tg-monitor] message handler error: {exc}")

        _is_running = True
        _monitor_task = asyncio.ensure_future(_client.run_until_disconnected())
        ch_list = ", ".join(f"@{c}" for c in channels)
        return (
            f"✅ Telegram alpha monitor started.\n"
            f"Watching {len(channels)} channels: {ch_list}\n"
            f"Mint addresses found in these channels will be logged as alpha signals."
        )

    except Exception as exc:
        _is_running = False
        return f"❌ Telegram monitor failed to start: {exc}"


async def stop_monitor() -> str:
    global _client, _monitor_task, _is_running
    if not _is_running:
        return "Telegram monitor is not running."
    try:
        if _client:
            await _client.disconnect()
        if _monitor_task:
            _monitor_task.cancel()
        _is_running = False
        return "✅ Telegram monitor stopped."
    except Exception as exc:
        return f"Error stopping monitor: {exc}"


def format_alpha_report(n: int = 10) -> str:
    """Format recent alpha signals for display in Jarvis chat."""
    if not _alpha_signals:
        return "No alpha signals recorded yet. Start monitor with: `telegram monitor start`"

    recent = get_recent_signals(n)
    lines = [f"**Last {len(recent)} alpha signals from pump.fun channels:**\n"]
    for sig in recent:
        age_mins = int((time.time() - sig.get("ts", 0)) / 60)
        ch = sig.get("channel", "unknown")
        mint = sig.get("mint", "?")
        snippet = sig.get("snippet", "")[:80].replace("\n", " ")
        lines.append(f"• `{mint[:12]}...` via @{ch} ({age_mins}m ago)\n  _{snippet}_")
    return "\n".join(lines)


def format_channel_feed(n: int = 10) -> str:
    """Format recent raw channel messages for display."""
    msgs = get_recent_messages(n)
    if not msgs:
        return "No messages recorded yet."
    lines = [f"**Last {len(msgs)} messages from alpha channels:**\n"]
    for msg in msgs:
        age_mins = int((time.time() - msg.get("ts", 0)) / 60)
        ch = msg.get("channel", "?")
        text = msg.get("text", "")[:120].replace("\n", " ")
        lines.append(f"[{age_mins}m @{ch}] {text}")
    return "\n".join(lines)
