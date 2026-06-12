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
    """Mirror EXACTLY what the bubble map shows: same score, same zone label
    (LOW RISK / CAUTION / HIGH RISK at 35/65), same deployer history, same
    bundle flag. The TG channel and the map must never disagree.
    """
    score     = float(result.get("cabal_score") or 0)
    clusters  = result.get("clusters") or result.get("coordinated_clusters") or []
    deployer  = result.get("deployer") or {}
    time_sync = bool(result.get("time_sync")) or any(
        c.get("type") == "time_sync" for c in clusters
    )

    # Zone label — identical thresholds + wording to the map's score guide
    if score >= 65:
        icon, zone = "🚨", "HIGH RISK"
    elif score >= 35:
        icon, zone = "⚠️", "CAUTION"
    else:
        icon, zone = "✅", "LOW SIGNAL"

    name_display = token_name.strip() if token_name and token_name.strip() else mint[:8] + "…"

    lines = [
        f"{icon} *{zone} — {name_display}*",
        "",
        f"Cabal Score: *{score:.0f}/100*",
    ]

    if time_sync:
        lines.append("⚡ *BUNDLED LAUNCH* — wallets bought in the same block")

    # Deployer history — same data as the map's Deployer History panel
    dep_verdict = deployer.get("verdict")
    if dep_verdict and dep_verdict != "UNKNOWN" and deployer.get("creator"):
        dep_label = {
            "SERIAL_RUGGER":     "⛔ SERIAL RUGGER",
            "POOR_TRACK_RECORD": "⚠ POOR TRACK RECORD",
            "FIRST_LAUNCH":      "🆕 FIRST LAUNCH",
            "NORMAL":            "✓ NO RED FLAGS",
        }.get(dep_verdict, dep_verdict)
        lines.append("")
        lines.append(f"👤 *Deployer:* {dep_label}")
        launched = deployer.get("tokens_launched", 0)
        if launched > 1:
            lines.append(
                f"   {launched} tokens launched · "
                f"{deployer.get('dead', 0)}/{deployer.get('sampled', 0)} dead "
                f"({deployer.get('dead_pct', 0):.0f}%)"
            )

    if clusters:
        lines.append("")
        lines.append("📊 *Coordinated Clusters:*")
        for c in clusters[:3]:
            w   = c.get("wallet_count") or c.get("wallets", "?")
            pct = float(c.get("combined_pct") or 0)
            if c.get("type") == "time_sync":
                lines.append(f"  • {w} wallets, same-block bundle — {pct:.1f}% supply")
            else:
                funder = c.get("master_short") or str(c.get("master", "unknown"))[:20]
                lines.append(f"  • {w} wallets from `{funder}` — {pct:.1f}% supply")

    lines += [
        "",
        f"🗺 [Visual Bubble Map](https://api.cabal-hunter.com/map?mint={mint})",
        "🔍 [Free API — 100 queries/month](https://api.cabal-hunter.com)",
        "",
        f"`{mint}`",
    ]

    return "\n".join(lines)


async def send_cabal_alert(
    mint: str,
    token_name: str,
    result: dict,
    session: aiohttp.ClientSession | None = None,
    force_post: bool = False,
) -> bool:
    """Post a cabal detection alert to @CabalHunterAlerts.

    Returns True if the message was sent, False otherwise.
    By default, only fires for HIGH or MEDIUM risk. Deduplicates by mint over 24h.
    If force_post=True (e.g., entry alerts), posts regardless of risk level.
    """
    bot_token = os.getenv("TELEGRAM_CABAL_BOT_TOKEN", "")
    channel   = os.getenv("TELEGRAM_CABAL_CHANNEL", "@CabalHunterAlerts")

    if not bot_token:
        return False  # not configured

    risk = result.get("risk", "CLEAN")
    if not force_post and risk not in ("HIGH", "MEDIUM"):
        return False  # CLEAN — don't post unless forced

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
