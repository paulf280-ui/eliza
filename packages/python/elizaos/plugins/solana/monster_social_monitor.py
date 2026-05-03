"""Monster strategy — Tier-1 social monitor (tweet-anchor health).

Scoped per MONSTER_SOCIAL_SCOPE_2026_04_19.md. Tiers 2–4 are deliberately
NOT built yet — wait until Tier 1 proves out on paper.

What this does:
- For every open monster position with a `tweet_anchor` URL in its metadata,
  poll Twitter API v2 for public_metrics on a tiered cadence.
- Fire `on_social_event(mint, event)` into strategy_e_monster when:
    * Tweet is deleted                → event="tweet_deleted"
    * Engagement velocity collapses   → event="engagement_collapse"
    * Author posts a disavowing reply → event="author_disavow"

Gating:
- Requires `MONSTER_SOCIAL_MONITOR_ENABLED=true` AND `TWITTER_BEARER_TOKEN`.
- Silent no-op otherwise — safe to import always.

The monitor is intentionally conservative: every event we fire causes a real
sell, so the disavow detector requires an exact-match substring within the
author's recent quote/reply tweets ("scam" / "not my token" / "be careful" /
"rug"). False positives here are more costly than missed exits.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from typing import Any

import aiohttp

from . import strategy_e_monster as monster

TWITTER_BEARER    = os.environ.get("TWITTER_BEARER_TOKEN", "")
SOCIAL_ENABLED    = os.getenv("MONSTER_SOCIAL_MONITOR_ENABLED", "false").lower() in (
    "1", "true", "yes", "on",
)

# Polling cadence per position-age (aligned with scope doc)
FAST_WINDOW_SECS  = 30 * 60           # first 30 min
FAST_CADENCE      = 60
MID_WINDOW_SECS   = 6 * 60 * 60       # up to 6h
MID_CADENCE       = 5 * 60
SLOW_CADENCE      = 30 * 60

# Engagement-collapse detector state
HISTORY_MAX        = 20
ENGAGEMENT_DROP_THRESHOLD_PCT = 80.0
PRICE_DROP_REQUIREMENT_PCT    = 15.0

# Disavow pattern — case-insensitive, whole-word match
DISAVOW_PATTERNS = re.compile(
    r"\b(scam|rug|rugpull|not[\s\-]*my[\s\-]*token|disavow|be[\s\-]*careful|fake)\b",
    re.IGNORECASE,
)

TWEET_URL_RE = re.compile(r"x\.com/([A-Za-z0-9_]+)/status/(\d+)")


def _parse_tweet_url(url: str) -> tuple[str, str] | None:
    m = TWEET_URL_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None


# Per-tweet history of (ts, like_count) tuples for velocity computation
_engagement_history: dict[str, deque] = {}


async def _twitter_get(session: aiohttp.ClientSession, path: str,
                        params: dict) -> dict | None:
    if not TWITTER_BEARER:
        return None
    headers = {"Authorization": f"Bearer {TWITTER_BEARER}"}
    url = f"https://api.twitter.com/2{path}"
    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 404:
                return {"_deleted": True}
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


async def _check_tweet(session: aiohttp.ClientSession, tweet_id: str,
                        handle: str, mint: str, pos: dict) -> str | None:
    """Evaluate a single tweet. Returns event-name to fire, or None."""
    res = await _twitter_get(session, f"/tweets/{tweet_id}", {
        "tweet.fields": "public_metrics,created_at,author_id",
    })
    if res is None:
        return None
    if res.get("_deleted"):
        return "tweet_deleted"

    data = res.get("data") or {}
    metrics = data.get("public_metrics") or {}
    likes = int(metrics.get("like_count") or 0)
    now = time.time()

    hist = _engagement_history.setdefault(tweet_id, deque(maxlen=HISTORY_MAX))
    hist.append((now, likes))

    if len(hist) >= 3:
        # Compute velocity: likes-per-minute over last 10 min vs prior 10 min
        cutoff = now - 10 * 60
        recent = [e for e in hist if e[0] >= cutoff]
        older  = [e for e in hist if e[0] < cutoff]
        if recent and older and len(recent) >= 2 and len(older) >= 2:
            def _rate(points):
                if len(points) < 2:
                    return 0.0
                dt = max(points[-1][0] - points[0][0], 1)
                return (points[-1][1] - points[0][1]) / dt
            r_recent, r_older = _rate(recent), _rate(older)
            peak = max(r_recent, r_older, 1e-9)
            drop_pct = (1.0 - (r_recent / peak)) * 100 if peak > 0 else 0.0
            if r_older > r_recent and drop_pct >= ENGAGEMENT_DROP_THRESHOLD_PCT:
                # Gate with price drop too
                entry = pos.get("entry_price") or 0.0
                current = pos.get("current_price") or entry
                if entry > 0 and current > 0:
                    pnl_from_peak = (
                        (current - (pos.get("peak_price") or entry)) /
                        (pos.get("peak_price") or entry) * 100
                    )
                    if pnl_from_peak <= -PRICE_DROP_REQUIREMENT_PCT:
                        return "engagement_collapse"

    # Disavow detection: fetch author's recent 5 tweets, look for disavow words
    # that also mention the token ticker or mint prefix.
    token_name = (pos.get("token_name") or "").lower()
    mint_prefix = mint[:8].lower()
    author_id = data.get("author_id")
    if author_id:
        recent = await _twitter_get(session, f"/users/{author_id}/tweets", {
            "max_results": 5, "tweet.fields": "text,created_at",
        })
        if recent and (recent.get("data") or []):
            for t in recent["data"]:
                txt = (t.get("text") or "").lower()
                if DISAVOW_PATTERNS.search(txt) and (
                    (token_name and token_name in txt) or mint_prefix in txt
                ):
                    return "author_disavow"
    return None


def _next_cadence(entry_ts: float) -> float:
    age = time.time() - entry_ts
    if age < FAST_WINDOW_SECS:
        return FAST_CADENCE
    if age < MID_WINDOW_SECS:
        return MID_CADENCE
    return SLOW_CADENCE


async def monster_social_monitor_loop(runtime: Any,
                                       session: aiohttp.ClientSession) -> None:
    if not SOCIAL_ENABLED:
        print("[monster-social] disabled (MONSTER_SOCIAL_MONITOR_ENABLED=false)")
        return
    if not TWITTER_BEARER:
        print("[monster-social] TWITTER_BEARER_TOKEN missing — loop exiting")
        return
    print("[monster-social] Tier-1 tweet-anchor monitor started")

    # Per-mint last-poll timestamp
    last_polled: dict[str, float] = {}

    while True:
        try:
            now = time.time()
            positions = monster.open_positions()
            for mint, pos in positions.items():
                anchor = (pos.get("metadata") or {}).get("tweet_anchor") or \
                         (pos.get("metadata") or {}).get("tweet_url")
                if not anchor:
                    continue
                parsed = _parse_tweet_url(anchor)
                if not parsed:
                    continue
                handle, tweet_id = parsed

                cadence = _next_cadence(pos.get("entry_ts") or now)
                if (now - last_polled.get(mint, 0)) < cadence:
                    continue
                last_polled[mint] = now

                event = await _check_tweet(session, tweet_id, handle, mint, pos)
                if not event:
                    continue
                print(f"[monster-social] 🚨 {event} on {pos.get('token_name')} "
                      f"({mint[:8]}) — firing exit")
                try:
                    await monster.on_social_event(mint, event, runtime)
                except Exception as e:
                    print(f"[monster-social] on_social_event error: {e}")
        except Exception as e:
            print(f"[monster-social] loop error: {e}")
        # Small poll heartbeat — the per-mint cadence check is the real gate
        await asyncio.sleep(15)
