"""cabal_twitter.py — Auto-posts cabal detections to @CabalHunterAPI on X.

Runs alongside cabal_telegram.py — every HIGH or MEDIUM risk detection
posts to both Telegram and Twitter simultaneously.
Deduplication: same mint won't post twice within 24h.
"""

from __future__ import annotations

import os
import time

# Dedup — same mint, same day = skip
_posted_mints: dict[str, float] = {}
_DEDUP_SECS = 86400


def _already_posted(mint: str) -> bool:
    return _posted_mints.get(mint, 0.0) > time.time() - _DEDUP_SECS


def _mark_posted(mint: str) -> None:
    _posted_mints[mint] = time.time()
    now = time.time()
    for m in [k for k, t in _posted_mints.items() if now - t > _DEDUP_SECS]:
        _posted_mints.pop(m, None)


def _build_tweet(mint: str, token_name: str, result: dict) -> str:
    risk     = result.get("risk", "CLEAN")
    score    = result.get("cabal_score", 0)
    clusters = result.get("clusters") or result.get("coordinated_clusters") or []
    icon     = "🚨" if risk == "HIGH" else "⚠️"
    name     = (token_name or "").strip() or mint[:8] + "…"

    lines = [f"{icon} CABAL {risk} — {name}"]
    lines.append(f"Score: {score:.0f}/100")

    if clusters:
        c = clusters[0]
        w   = c.get("wallet_count") or c.get("wallets", "?")
        pct = c.get("combined_pct", 0)
        lines.append(f"{w} wallets, same funder — {pct:.1f}% supply")

    lines.append(f"\n📊 api.cabal-hunter.com/map?mint={mint}")
    lines.append("\n#Solana #DeFi #CabalHunter")

    tweet = "\n".join(lines)
    return tweet[:280]  # Twitter hard limit


def post_cabal_tweet(mint: str, token_name: str, result: dict) -> bool:
    """Post a cabal detection tweet to @CabalHunterAPI.

    Returns True if posted, False otherwise.
    Only fires for HIGH or MEDIUM risk. Deduplicates over 24h.
    """
    # Credentials
    api_key        = os.getenv("TWITTER_CABAL_API_KEY", "")
    api_secret     = os.getenv("TWITTER_CABAL_API_SECRET", "")
    access_token   = os.getenv("TWITTER_CABAL_ACCESS_TOKEN", "")
    access_secret  = os.getenv("TWITTER_CABAL_ACCESS_SECRET", "")

    if not all([api_key, api_secret, access_token, access_secret]):
        return False  # not configured

    risk = result.get("risk", "CLEAN")
    if risk not in ("HIGH", "MEDIUM"):
        return False

    if _already_posted(mint):
        return False

    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        tweet = _build_tweet(mint, token_name, result)
        resp  = client.create_tweet(text=tweet)
        if resp.data:
            _mark_posted(mint)
            tweet_id = resp.data.get("id", "?")
            print(f"[cabal-twitter] ✅ Tweeted {risk} alert for {token_name or mint[:8]} — id={tweet_id}")
            return True
        return False
    except Exception as e:
        print(f"[cabal-twitter] ❌ Tweet error: {e}")
        return False
