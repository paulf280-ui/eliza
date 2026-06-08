"""cabal_telegram.py — Telegram auto-poster for Cabal-Hunter alerts.

Posts to @CabalHunterAlerts whenever the scanner detects a HIGH or MEDIUM
risk token. Each detection is a free marketing post that advertises the
paid API — subscribers see the tool working in real time.

Deduplication: mints are only posted once per 24h to avoid spam.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import aiohttp

_BASE = Path(__file__).parent

# Deduplicate — don't post the same mint twice within 24h
_posted_mints: dict[str, float] = {}  # mint → posted_at_ts
_DEDUP_SECS = 86400  # 24 hours


def _already_posted(mint: str) -> bool:
    cutoff = time.time() - _DEDUP_SECS
    ts = _posted_mints.get(mint, 0.0)
    return ts > cutoff


def _mark_posted(mint: str) -> None:
    _posted_mints[mint] = time.time()
    # Prune old entries
    now = time.time()
    to_remove = [m for m, t in _posted_mints.items() if now - t > _DEDUP_SECS]
    for m in to_remove:
        _posted_mints.pop(m, None)


def _build_message(mint: str, token_name: str, result: dict) -> str:
    risk      = result.get("risk", "CLEAN")
    score     = result.get("cabal_score", 0) or result.get("cabal_score", 0)
    clusters  = result.get("clusters") or result.get("coordinated_clusters") or []
    controlled = result.get("is_controlled", False)

    icon = "🚨" if risk == "HIGH" else "⚠️"
    risk_line = f"HIGH ⛔" if risk == "HIGH" else f"MEDIUM ⚡"

    name_display = token_name.strip() if token_name and token_name.strip() else mint[:8] + "…"

    lines = [
        f"{icon} *CABAL {risk} — {name_display}*",
        "",
        f"Risk Level: {risk_line}",
        f"Cabal Score: {score:.0f}/100",
        f"Controlled: {'YES 🔴' if controlled else 'NO'}",
    ]

    if clusters:
        lines.append("")
        lines.append("📊 *Coordinated Clusters:*")
        for c in clusters[:3]:
            w   = c.get("wallet_count") or c.get("wallets", "?")
            pct = c.get("combined_pct", 0)
            funder = c.get("master_short") or c.get("master", "unknown")[:20]
            r   = c.get("risk", "?")
            lines.append(f"  • {w} wallets from `{funder}` — {pct:.1f}% supply [{r}]")

    lines += [
        "",
        f"🗺 [Visual Bubble Map](https://api.cabal-hunter.com/map?mint={mint})",
        f"🔍 [Free API](https://api.cabal-hunter.com) · $0.05/query",
        "",
        f"`{mint}`",
    ]

    return "\n".join(lines)


async def send_cabal_alert(
    mint: str,
    token_name: str,
    result: dict,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """Post a cabal detection alert to @CabalHunterAlerts.

    Returns True if the message was sent, False otherwise.
    Only fires for HIGH or MEDIUM risk. Deduplicates by mint over 24h.
    """
    bot_token = os.getenv("TELEGRAM_CABAL_BOT_TOKEN", "")
    channel   = os.getenv("TELEGRAM_CABAL_CHANNEL", "@CabalHunterAlerts")

    if not bot_token:
        return False  # not configured

    risk = result.get("risk", "CLEAN")
    if risk not in ("HIGH", "MEDIUM"):
        return False  # CLEAN — don't post

    if _already_posted(mint):
        return False  # already posted today

    message = _build_message(mint, token_name, result)
    url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id":    channel,
        "text":       message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                _mark_posted(mint)
                print(f"[cabal-telegram] ✅ Posted {risk} alert for {token_name or mint[:8]} to {channel}")
                return True
            else:
                body = await r.text()
                print(f"[cabal-telegram] ❌ Telegram API {r.status}: {body[:100]}")
                return False
    except Exception as e:
        print(f"[cabal-telegram] ❌ Send error: {e}")
        return False
    finally:
        if own_session:
            await session.close()
