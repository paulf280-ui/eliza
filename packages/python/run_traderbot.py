import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

# Load .env from the repo root
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from elizaos import AgentRuntime, Character
from elizaos.bootstrap.plugin import create_bootstrap_plugin
from elizaos.bootstrap.types import CapabilityConfig
from elizaos.plugins.solana import create_solana_plugin
from elizaos.plugins.openai import create_openai_plugin
from elizaos.plugins.solana.dev_reputation import get_reputation


HTTP_PORT = int(os.environ.get("TRADERBOT_PORT", "3001"))


async def start_http_bridge(runtime: AgentRuntime) -> None:
    """Start a lightweight HTTP server so external clients (including the TS
    server) can POST messages to the Python agent runtime.

    Endpoints:
        POST /message   {"text": "...", "user_id": "...", "room_id": "..."}
                        -> {"text": "...", "thought": "...", "actions": [...]}
        GET  /health    -> {"status": "ok", "agent": "TraderBot"}
    """
    try:
        from aiohttp import web
    except ImportError:
        print(
            "[bridge] aiohttp not installed — HTTP bridge disabled. "
            "Install with: pip install aiohttp",
            file=sys.stderr,
        )
        return

    async def handle_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)

        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)

        # ── Jarvis Command Centre — intercept before generic LLM handler ──────
        try:
            from elizaos.plugins.solana.jarvis_command_centre import route as jarvis_route
            reply = await jarvis_route(text, runtime)
            return web.json_response({"text": reply, "thought": None, "actions": [], "source": "jarvis"})
        except Exception as _jcc_exc:
            print(f"[jarvis-cc] Command centre error: {_jcc_exc} — falling back to runtime", file=sys.stderr)

        # ── Fallback: original runtime.send_message path ──────────────────────
        user_id_str = body.get("user_id") or str(uuid.uuid4())
        room_id_str = body.get("room_id") or str(uuid.uuid4())

        def _to_uuid(s: str) -> str:
            try:
                return str(uuid.UUID(s))
            except ValueError:
                return str(uuid.uuid5(uuid.NAMESPACE_URL, s))

        response = await runtime.send_message(
            text,
            user_id=_to_uuid(user_id_str),
            room_id=_to_uuid(room_id_str),
        )

        return web.json_response(
            {
                "text": response.content.text or "",
                "thought": getattr(response.content, "thought", None) or None,
                "actions": list(response.content.actions) if response.content.actions else [],
            }
        )

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "agent": runtime.character.name,
                "providers": [p.name for p in runtime.providers],
                "actions": [a.name for a in runtime.actions],
            }
        )

    app = web.Application()
    app.router.add_post("/message", handle_message)
    app.router.add_get("/health", handle_health)

    # Register dashboard API routes (REST + WebSocket)
    try:
        from elizaos.plugins.solana.dashboard_api import register_dashboard_routes
        register_dashboard_routes(app, runtime)
        print("[bridge] Dashboard API registered: /api/status, /api/positions, /api/history, /api/equity, /api/launches, /api/chat, /ws")
    except Exception as exc:
        print(f"[bridge] Dashboard API registration failed (non-fatal): {exc}", file=sys.stderr)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"[bridge] HTTP bridge listening on http://0.0.0.0:{HTTP_PORT}")
    print(f"[bridge]   POST /message  {{\"text\": \"...\"}}")
    print(f"[bridge]   GET  /health")


async def interactive_repl(runtime: AgentRuntime) -> None:
    """Simple interactive console loop for chatting with the bot."""
    user_id = uuid.uuid4()
    room_id = uuid.uuid4()

    from elizaos.types.primitives import as_uuid

    uid = as_uuid(str(user_id))
    rid = as_uuid(str(room_id))

    print("\n--- TraderBot Interactive REPL ---")
    print("Type your message and press Enter. Ctrl+C or 'quit' to exit.\n")

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("You: ")
            )
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if user_input.strip().lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        if not user_input.strip():
            continue

        try:
            response = await runtime.send_message(user_input.strip(), uid, rid)
            print(f"TraderBot: {response.content.text or '(no response)'}\n")
        except Exception as exc:
            print(f"[error] {exc}\n", file=sys.stderr)


async def _fetch_recent_pump_fun_creates(limit: int = 20) -> list[dict]:
    """Fetch recent pump.fun token launches from DexScreener.

    Uses DexScreener's public token-profiles API which lists recently launched
    Solana tokens (no auth required, works from WSL2).  Pump.fun tokens have
    addresses ending in 'pump'.

    Returns normalised launch dicts (price/progress filled by caller via bonding curve).
    """
    import aiohttp as _aiohttp

    DEXSCREENER_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
    MAX_AGE_SECS = 60 * 60  # only consider tokens < 60 minutes old
    now = time.time()

    try:
        timeout = _aiohttp.ClientTimeout(total=15)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(DEXSCREENER_URL) as resp:
                resp.raise_for_status()
                profiles: list[dict] = await resp.json()

        results: list[dict] = []
        for profile in profiles:
            if len(results) >= limit:
                break
            # Only Solana pump.fun tokens (address ends with 'pump')
            if profile.get("chainId") != "solana":
                continue
            token_address = profile.get("tokenAddress", "")
            if not token_address.lower().endswith("pump"):
                continue
            if len(token_address) < 32:
                continue

            # Extract social links from the profiles endpoint (has `links` array)
            raw_links = profile.get("links") or []
            social_urls = [lnk.get("url", "") for lnk in raw_links if lnk.get("url")]
            # Icon/header presence also implies custom branding = social account
            has_branding = bool(profile.get("icon") or profile.get("header"))
            has_social = bool(social_urls) or has_branding

            results.append({
                "mint": token_address,
                "timestamp": now,
                "price_sol": 0.0,
                "progress_pct": 0.0,
                "complete": False,
                "_source": "dexscreener",
                "_age_known": False,  # No creation timestamp — skip age bonus in scoring
                "_has_social": has_social,
                "_social_urls": social_urls,
            })

        return results

    except Exception as exc:
        print(f"[scout] DexScreener fetch failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Celebrity / meme / narrative keywords that strongly correlate with moonshots.
# Research: wcd (+83,945%), BEDROCK (+957%), gork (+244%), 𝕏Money (+889%) all
# had recognisable viral keywords.  Matches against token name + symbol.
# ---------------------------------------------------------------------------
_CELEBRITY_KEYWORDS: frozenset[str] = frozenset({
    # Crypto celebrities
    "cz", "binance", "bnb", "saylor", "vitalik", "sbf", "buterin",
    # Elon / X ecosystem
    "elon", "musk", "grok", "xai", "xmoney", "x.com", "tesla", "spacex",
    # Politics (high meme energy)
    "trump", "melania", "maga", "doge", "dogefather",
    # AI brands / chat
    "chatgpt", "claude", "gemini", "copilot", "deepseek", "llama", "openai",
    # Classic crypto culture
    "lambo", "moon", "wen", "wojak", "pepe", "chad", "sigma", "alpha", "ape",
    "brainlet", "retard", "based", "normie", "gigachad",
    # pump.fun meta-narratives (new tokens often reference the platform itself)
    "bedrock", "pumpfees", "pumpdao", "launchlab",
    # Finance / macro
    "fed", "powell", "yellen", "blackrock", "jpmorgan",
    # Animal memes — top pattern across all moonshots (Barking Puppy +266k%, MonkeCoin +1315%, etc.)
    # Every animal wave on Solana has produced 100x+ tokens
    "dog", "puppy", "pup", "cat", "kitty", "kitten", "frog", "toad",
    "monkey", "monke", "bear", "bull", "wolf", "shark", "fish", "bird",
    "penguin", "duck", "hamster", "rabbit", "bunny", "fox", "tiger", "lion",
    "crab", "bat", "rat", "pig", "cow", "goat", "horse", "pony", "snake",
    # Chinese community wave — 索拉纳人生 (+979%), 哈基米 (+334%), China (+1315%)
    # Chinese crypto community = largest coordinated buying group on Solana (pre-builds
    # community before launch, graduates bonding curve in < 5 min)
    "china", "chinese", "hakimi", "panda", "dragon",
    # Dark / sci-fi / void narrative — MINDVOID (+90%), synthetic media, oblivion
    "void", "mindvoid", "dark", "abyss", "oblivion", "synthetic", "matrix",
    "cyber", "quantum", "neural", "nexus", "shadow", "phantom", "ghost",
    # Viral internet / sports memes
    "messi", "ronaldo", "neymar", "mbappe", "lebron", "curry",
    "rizz", "npc", "fomo", "yolo", "grind", "hustle",
    # Solana ecosystem meta — tokens referencing the ecosystem itself
    "solana", "raydium", "jupiter", "backpack",
})


def _has_narrative_keyword(name: str, symbol: str) -> bool:
    """Return True if token name or symbol contains a high-value narrative keyword."""
    text = (name + " " + symbol).lower()
    return any(kw in text for kw in _CELEBRITY_KEYWORDS)


async def _fetch_dex_social(session: Any, mint: str) -> tuple[bool, list[str]]:
    """Check DexScreener pairs for social links. Returns (has_social, urls).

    Times out quickly so it never blocks the fast path significantly.
    """
    try:
        import aiohttp as _aio
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=_aio.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                return False, []
            data = await resp.json()
        for pair in (data.get("pairs") or []):
            info = pair.get("info") or {}
            urls = (
                [s.get("url", "") for s in (info.get("socials") or [])]
                + [w.get("url", "") for w in (info.get("websites") or [])]
            )
            urls = [u for u in urls if u]
            if urls:
                return True, urls
    except Exception:
        pass
    return False, []


def _score_launch(launch: dict, now: float) -> tuple[int, list[str]]:
    """Score a pump.fun launch (0–7 pre-safety). Caller adds +3 after safety check.

    Returns (pre_safety_score, reasons).

    Scoring breakdown (max 7 before safety, bonuses can push higher):
      +3  fill 5–45%   (bonding curve sweet spot)
      +1  fill 1–5% or 45–65%  (marginal momentum)
      +2  age < 15 min (WebSocket timestamp only)
      +1  age 15–60 min
      +1  traction ≥ 1% fill
      +1  price data available (bonding curve readable)
      +1  WebSocket source (live data, not polled)
      +1  nascent launch (WS, fill < 1%, age < 60s — very first seconds)
      +2  social presence (website or Twitter/X link on DexScreener)
      +2  celebrity/meme narrative keyword in name/symbol
      +1  watchlist token (pre-loaded via /watch command)
    """
    age_secs = now - launch.get("timestamp", now)
    progress = launch.get("progress_pct", 0.0)
    price_sol = launch.get("price_sol", 0.0)
    age_known = launch.get("_age_known", True)
    source = launch.get("_source", "unknown")
    name = launch.get("name", "")
    symbol = launch.get("symbol", "")

    score = 0
    reasons: list[str] = []

    # Fill-based scoring
    if 5.0 <= progress <= 45.0:
        score += 3
        reasons.append(f"fill {progress:.1f}% (sweet spot +3)")
    elif 1.0 <= progress < 5.0 or 45.0 < progress <= 65.0:
        score += 1
        reasons.append(f"fill {progress:.1f}% (marginal +1)")

    # Age-based scoring — only when timestamp is reliable (WebSocket tokens)
    if age_known:
        if age_secs < 900:
            score += 2
            reasons.append(f"age {age_secs/60:.1f}m (fresh +2)")
        elif age_secs < 3600:
            score += 1
            reasons.append(f"age {age_secs/60:.0f}m (+1)")
    else:
        reasons.append("age unknown (DexScreener)")

    if progress >= 1.0:
        score += 1
        reasons.append("traction>=1% (+1)")

    if price_sol > 0:
        score += 1
        reasons.append("price data (+1)")

    # WebSocket source bonus — live data is more reliable than polled
    if source == "websocket":
        score += 1
        reasons.append("WebSocket source (+1)")

    # Nascent launch bonus — very fresh WS create (first ~60s at 0% fill)
    if source == "websocket" and progress < 1.0 and age_known and age_secs < 60:
        score += 1
        reasons.append("nascent launch (+1)")

    # Social presence — token has website/Twitter linked on DexScreener.
    # Research: 100% of moonshots (BEDROCK, wcd, EMC2, LAMBO…) had social links.
    # Absence of socials = anonymous rug profile.
    if launch.get("_has_social"):
        score += 2
        social_urls = launch.get("_social_urls", [])
        hint = social_urls[0][:30] if social_urls else "yes"
        reasons.append(f"has socials (+2) [{hint}]")

    # Celebrity / meme narrative keyword bonus
    if name or symbol:
        if _has_narrative_keyword(name, symbol):
            score += 2
            reasons.append(f"narrative keyword (+2) [{symbol or name}]")

    # Watchlist pre-load bonus — user explicitly flagged this token via /watch
    if launch.get("_watchlisted"):
        score += 3
        reasons.append("watchlisted (+3)")

    return score, reasons


# Bonding curve fill cap — reject tokens already sniped ABOVE this fill %.
# Data (2026-03-02): snipers fill EVERY create to 55-80% within 3-6 seconds.
# The 35% cap was blocking 100% of pump.fun opportunities — zero data collected.
# 68% cap: tightened from 75% on 2026-03-16 after -63.5% rug on a 73.3% fill token.
# Data: all wins came from 62-67% range. 68-75% = near-graduation danger zone where
# large BC holders dump simultaneously the moment a new buyer enters.
FILL_CAP_PCT: float = 68.0

# Minimum fill — 50% floor. Historical wins at 55% fill (CsvGNJRW, 3ugQqfxF).
# Market in Apr 2026 produces tokens at 55-59% fill; 60% threshold was blocking everything.
FILL_MIN_PCT: float = 50.0

# Price sanity — at 55-65% fill the bonding curve price is ~1.2-2e-7 SOL
# (roughly 5-7x the launch price of ~2.8e-8 due to curve shape).
# Reject extreme outliers — tokens already at 80%+ fill often price above 3e-7.
# Previous value 8e-8 was blocking all 55-65% fill tokens. Calibrated to data.
PRICE_SANITY_MAX_SOL: float = 2.5e-7   # ~9x initial price = deeply sniped but not extreme

# Dedup guard: mints currently in-flight through the buy pipeline.
# Prevents async race where multiple WS events for the same mint all pass
# the `mint in pos_mgr.positions` check before any of them finish open_position.
_buying_mints: set[str] = set()

# Loss-exit cooldown: mint → timestamp of loss exit.
# Any mint exited via stop_loss / early_stop_loss is blocked from re-entry for 2h.
_loss_exit_times: dict[str, float] = {}
LOSS_COOLDOWN_SECS: float = 2 * 3600  # 2 hours

# All-exit cooldown: mint → timestamp of ANY exit (win or loss).
# Prevents dead-cat-bounce re-entries in raydium/pumpswap/graduation scouts.
_all_exit_times: dict[str, float] = {}
ALL_EXIT_COOLDOWN_SECS: float = 3600  # 1 hour before raydium-scout can re-enter

# Win-exit tracker: mint → timestamp of profitable exit.
# Used to allow re-entry when a token shows renewed momentum after a win.
_win_exit_times: dict[str, float] = {}
WIN_REENTRY_COOLDOWN_SECS: float = 1800   # 30 min min gap before re-entry after a win
WIN_REENTRY_MIN_M5: float = 20.0          # m5 must be > +20% to justify re-entry

# Post-SL global cooldown: no new trade from ANY strategy for 3 min after any stop-loss exit.
# Prevents panic-chasing into a bad market immediately after a loss.
_last_sl_exit_time: float = 0.0
POST_SL_COOLDOWN_SECS: float = 180.0  # fallback default — Jarvis controls via live_config


def _get_post_sl_cooldown() -> float:
    """Live-config-aware post-SL cooldown. Jarvis can adjust at runtime."""
    try:
        from elizaos.plugins.solana import live_config as _lc_sl
        return float(_lc_sl.get("post_sl_cooldown_secs", POST_SL_COOLDOWN_SECS))
    except Exception:
        return POST_SL_COOLDOWN_SECS


def _allow_reentry(mint: str, m5: float | None) -> bool:
    """Return True if we should allow re-entry on a previously traded mint.

    Conditions (all must be met):
      - Exit was a WIN (recorded in _win_exit_times)
      - ≥ 30 min since that win exit
      - m5 > +20% RIGHT NOW (fresh momentum spike, not dead-cat)
    """
    win_ts = _win_exit_times.get(mint, 0.0)
    if win_ts == 0.0:
        return False
    if time.time() - win_ts < WIN_REENTRY_COOLDOWN_SECS:
        return False
    if m5 is None or float(m5) < WIN_REENTRY_MIN_M5:
        return False
    return True

# Cooldowns are persisted to a separate file (independent of trade history).
# This means recently-traded tokens stay blocked even after a history wipe.
_COOLDOWNS_FILE = os.path.join(os.path.dirname(__file__), "elizaos", "plugins", "solana", "cooldowns.json")

# ── Jarvis persistent lessons (cross-session learning) ─────────────────────────
_LESSONS_FILE = os.path.join(os.path.dirname(__file__), "elizaos", "eliza_lessons.txt")

# ── Cross-model shared intelligence ─────────────────────────────────────────────
# Sonnet writes a tactical briefing here after each analysis cycle.
# Haiku reads it as context before making EXIT/HOLD decisions.
_sonnet_intel: dict = {
    "summary": "",           # Sonnet's current market read (written every 30 min)
    "timestamp": 0.0,        # When it was last written
    "adjustments": [],       # Most recent ADJUST commands issued
}

# Haiku logs every EXIT/HOLD decision here so Sonnet can audit them.
import collections as _collections
_haiku_decisions: _collections.deque = _collections.deque(maxlen=200)

# ── Real-time market context (free APIs, cached 30 min) ─────────────────────────
_fng_cache:      dict = {"value": 50, "classification": "Neutral", "timestamp": 0.0}
_trending_cache: dict = {"coins": [], "timestamp": 0.0}

# ── DexScreener boost cache (5-min TTL) — mint → total boost amount ──────────────
_boost_cache: dict[str, int] = {}
_boost_cache_ts: float = 0.0


async def _fetch_fear_greed() -> dict:
    """Crypto Fear & Greed Index from alternative.me. No API key. 30-min cache."""
    global _fng_cache
    if time.time() - _fng_cache["timestamp"] < 1800:
        return _fng_cache
    try:
        import aiohttp as _aio_fng
        async with _aio_fng.ClientSession() as _s:
            async with _s.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=_aio_fng.ClientTimeout(total=5),
            ) as _r:
                if _r.status == 200:
                    _d = await _r.json(content_type=None)
                    _entry = _d["data"][0]
                    _fng_cache = {
                        "value": int(_entry["value"]),
                        "classification": _entry["value_classification"],
                        "timestamp": time.time(),
                    }
                    print(f"[market-ctx] Fear & Greed: {_fng_cache['value']}/100 ({_fng_cache['classification']})")
    except Exception:
        pass
    return _fng_cache


async def _fetch_trending_coins() -> list:
    """CoinGecko top-7 trending coins. No API key. 30-min cache."""
    global _trending_cache
    if time.time() - _trending_cache["timestamp"] < 1800:
        return _trending_cache["coins"]
    try:
        import aiohttp as _aio_tr
        async with _aio_tr.ClientSession() as _s:
            async with _s.get(
                "https://api.coingecko.com/api/v3/search/trending",
                timeout=_aio_tr.ClientTimeout(total=6),
            ) as _r:
                if _r.status == 200:
                    _d = await _r.json()
                    _coins = [
                        f"{c['item']['name']} ({c['item']['symbol'].upper()})"
                        for c in _d.get("coins", [])[:7]
                    ]
                    _trending_cache = {"coins": _coins, "timestamp": time.time()}
                    print(f"[market-ctx] Trending: {', '.join(_coins[:4])}")
    except Exception:
        pass
    return _trending_cache["coins"]


async def _refresh_boost_cache() -> None:
    """Fetch DexScreener active token boosts (Solana only). 5-min TTL."""
    global _boost_cache, _boost_cache_ts
    if time.time() - _boost_cache_ts < 300:
        return
    try:
        import aiohttp as _aio_boost
        async with _aio_boost.ClientSession() as _s:
            async with _s.get(
                "https://api.dexscreener.com/token-boosts/latest/v1",
                timeout=_aio_boost.ClientTimeout(total=6),
            ) as _r:
                if _r.status == 200:
                    _items = await _r.json()
                    _new: dict[str, int] = {}
                    for _b in (_items if isinstance(_items, list) else []):
                        if _b.get("chainId") == "solana":
                            _addr = _b.get("tokenAddress", "")
                            _amt  = int(_b.get("totalAmount", 0) or 0)
                            if _addr and _amt > 0:
                                _new[_addr] = _amt
                    _boost_cache = _new
                    _boost_cache_ts = time.time()
    except Exception:
        pass


async def _web_reputation_check(name: str, symbol: str, mint: str) -> tuple[bool, str]:
    """
    Web reputation check — disabled (gpt-4o-search-preview removed to save costs).
    RugCheck already covers safety; this function now always passes through.
    Returns (safe=True, reason). Fails open by design.
    """
    return True, "web-check disabled (RugCheck covers safety)"


def _load_recent_lessons(n: int = 5) -> str:
    """Return recent wins AND losses from the lessons file as a prompt prefix for Jarvis.

    Jarvis needs to learn both patterns: what signals led to winning trades so she
    can recognise them again, and what signals were red flags she should have caught.
    """
    try:
        with open(_LESSONS_FILE) as _lf:
            _lines = [_l.strip() for _l in _lf if _l.strip()]
        if not _lines:
            return ""
        _recent = _lines[-n * 2:]  # read more lines to find both wins and losses
        _wins   = [l for l in _recent if l.startswith("WIN:")][-2:]
        _losses = [l for l in _recent if not l.startswith("WIN:")][-3:]
        out = ""
        if _losses:
            out += "LOSING TRADE PATTERNS (these signals preceded losses — avoid):\n"
            out += "\n".join(f"  ✗ {l}" for l in _losses) + "\n"
        if _wins:
            out += "WINNING TRADE PATTERNS (these signals preceded wins — seek them):\n"
            out += "\n".join(f"  ✓ {l}" for l in _wins) + "\n"
        return out + "\n" if out else ""
    except Exception:
        return ""

def _append_lesson(lesson: str) -> None:
    """Append a one-line lesson to disk so Jarvis learns across bot restarts."""
    import datetime as _dt2
    try:
        _ts = _dt2.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        with open(_LESSONS_FILE, "a") as _lf:
            _lf.write(f"[{_ts}] {lesson}\n")
    except Exception:
        pass

def _get_market_context(pos_mgr_svc) -> str:
    """Session stats for Jarvis — win rate, average P&L, streak, and 24h P&L.

    This gives Jarvis a calibrated sense of how the current session is going so she
    can be more conservative during losing streaks and more confident during winning ones.
    """
    try:
        _hist = list(getattr(pos_mgr_svc, "_trade_history", []))
        _sells = [t for t in _hist if t.get("side") == "sell"
                  and t.get("pnl_pct") is not None]
        if not _sells:
            return ""
        _recent = _sells[-10:]
        _last3  = ["WIN" if t["pnl_pct"] > 0 else "LOSS" for t in _recent[-3:]]
        _wr     = sum(1 for t in _recent if t["pnl_pct"] > 0) / len(_recent) * 100

        _wins   = [t for t in _recent if t["pnl_pct"] > 0]
        _losses = [t for t in _recent if t["pnl_pct"] <= 0]
        _avg_w  = sum(t["pnl_pct"] for t in _wins)   / len(_wins)   if _wins   else 0.0
        _avg_l  = sum(t["pnl_pct"] for t in _losses) / len(_losses) if _losses else 0.0

        _cutoff     = time.time() - 86400
        _today      = [t for t in _sells if (t.get("timestamp") or 0) > _cutoff]
        _today_pnl  = sum(t.get("pnl_sol") or 0 for t in _today)
        _today_wr   = (sum(1 for t in _today if t["pnl_pct"] > 0) / len(_today) * 100) if _today else 0

        # Consecutive streak
        _streak = 0
        for t in reversed(_recent):
            is_win = t["pnl_pct"] > 0
            if _streak == 0:
                _streak = 1 if is_win else -1
            elif (_streak > 0) == is_win:
                _streak += (1 if is_win else -1)
            else:
                break
        _streak_str = (f"{abs(_streak)}-trade win streak" if _streak > 0
                       else f"{abs(_streak)}-trade losing streak")

        return (
            f"SESSION STATS ({len(_recent)} recent trades): "
            f"win rate {_wr:.0f}% | {_streak_str} | "
            f"avg win +{_avg_w:.0f}% avg loss {_avg_l:.0f}% | "
            f"24h P&L {_today_pnl:+.4f} SOL ({len(_today)} trades today, {_today_wr:.0f}% win)\n"
        )
    except Exception:
        return ""


def _check_grok_premium(mint: str, social_svc, wallet_sol: float) -> tuple[bool, float]:
    """Return (is_premium, buy_sol) — True when Grok score ≥ grok_premium_min_score.

    If premium conditions are met:
      - Returns True and the configured grok_premium_buy_sol (default 0.3 SOL)
      - Falls back to (False, 0.0) if balance is insufficient or premium is disabled
    """
    try:
        from elizaos.plugins.solana import live_config as _lc_p
        if not _lc_p.get("grok_premium_enabled", True):
            return False, 0.0
        if social_svc is None:
            return False, 0.0
        score = getattr(social_svc, "get_confidence", lambda m: 0)(mint)
        min_score = int(_lc_p.get("grok_premium_min_score", 8))
        if score < min_score:
            return False, 0.0
        premium_buy = float(_lc_p.get("grok_premium_buy_sol", 0.3))
        if wallet_sol < premium_buy + 0.01:  # need at least 0.01 SOL buffer for fees
            return False, 0.0
        return True, premium_buy
    except Exception:
        return False, 0.0


def _hard_buy_gate(
    tag: str,
    mint: str,
    strategy: str,
    *,
    liq_usd: float = 0.0,
    mc_usd: float = 0.0,
    vol_h1_usd: float = 0.0,
    buy_txns: int = 0,
    sell_txns: int = 0,
    safety_passed: bool = True,
    safety_reason: str = "",
    holders: int = 0,
) -> tuple[bool, str]:
    """Final hard pre-buy gate — ALL 8 checks must pass or buy is blocked.

    Call this IMMEDIATELY before executing any buy transaction.
    Reads thresholds from live_config on every call (no stale variables).

    Args:
        tag:            Log prefix e.g. "[grad-snipe]"
        mint:           Token mint address
        strategy:       "b" | "c" | "d" | "a2" | "monster"
        liq_usd:        Pool liquidity in USD
        mc_usd:         Market cap in USD (0 = unknown)
        vol_h1_usd:     1-hour trading volume in USD
        buy_txns:       H1 buy transaction count
        sell_txns:      H1 sell transaction count
        safety_passed:  Result of check_token_safety() — covers rugcheck + LP lock
        safety_reason:  Reason string from check_token_safety() (for logging)
        holders:        Current holder count (0 = not available)

    Returns:
        (True, "") if all checks pass
        (False, "HARD REJECT: ...") if any check fails
    """
    from elizaos.plugins.solana import live_config as _lc_hbg

    # Config key prefix per strategy
    _pfx = {"b": "b", "c": "c", "d": "d", "a2": "a2", "monster": "monster_scanner"}.get(strategy, "c")
    _liq_key = "strategy_b_min_liq_usd" if strategy == "b" else f"{_pfx}_min_liq_usd"

    # ── Step 1: Liquidity floor ───────────────────────────────────────────────
    _min_liq = float(_lc_hbg.get(_liq_key, 0) or 0)
    if _min_liq > 0 and liq_usd < _min_liq:
        _r = f"HARD REJECT: liq ${liq_usd:,.0f} < ${_min_liq:,.0f} min"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 2: Market cap floor ──────────────────────────────────────────────
    _min_mc = float(_lc_hbg.get(f"{_pfx}_min_mc_usd", 0) or 0)
    if _min_mc > 0 and mc_usd < _min_mc:
        _r = f"HARD REJECT: MC ${mc_usd:,.0f} < ${_min_mc:,.0f} min"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 3: Vol/Liq max (wash trading) ───────────────────────────────────
    _vl_ratio = (vol_h1_usd / liq_usd) if liq_usd > 0 else 0.0
    _max_vl = float(_lc_hbg.get(f"{_pfx}_max_vol_liq_ratio", 0) or 0)
    if _max_vl > 0 and _vl_ratio > _max_vl:
        _r = f"HARD REJECT: vol/liq {_vl_ratio:.0f}x > {_max_vl:.0f}x (wash trading)"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 4: Vol/Liq min (no momentum) ────────────────────────────────────
    _min_vl = float(_lc_hbg.get(f"{_pfx}_min_vol_liq_ratio", 0) or 0)
    if _min_vl > 0 and liq_usd > 0 and _vl_ratio < _min_vl:
        _r = f"HARD REJECT: vol/liq {_vl_ratio:.1f}x < {_min_vl:.0f}x (no momentum)"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 5: Buy ratio (buyers must dominate) ──────────────────────────────
    _total = buy_txns + sell_txns
    _buy_pct = round(buy_txns / _total * 100, 1) if _total > 0 else 0.0
    _min_br = float(_lc_hbg.get(f"{_pfx}_min_buy_ratio", 0) or 0)
    if _min_br > 0 and _total >= 10 and _buy_pct < _min_br:
        _r = f"HARD REJECT: buy% {_buy_pct:.0f}% < {_min_br:.0f}% (sellers winning)"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 5b: Buy-ratio CEILING (monster DNA — 50–70%, not a parabolic pump) ─
    # Monsters cluster 50–65% buy-ratio at entry. Anything > 70% is usually a
    # cold-start pump that reverts hard. Disabled when config key absent/0.
    _max_br = float(_lc_hbg.get(f"{_pfx}_max_buy_ratio", 0) or 0)
    if _max_br > 0 and _total >= 10 and _buy_pct > _max_br:
        _r = f"HARD REJECT: buy% {_buy_pct:.0f}% > {_max_br:.0f}% (parabolic — late entry)"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 5c: Liquidity CEILING (monster DNA — $25k–$150k sweet spot) ───────
    # Disabled when config key absent/0. Monsters don't emerge from $500k+ pools —
    # you're buying the exit by then.
    _max_liq = float(_lc_hbg.get(f"{_pfx}_max_liq_usd", 0) or 0)
    if _max_liq > 0 and liq_usd > _max_liq:
        _r = f"HARD REJECT: liq ${liq_usd:,.0f} > ${_max_liq:,.0f} max (already mature)"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Steps 6+7: Rugcheck score + LP locked (via check_token_safety) ────────
    if not safety_passed:
        _r = f"HARD REJECT: {safety_reason}"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    # ── Step 8: Holders ───────────────────────────────────────────────────────
    _holders_key = "a2_min_holders" if strategy == "a2" else f"{_pfx}_min_holders"
    _min_h = int(_lc_hbg.get(_holders_key, 0) or 0)
    if _min_h > 0 and holders > 0 and holders < _min_h:
        _r = f"HARD REJECT: holders {holders} < {_min_h} min"
        print(f"{tag} x {mint[:8]}... {_r}")
        return False, _r

    return True, "all gates clear"


async def _execute_split_buy_b(
    runtime,
    pos_mgr,
    raydium_svc,
    wallet_svc,
    mint: str,
    dex: str,
    entry_price_sol: float,  # fill price from position A — used as B's entry estimate
    meta: dict,
    token_decimals: int = 9,
    score: int = 0,
    grok_confirmed: bool = False,
    grok_premium: bool = False,
    tag: str = "[split-b]",
) -> None:
    """Open the MONSTER HUNTER position (B) after the HARVESTER (A) has been bought.

    Called immediately after pos_mgr.open_position(..., label="A") for split-buy tokens.
    Executes a second Jupiter buy for split_buy_b_sol, then opens position B with
    label="B" and _is_split_b=True (bypasses session guard and cap guard).
    No-op when split_buy_enabled=False.
    """
    from elizaos.plugins.solana import live_config as _lc_sb
    if not bool(_lc_sb.get("split_buy_enabled", False)):
        return  # feature disabled — no-op

    _b_sol = float(_lc_sb.get("split_buy_b_sol", 0.025))
    if _b_sol <= 0:
        return

    # Estimate token amount from A's fill price (same pool, near-identical price)
    token_amount_b = int((_b_sol / entry_price_sol) * (10 ** token_decimals)) if entry_price_sol > 0 else 0
    price_b = entry_price_sol
    sig_b = ""

    try:
        if PAPER_TRADING:
            sig_b = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
        elif raydium_svc is not None:
            _b_last_exc: Exception | None = None
            for _b_slip_bps in (1500, 3000, 5000):
                try:
                    sig_b = await raydium_svc.swap_jupiter_buy(
                        mint, _b_sol, slippage_bps=_b_slip_bps, priority_fee=0.002
                    )
                    _b_last_exc = None
                    break
                except Exception as _b_exc:
                    _b_last_exc = _b_exc
            if _b_last_exc is not None:
                raise _b_last_exc

        if not sig_b:
            print(f"{tag} Split-B buy failed for {mint[:8]}... — MONSTER HUNTER skipped")
            return

        # Read actual tokens received for B (wallet shows A+B combined — use estimated split)
        # We rely on A's fill price since it's the same pool milliseconds later.
        if not PAPER_TRADING and wallet_svc is not None:
            try:
                await asyncio.sleep(3)
                _b_bals = await wallet_svc.get_token_balances()
                _b_raw = next((int(t["raw_amount"]) for t in _b_bals if t["mint"] == mint), 0)
                # Estimate B's tokens by backing out what A should have received:
                #   total_wallet_tokens ≈ A_tokens + B_tokens
                #   B_tokens ≈ total * (b_sol / (a_sol + b_sol))
                _a_sol = float(_lc_sb.get("split_buy_a_sol", 0.05))
                if _b_raw > 0 and (_a_sol + _b_sol) > 0:
                    _b_frac = _b_sol / (_a_sol + _b_sol)
                    token_amount_b = int(_b_raw * _b_frac)
                    if entry_price_sol > 0:
                        _b_decimals = next((t.get("decimals", token_decimals) for t in _b_bals if t["mint"] == mint), token_decimals)
                        price_b = _b_sol / (token_amount_b / 10 ** _b_decimals) if token_amount_b > 0 else entry_price_sol
            except Exception as _bfe:
                print(f"{tag} Warning: could not split fill for {mint[:8]}... ({_bfe}), using estimated tokens")

        pos_mgr.open_position(
            mint=mint,
            dex=dex,
            entry_price_sol=price_b,
            entry_sol_spent=_b_sol,
            token_amount=token_amount_b,
            token_decimals=token_decimals,
            signature=sig_b,
            score=score,
            grok_confirmed=grok_confirmed,
            grok_premium=grok_premium,
            meta=meta,
            label="B",
            _is_split_b=True,
        )
        print(
            f"{tag} MONSTER HUNTER opened: {mint[:8]}... {_b_sol:.4f} SOL @ {price_b:.8f} SOL/token "
            f"sig={sig_b[:20]}"
        )

    except Exception as exc:
        print(f"{tag} Split-B error for {mint[:8]}...: {exc}", file=sys.stderr)


def _dynamic_buy_sol(desired_sol: float, wallet_sol: float) -> float:
    """Return the FIXED position size (desired_sol) if the wallet can afford it.

    No percentage scaling — the user has set a hard position size and the bot
    trades at exactly that size every time, or not at all.

    Returns 0.0 (skip signal) only if:
      - wallet cannot cover the full position + MIN_RESERVE_SOL (0.05 SOL for fees)
    In paper mode the balance check is skipped — no real SOL at risk.
    """
    _paper = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    if _paper:
        return desired_sol
    MIN_RESERVE_SOL = 0.05  # always keep 0.05 SOL for fees / emergency exits
    if wallet_sol - MIN_RESERVE_SOL >= desired_sol:
        return desired_sol  # wallet has enough — trade at full size, no scaling
    return 0.0  # wallet too low to open a full position — skip entirely


def _compound_buy_sol(wallet_sol: float, fallback_sol: float) -> float:
    """Compound position sizing — stake scales as a % of wallet balance.

    When BUY_SOL_BC_PCT is set in .env, position size = wallet * pct / 100.
    This means as the wallet grows from wins, each stake grows with it
    proportionally. If the wallet shrinks, stakes shrink too (built-in
    risk management).

    Example (BUY_SOL_BC_PCT=50):
      wallet=0.50 SOL  →  stake=0.25 SOL
      wallet=1.00 SOL  →  stake=0.50 SOL
      wallet=2.00 SOL  →  stake=1.00 SOL

    Safety bounds (from .env):
      COMPOUND_MIN_SOL  — minimum stake regardless of wallet size (default 0.10 SOL)
                          prevents trades too small to cover Jito fees
      COMPOUND_MAX_SOL  — maximum stake cap (default 2.0 SOL)
                          prevents over-exposure on a single BC token
      MIN_RESERVE_SOL   — 0.05 SOL always kept for fees; if wallet can't cover
                          stake + reserve, returns 0.0 (skip trade)

    Falls back to fixed fallback_sol if BUY_SOL_BC_PCT is not set.
    """
    _pct = float(os.getenv("BUY_SOL_BC_PCT", "0") or "0")
    if _pct <= 0:
        # Compound mode not enabled — use fixed size via normal path
        return _dynamic_buy_sol(fallback_sol, wallet_sol)

    _min_sol  = float(os.getenv("COMPOUND_MIN_SOL", "0.10") or "0.10")
    _max_sol  = float(os.getenv("COMPOUND_MAX_SOL", "2.0")  or "2.0")
    _paper    = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    MIN_RESERVE_SOL = 0.05

    # Core calculation: % of current wallet
    stake = wallet_sol * (_pct / 100.0)

    # Apply safety bounds
    stake = max(_min_sol, min(_max_sol, stake))
    stake = round(stake, 4)  # clean to 4dp

    if _paper:
        return stake

    # Live: ensure wallet can cover stake + fee reserve
    if wallet_sol - MIN_RESERVE_SOL >= stake:
        return stake
    return 0.0  # wallet too small — skip trade


async def _effective_wallet_sol(wallet_svc: Any) -> float:  # type: ignore[return]
    """Return wallet SOL balance, substituting a simulated balance in paper mode.

    When PAPER_TRADING=true and PAPER_WALLET_SOL is set, every scout loop uses
    the simulated balance so position-sizing maths work at the intended scale
    without touching real funds.
    """
    _paper = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    _paper_wallet = float(os.getenv("PAPER_WALLET_SOL", "0.0"))
    if _paper and _paper_wallet > 0:
        return _paper_wallet
    return await wallet_svc.get_sol_balance()


def _save_cooldowns() -> None:
    """Persist exit cooldown dicts to disk (survives trade history wipes)."""
    try:
        with open(_COOLDOWNS_FILE, "w") as f:
            json.dump({"all_exit": _all_exit_times, "loss_exit": _loss_exit_times}, f)
    except Exception:
        pass

def _load_cooldowns() -> None:
    """Load exit cooldown dicts from disk on startup."""
    global _all_exit_times, _loss_exit_times
    try:
        with open(_COOLDOWNS_FILE) as f:
            data = json.load(f)
        now = time.time()
        cutoff = now - max(LOSS_COOLDOWN_SECS, ALL_EXIT_COOLDOWN_SECS)
        # Only keep entries that are still within their cooldown windows
        _all_exit_times.update({m: t for m, t in data.get("all_exit", {}).items() if t > cutoff})
        _loss_exit_times.update({m: t for m, t in data.get("loss_exit", {}).items() if t > cutoff})
    except (FileNotFoundError, json.JSONDecodeError):
        pass

# Rug-streak detection: track recent instant-dump timestamps.
# If 3+ exits >-25% happen within 10 min, pause buying for 10 min (heavy rug phase).
# Reduced from 20 min → 10 min: trading window is only 4h, 20 min pause costs too much runway.
_instant_dump_times: list[float] = []  # timestamps of >-25% early_stop exits
RUG_STREAK_THRESHOLD: int = 3       # N instant dumps within window → pause
RUG_STREAK_WINDOW_SECS: float = 600  # 10-minute window
RUG_STREAK_PAUSE_SECS: float = 600   # 10-minute pause (was 20)
_rug_streak_pause_until: float = 0.0  # timestamp when pause ends

# Semaphore: limit concurrent send_message buy calls to avoid OpenAI TPM rate limits.
# When stall exits free 4+ slots simultaneously, without this we'd send 4+ LLM calls at once.
_buy_semaphore = asyncio.Semaphore(2)

# Watchlist: tokens pre-flagged by the user via dashboard or /watch command.
# Structure: mint → {"name": str, "reason": str, "added_at": float}
# Watchlisted tokens get +3 score bonus and skip the stability wait.
_token_watchlist: dict[str, dict] = {}

# Creation time tracker for fast-graduation detection.
# mint → unix timestamp when first seen via WS Create event.
# Used by Strategy B to filter for fast-graduated tokens (< FAST_GRAD_MAX_SECS).
_mint_creation_times: dict[str, float] = {}
_mint_pool_addresses: dict[str, str] = {}   # mint → PumpSwap pool_address (from pump.fun API at graduation)
_mint_pf_metadata: dict[str, dict] = {}     # mint → pump.fun coin metadata (name, description, twitter, telegram, discord)
FAST_GRAD_MAX_SECS: float = 1800  # 30 minutes — raised from 5min; Extreme Fear market = slow graduations

# Strategy C fast-path: mints that just graduated and should be evaluated
# by raydium_scout_loop at T+5min (min age window), bypassing DexScreener
# profiles discovery lag.  mint → graduation unix timestamp.
_strat_c_grad_queue: dict[str, float] = {}

# ── Momentum confirmation queue — tokens waiting for 5-min re-check before buy ──
# mint → {"ts": float, "price": float, "vol_h1": float, "score": int}
_momentum_pending: dict[str, dict] = {}
MOMENTUM_CONFIRM_SECS = 300  # 5 minutes

# Holder threshold watcher — tokens that passed ALL gates except holder count
# (20-29 holders at evaluation time). Polled every 20s; bought the instant ≥30.
_holder_watch_pending: dict[str, dict] = {}   # mint → {launch, first_seen, holders_at_detect}

# Graduation confirmation timer — tokens that passed all Strategy B filters but
# are parked for graduation_confirmation_wait seconds before buying.
# Loop _graduation_confirmation_loop checks these every 30s and buys on confirmed momentum.
_grad_confirmation_pending: dict[str, dict] = {}  # mint → snap data
STRAT_C_MIN_AGE_SECS = 120   # 2 min post-graduation before Strategy C evaluates
STRAT_C_MAX_AGE_SECS = 1800  # 30 min — expire stale queue entries

# Wallet health — warn if balance drops below these thresholds
WALLET_WARN_SOL = 0.05   # warning: fees eating into profits
WALLET_CRIT_SOL = 0.02   # critical: stop trading shortly

# Pending metadata: score and fill_pct for tokens in the buy pipeline.
# Pump.fun buys go through send_message → BUY_TOKEN action, so we stash
# metadata here keyed by mint and apply it to the position after the buy.
_pending_meta: dict[str, tuple[int, float]] = {}  # mint → (score, fill_pct)


async def _evaluate_and_maybe_buy(
    runtime: AgentRuntime,
    launch: dict,
    pos_mgr: Any,
    evaluated: set[str],
    paper_trading: bool,
    buy_sol: float,
    scout_user_id: str,
    scout_room_id: str,
    min_score: int = 9,
    tag: str = "[scout]",
    stats: dict | None = None,
) -> bool:
    """Evaluate a pump.fun launch and trigger a buy if score >= threshold.

    Returns True if a buy was triggered. Always adds mint to evaluated on exit.
    """
    mint: str = launch.get("mint", "")
    if not mint or mint in evaluated or mint in pos_mgr.positions or mint in _buying_mints:
        return False

    # Skip mints recently exited with a loss (cooldown 2h)
    import time as _time
    loss_ts = _loss_exit_times.get(mint, 0)
    if _time.time() - loss_ts < LOSS_COOLDOWN_SECS:
        evaluated.add(mint)
        return False

    # Skip if in rug-streak pause (3+ instant dumps detected recently)
    if _time.time() < _rug_streak_pause_until:
        remaining = (_rug_streak_pause_until - _time.time()) / 60
        print(f"{tag} x {mint[:8]}... rug-streak pause active ({remaining:.0f}min remaining) — skip")
        evaluated.add(mint)
        return False

    if launch.get("complete", False):
        evaluated.add(mint)
        return False

    # ── Rejection tracker import (lazy — only once) ─────────────────────────
    try:
        from elizaos.plugins.solana import rejection_tracker as _rt
        _rt_ok = True
    except Exception:
        _rt_ok = False

    # ── Fill range gate — reject tokens outside proven fill window ──────────
    progress = launch.get("progress_pct", 0.0)
    if progress > FILL_CAP_PCT:
        print(f"{tag} x {mint[:8]}... fill {progress:.1f}% > {FILL_CAP_PCT}% cap -- skip (sniped)")
        evaluated.add(mint)
        if _rt_ok:
            _rt.record(mint, "fill_cap", filter_name="fill_cap_pct",
                       filter_value=round(progress, 1), threshold=FILL_CAP_PCT, strategy="a2")
        return False
    if 0 < progress < FILL_MIN_PCT:
        print(f"{tag} x {mint[:8]}... fill {progress:.1f}% < {FILL_MIN_PCT}% min -- skip (rug zone)")
        evaluated.add(mint)
        if _rt_ok:
            _rt.record(mint, "fill_min", filter_name="fill_min_pct",
                       filter_value=round(progress, 1), threshold=FILL_MIN_PCT, strategy="a2")
        return False

    # ── Price sanity — reject tokens with inflated entry prices ────────────
    price_sol = launch.get("price_sol", 0.0)
    if price_sol > 0 and price_sol > PRICE_SANITY_MAX_SOL:
        print(f"{tag} x {mint[:8]}... price {price_sol:.2e} SOL > sanity cap -- skip (sniped entry)")
        evaluated.add(mint)
        if _rt_ok:
            _rt.record(mint, "price_sanity", filter_name="price_sanity_max_sol",
                       filter_value=price_sol, threshold=PRICE_SANITY_MAX_SOL, strategy="a2")
        return False

    # ── Developer reputation gate ──────────────────────────────────────────
    creator_wallet: str = launch.get("creator_wallet", "")
    dev_rep = get_reputation()
    if creator_wallet and dev_rep.is_blacklisted(creator_wallet):
        status, reason = dev_rep.check(creator_wallet)
        print(f"{tag} x {mint[:8]}... creator {creator_wallet[:12]}... BLACKLISTED: {reason}")
        evaluated.add(mint)
        return False

    now = time.time()
    pre_score, reasons = _score_launch(launch, now)

    # Whitelist bonus: proven dev wallet gets +1 boost
    if creator_wallet and dev_rep.is_whitelisted(creator_wallet):
        pre_score += 1
        reasons.append("whitelisted dev (+1)")

    # Reserve mint BEFORE the first await so no concurrent task can sneak through.
    # If the token fails safety/score, we discard from _buying_mints in the finally.
    if mint in _buying_mints:
        return False
    _buying_mints.add(mint)

    try:
        # Safety check — hard gate, adds +3
        safe, safety_reason = await pos_mgr.check_token_safety(mint)
        if not safe:
            print(f"{tag} x {mint[:8]}... UNSAFE: {safety_reason}")
            evaluated.add(mint)
            if _rt_ok:
                _rt.record(mint, "unsafe_rugcheck", filter_name="rugcheck",
                           filter_value=safety_reason[:60], strategy="a2",
                           score=pre_score,
                           extra={"fill": round(progress, 1), "mc": launch.get("usd_market_cap")})
            return False

        score = pre_score + 3
        reasons.append("safety ok (+3)")

        print(f"{tag} {mint[:8]}... score={score}/10 | {' | '.join(reasons)}")

        if score < min_score:
            print(f"{tag} x score {score} < {min_score} threshold -- skip")
            evaluated.add(mint)
            if _rt_ok:
                _rt.record(mint, "score_low", filter_name="min_score",
                           filter_value=score, threshold=min_score, strategy="a2",
                           score=score,
                           extra={"fill": round(progress, 1), "mc": launch.get("usd_market_cap"),
                                  "reasons": reasons})
            return False

        # Hard floor — never enter a score-0 token regardless of strategy override
        if score <= 0:
            print(f"{tag} x score {score} — absolute zero, skip")
            evaluated.add(mint)
            return False

        # ── Stability delay (WS fast-path only) ───────────────────────────────
        # Wait 20s then re-check bonding curve fill AND price.
        # Bonding curve is non-linear at 60%+ fill: each 1pp fill drop = ~6-8% price drop.
        # Tightened thresholds: skip if fill drops >1.5pp OR price drops >5%.
        initial_fill = launch.get("progress_pct", 0.0)
        initial_price = launch.get("price_sol", 0.0)
        if "/ws]" in tag and initial_fill >= FILL_MIN_PCT:
            STABILITY_WAIT_SECS = 20
            print(f"{tag} {mint[:8]}... stability check: waiting {STABILITY_WAIT_SECS}s (fill={initial_fill:.1f}% price={initial_price:.2e})", flush=True)
            await asyncio.sleep(STABILITY_WAIT_SECS)
            pump_svc_check = runtime.get_service("token_data")
            if pump_svc_check is not None:
                try:
                    bc2 = await pump_svc_check.get_bonding_curve(mint)
                    if bc2:
                        new_fill = bc2.get("progress_pct", initial_fill)
                        new_price = bc2.get("price_sol", 0.0)
                        if bc2.get("complete", False):
                            print(f"{tag} x {mint[:8]}... graduated during stability wait — skip")
                            evaluated.add(mint)
                            return False
                        # Fill rose above cap — snipers heavily pumped during wait, skip
                        if new_fill > FILL_CAP_PCT:
                            print(f"{tag} x {mint[:8]}... fill rose {initial_fill:.1f}%→{new_fill:.1f}% (above {FILL_CAP_PCT}% cap) — over-sniped, skip")
                            evaluated.add(mint)
                            return False
                        # Fill drop >0.8pp at 60%+ fill ≈ 5-6% price drop on curve (sell pressure)
                        # Tightened from 1.5pp: losing tokens often shed fill during stability window.
                        fill_dropped = initial_fill - new_fill
                        price_drop_pct = (new_price - initial_price) / initial_price if initial_price > 0 and new_price > 0 else 0
                        if fill_dropped > 0.8:
                            print(f"{tag} x {mint[:8]}... fill {initial_fill:.1f}%→{new_fill:.1f}% (−{fill_dropped:.1f}pp) in {STABILITY_WAIT_SECS}s — rug, skip")
                            evaluated.add(mint)
                            return False
                        # Require any positive price movement during stability window (>+0.1%).
                        # Data (54 passes): 0.0% tokens → 0% WR (all losses); 0.1-1% → ~57% WR;
                        # 1-5% → 58% WR. Filter blocks flat/declining, allows any real momentum.
                        if price_drop_pct < 0.001:
                            reason_str = "selling pressure" if price_drop_pct < 0 else "no momentum"
                            print(f"{tag} x {mint[:8]}... price_Δ={price_drop_pct:+.2%} in {STABILITY_WAIT_SECS}s — {reason_str}, skip")
                            evaluated.add(mint)
                            return False
                        # Price spiked >5% in window → overbought, likely to correct.
                        # Data: Δ≤2% tokens have 78% win rate; Δ>7% tokens have 8% win rate.
                        # Threshold 5% captures the transition zone cleanly.
                        if price_drop_pct > 0.05:
                            print(f"{tag} x {mint[:8]}... price +{price_drop_pct:.1%} in {STABILITY_WAIT_SECS}s — overbought pump, skip")
                            evaluated.add(mint)
                            return False
                        # Update to freshest data
                        launch["progress_pct"] = new_fill
                        if new_price > 0:
                            launch["price_sol"] = new_price

                        # Post-stability 5-second confirmation: catch "patience dumps" that start
                        # exactly when our 20s window expires. Re-check bonding curve — if price
                        # has already started declining from stability-end value, abort.
                        await asyncio.sleep(5)
                        try:
                            bc3 = await pump_svc_check.get_bonding_curve(mint)
                            if bc3:
                                confirm_price = bc3.get("price_sol", 0.0)
                                confirm_fill = bc3.get("progress_pct", new_fill)
                                if bc3.get("complete", False):
                                    print(f"{tag} x {mint[:8]}... graduated during confirmation — skip")
                                    evaluated.add(mint)
                                    return False
                                if confirm_price > 0 and new_price > 0:
                                    confirm_delta = (confirm_price - new_price) / new_price
                                    if confirm_delta < -0.005:
                                        print(f"{tag} x {mint[:8]}... confirm_Δ={confirm_delta:+.2%} — patience dump detected, skip")
                                        evaluated.add(mint)
                                        return False
                                if confirm_price > 0:
                                    launch["price_sol"] = confirm_price
                                if confirm_fill > 0:
                                    launch["progress_pct"] = confirm_fill
                        except Exception:
                            pass  # confirmation fetch failed — proceed with stability data

                        print(f"{tag} {mint[:8]}... stability OK fill={new_fill:.1f}% price_Δ={price_drop_pct:+.1%} — buying")
                except Exception:
                    pass  # re-fetch failed — proceed with initial data

        print(f"{tag} BUYING {mint[:8]}... ({buy_sol} SOL, score={score}/10)")
        evaluated.add(mint)
        _pending_meta[mint] = (score, launch.get("progress_pct", 0.0))
        async with _buy_semaphore:  # max 2 concurrent LLM buy calls (avoids OpenAI TPM 429s)
            response = await runtime.send_message(
                f"confirm buy {buy_sol} SOL of {mint}",
                user_id=scout_user_id,
                room_id=scout_room_id,
            )
        reply = response.content.text or ""
        print(f"{tag} -> {reply[:150]}")
        # Backfill score/fill_pct into the position now that open_position has run
        if mint in pos_mgr.positions:
            pos_mgr.positions[mint].score = score
            pos_mgr.positions[mint].fill_pct = launch.get("progress_pct", 0.0)
        _pending_meta.pop(mint, None)
    except Exception as exc:
        print(f"{tag} Buy failed: {exc}", file=sys.stderr)
        _pending_meta.pop(mint, None)
        return False
    finally:
        _buying_mints.discard(mint)

    return True


async def autonomous_scout_loop(runtime: AgentRuntime, graduation_queue: asyncio.Queue | None = None) -> None:
    """Autonomous pump.fun trading loop (Strategy A).

    Disabled when STRATEGY_A_ENABLED=false in .env.

    Two complementary paths:
      FAST: register_event("token_launch") fires immediately on WebSocket Creates
      SLOW: poll DexScreener every 90s as fallback when WS stream is quiet

    Moonshot scoring (max 10, threshold 7):
      +3  fill 5–45%   (sweet spot)
      +3  safety check (mint + freeze authorities burned)
      +2  age < 15 min (WebSocket timestamp only)
      +1  fill 1–5% or 45–65% (marginal)
      +1  traction >= 1% fill
      +1  price data available
      +1  WebSocket source bonus
      +1  nascent launch (WS, fill<1%, age<60s)
    """
    from elizaos.types import ServiceTypeRegistry

    SCOUT_USER_ID = "00000000-0000-0000-0000-000000000099"
    SCOUT_ROOM_ID = "00000000-0000-0000-0000-000000000098"

    PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    # Monster data (Apr 2026): PISS=22x scored 3, NoHat=18x scored 4, LOLA=15x scored 4.
    # Score floor of 8 would reject 4 of 5 confirmed Raydium monsters. Floor = 3.
    MIN_SCORE = 3
    BUY_SOL = float(os.getenv("BUY_SOL", "0.010"))
    SCAN_INTERVAL = 90

    evaluated: set[str] = set()
    # Track creator_wallet per mint so position_closed handler can record outcomes
    mint_to_creator: dict[str, str] = {}
    # Rejection stats — fed to Jarvis analysis and 30-min status report
    reject_stats: dict[str, int] = {
        "sniped_fill": 0, "sniped_price": 0, "blacklisted": 0,
        "unsafe": 0, "low_score": 0, "complete": 0, "bought": 0,
    }

    print(
        f"[scout] Autonomous scout loop started -- scanning every {SCAN_INTERVAL}s. "
        f"{'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}"
    )

    # ── fast path: react immediately to WebSocket token_launch events ─────────
    # emit_event awaits handlers synchronously, so we spawn a task to avoid
    # blocking the TokenLaunchMonitorService's _handle_log coroutine.
    async def _ws_fast_handler(launch: dict) -> None:
        asyncio.create_task(_process_ws_launch(launch))

    async def _process_ws_launch(launch: dict) -> None:
        mint = launch.get("mint", "")
        # All pump.fun bonding-curve token addresses end with "pump".
        # Anything else is a false-positive from the WS account extraction.
        if not mint or not mint.lower().endswith("pump") or mint in evaluated:
            return

        # Record creation time for fast-graduation Strategy B detection
        if mint not in _mint_creation_times:
            _mint_creation_times[mint] = time.time()

        # Apply watchlist bonus if user pre-flagged this token
        if mint in _token_watchlist:
            launch["_watchlisted"] = True
            wl_info = _token_watchlist[mint]
            if wl_info.get("name") and not launch.get("name"):
                launch["name"] = wl_info["name"]

        pm = runtime.get_service("position_manager")
        wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
        if pm is None or wallet_svc is None:
            return

        # Circuit breaker gate
        try:
            bal = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
            actual_buy = _dynamic_buy_sol(BUY_SOL, bal)
            allowed, reason = pm.check_trade_allowed(actual_buy, bal)  # type: ignore[union-attr]
            if not allowed:
                print(f"[scout/ws] Trading gated: {reason}")
                return
            # Post-SL global cooldown: no new entry within 3 min of a stop-loss exit
            _sl_gap = time.time() - _last_sl_exit_time
            if _sl_gap < _get_post_sl_cooldown():
                print(f"[scout/ws] Post-SL cooldown active — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                return
        except Exception:
            return

        # Always re-fetch bonding curve for freshest fill % at evaluation time.
        # token_monitor fetched data T+6s after create; snipers may have moved
        # fill from 20% → 55%+ since then. Fresh fetch = accurate fill cap check.
        pump_svc = runtime.get_service("token_data")
        if pump_svc is not None:
            try:
                bc = await pump_svc.get_bonding_curve(mint)  # type: ignore[union-attr]
                if bc:
                    launch["price_sol"] = bc.get("price_sol", 0.0)
                    launch["progress_pct"] = bc.get("progress_pct", 0.0)
                    launch["complete"] = bc.get("complete", False)
                    # If token just graduated, queue for Strategy B graduation snipe
                    if bc.get("complete") and graduation_queue is not None:
                        try:
                            graduation_queue.put_nowait(mint)
                            print(f"[scout/ws] 🎓 {mint[:8]}... GRADUATED — queued for PumpSwap snipe")
                        except asyncio.QueueFull:
                            pass
            except Exception:
                pass

        creator_wallet = launch.get("creator_wallet", "")
        if creator_wallet and mint:
            mint_to_creator[mint] = creator_wallet

        # Social + name/symbol enrichment via DexScreener (best-effort, 6s timeout).
        # DexScreener may not have indexed this token yet (< 30s old), that's fine —
        # the social bonus simply won't apply if the lookup fails.
        if not launch.get("_has_social") and not launch.get("name"):
            try:
                import aiohttp as _aio_ws
                async with _aio_ws.ClientSession(timeout=_aio_ws.ClientTimeout(total=8)) as _sess:
                    has_soc, soc_urls = await _fetch_dex_social(_sess, mint)
                    if has_soc:
                        launch["_has_social"] = True
                        launch["_social_urls"] = soc_urls
                    # Also try to pick up name/symbol for keyword scoring
                    if not launch.get("name"):
                        try:
                            async with _sess.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                timeout=_aio_ws.ClientTimeout(total=5),
                            ) as _r:
                                if _r.status == 200:
                                    _d = await _r.json()
                                    _pairs = (_d.get("pairs") or [])
                                    if _pairs:
                                        _bt = _pairs[0].get("baseToken") or {}
                                        if _bt.get("name"):
                                            launch["name"] = _bt["name"]
                                            launch["symbol"] = _bt.get("symbol", "")
                        except Exception:
                            pass
            except Exception:
                pass

        print(f"[scout/ws] New Create detected: {mint[:8]}... evaluating immediately")
        bought = await _evaluate_and_maybe_buy(
            runtime, launch, pm, evaluated,
            PAPER_TRADING, actual_buy,
            SCOUT_USER_ID, SCOUT_ROOM_ID, MIN_SCORE,
            tag="[scout/ws]",
        )
        # If buy succeeded, update position's creator_wallet via pos_mgr
        if bought and mint and creator_wallet:
            pos = pm.positions.get(mint)
            if pos and not pos.creator_wallet:
                pos.creator_wallet = creator_wallet

    runtime.register_event("token_launch", _ws_fast_handler)
    print("[scout] WebSocket fast path registered (token_launch event handler active)")

    # ── position_closed event → record dev reputation outcome ─────────────
    dev_rep = get_reputation()

    async def _on_position_closed(trade: dict) -> None:
        """Record trade outcome for the token's creator wallet."""
        import time as _time
        pnl_pct = trade.get("pnl_pct", 0.0)
        hold_secs = trade.get("hold_secs", 0.0)
        mint = trade.get("mint", "")
        reason = trade.get("reason", "")

        # All-exit cooldown: block raydium/pumpswap/grad-snipe re-entry for 1h after any exit
        _all_exit_times[mint] = _time.time()

        # Loss-exit cooldown: block re-entry on loss exits for 2h
        if reason in ("stop_loss", "early_stop_loss", "stall_exit") or pnl_pct < -5:
            _loss_exit_times[mint] = _time.time()
            print(f"[loss-cooldown] {mint[:8]}... blocked for {LOSS_COOLDOWN_SECS/3600:.0f}h (reason={reason} pnl={pnl_pct:+.1f}%)")

        # Global post-SL cooldown: freeze ALL new entries for 3 min after any hard stop-loss.
        # Prevents immediately chasing the next token right after a dump — the market is bad.
        if reason in ("stop_loss", "early_stop_loss", "rug_stop_loss"):
            global _last_sl_exit_time
            _last_sl_exit_time = _time.time()
            print(f"[post-sl-cooldown] All strategies frozen for {_get_post_sl_cooldown():.0f}s after {reason}")

        # Win-exit tracker: record profitable exits so momentum re-entry can be considered
        # after 30 min if the token shows a fresh m5 spike.
        if pnl_pct > 0 and reason not in ("stop_loss", "early_stop_loss", "stall_exit", "rug_stop_loss"):
            _win_exit_times[mint] = _time.time()
            print(f"[win-exit] {mint[:8]}... recorded win exit — re-entry possible after {WIN_REENTRY_COOLDOWN_SECS/60:.0f}min if momentum spikes")

        # Persist cooldowns so they survive trade history wipes and restarts
        _save_cooldowns()

        # Rug-streak detection: if 3+ instant dumps (>-25%) in 10 min → pause buying 20 min
        global _instant_dump_times, _rug_streak_pause_until
        if pnl_pct < -25 and reason == "early_stop_loss":
            now_ts = _time.time()
            _instant_dump_times.append(now_ts)
            # Prune old entries outside window
            _instant_dump_times = [t for t in _instant_dump_times if now_ts - t < RUG_STREAK_WINDOW_SECS]
            if len(_instant_dump_times) >= RUG_STREAK_THRESHOLD:
                _rug_streak_pause_until = now_ts + RUG_STREAK_PAUSE_SECS
                _instant_dump_times.clear()
                print(f"[rug-streak] {RUG_STREAK_THRESHOLD} instant dumps in {RUG_STREAK_WINDOW_SECS/60:.0f}min — pausing buying for {RUG_STREAK_PAUSE_SECS/60:.0f}min")

        creator = trade.get("creator_wallet") or mint_to_creator.get(mint, "")
        if not creator:
            return
        outcome = dev_rep.classify_outcome(pnl_pct / 100.0, hold_secs, reason)
        dev_rep.record_outcome(creator, mint, outcome, pnl_pct / 100.0, hold_secs)
        print(
            f"[dev-rep] Outcome recorded: creator={creator[:12]}... "
            f"mint={mint[:8]}... outcome={outcome} pnl={pnl_pct:+.1f}% hold={hold_secs:.0f}s"
        )

    runtime.register_event("position_closed", _on_position_closed)
    print("[scout] Dev reputation recorder registered (position_closed event handler active)")

    # ═══════════════════════════════════════════════════════════════════════════
    # JARVIS AI ADVISOR — 5 integrated intelligence layers
    # (helpers _load_recent_lessons / _append_lesson / _get_market_context
    #  are module-level so bc_social_sniper_loop can also use them)
    # ═══════════════════════════════════════════════════════════════════════════

    # ── #1: Post-entry assessment (lessons + market context) ─────────────────
    async def _jarvis_assess_position(trade: dict) -> None:
        mint = trade.get("mint", "")
        dex = trade.get("dex", "?")
        entry = trade.get("entry_price_sol", 0.0)
        score = trade.get("score", 0)
        socials = trade.get("social_urls", [])
        name = trade.get("name", mint[:8])
        symbol = trade.get("symbol", "?")
        sol_spent = trade.get("sol_spent", 0.0)

        pos_mgr_svc = runtime.get_service("position_manager")
        lessons = _load_recent_lessons(3)
        mkt_ctx = _get_market_context(pos_mgr_svc) if pos_mgr_svc else ""

        prompt = (
            f"{lessons}"
            f"{mkt_ctx}"
            f"You are a crypto trading assistant. Assess this just-purchased Solana memecoin.\n\n"
            f"Token: {name} ({symbol})\n"
            f"DEX: {dex} | Entry: {entry:.2e} SOL | Bot score: {score}/16 | SOL: {sol_spent:.4f}\n"
            f"Socials: {', '.join(socials[:3]) if socials else 'none'}\n\n"
            f"2 sentences: (1) rug risk from name/socials, (2) entry timing (early/late). "
            f"Be blunt. Start with HOLD or CAUTION."
        )
        try:
            from elizaos.types.model import ModelType, GenerateTextOptions
            result = await asyncio.wait_for(
                runtime.generate_text(
                    prompt, GenerateTextOptions(model_type=ModelType.TEXT_SMALL)
                ),
                timeout=8.0,
            )
            verdict = (result.text if result else "").strip()[:250]
            if pos_mgr_svc and verdict:
                pos_mgr_svc._log_activity(  # type: ignore[union-attr]
                    "info" if verdict.upper().startswith("HOLD") else "warning",
                    f"[jarvis] {symbol}: {verdict}",
                )
            print(f"[jarvis] {mint[:8]}... {verdict[:120]}")
        except Exception as exc:
            print(f"[jarvis] assessment skipped {mint[:8]}...: {exc}")

    async def _on_position_opened_jarvis(trade: dict) -> None:
        asyncio.create_task(_jarvis_assess_position(trade))

    runtime.register_event("position_opened", _on_position_opened_jarvis)

    # ── #1: Live position coaching — stalling or pulling back from peak ───────
    _coach_last_asked: dict[str, float] = {}
    COACH_INTERVAL = 5  # Haiku watches every 5s — memecoins move in seconds

    async def _jarvis_position_coach(data: dict) -> None:
        """Haiku watches every open position every 5s with live market data.

        Fetches DexScreener for real-time volume, buys/sells ratio, price momentum,
        and liquidity. Jarvis has FULL SELL AUTHORITY — EXIT verdict triggers an
        immediate on-chain sell via _execute_auto_close().
        """
        mint = data.get("mint", "")
        if not mint:
            return

        # Don't fire until position has been open 45s (buy tx needs to settle)
        age_secs = float(data.get("age_seconds") or 0)
        if age_secs < 45:
            return

        # Monster DNA: PumpSwap tokens peak at 14-48h, Raydium at 6-10h.
        # Haiku was exiting PumpSwap at 1-4 minutes — catastrophically early.
        # Minimum hold before Haiku can EXIT: 30min PumpSwap, 15min Raydium/Meteora.
        # Mechanical SL/trailing stop still fires immediately — this only blocks
        # Haiku's manual EXIT verdict.
        _dex_now = data.get("dex", "pumpswap")
        _min_hold_before_exit = 1800 if _dex_now == "pumpswap" else 900  # 30min / 15min
        if age_secs < _min_hold_before_exit:
            return

        now = time.time()
        if now - _coach_last_asked.get(mint, 0) < COACH_INTERVAL:
            return
        _coach_last_asked[mint] = now

        pnl_pct       = float(data.get("pnl_pct") or 0)
        current_price = float(data.get("current_price_sol") or 0)
        peak_price    = float(data.get("peak_price") or current_price)
        entry_price   = float(data.get("entry_price_sol") or 0)
        hold_secs     = float(data.get("age_seconds") or 0)
        dex           = data.get("dex", "pumpswap")
        tp1_hit       = data.get("tp1_hit", False)
        tp2_hit       = data.get("tp2_hit", False)
        symbol        = data.get("symbol") or mint[:8]

        peak_pct = ((current_price / peak_price) - 1) * 100 if peak_price > 0 else 0

        # ── Fetch live DexScreener market data ───────────────────────────────
        import aiohttp as _aio_coach
        _vol_m5 = _vol_h1 = _liq_usd = 0.0
        _buys_m5 = _sells_m5 = _buys_h1 = _sells_h1 = 0
        _chg_m5 = _chg_h1 = 0.0
        try:
            async with _aio_coach.ClientSession() as _sess_c:
                async with _sess_c.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=_aio_coach.ClientTimeout(total=6),
                ) as _r:
                    if _r.status == 200:
                        _d = await _r.json()
                        _pairs = _d.get("pairs") or []
                        if _pairs:
                            _best = max(_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                            _pc   = _best.get("priceChange") or {}
                            _tx   = _best.get("txns") or {}
                            _vl   = _best.get("volume") or {}
                            _chg_m5   = float(_pc.get("m5") or 0)
                            _chg_h1   = float(_pc.get("h1") or 0)
                            _buys_m5  = int((_tx.get("m5") or {}).get("buys") or 0)
                            _sells_m5 = int((_tx.get("m5") or {}).get("sells") or 0)
                            _buys_h1  = int((_tx.get("h1") or {}).get("buys") or 0)
                            _sells_h1 = int((_tx.get("h1") or {}).get("sells") or 0)
                            _vol_m5   = float(_vl.get("m5") or 0)
                            _vol_h1   = float(_vl.get("h1") or 0)
                            _liq_usd  = float((_best.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            pass  # Jarvis will note missing data and lean toward HOLD

        pos_mgr_svc = runtime.get_service("position_manager")
        lessons     = _load_recent_lessons(2)

        phase = (
            "moonbag (TP1+TP2 done, 12.5% remaining)" if tp2_hit else
            "post-TP1 (25% remaining)" if tp1_hit else
            "full position (pre-TP1)"
        )
        _txn_str = (
            f"{_buys_m5}B / {_sells_m5}S"
            if (_buys_m5 + _sells_m5) > 0 else "no data"
        )

        # ── Include Sonnet's latest market intelligence if fresh (<35 min) ──────
        _intel_age = time.time() - _sonnet_intel.get("timestamp", 0)
        _intel_prefix = ""
        if _sonnet_intel.get("summary") and _intel_age < 2100:
            _intel_prefix = (
                f"SONNET STRATEGY BRIEFING ({_intel_age/60:.0f} min ago):\n"
                f"{_sonnet_intel['summary']}\n\n"
            )

        # ── Live market context (Fear & Greed) ───────────────────────────────
        _fng = _fng_cache  # use cached value — don't await in hot path
        _fng_str = f"{_fng['value']}/100 ({_fng['classification']})"
        _fng_tip = (
            "Market is FEARFUL — price moves are sharper, exits should be faster."
            if _fng["value"] < 40 else
            "Market is GREEDY — dumps can reverse; don't exit too early on dips."
            if _fng["value"] > 65 else
            "Market sentiment is NEUTRAL."
        )

        prompt = (
            f"{_intel_prefix}"
            f"{lessons}"
            f"You are Jarvis — autonomous Solana memecoin trade manager with FULL SELL AUTHORITY.\n"
            f"Study the live data below and decide: EXIT now or HOLD.\n\n"
            f"TOKEN: {symbol} ({mint[:12]}...) on {dex}\n"
            f"Phase: {phase} | Hold: {hold_secs/60:.1f} min\n"
            f"Entry: {entry_price:.8f} SOL | Current: {current_price:.8f} SOL | Peak: {peak_price:.8f} SOL\n"
            f"P&L from entry: {pnl_pct:+.1f}% | Drawdown from peak: {peak_pct:+.1f}%\n\n"
            f"LIVE MARKET DATA:\n"
            f"  Price change   — m5: {_chg_m5:+.1f}%  |  h1: {_chg_h1:+.1f}%\n"
            f"  Transactions   — m5: {_txn_str}  |  h1: {_buys_h1}B / {_sells_h1}S\n"
            f"  Volume         — m5: ${_vol_m5:,.0f}  |  h1: ${_vol_h1:,.0f}\n"
            f"  Liquidity      — ${_liq_usd:,.0f}\n\n"
            f"MARKET CONTEXT: Fear & Greed = {_fng_str} — {_fng_tip}\n\n"
            f"EXIT signals: sellers > buyers, negative m5, volume collapsing, liquidity draining.\n"
            f"HOLD signals:  buyers ≥ sellers, flat or positive m5, volume steady/rising.\n"
            f"If market data is missing, lean HOLD unless P&L is deeply negative.\n\n"
            f"Reply with exactly one line: EXIT or HOLD, then a single sentence reason.\n"
            f"Example: EXIT — 3B/18S in m5 with -9% price drop and volume gone, dump in progress."
        )

        try:
            # ── Llama 3.1 via Groq: ultra-fast EXIT/HOLD verdict (low latency critical here) ──
            import groq as _groq_coach
            _groq_client = _groq_coach.AsyncGroq(
                api_key=os.getenv("GROQ_API_KEY", "")
            )
            _groq_resp = await asyncio.wait_for(
                _groq_client.chat.completions.create(
                    model="llama3.1-8b-instant",
                    max_tokens=80,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=8.0,
            )
            advice = (_groq_resp.choices[0].message.content if _groq_resp.choices else "").strip()[:300]
            if not advice:
                return

            verdict = advice.upper()[:4]
            level   = "error" if verdict == "EXIT" else "success" if verdict == "HOLD" else "warning"

            # ── Log decision to shared intelligence for Sonnet to audit ─────
            _haiku_decisions.append({
                "timestamp": time.time(),
                "mint": mint,
                "symbol": symbol,
                "dex": dex,
                "verdict": verdict,
                "pnl_pct": pnl_pct,
                "peak_pct": peak_pct,
                "chg_m5": _chg_m5,
                "buys_m5": _buys_m5,
                "sells_m5": _sells_m5,
                "liq_usd": _liq_usd,
                "reason": advice[7:80] if len(advice) > 7 else advice,
                "executed": False,  # updated to True if EXIT was actually run
            })

            if pos_mgr_svc:
                pos_mgr_svc._log_activity(  # type: ignore[union-attr]
                    level,
                    f"[jarvis-coach] {symbol}: {advice[:160]}"
                )
            print(
                f"[jarvis-coach] {mint[:8]}... "
                f"({pnl_pct:+.0f}% | peak{peak_pct:+.0f}% | m5:{_chg_m5:+.1f}% | "
                f"B{_buys_m5}/S{_sells_m5}): {advice[:120]}"
            )

            # ── EXECUTE EXIT if Jarvis says so ───────────────────────────────
            if verdict == "EXIT" and pos_mgr_svc is not None:
                pos = pos_mgr_svc.positions.get(mint)  # type: ignore[union-attr]
                if pos is not None:
                    # Safety: don't override if we're already at or above TP1
                    # (mechanical TP logic will handle the exit cleanly)
                    tp1_price = getattr(pos, "tp1_price", 0)
                    if tp1_price > 0 and current_price >= tp1_price:
                        print(
                            f"[jarvis-coach] EXIT overridden — price at/above TP1, "
                            f"letting TP logic handle {mint[:8]}"
                        )
                    else:
                        print(f"[jarvis-coach] 🤖 EXECUTING EXIT: {mint[:8]}... — {advice[:80]}")
                        pos_mgr_svc._log_activity(  # type: ignore[union-attr]
                            "error",
                            f"[jarvis-EXIT] {symbol}: selling now — {advice[:120]}"
                        )
                        # Mark this decision as executed in the shared log
                        if _haiku_decisions:
                            _haiku_decisions[-1]["executed"] = True
                        asyncio.create_task(
                            pos_mgr_svc._execute_auto_close(mint, "jarvis_exit")  # type: ignore[union-attr]
                        )

        except Exception as exc:
            print(f"[jarvis-coach] skipped {mint[:8]}...: {exc}")

    async def _on_position_update_jarvis(data: dict) -> None:
        # TP2 moonbag advice
        if data.get("tp2_hit") and not data.get("tp3_hit"):
            asyncio.create_task(_jarvis_moonbag_advice(data))
        # Position coaching (stalling/reversing)
        asyncio.create_task(_jarvis_position_coach(data))

    # ── #1: Moonbag advice at TP2 (fixed API) ────────────────────────────────
    async def _jarvis_moonbag_advice(data: dict) -> None:
        mint = data.get("mint", "")
        current_price = float(data.get("current_price_sol") or 0)
        entry_price = float(data.get("entry_price_sol") or 0)
        peak_price = float(data.get("peak_price") or current_price)
        dex = data.get("dex", "pumpswap")

        if not mint or entry_price <= 0:
            return

        gain_from_entry = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0
        gain_from_peak = ((current_price / peak_price) - 1) * 100 if peak_price > 0 else 0

        pos_mgr_svc = runtime.get_service("position_manager")
        lessons = _load_recent_lessons(2)

        prompt = (
            f"{lessons}"
            f"Solana memecoin moonbag just hit TP2 (+56%). Bot holds 12.5% with -12% trailing stop.\n\n"
            f"Token: {mint[:12]}... on {dex}\n"
            f"Entry: {entry_price:.2e} | Current: {current_price:.2e} | Peak: {peak_price:.2e}\n"
            f"Gain from entry: +{gain_from_entry:.0f}% | Drawdown from peak: {gain_from_peak:+.1f}%\n\n"
            f"1 sentence: HOLD for more upside or TIGHTEN the stop? One concrete reason."
        )
        try:
            from elizaos.types.model import ModelType, GenerateTextOptions
            result = await asyncio.wait_for(
                runtime.generate_text(
                    prompt, GenerateTextOptions(model_type=ModelType.TEXT_SMALL)
                ),
                timeout=8.0,
            )
            advice = (result.text if result else "").strip()[:200]
            if pos_mgr_svc and advice:
                pos_mgr_svc._log_activity(  # type: ignore[union-attr]
                    "success" if "HOLD" in advice.upper() else "warning",
                    f"[jarvis-moonbag] {mint[:8]}: {advice}",
                )
            print(f"[jarvis-moonbag] {mint[:8]}... {advice[:100]}")
        except Exception as exc:
            print(f"[jarvis-moonbag] skipped {mint[:8]}...: {exc}")

    runtime.register_event("position_update", _on_position_update_jarvis)

    # ── #5: Post-loss analysis → lessons.txt (persistent Eliza memory) ───────
    async def _jarvis_post_loss_analysis(data: dict) -> None:
        pnl_pct = float(data.get("pnl_pct") or 0)
        if pnl_pct >= 0 or abs(pnl_pct) < 5:
            return  # ignore wins and tiny losses

        mint = data.get("mint", "")
        reason = data.get("reason", "unknown")
        entry_price = float(data.get("entry_price_sol") or 0)
        exit_price = float(data.get("exit_price_sol") or 0)
        hold_secs = float(data.get("hold_secs") or 0)
        dex = data.get("dex", "?")
        score = data.get("score", 0)

        pos_mgr_svc = runtime.get_service("position_manager")
        mkt_ctx = _get_market_context(pos_mgr_svc) if pos_mgr_svc else ""

        prompt = (
            f"{mkt_ctx}"
            f"=== POST-LOSS ANALYSIS ===\n"
            f"Our strategy: enter early on Solana memecoins, hit +40% TP1 to recoup cost, "
            f"moonbag for free. This trade failed. Identify exactly why.\n\n"
            f"Token: {mint[:12]}... | DEX: {dex} | Score: {score}/16\n"
            f"Exit reason: {reason} | Entry: {entry_price:.2e} SOL → Exit: {exit_price:.2e} SOL\n"
            f"P&L: {pnl_pct:+.1f}% | Held: {hold_secs/60:.0f} min\n\n"
            f"Exactly 2 sentences:\n"
            f"(1) What was the specific failure mode — was it a rug (instant dump), "
            f"whale exit (gradual bleed), no real momentum (flat after entry), "
            f"or entry too late (already overextended)?\n"
            f"(2) Name ONE concrete data signal that was present at entry time that "
            f"should have flagged this — e.g. 'h1 already +350% before entry', "
            f"'holder count was just at threshold', 'token age >3h', etc."
        )
        try:
            from elizaos.types.model import ModelType, GenerateTextOptions
            result = await asyncio.wait_for(
                runtime.generate_text(
                    prompt, GenerateTextOptions(model_type=ModelType.TEXT_SMALL)
                ),
                timeout=10.0,
            )
            analysis = (result.text if result else "").strip()[:350]
            if not analysis:
                return

            _append_lesson(
                f"{mint[:8]} ({dex}) {pnl_pct:+.0f}% [{reason}]: {analysis}"
            )
            if pos_mgr_svc:
                pos_mgr_svc._log_activity(  # type: ignore[union-attr]
                    "error",
                    f"[jarvis-postmortem] {mint[:8]}: {analysis}",
                )
            print(f"[jarvis-postmortem] {mint[:8]}... lesson saved: {analysis[:120]}")
        except Exception as exc:
            print(f"[jarvis-postmortem] skipped {mint[:8]}...: {exc}")

    # Path to Jarvis's win-config memory file
    _WIN_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "packages/python/elizaos/plugins/solana/jarvis_winning_configs.json")
    try:
        _WIN_CONFIG_PATH = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "elizaos/plugins/solana/jarvis_winning_configs.json"
        )
    except Exception:
        pass

    def _save_win_config(trade_data: dict) -> None:
        """Snapshot the active config when a trade wins so Jarvis can recall what worked."""
        import json as _json
        try:
            from elizaos.plugins.solana import live_config as _lc_win
            # Keys that actually influence entry quality — what Jarvis tunes
            _SNAP_KEYS = [
                "scout_max_m5_pct", "scout_min_age_secs", "pumpswap_min_age_secs",
                "scout_min_liq_mc_ratio", "scout_max_h1_pct",
                "b_min_vol_liq_ratio", "b_max_vol_liq_ratio", "b_min_buy_ratio",
                "c_min_vol_liq_ratio", "c_max_vol_liq_ratio", "c_min_buy_ratio",
                "c_min_liq_usd", "c_min_mc_usd", "strategy_c_min_liq_usd", "strategy_c_min_score",
                "d_min_vol_liq_ratio", "d_max_vol_liq_ratio", "d_min_buy_ratio",
                "d_min_liq_usd", "d_min_mc_usd",
                "a2_min_liq_usd", "a2_min_holders", "a2_min_real_sol", "a2_max_whale_pct",
                "rugcheck_max_score", "trailing_stop_pct", "trailing_stop_enabled",
                "post_sl_cooldown_secs", "max_concurrent_positions",
            ]
            config_snap = {k: _lc_win.get(k) for k in _SNAP_KEYS if _lc_win.get(k) is not None}
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "mint": trade_data.get("mint", "")[:12],
                "symbol": trade_data.get("symbol", "?"),
                "dex": trade_data.get("dex", "?"),
                "pnl_pct": round(float(trade_data.get("pnl_pct") or 0), 1),
                "hold_mins": round(float(trade_data.get("hold_secs") or 0) / 60, 1),
                "score": trade_data.get("score", 0),
                "reason": trade_data.get("reason", "?"),
                "config_snapshot": config_snap,
            }
            # Load existing, append, keep last 50 wins
            try:
                with open(_WIN_CONFIG_PATH) as _f:
                    _existing = _json.load(_f)
            except Exception:
                _existing = {"wins": []}
            _existing.setdefault("wins", [])
            _existing["wins"].append(entry)
            _existing["wins"] = _existing["wins"][-50:]  # keep last 50
            _existing["last_updated"] = datetime.utcnow().isoformat()
            with open(_WIN_CONFIG_PATH, "w") as _f:
                _json.dump(_existing, _f, indent=2)
            print(f"[win-memory] Saved config snapshot for {entry['symbol']} ({entry['dex']}) +{entry['pnl_pct']:.0f}%")
        except Exception as _e:
            print(f"[win-memory] Failed to save config: {_e}")

    async def _on_position_closed_jarvis(data: dict) -> None:
        pnl_pct   = float(data.get("pnl_pct") or 0)
        tp1_hit   = data.get("tp1_hit", False)
        tp2_hit   = data.get("tp2_hit", False)
        tp3_hit   = data.get("tp3_hit", False)
        dex       = data.get("dex", "?")
        hold_secs = float(data.get("hold_secs") or 0)
        score     = data.get("score", 0)
        mint      = data.get("mint", "")
        reason    = data.get("reason", "?")

        if pnl_pct > 5:
            # Log winning trade patterns so Jarvis learns what success looks like
            tps = "TP3+moonbag" if tp3_hit else ("TP2" if tp2_hit else ("TP1" if tp1_hit else "profit"))
            _append_lesson(
                f"WIN: {mint[:8]} ({dex}) +{pnl_pct:.0f}% via {tps} "
                f"in {hold_secs/60:.0f}min score={score} [{reason}]"
            )
            # Save the config state that produced this win — Jarvis can recall this
            _save_win_config(data)
        else:
            asyncio.create_task(_jarvis_post_loss_analysis(data))

    runtime.register_event("position_closed", _on_position_closed_jarvis)

    # ── Social signal handler — auto-watchlist mints from social feeds ────────
    async def _on_social_signal(data: dict) -> None:
        """React to social_signal events from SocialMonitorService.

        If a tweet contains a pump.fun mint address, add it to _token_watchlist
        with +3 bonus so it gets priority if spotted on pump.fun.
        """
        pump_mints: list[str] = data.get("pump_mints", [])
        author: str = data.get("author", "unknown")
        text_snippet: str = data.get("text", "")[:60]
        ts: float = data.get("timestamp", time.time())

        for mint in pump_mints:
            if mint in _token_watchlist:
                continue  # already watchlisted
            _token_watchlist[mint] = {
                "name": f"@{author} mention",
                "reason": f"social signal: {text_snippet}",
                "added_at": ts,
                "source": "social_monitor",
            }
            print(
                f"[social] Auto-watchlisted {mint[:8]}... from @{author}: {text_snippet}…"
            )

        # Even if no direct mint, log high-signal tweets to activity feed
        if not pump_mints and data.get("has_mint"):
            print(f"[social] Signal from @{author} (non-pump mints): {text_snippet}…")

    runtime.register_event("social_signal", _on_social_signal)
    print("[scout] Social signal handler registered (auto-watchlist active)")

    # Grok social brain — look up the service for direct confirmed-token queries
    _grok_svc = runtime.get_service("social_monitor")
    print(
        f"[grok] Grok brain: {'ACTIVE — TP=200% on X-confirmed tokens' if _grok_svc and getattr(_grok_svc, '_enabled', False) else 'inactive (no API key)'}"
    )

    # Seed exit cooldown dicts from persisted trade history (survives restarts)
    pos_mgr_init = runtime.get_service("position_manager")
    if pos_mgr_init is not None:
        _now = time.time()
        for trade in pos_mgr_init.get_trade_history(limit=500):  # type: ignore[union-attr]
            if trade.get("side") != "sell":
                continue
            mint_h = trade.get("mint", "")
            ts = trade.get("timestamp", 0.0)
            if not mint_h or _now - ts > max(LOSS_COOLDOWN_SECS, ALL_EXIT_COOLDOWN_SECS):
                continue
            _all_exit_times[mint_h] = max(_all_exit_times.get(mint_h, 0.0), ts)
            pnl = trade.get("pnl_pct", 0.0) or 0.0
            reason_h = trade.get("reason", "")
            if reason_h in ("stop_loss", "early_stop_loss", "stall_exit") or pnl < -5:
                _loss_exit_times[mint_h] = max(_loss_exit_times.get(mint_h, 0.0), ts)
        print(f"[scout] Seeded cooldowns from history: {len(_all_exit_times)} all-exit, {len(_loss_exit_times)} loss-exit")

    # Brief warm-up so services finish initialising
    await asyncio.sleep(15)

    # ── slow path: poll DexScreener every 90s as fallback ────────────────────
    while True:
        try:
            monitor_svc = runtime.get_service("token_monitor")
            pos_mgr = runtime.get_service("position_manager")
            wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)

            if None in (pos_mgr, wallet_svc):
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            wallet_sol = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
            actual_buy = _dynamic_buy_sol(BUY_SOL, wallet_sol)
            allowed, block_reason = pos_mgr.check_trade_allowed(actual_buy, wallet_sol)  # type: ignore[union-attr]
            if not allowed:
                print(f"[scout] Trading gated: {block_reason}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            _sl_gap = time.time() - _last_sl_exit_time
            if _sl_gap < _get_post_sl_cooldown():
                print(f"[scout] Post-SL cooldown active — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # WebSocket stream preferred; DexScreener if stream is empty
            ws_launches = monitor_svc.get_recent_launches(30) if monitor_svc else []
            if ws_launches:
                launches = ws_launches
                print(f"[scout] Scanning {len(launches)} launches (WebSocket stream)")
            else:
                launches = await _fetch_recent_pump_fun_creates(limit=20)
                print(f"[scout] Scanning {len(launches)} recent creates (DexScreener)")

            # Enrich with bonding curve data
            pump_svc = runtime.get_service("token_data")
            for launch in launches:
                if launch.get("price_sol", 0.0) == 0.0 and pump_svc is not None:
                    try:
                        bc = await pump_svc.get_bonding_curve(launch["mint"])  # type: ignore[union-attr]
                        if bc:
                            launch["price_sol"] = bc.get("price_sol", 0.0)
                            launch["progress_pct"] = bc.get("progress_pct", 0.0)
                            launch["complete"] = bc.get("complete", False)
                    except Exception:
                        pass

            bought_this_cycle = False
            for launch in launches:
                mint = launch.get("mint", "")
                # Pump.fun bonding-curve mints always end with "pump" — skip others
                if not mint or not mint.lower().endswith("pump"):
                    continue
                if mint in evaluated or mint in pos_mgr.positions:  # type: ignore[union-attr]
                    continue
                bought = await _evaluate_and_maybe_buy(
                    runtime, launch, pos_mgr, evaluated,
                    PAPER_TRADING, actual_buy,
                    SCOUT_USER_ID, SCOUT_ROOM_ID, MIN_SCORE,
                )
                if bought:
                    bought_this_cycle = True
                    break  # one trade per cycle

            if not bought_this_cycle:
                active = [la for la in launches if not la.get("complete")]
                print(
                    f"[scout] Cycle complete -- {len(active)} active tokens scanned, "
                    f"no qualifying entries this cycle"
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[scout] Unexpected error: {exc}", file=sys.stderr)

        await asyncio.sleep(SCAN_INTERVAL)


async def _test_sellable(mint: str, pool_order: list[str], public_key: str) -> bool:
    """Ask PumpPortal if it can route a sell for this token WITHOUT executing it.

    Sends a sell request for 1 raw unit — cheapest possible check.
    We read the HTTP status only; we never sign or broadcast the tx.
    Returns True if at least one pool responds HTTP 200 (sellable).
    Returns True on network errors so we don't block trades on transient failures.
    """
    import aiohttp as _ah
    PUMPPORTAL = "https://pumpportal.fun/api/trade-local"
    try:
        async with _ah.ClientSession(timeout=_ah.ClientTimeout(total=6)) as s:
            for pool in pool_order:
                payload = {
                    "publicKey": public_key,
                    "action": "sell",
                    "mint": mint,
                    "amount": 1,          # 1 raw unit — never signed or sent
                    "denominatedInSol": "false",
                    "slippage": 50,
                    "priorityFee": 0.0001,
                    "pool": pool,
                }
                async with s.post(PUMPPORTAL, json=payload) as r:
                    if r.status == 200:
                        return True
    except Exception:
        return True  # network error → don't block the trade
    return False


async def _fetch_single_pumpswap_token(mint: str) -> dict | None:
    """Fetch DexScreener data for a single graduated PumpSwap token.

    Used by the graduation watch queue — looks up a specific mint directly
    instead of waiting for it to appear in the profiles feed.
    Returns a token dict compatible with _score_raydium_token, or None.
    """
    import aiohttp as _aiohttp

    TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
    _PSWAP_DEX_IDS = {"pumpswap", "pump-amm", "pumpfun-amm"}

    try:
        timeout = _aiohttp.ClientTimeout(total=10)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{TOKENS_URL}/{mint}") as r:
                if r.status != 200:
                    return None
                data = await r.json()

        pairs = data.get("pairs") or []
        now_ms = time.time() * 1000

        # Find best PumpSwap pair for this mint
        pswap_pairs = []
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            if p.get("dexId", "") not in _PSWAP_DEX_IDS:
                continue
            created_ms = p.get("pairCreatedAt") or 0
            age_mins = (now_ms - created_ms) / 60000 if created_ms else 0
            if age_mins > 360:  # older than 6h, skip
                continue
            pswap_pairs.append((p, age_mins))

        if not pswap_pairs:
            return None

        best, age_mins = max(pswap_pairs, key=lambda x: float((x[0].get("liquidity") or {}).get("usd") or 0))
        vol = best.get("volume", {})
        liq = best.get("liquidity", {})
        price_chg = best.get("priceChange", {})
        base_token = best.get("baseToken", {})
        pair_info = best.get("info", {})
        pair_socials = pair_info.get("socials", []) or []
        pair_websites = pair_info.get("websites", []) or []
        has_social = bool(pair_socials or pair_websites)
        social_urls = [s.get("url", "") for s in pair_socials + pair_websites if s.get("url")]
        txns = best.get("txns", {})

        return {
            "mint": mint,
            "name": base_token.get("name", ""),
            "symbol": base_token.get("symbol", ""),
            "price_sol": float(best.get("priceNative") or 0),
            "price_usd": float(best.get("priceUsd") or 0),
            "volume_h1_usd": float(vol.get("h1") or 0),
            "volume_h6_usd": float(vol.get("h6") or 0),
            "volume_h24_usd": float(vol.get("h24") or 0),
            "liquidity_usd": float(liq.get("usd") or 0),
            "price_change_m5": float(price_chg.get("m5") or 0),
            "price_change_h1": float(price_chg.get("h1") or 0),
            "price_change_h6": float(price_chg.get("h6") or 0),
            "pair_created_at": best.get("pairCreatedAt") or 0,
            "market_cap_usd": float(best.get("marketCap") or best.get("fdv") or 0),
            "txns_h1_buys": int((txns.get("h1") or {}).get("buys") or 0),
            "txns_h1_sells": int((txns.get("h1") or {}).get("sells") or 0),
            "txns_m5_buys": int((txns.get("m5") or {}).get("buys") or 0),
            "dex": "pumpswap",
            "_source": "grad_watch_queue",
            "_age_known": True,
            "_has_social": has_social,
            "_social_urls": social_urls,
        }
    except Exception:
        return None


async def _fetch_graduated_raydium_tokens(limit: int = 10) -> list[dict]:
    """Find tokens to trade via Strategy C: Raydium, Orca, or PumpSwap second-wave.

    Strategy:
      1. GET DexScreener /token-profiles/latest/v1  (recent Solana profiles)
      2. For each, look for Raydium/Orca pairs (direct listings) OR PumpSwap pairs
         that are 15 min–6 hours old (graduated, survived initial dump, still running)
      3. Return tokens sorted by 24h volume (highest first)

    The PumpSwap second-wave picks up tokens that Strategy B missed at graduation
    but which proved their strength by sustaining volume and price for 15+ minutes.
    """
    import aiohttp as _aiohttp

    PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
    TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"

    try:
        timeout = _aiohttp.ClientTimeout(total=20)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(PROFILES_URL) as resp:
                resp.raise_for_status()
                profiles: list[dict] = await resp.json()

            # Filter: Solana, not pump.fun, valid address
            # Include ALL Solana tokens — graduated pump.fun tokens still end in "pump"
            # but will have Raydium/Orca pairs. The inner dexId filter handles the selection.
            candidates = [
                p for p in profiles
                if p.get("chainId") == "solana"
                and len(p.get("tokenAddress", "")) >= 32
            ][: limit * 4]  # fetch extra since many won't have Raydium/Orca pairs

            if not candidates:
                return []

            async def _fetch_raydium_pair(profile: dict) -> dict | None:
                mint = profile.get("tokenAddress", "")
                # Extract social/website links from the profile record
                profile_links: list[dict] = profile.get("links", [])
                has_social = any(
                    lk.get("type") in ("twitter", "website", "telegram", "discord")
                    for lk in profile_links
                )
                social_urls = [lk.get("url", "") for lk in profile_links if lk.get("url")]
                try:
                    async with session.get(f"{TOKENS_URL}/{mint}") as r:
                        r.raise_for_status()
                        data = await r.json()
                    pairs = data.get("pairs") or []
                    _RAYDIUM_DEX_IDS = {"raydium", "raydium-clmm"}
                    _ORCA_DEX_IDS = {"orca", "orca-whirlpool"}
                    _PSWAP_DEX_IDS = {"pumpswap", "pump-amm", "pumpfun-amm"}
                    _ALL_TARGET = _RAYDIUM_DEX_IDS | _ORCA_DEX_IDS | _PSWAP_DEX_IDS
                    now_ms = time.time() * 1000

                    candidate_pairs = []
                    for p in pairs:
                        if p.get("chainId") != "solana":
                            continue
                        dex = p.get("dexId", "")
                        if dex not in _ALL_TARGET:
                            continue
                        # For PumpSwap: allow from 5 min post-graduation (down from 15) so we
                        # catch tokens earlier in their run. Liq + score filters protect us.
                        if dex in _PSWAP_DEX_IDS:
                            created_ms = p.get("pairCreatedAt") or 0
                            age_mins = (now_ms - created_ms) / 60000 if created_ms else 0
                            if not (5 <= age_mins <= 360):
                                continue
                        candidate_pairs.append(p)

                    if not candidate_pairs:
                        return None
                    # CRITICAL: Pick highest-LIQUIDITY pair, NOT highest volume.
                    # After graduation the drained bonding-curve pool still has high h24
                    # volume (it was the active pool pre-graduation) but minimal liquidity.
                    # Picking by volume selects the wrong (stale BC) pool and gives wrong liq/price.
                    best = max(
                        candidate_pairs,
                        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                    )
                    best_dex = best.get("dexId", "")
                    if best_dex in _ORCA_DEX_IDS:
                        _dex_label = "orca"
                    elif best_dex in _PSWAP_DEX_IDS:
                        _dex_label = "pumpswap"
                    else:
                        _dex_label = "raydium"
                    vol = best.get("volume", {})
                    liq = best.get("liquidity", {})
                    price_chg = best.get("priceChange", {})
                    base_token = best.get("baseToken", {})
                    # Also pick up social from pair-level info (DexScreener may have it here too)
                    pair_info = best.get("info", {})
                    pair_socials = pair_info.get("socials", []) or []
                    pair_websites = pair_info.get("websites", []) or []
                    if not has_social and (pair_socials or pair_websites):
                        has_social = True
                        social_urls = [s.get("url", "") for s in pair_socials + pair_websites if s.get("url")]
                    _txns = best.get("txns", {})
                    return {
                        "mint": mint,
                        "pair_address": best.get("pairAddress", ""),
                        "name": base_token.get("name", ""),
                        "symbol": base_token.get("symbol", ""),
                        "price_sol": float(best.get("priceNative") or 0),
                        "price_usd": float(best.get("priceUsd") or 0),
                        "volume_h1_usd": float(vol.get("h1") or 0),
                        "volume_h6_usd": float(vol.get("h6") or 0),
                        "volume_h24_usd": float(vol.get("h24") or 0),
                        "liquidity_usd": float(liq.get("usd") or 0),
                        "price_change_h1": float(price_chg.get("h1") or 0),
                        "price_change_h6": float(price_chg.get("h6") or 0),
                        "price_change_h24": float(price_chg.get("h24") or 0),
                        "price_change_m5": float(price_chg.get("m5") or 0),
                        "pair_created_at": best.get("pairCreatedAt") or 0,  # unix ms
                        "age_mins": (time.time() - (best.get("pairCreatedAt") or 0) / 1000.0) / 60.0 if (best.get("pairCreatedAt") or 0) > 0 else 0.0,
                        "market_cap_usd": float(best.get("marketCap") or best.get("fdv") or 0),
                        "txns_h1_buys": int((_txns.get("h1") or {}).get("buys") or 0),
                        "txns_h1_sells": int((_txns.get("h1") or {}).get("sells") or 0),
                        "txns_m5_buys": int((_txns.get("m5") or {}).get("buys") or 0),
                        "dex": _dex_label,
                        "_source": f"dexscreener_{_dex_label}_c",
                        "_age_known": True,
                        "_has_social": has_social,
                        "_social_urls": social_urls,
                    }
                except Exception:
                    return None

            tasks = [_fetch_raydium_pair(p) for p in candidates]
            raw = await asyncio.gather(*tasks, return_exceptions=True)

            results = []
            for r in raw:
                if isinstance(r, dict):
                    results.append(r)
                    if len(results) >= limit:
                        break

            # Sort by 24h volume descending (most active first)
            results.sort(key=lambda t: t.get("volume_h24_usd", 0), reverse=True)
            return results

    except Exception as exc:
        print(f"[raydium-scout] DexScreener fetch failed: {exc}", file=sys.stderr)
        return []


async def _fetch_native_raydium_tokens(limit: int = 10) -> list[dict]:
    """Find tokens freshly listed on native Raydium Standard (V4 AMM) pools.

    Strategy:
      1. GET Raydium v3 API for Standard pools sorted by 24h volume (5 pages × 100)
      2. Filter: WSOL-quote, liq $1k–$500k, V/L < 100x, has openTime (newer pools)
      3. Sort: 20% newest × 80% volume to balance recency vs activity
      4. Enrich top candidates with DexScreener for h1 vol, price change, social data
    """
    import aiohttp as _aiohttp

    WSOL = "So11111111111111111111111111111111111111112"
    RAYDIUM_URL = "https://api-v3.raydium.io/pools/info/list"
    DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"

    try:
        timeout = _aiohttp.ClientTimeout(total=25)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            # ── 1. Fetch Raydium Standard pools — scan wide for fresh tokens ──
            # Raydium API does not support openTime sort (returns 500).
            # volume24h sort buries new tokens in pages 3-8. Scan 10 pages
            # then filter by openTime < 48h to find fresh pairs.
            all_pools: list[dict] = []
            for page in range(1, 11):  # 10 pages × 100 = 1000 pools, filter for fresh
                url = (
                    f"{RAYDIUM_URL}?poolType=standard&poolSortField=volume24h"
                    f"&sortType=desc&pageSize=100&page={page}"
                )
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                    page_pools = (data.get("data") or {}).get("data", [])
                    if not page_pools:
                        break
                    all_pools.extend(page_pools)
                except Exception:
                    break

            if not all_pools:
                return []

            # ── 2. Filter for WSOL-quote with real liquidity ───────────────────
            now_ts = time.time()
            qualified: list[tuple] = []
            for p in all_pools:
                mintA = p.get("mintA") or {}
                mintB = p.get("mintB") or {}
                if mintB.get("address") == WSOL:
                    token = mintA
                elif mintA.get("address") == WSOL:
                    token = mintB
                else:
                    continue

                mint = token.get("address", "")
                if not mint or len(mint) < 32:
                    continue

                liq = float(p.get("tvl") or 0)
                vol24 = float((p.get("day") or {}).get("volume") or 0)
                open_time = int(p.get("openTime") or 0)

                if liq < 500 or liq > 500_000:
                    continue
                if vol24 > 0 and (vol24 / liq) > 200:
                    continue  # wash-trade filter
                if open_time == 0:
                    continue  # no timestamp = old/ambiguous pool

                age_h = (now_ts - open_time) / 3600
                if age_h > 72:  # tokens up to 72h — monster data shows TARO peaked at 189h
                    continue

                # Composite score: heavily weight recency — we want fresh pairs
                recency_score = max(0.0, 1.0 - age_h / 48)   # 1.0 = just created, 0 = 48h old
                vol_score = min(vol24 / 500_000, 1.0)          # 1.0 = $500k daily vol
                composite = 0.7 * recency_score + 0.3 * vol_score
                qualified.append((composite, mint, age_h, vol24, liq, token, p))

            qualified.sort(key=lambda x: x[0], reverse=True)
            top_candidates = qualified[: limit * 3]

            if not top_candidates:
                return []

            # ── 3. Enrich with DexScreener ────────────────────────────────────
            async def _enrich_raydium(composite: float, mint: str, age_h: float,
                                      vol24: float, liq: float, token: dict, rp: dict) -> dict | None:
                try:
                    async with session.get(f"{DEXSCREENER_URL}/{mint}") as r:
                        if r.status != 200:
                            raise ValueError(f"HTTP {r.status}")
                        ds_data = await r.json()
                    pairs = ds_data.get("pairs") or []

                    # Prefer native Raydium pairs; fall back to any SOL-quote pair
                    ray_pairs = [
                        p for p in pairs
                        if p.get("chainId") == "solana"
                        and p.get("dexId") in ("raydium", "raydium-clmm")
                        and (p.get("quoteToken") or {}).get("address") == WSOL
                    ]
                    sol_pairs = [
                        p for p in pairs
                        if p.get("chainId") == "solana"
                        and (p.get("quoteToken") or {}).get("address") == WSOL
                    ]
                    best_ds = None
                    if ray_pairs:
                        best_ds = max(ray_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                    elif sol_pairs:
                        best_ds = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

                    if best_ds:
                        price_chg = best_ds.get("priceChange") or {}
                        pinfo = best_ds.get("info") or {}
                        socials = pinfo.get("socials") or []
                        websites = pinfo.get("websites") or []
                        has_social = bool(socials or websites)
                        social_urls = [s.get("url", "") for s in socials + websites if s.get("url")]
                        txns = best_ds.get("txns") or {}
                        name = (best_ds.get("baseToken") or {}).get("name", token.get("name", ""))
                        symbol = (best_ds.get("baseToken") or {}).get("symbol", token.get("symbol", ""))
                        price_sol = float(best_ds.get("priceNative") or 0)
                        ds_liq = float((best_ds.get("liquidity") or {}).get("usd") or 0)
                        ds_vol_h1 = float((best_ds.get("volume") or {}).get("h1") or 0)
                        ds_vol_h24 = float((best_ds.get("volume") or {}).get("h24") or 0)
                        pair_address = best_ds.get("pairAddress", "")
                        pair_created_at = best_ds.get("pairCreatedAt") or 0
                        price_change_h1 = float(price_chg.get("h1") or 0)
                        price_change_h6 = float(price_chg.get("h6") or 0)
                        price_change_h24 = float(price_chg.get("h24") or 0)
                        price_change_m5 = float(price_chg.get("m5") or 0)
                        txns_h1_buys = int((txns.get("h1") or {}).get("buys") or 0)
                        txns_m5_buys = int((txns.get("m5") or {}).get("buys") or 0)
                    else:
                        # No DexScreener coverage — use Raydium API data
                        has_social = False
                        social_urls = []
                        name = token.get("name", "")
                        symbol = token.get("symbol", "")
                        price_sol = 0.0  # will be fetched at buy time
                        ds_liq = liq
                        ds_vol_h1 = 0.0
                        ds_vol_h24 = vol24
                        pair_address = rp.get("id", "")
                        pair_created_at = int(rp.get("openTime") or 0) * 1000  # to ms
                        price_change_h1 = 0.0
                        price_change_h6 = 0.0
                        price_change_h24 = 0.0
                        price_change_m5 = None  # unknown
                        txns_h1_buys = 0
                        txns_m5_buys = 0

                    if price_sol <= 0:
                        return None  # can't trade without a price

                    return {
                        "mint": mint,
                        "pair_address": pair_address,
                        "name": name,
                        "symbol": symbol,
                        "price_sol": price_sol,
                        "price_usd": 0.0,
                        "volume_h1_usd": ds_vol_h1,
                        "volume_h6_usd": 0.0,
                        "volume_h24_usd": ds_vol_h24 or vol24,
                        "liquidity_usd": ds_liq or liq,
                        "price_change_h1": price_change_h1,
                        "price_change_h6": price_change_h6,
                        "price_change_h24": price_change_h24,
                        "price_change_m5": price_change_m5,
                        "pair_created_at": pair_created_at,
                        "age_mins": (time.time() - pair_created_at / 1000.0) / 60.0 if pair_created_at > 0 else 0.0,
                        "market_cap_usd": 0.0,
                        "txns_h1_buys": txns_h1_buys,
                        "txns_m5_buys": txns_m5_buys,
                        "dex": "raydium",
                        "_source": "raydium_v3_standard",
                        "_age_known": True,
                        "_has_social": has_social,
                        "_social_urls": social_urls,
                    }
                except Exception:
                    return None

            tasks = [_enrich_raydium(*c) for c in top_candidates]
            raw = await asyncio.gather(*tasks, return_exceptions=True)

            results = [r for r in raw if isinstance(r, dict)]
            results.sort(key=lambda t: t.get("volume_h24_usd", 0), reverse=True)
            return results[:limit]

    except Exception as exc:
        print(f"[raydium-scout] Raydium v3 fetch failed: {exc}", file=sys.stderr)
        return []


async def _fetch_meteora_tokens(limit: int = 10) -> list[dict]:
    """Find active tokens trading on Meteora DLMM pools.

    Strategy:
      1. GET DexScreener search results for Meteora/Solana pairs (3 search terms)
      2. Deduplicate by mint, filter size/liq
      3. Score by h1_volume/liquidity ratio (high ratio = hot activity right now)
      4. Enrich top candidates with DexScreener token data for price change + social signals

    Note: Meteora's own DLMM API went offline 2026-04-01.
          GeckoTerminal replaced it but returned 403 from 2026-04-03.
          DexScreener search is the current working source.
    """
    import aiohttp as _aiohttp

    DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"
    DS_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
    HEADERS = {"User-Agent": "Mozilla/5.0"}

    try:
        timeout = _aiohttp.ClientTimeout(total=25)
        async with _aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
            # ── 1. Fetch Meteora pairs from DexScreener search (3 queries) ──────
            all_ds_pairs: list[dict] = []
            for term in ["WSOL meteora", "SOL meteora", "pump meteora"]:
                try:
                    async with session.get(DS_SEARCH_URL, params={"q": term}) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                    for p in data.get("pairs") or []:
                        if (p.get("chainId") == "solana"
                                and "meteora" in (p.get("dexId") or "").lower()):
                            all_ds_pairs.append(p)
                except Exception:
                    continue

            if not all_ds_pairs:
                return []

            # ── 2. Deduplicate by base mint, keep highest-liquidity pair ──────
            best_by_mint: dict[str, dict] = {}
            for p in all_ds_pairs:
                mint = (p.get("baseToken") or {}).get("address", "")
                if not mint or len(mint) < 32:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                existing = best_by_mint.get(mint)
                if existing is None or liq > float((existing.get("liquidity") or {}).get("usd") or 0):
                    best_by_mint[mint] = p

            # ── 3. Score by h1_vol/liquidity ratio ────────────────────────────
            scored: list[tuple[float, str, dict]] = []
            for mint, p in best_by_mint.items():
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                v_h1 = float((p.get("volume") or {}).get("h1") or 0)
                if liq < 500 or liq > 500_000 or v_h1 < 500:
                    continue
                ratio = v_h1 / liq
                txns_h1 = (p.get("txns") or {}).get("h1") or {}
                compat: dict = {
                    "mint_x": mint,
                    "address": p.get("pairAddress", ""),
                    "name": (p.get("baseToken") or {}).get("name", ""),
                    "liquidity": liq,
                    "volume": {"hour_1": v_h1},
                    "trade_volume_24h": float((p.get("volume") or {}).get("h24") or 0),
                    "current_price": float(p.get("priceNative") or 0),
                    "_gt_txns_h1_buys": int(txns_h1.get("buys") or 0),
                    "_gt_txns_h1_sells": int(txns_h1.get("sells") or 0),
                }
                scored.append((ratio, mint, compat))

            scored.sort(key=lambda x: x[0], reverse=True)
            top_candidates = scored[: limit * 3]  # enrich top N×3, return best N

            if not top_candidates:
                return []

            # ── 4. Enrich with DexScreener for price change + social ──────────
            async def _enrich(ratio: float, mint: str, mp: dict) -> dict | None:
                try:
                    async with session.get(f"{DEXSCREENER_URL}/{mint}") as r:
                        if r.status != 200:
                            raise ValueError(f"HTTP {r.status}")
                        ds_data = await r.json()
                    pairs = ds_data.get("pairs") or []
                    ds_pairs = [
                        p for p in pairs
                        if p.get("chainId") == "solana"
                        and "meteora" in (p.get("dexId") or "").lower()
                    ]
                    # Find the best DexScreener pair for pricing — prefer Meteora pairs,
                    # fall back to any SOL-quote pair (same price source as the position monitor).
                    sol_pairs_ds = [
                        p for p in pairs
                        if p.get("chainId") == "solana"
                        and (p.get("quoteToken") or {}).get("address") == "So11111111111111111111111111111111111111112"
                    ]
                    best_any_ds = (
                        max(sol_pairs_ds, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                        if sol_pairs_ds else None
                    )

                    # Initialise txn counters (populated from whichever ds pair we use)
                    txns_h1_buys = 0
                    txns_h1_sells = 0

                    if ds_pairs:
                        best_ds = max(ds_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
                        price_chg = best_ds.get("priceChange") or {}
                        price_change_h1 = float(price_chg.get("h1") or 0)
                        price_change_h6 = float(price_chg.get("h6") or 0)
                        price_change_h24 = float(price_chg.get("h24") or 0)
                        price_change_m5 = float(price_chg.get("m5") or 0)
                        pair_info = best_ds.get("info") or {}
                        socials = pair_info.get("socials") or []
                        websites = pair_info.get("websites") or []
                        has_social = bool(socials or websites)
                        social_urls = [s.get("url", "") for s in socials + websites if s.get("url")]
                        pair_address = best_ds.get("pairAddress", "")
                        pair_created_at = best_ds.get("pairCreatedAt") or 0
                        name = (best_ds.get("baseToken") or {}).get("name", mp.get("name", ""))
                        symbol = (best_ds.get("baseToken") or {}).get("symbol", "")
                        ds_liq_usd = float((best_ds.get("liquidity") or {}).get("usd") or 0)
                        ds_vol_h1 = float((best_ds.get("volume") or {}).get("h1") or 0)
                        ds_vol_h24 = float((best_ds.get("volume") or {}).get("h24") or 0)
                        price_sol = float(best_ds.get("priceNative") or 0) or float(mp.get("current_price") or 0)
                        _ds_txns = (best_ds.get("txns") or {}).get("h1") or {}
                        txns_h1_buys = int(_ds_txns.get("buys") or 0)
                        txns_h1_sells = int(_ds_txns.get("sells") or 0)
                    elif best_any_ds:
                        # DexScreener has non-Meteora SOL pairs — use for price consistency
                        price_chg = best_any_ds.get("priceChange") or {}
                        price_change_h1 = float(price_chg.get("h1") or 0)
                        price_change_h6 = float(price_chg.get("h6") or 0)
                        price_change_h24 = float(price_chg.get("h24") or 0)
                        price_change_m5 = float(price_chg.get("m5") or 0)
                        pair_info = best_any_ds.get("info") or {}
                        socials = pair_info.get("socials") or []
                        websites = pair_info.get("websites") or []
                        has_social = bool(socials or websites)
                        social_urls = [s.get("url", "") for s in socials + websites if s.get("url")]
                        pair_address = mp.get("address", "")
                        pair_created_at = best_any_ds.get("pairCreatedAt") or 0
                        name = (best_any_ds.get("baseToken") or {}).get("name", "")
                        name_parts = (mp.get("name") or "").split("-")
                        if not name:
                            name = name_parts[0] if name_parts else ""
                        symbol = (best_any_ds.get("baseToken") or {}).get("symbol", name)
                        ds_liq_usd = float((best_any_ds.get("liquidity") or {}).get("usd") or 0)
                        ds_vol_h1 = float((best_any_ds.get("volume") or {}).get("h1") or 0)
                        ds_vol_h24 = float((best_any_ds.get("volume") or {}).get("h24") or 0)
                        price_sol = float(best_any_ds.get("priceNative") or 0) or float(mp.get("current_price") or 0)
                        _ds_txns = (best_any_ds.get("txns") or {}).get("h1") or {}
                        txns_h1_buys = int(_ds_txns.get("buys") or 0)
                        txns_h1_sells = int(_ds_txns.get("sells") or 0)
                    else:
                        # No DexScreener data — use GeckoTerminal compat dict data
                        price_change_h1 = 0.0
                        price_change_h6 = 0.0
                        price_change_h24 = 0.0
                        price_change_m5 = 0.0
                        has_social = False
                        social_urls = []
                        pair_address = mp.get("address", "")
                        pair_created_at = 0
                        vol = mp.get("volume") or {}
                        name_parts = (mp.get("name") or "").split("/")
                        name = name_parts[0].strip() if name_parts else ""
                        symbol = name
                        ds_liq_usd = float(mp.get("liquidity") or 0)
                        ds_vol_h1 = float(vol.get("hour_1") or 0)
                        ds_vol_h24 = float(mp.get("trade_volume_24h") or 0)
                        price_sol = float(mp.get("current_price") or 0)
                        txns_h1_buys = int(mp.get("_gt_txns_h1_buys") or 0)
                        txns_h1_sells = int(mp.get("_gt_txns_h1_sells") or 0)

                    return {
                        "mint": mint,
                        "pair_address": pair_address,
                        "name": name,
                        "symbol": symbol,
                        "price_sol": price_sol,
                        "price_usd": 0.0,  # not critical for trading
                        "volume_h1_usd": ds_vol_h1,
                        "volume_h6_usd": 0.0,
                        "volume_h24_usd": ds_vol_h24,
                        "liquidity_usd": ds_liq_usd or float(mp.get("liquidity") or 0),
                        "price_change_h1": price_change_h1,
                        "price_change_h6": price_change_h6,
                        "price_change_h24": price_change_h24,
                        "price_change_m5": price_change_m5,
                        "pair_created_at": pair_created_at,
                        "age_mins": (time.time() - pair_created_at / 1000.0) / 60.0 if pair_created_at > 0 else 0.0,
                        "dex": "meteora",
                        "_source": "meteora_dlmm",
                        "_age_known": pair_created_at > 0,
                        "_has_social": has_social,
                        "_social_urls": social_urls,
                        "_activity_ratio": ratio,
                        "txns_h1_buys": txns_h1_buys,
                        "txns_h1_sells": txns_h1_sells,
                        "vol_liq_ratio": round(ds_vol_h1 / ds_liq_usd, 1) if ds_liq_usd > 0 else 0.0,
                    }
                except Exception:
                    return None

            tasks = [_enrich(r, m, p) for r, m, p in top_candidates]
            raw = await asyncio.gather(*tasks, return_exceptions=True)

            results = [r for r in raw if isinstance(r, dict)]
            # Sort by activity ratio descending (already pre-scored)
            results.sort(key=lambda t: t.get("_activity_ratio", 0), reverse=True)
            return results[:limit]

    except Exception as exc:
        print(f"[meteora-scout] Meteora API fetch failed: {exc}", file=sys.stderr)
        return []


def _score_raydium_token(token: dict, now: float) -> tuple[int, list[str]]:
    """Score a post-graduation PumpSwap/Raydium token (0–16 pre-safety).

    Scoring breakdown:
      VOLUME (max +3)
        +3  volume h24 > $100k
        +2  volume h24 $20k–100k
        +1  volume h24 $3k–20k

      LIQUIDITY (max +2)
        +2  liquidity > $15k
        +1  liquidity $5k–15k

      PAIR AGE (max +2)
        +2  pair age < 2h  (hottest entry window)
        +1  pair age 2–6h

      VOLUME MOMENTUM (max +2) — h1 vol share of h24
        +2  h1 > 25% of h24 (explosive)
        +1  h1 10–25% of h24

      BUYER ACTIVITY (max +2) — h1 buy transaction count (proxy for unique holders)
        +2  h1 buys >= 200 (very active community, many distinct buyers)
        +1  h1 buys >= 60  (moderate organic demand)

      SOCIAL PRESENCE (max +2)
        +2  has Twitter/website

      NARRATIVE KEYWORD (max +2)
        +2  viral/celebrity keyword in name or symbol

      PRICE DIRECTION h1 (max +1)
        +1  h1 > +3%
        -1  h1 < -10%

      M5 MOMENTUM (max +1) — is it pumping RIGHT NOW?
        +1  m5 > +2%  (actively running this minute)

    Hard rejects: crashed h1 ≤ -30%, m5 ≤ -15% (momentum dead), wash-trade V/L > 100x.
    """
    volume_h24 = token.get("volume_h24_usd", 0.0)
    volume_h1 = token.get("volume_h1_usd", 0.0)
    liquidity = token.get("liquidity_usd", 0.0)
    price_change_h1 = token.get("price_change_h1", 0.0)
    price_change_h6 = token.get("price_change_h6", None)   # None = not available
    price_change_h24 = token.get("price_change_h24", None)  # None = not available
    price_change_m5 = token.get("price_change_m5", None)  # None = not available
    pair_created_ms = token.get("pair_created_at", 0) or 0
    pair_age_secs = (now - pair_created_ms / 1000.0) if pair_created_ms > 0 else None
    name = token.get("name", "")
    symbol = token.get("symbol", "")
    has_social = token.get("_has_social", False)

    score = 0
    reasons: list[str] = []

    # ── HARD REJECTS: Multi-timeframe trend analysis ─────────────────────────
    # These checks must run BEFORE scoring to avoid wasting time on dead tokens.

    # REJECT: structural decline — h24 deeply negative means the token already pumped
    # and is in a multi-hour downtrend. A brief m5/h1 bounce does NOT reverse this.
    # Example: SCUBA peaked 4 days ago, now -78% from ATH, briefly +3.7% on h4.
    if price_change_h24 is not None and price_change_h24 <= -25:
        reasons.append(
            f"REJECTED: structural decline h24={price_change_h24:.0f}% "
            f"— token already had its pump, don't buy the grave"
        )
        return -10, reasons

    # REJECT: h6 shows recent sharp decline — even if h24 looks moderate
    if price_change_h6 is not None and price_change_h6 <= -20:
        reasons.append(
            f"REJECTED: recent collapse h6={price_change_h6:.0f}% "
            f"— active downtrend in last 6 hours"
        )
        return -10, reasons

    # REJECT: token older than 24h — scouts are for fresh launches only.
    # Old tokens have lower alpha, higher rug risk, and declining holder base.
    if pair_age_secs is not None and pair_age_secs > 86_400:  # > 24h
        reasons.append(
            f"REJECTED: token is {pair_age_secs/3600:.0f}h old "
            f"— scouts target fresh launches only (< 24h)"
        )
        return -10, reasons
    reasons: list[str] = []

    # Wash-trade pre-filter — vol/liq > 100x means volume is circular bot-trading, not real demand.
    # Lowered from 200x to 100x: 9at8bPpW had 6011% which passed 200x and was unexitable.
    if volume_h24 > 0 and liquidity > 0:
        vl_ratio = volume_h24 / liquidity
        if vl_ratio > 100:
            reasons.append(f"⚠ wash-trade V/L {vl_ratio:.0f}x")
            return -5, reasons

    # Volume scoring
    if volume_h24 >= 100_000:
        score += 3
        reasons.append(f"vol ${volume_h24:,.0f} (+3)")
    elif volume_h24 >= 20_000:
        score += 2
        reasons.append(f"vol ${volume_h24:,.0f} (+2)")
    elif volume_h24 >= 3_000:
        score += 1
        reasons.append(f"vol ${volume_h24:,.0f} (+1)")
    else:
        reasons.append(f"vol ${volume_h24:,.0f} (low)")

    # Liquidity scoring
    if liquidity >= 15_000:
        score += 2
        reasons.append(f"liq ${liquidity:,.0f} (+2)")
    elif liquidity >= 5_000:
        score += 1
        reasons.append(f"liq ${liquidity:,.0f} (+1)")
    else:
        reasons.append(f"liq ${liquidity:,.0f} (thin)")

    # Pair age — freshness of pool
    if pair_age_secs is not None:
        if pair_age_secs < 7_200:   # < 2h
            score += 2
            reasons.append(f"age {pair_age_secs/3600:.1f}h (fresh +2)")
        elif pair_age_secs < 21_600:  # < 6h
            score += 1
            reasons.append(f"age {pair_age_secs/3600:.1f}h (+1)")
        else:
            reasons.append(f"age {pair_age_secs/3600:.1f}h (old)")
    else:
        reasons.append("age unknown")

    # Volume momentum — is it pumping RIGHT NOW?
    if volume_h24 > 0 and volume_h1 > 0:
        momentum_pct = (volume_h1 / volume_h24) * 100
        if momentum_pct >= 25:
            score += 2
            reasons.append(f"momentum {momentum_pct:.0f}% h1/h24 (+2)")
        elif momentum_pct >= 10:
            score += 1
            reasons.append(f"momentum {momentum_pct:.0f}% h1/h24 (+1)")
        else:
            reasons.append(f"momentum {momentum_pct:.0f}% h1/h24 (fading)")
    else:
        reasons.append("momentum unknown")

    # Buyer activity — h1 buy transaction count as proxy for unique holder engagement.
    # High buy count = many wallets participating = strong organic community signal.
    # This is the "holder count" signal the user identified as more powerful than socials alone.
    h1_buys = token.get("txns_h1_buys", 0) or 0
    if h1_buys >= 200:
        score += 2
        reasons.append(f"buyers h1={h1_buys} — very active (+2)")
    elif h1_buys >= 60:
        score += 1
        reasons.append(f"buyers h1={h1_buys} — organic demand (+1)")
    elif h1_buys > 0:
        reasons.append(f"buyers h1={h1_buys} (low)")

    # Social presence — 100% of major winners had Twitter+website at launch
    if has_social:
        score += 2
        social_hint = (token.get("_social_urls") or [""])[0][:30]
        reasons.append(f"has socials (+2) [{social_hint}]")
    else:
        reasons.append("no socials")

    # Narrative keyword — viral/celebrity narrative (weight controlled by live_config)
    if name or symbol:
        if _has_narrative_keyword(name, symbol):
            try:
                from elizaos.plugins.solana import live_config as _lc_kw
                _kw_pts = int(_lc_kw.get("narrative_keyword_score", 0))
            except Exception:
                _kw_pts = 0
            if _kw_pts > 0:
                score += _kw_pts
                reasons.append(f"viral keyword (+{_kw_pts}) [{symbol or name}]")
            else:
                reasons.append(f"viral keyword (disabled) [{symbol or name}]")

    # Volume spike detector — annualised vol/hr from h1 data
    # $50k/hr = HIGH MOMENTUM (+3) | $200k/hr = MONSTER MOMENTUM (+5)
    _vol_h1_usd = float(token.get("volume_h1_usd") or 0)
    if _vol_h1_usd >= 200_000:
        score += 5
        reasons.append(f"MONSTER MOMENTUM vol/hr ${_vol_h1_usd/1000:.0f}k (+5) 🔥")
    elif _vol_h1_usd >= 50_000:
        score += 3
        reasons.append(f"HIGH MOMENTUM vol/hr ${_vol_h1_usd/1000:.0f}k (+3) 📈")

    # DexScreener boost — developer paid real money to promote this token
    # Tiers: 50+ boost → +5 | 100+ → +10 | 300+ → +20
    _boost_amt = int(token.get("_boost_amount", 0) or 0)
    if _boost_amt >= 300:
        score += 20
        reasons.append(f"DexScreener boost {_boost_amt} (+20) 🚀")
    elif _boost_amt >= 100:
        score += 10
        reasons.append(f"DexScreener boost {_boost_amt} (+10)")
    elif _boost_amt >= 50:
        score += 5
        reasons.append(f"DexScreener boost {_boost_amt} (+5)")

    # Price direction h1 — HARD REJECT for crashed OR dead-cat-bounce tokens
    if price_change_h1 <= -30:
        reasons.append(f"REJECTED: crashed {price_change_h1:.0f}% h1 — dead cat territory")
        return -10, reasons
    elif price_change_h1 >= 20 and pair_age_secs is not None and pair_age_secs > 7_200:
        # h1 > 20% on a 2h+ old token = likely dead cat bounce.
        # Token already had its pump cycle, this is a brief recovery before further decline.
        # CLAIR pattern: h1 went -61% → rolled to +36% when the crash left the 1h window.
        _age_h_r = pair_age_secs / 3600
        reasons.append(
            f"REJECTED: h1=+{price_change_h1:.0f}% on {_age_h_r:.1f}h-old token "
            f"— dead cat bounce, opportunity window already passed"
        )
        return -10, reasons
    elif price_change_h1 >= 3:
        score += 1
        reasons.append(f"rising +{price_change_h1:.0f}% h1 (+1)")
    elif price_change_h1 <= -10:
        score -= 1
        reasons.append(f"fading {price_change_h1:+.0f}% h1 (-1)")
    else:
        reasons.append(f"price Δ{price_change_h1:+.0f}% h1")

    # M5 momentum — is it pumping RIGHT NOW? (most timely signal available)
    # HARD REJECT if m5 ≤ -15%: momentum has died, this is a dead-cat setup.
    if price_change_m5 is not None:
        if price_change_m5 <= -15:
            reasons.append(f"REJECTED: m5 {price_change_m5:+.0f}% — momentum dead")
            return -10, reasons
        elif price_change_m5 >= 2:
            score += 1
            reasons.append(f"m5 +{price_change_m5:.0f}% — running now (+1)")
        elif price_change_m5 <= -5:
            reasons.append(f"m5 {price_change_m5:+.0f}% — cooling")

    # Volume/MCap ratio — community activity signal.
    # On FRESH tokens (<1.5h): high vol/mcap = strong launch energy (+2/+1).
    # On OLD tokens (>2h): very high vol/mcap = early holders churning/exiting (-1).
    # CLAIR lesson: 1148% vol/mcap on a 3h token = overwhelmingly sell volume.
    market_cap = token.get("market_cap_usd", 0.0) or 0.0
    if market_cap > 0 and volume_h24 > 0:
        vol_mcap_ratio = (volume_h24 / market_cap) * 100
        _is_old = pair_age_secs is not None and pair_age_secs > 7_200
        if vol_mcap_ratio >= 800 and _is_old:
            score -= 1
            reasons.append(f"vol/mcap {vol_mcap_ratio:.0f}% on old token — churn signal (-1)")
        elif vol_mcap_ratio >= 400 and not _is_old:
            score += 2
            reasons.append(f"vol/mcap {vol_mcap_ratio:.0f}% — strong launch energy (+2)")
        elif vol_mcap_ratio >= 150 and not _is_old:
            score += 1
            reasons.append(f"vol/mcap {vol_mcap_ratio:.0f}% — active (+1)")
        else:
            reasons.append(f"vol/mcap {vol_mcap_ratio:.0f}%")

    return score, reasons


async def _fetch_live_liq_usd(mint: str, token_dex: str) -> tuple[float, str]:
    """
    Fetch live pool liquidity from DexScreener immediately before a buy.

    Selects the highest-liquidity Solana pair matching the expected DEX — this
    avoids reading stale bonding-curve data for graduated PumpSwap tokens.

    Returns:
        (liquidity_usd, pair_address) on success
        (-1.0, "")                   on fetch failure (caller should proceed with cached data)
    """
    import aiohttp as _aiohttp

    _DEX_IDS: dict[str, set[str]] = {
        "pumpswap": {"pumpswap", "pump-amm", "pumpfun-amm"},
        "raydium":  {"raydium"},
        "meteora":  {"meteora"},
    }
    target_ids = _DEX_IDS.get(token_dex, set())

    try:
        timeout = _aiohttp.ClientTimeout(total=8)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            ) as resp:
                if resp.status != 200:
                    return -1.0, ""
                data = await resp.json()
    except Exception:
        return -1.0, ""

    pairs = data.get("pairs") or []
    candidates: list[tuple[float, str]] = []
    for p in pairs:
        if p.get("chainId") != "solana":
            continue
        # Prefer pairs on the expected DEX; fall back to any Solana pair below
        if target_ids and p.get("dexId", "") not in target_ids:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        candidates.append((liq, p.get("pairAddress", "")))

    if not candidates:
        # Fallback: any Solana pair (covers edge cases where DEX label differs)
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            candidates.append((liq, p.get("pairAddress", "")))

    if not candidates:
        return -1.0, ""

    best_liq, best_pair = max(candidates, key=lambda x: x[0])
    return best_liq, best_pair


async def raydium_scout_loop(runtime: AgentRuntime) -> None:
    """Autonomous Raydium scout — finds freshly graduated pump.fun tokens every 5 min.

    Scoring (max 16, threshold 10 — research-backed from Dec 2025–Mar 2026 100x analysis):
      +3  safety check
      +3  volume h24 > $100k / +2 $20k-100k / +1 $3k-20k
      +2  liquidity > $15k / +1 $5k-15k
      +2  pair age < 2h / +1 2-6h
      +2  momentum h1/h24 > 25% / +1 > 10%  (currently pumping)
      +2  social presence (Twitter/website in DexScreener profile)
      +2  viral narrative keyword in name/symbol
      +1  price rising > +3% last hour / -1 if dumping < -20%
    REJECT: volume/liquidity ratio > 200x (wash-trading bots)

    Unlike pump.fun, Raydium buys open positions directly (no agent message round-trip)
    so that positions are tagged dex='raydium' for correct SL/TP price tracking.
    """
    from elizaos.types import ServiceTypeRegistry

    from elizaos.plugins.solana import live_config as _lc
    PAPER_TRADING = _lc.get("paper_trading_scout", _lc.get("paper_trading", False))
    TOKEN_DECIMALS = 6   # graduated pump.fun tokens use 6 decimals on Raydium
    SCAN_INTERVAL = 30   # 30s — fast cycles to catch tokens early in their move

    evaluated: set[str] = set()
    _eval_times: dict[str, float] = {}  # mint → timestamp when added to evaluated
    _EVAL_EXPIRY_SECS = 600   # 10min — re-evaluate every 10min so momentum shifts are caught
    _last_eval_clear = time.time()

    print(
        f"[raydium-scout] Raydium scout loop started -- scanning every "
        f"{SCAN_INTERVAL}s. "
        f"{'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}"
    )

    while True:
        try:
            # Re-read live config on every cycle — Jarvis can change these at runtime
            _c_on = _lc.get("strategy_c_enabled", False)
            _d_on = _lc.get("strategy_d_enabled", False)
            if not _c_on and not _d_on:
                print("[raydium-scout] Both Strategy C and D disabled — sleeping")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # ── Trading hours gate (same window as Strategy B) ────────────────
            _rc_utc_hour  = datetime.utcnow().hour
            _rc_win_start = _lc.get("trading_window_start_utc", 21)
            _rc_win_end   = _lc.get("trading_window_end_utc", 3)
            if _rc_win_start == _rc_win_end:
                _rc_in_window = True
            elif _rc_win_start > _rc_win_end:
                _rc_in_window = (_rc_utc_hour >= _rc_win_start or _rc_utc_hour < _rc_win_end)
            else:
                _rc_in_window = (_rc_win_start <= _rc_utc_hour < _rc_win_end)
            if not _rc_in_window:
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            PAPER_TRADING = _lc.get("paper_trading_scout", _lc.get("paper_trading", False))
            MIN_SCORE     = _lc.get("strategy_c_min_score", 3)
            _c_override   = _lc.get("strategy_c_buy_sol", 0.0)
            BUY_SOL       = _c_override if _c_override > 0 else _lc.get("buy_sol", 0.05)

            pos_mgr = runtime.get_service("position_manager")
            wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
            raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)

            if None in (pos_mgr, wallet_svc):
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            wallet_sol = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]

            # Wallet health check
            if wallet_sol < WALLET_CRIT_SOL:
                print(
                    f"[raydium-scout] ⚠️  WALLET CRITICAL: {wallet_sol:.4f} SOL — "
                    f"below {WALLET_CRIT_SOL} SOL. Please top up to continue trading.",
                    file=sys.stderr,
                )
            elif wallet_sol < WALLET_WARN_SOL:
                print(
                    f"[raydium-scout] ⚠️  WALLET LOW: {wallet_sol:.4f} SOL — "
                    f"fees are {0.001/max(0.001,_dynamic_buy_sol(BUY_SOL,wallet_sol))*100:.0f}% of trade size. "
                    f"Consider topping up."
                )

            _split_enabled_rs = bool(_lc.get("split_buy_enabled", False))
            if _split_enabled_rs:
                actual_buy = float(_lc.get("split_buy_a_sol", 0.05))
            else:
                actual_buy = _dynamic_buy_sol(BUY_SOL, wallet_sol)
            allowed, block_reason = pos_mgr.check_trade_allowed(actual_buy, wallet_sol)  # type: ignore[union-attr]
            if not allowed:
                print(f"[raydium-scout] Trading gated: {block_reason}")
                await asyncio.sleep(SCAN_INTERVAL)
                continue
            _sl_gap = time.time() - _last_sl_exit_time
            if _sl_gap < _get_post_sl_cooldown():
                print(f"[raydium-scout] Post-SL cooldown active — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # ── Prune stale evaluated entries — allow re-evaluation after 2h ────
            # Without this, tokens rejected once (e.g. too young) are blocked forever.
            # Permanent blacklists (session_traded_mints, all_exit_times) are checked
            # independently so clearing evaluated is safe — they re-add immediately.
            _now_prune = time.time()
            if _now_prune - _last_eval_clear > _EVAL_EXPIRY_SECS:
                _pruned_count = len(evaluated)
                evaluated.clear()
                _eval_times.clear()
                _last_eval_clear = _now_prune
                print(f"[raydium-scout] ♻️  Cleared {_pruned_count} evaluated entries (2h expiry) — all tokens re-eligible")

            grad_tokens: list[dict] = []

            # ── Fast path: evaluate recently graduated mints at T+5min ─────────
            # These come from the graduation WS event, bypassing DexScreener
            # profiles discovery lag.  We know exactly when they graduated.
            now_ts = time.time()
            grad_mints_ready = [
                m for m, grad_ts in list(_strat_c_grad_queue.items())
                if STRAT_C_MIN_AGE_SECS <= (now_ts - grad_ts) <= STRAT_C_MAX_AGE_SECS
                and m not in evaluated
                and m not in (pos_mgr.positions or {})  # type: ignore[union-attr]
                and m not in pos_mgr._session_traded_mints  # type: ignore[union-attr]
            ]
            # Expire stale entries (> 30 min)
            for m, grad_ts in list(_strat_c_grad_queue.items()):
                if now_ts - grad_ts > STRAT_C_MAX_AGE_SECS:
                    del _strat_c_grad_queue[m]

            if grad_mints_ready:
                print(f"[raydium-scout] Fast-path: checking {len(grad_mints_ready)} graduated mint(s) from WS queue")
                grad_results = await asyncio.gather(
                    *[_fetch_single_pumpswap_token(m) for m in grad_mints_ready],
                    return_exceptions=True,
                )
                grad_tokens = [
                    r for r in grad_results
                    if isinstance(r, dict) and r is not None
                ]
                if grad_tokens:
                    print(f"[raydium-scout] Fast-path found {len(grad_tokens)} PumpSwap pair(s) ready to evaluate")

            # Fetch from all sources in parallel: PumpSwap second-wave + native Raydium + Meteora (D)
            _d_enabled = _lc.get("strategy_d_enabled", False)
            _gather_sources = [
                _fetch_graduated_raydium_tokens(limit=15),   # strategy C — pumpswap second-wave + raydium
                _fetch_native_raydium_tokens(limit=15),      # native raydium pools
            ]
            if _d_enabled:
                # Strategy D: second pass of graduated tokens (different sort/offset) for more coverage
                # Meteora DLMM source is currently dead — use graduated tokens as secondary scan
                _gather_sources.append(_fetch_graduated_raydium_tokens(limit=15))
            _gather_results = await asyncio.gather(*_gather_sources, return_exceptions=True)
            dex_tokens = _gather_results[0] if not isinstance(_gather_results[0], Exception) else []
            native_ray_tokens = _gather_results[1] if not isinstance(_gather_results[1], Exception) else []
            meteora_tokens = (_gather_results[2] if len(_gather_results) > 2 and not isinstance(_gather_results[2], Exception) else []) if _d_enabled else []

            # Refresh DexScreener boost cache (5-min TTL, fire-and-forget)
            asyncio.create_task(_refresh_boost_cache())

            # Merge: native Raydium first (fresh opportunities), then PumpSwap second-wave, then Meteora
            # Deduplicate by mint
            seen_mints_merge: set[str] = set()
            tokens: list[dict] = []
            for t in (native_ray_tokens or []) + (dex_tokens or []) + (meteora_tokens or []):
                m = t.get("mint", "")
                if m and m not in seen_mints_merge:
                    seen_mints_merge.add(m)
                    # Stamp DexScreener boost amount from cache (0 if not boosted)
                    t["_boost_amount"] = _boost_cache.get(m, 0)
                    tokens.append(t)

            # Also prepend fast-path graduated tokens (highest priority)
            if grad_mints_ready:
                extra = [t for t in grad_tokens if t["mint"] not in {tok["mint"] for tok in tokens}]
                for t in extra:
                    t["_boost_amount"] = _boost_cache.get(t.get("mint", ""), 0)
                tokens = extra + tokens
                # Remove evaluated grad mints from the queue
                for m in grad_mints_ready:
                    _strat_c_grad_queue.pop(m, None)

            native_count = len(native_ray_tokens) if not isinstance(native_ray_tokens, Exception) else 0
            meteora_count = len(meteora_tokens) if _d_enabled else 0
            print(f"[raydium-scout] Scanning {len(tokens)} token(s) ({native_count} native Raydium, {meteora_count} Meteora, {len(grad_tokens) if grad_mints_ready else 0} from grad queue)")

            if not tokens:
                print("[raydium-scout] No tokens found this cycle")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            now = time.time()
            bought_this_cycle = False

            for token in tokens:
                mint: str = token.get("mint", "")
                if not mint or mint in evaluated or mint in pos_mgr.positions:  # type: ignore[union-attr]
                    continue

                # Permanent blacklist check — but allow override on momentum re-entry:
                # if this was a WIN exit ≥30 min ago and m5 > +20% right now, unblock.
                if mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
                    _m5_reentry = token.get("price_change_m5", None)
                    if _allow_reentry(mint, _m5_reentry):
                        pos_mgr._session_traded_mints.discard(mint)  # type: ignore[union-attr]
                        _all_exit_times.pop(mint, None)
                        print(f"[re-entry] {mint[:8]}... win-exit + m5={float(_m5_reentry):+.0f}% spike — re-entry ALLOWED")
                    else:
                        evaluated.add(mint)
                        continue

                # Skip tokens recently exited (any reason) — prevents dead-cat-bounce re-entry
                if time.time() - _all_exit_times.get(mint, 0) < ALL_EXIT_COOLDOWN_SECS:
                    evaluated.add(mint)
                    continue

                pre_score, reasons = _score_raydium_token(token, now)

                # Data gathering mode: pre-filter disabled (MIN_SCORE=3, almost nothing filtered)

                # Data gathering mode: liq, m5, h1 gates removed — log values but don't filter
                liq = token.get("liquidity_usd", 0.0)
                token_dex = token.get("dex", "raydium")
                m5_now = token.get("price_change_m5", None)
                h1_now = token.get("price_change_h1", None)

                # ── Circuit breaker gate ──────────────────────────────────────
                _cb_level = getattr(pos_mgr, "_circuit_level", 0)
                if token_dex == "pumpswap":
                    _ps_paused, _ps_remaining = pos_mgr.is_pumpswap_paused()  # type: ignore[union-attr]
                    if _ps_paused:
                        print(f"[raydium-scout] x {mint[:8]}... PUMPSWAP PAUSED (Level 2 CB — {_ps_remaining/60:.0f}min remaining)")
                        evaluated.add(mint)
                        continue
                if _cb_level >= 1:
                    print(f"[raydium-scout] ⚠️ Level {_cb_level} circuit caution active")

                print(f"[raydium-scout] {mint[:8]}... data: liq=${liq:,.0f} m5={float(m5_now):+.0f}% h1={float(h1_now) if h1_now is not None else '?'}%")

                # Get current price (needed for both buy size and sellability check)
                price_sol = token.get("price_sol", 0.0)
                if price_sol <= 0 and raydium_svc is not None:
                    try:
                        price_sol = await raydium_svc.get_price(mint, vs_token="SOL")  # type: ignore[union-attr]
                    except Exception:
                        pass

                if price_sol <= 0:
                    print(f"[raydium-scout] x {mint[:8]}... no price data -- skip")
                    evaluated.add(mint)
                    continue

                # Safety check — Jupiter doesn't need a PumpPortal pre-buy sellability test
                safe, safety_reason = await pos_mgr.check_token_safety(mint)  # type: ignore[union-attr]

                if not safe:
                    print(f"[raydium-scout] x {mint[:8]}... UNSAFE: {safety_reason}")
                    evaluated.add(mint)
                    continue

                score = pre_score + 3
                reasons.append("safety ok (+3)")

                # ── Per-platform DNA: liquidity + market cap floors ────────────
                from elizaos.plugins.solana import live_config as _lc_dna
                _dna_pfx     = "d" if token_dex == "meteora" else "c"
                _dna_min_liq = float(_lc_dna.get(f"{_dna_pfx}_min_liq_usd", 0) or 0)
                _dna_min_mc  = float(_lc_dna.get(f"{_dna_pfx}_min_mc_usd",  0) or 0)
                _dna_liq     = float(token.get("liquidity_usd") or liq or 0)
                _dna_mc      = float(token.get("market_cap_usd") or token.get("mc_usd") or 0)

                if _dna_min_liq > 0 and _dna_liq < _dna_min_liq:
                    print(f"[raydium-scout] x {mint[:8]}... {token_dex} liq=${_dna_liq:,.0f} < ${_dna_min_liq:,.0f} min — skip")
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_dna
                        _rt_dna.record(mint, "liq_too_low", filter_name=f"{_dna_pfx}_min_liq_usd",
                                       filter_value=round(_dna_liq, 0), threshold=_dna_min_liq, strategy=token_dex)
                    except Exception:
                        pass
                    evaluated.add(mint)
                    continue

                if _dna_min_mc > 0 and _dna_mc > 0 and _dna_mc < _dna_min_mc:
                    print(f"[raydium-scout] x {mint[:8]}... {token_dex} mc=${_dna_mc:,.0f} < ${_dna_min_mc:,.0f} min — skip")
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_dna2
                        _rt_dna2.record(mint, "mc_too_low", filter_name=f"{_dna_pfx}_min_mc_usd",
                                        filter_value=round(_dna_mc, 0), threshold=_dna_min_mc, strategy=token_dex)
                    except Exception:
                        pass
                    evaluated.add(mint)
                    continue

                # ── Vol/Liq ratio + buy ratio filters (B uses b_ keys, Meteora uses d_ keys, others c_) ─
                from elizaos.plugins.solana import live_config as _lc_c_vl
                _scout_pfx  = "d" if token_dex == "meteora" else "c"
                _c_vol_h1   = float(token.get("volume_usd_h1") or token.get("vol_h1_usd") or token.get("volume_h1_usd") or 0)
                _c_vol_h24  = float(token.get("volume_usd_24h") or token.get("volume_h24_usd") or 0)
                _c_liq      = float(token.get("liquidity_usd") or liq)
                # Use h1 vol if available, fall back to h24/24 as hourly proxy
                _c_vol_for_ratio = _c_vol_h1 if _c_vol_h1 > 0 else (_c_vol_h24 / 24 if _c_vol_h24 > 0 else 0)
                _c_vl_ratio = round(_c_vol_for_ratio / _c_liq, 1) if _c_liq > 0 else 0.0
                _c_max_vl   = float(_lc_c_vl.get(f"{_scout_pfx}_max_vol_liq_ratio", 0) or 0)
                _c_min_vl   = float(_lc_c_vl.get(f"{_scout_pfx}_min_vol_liq_ratio", 0) or 0)

                if _c_max_vl > 0 and _c_vl_ratio > _c_max_vl:
                    print(
                        f"[raydium-scout] x {mint[:8]}... vol/liq={_c_vl_ratio:.0f}x > {_c_max_vl:.0f}x "
                        f"— WASH TRADING detected ({token_dex}), skip"
                    )
                    evaluated.add(mint)
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_cvl
                        _rt_cvl.record(mint, "wash_trade_vol_liq", filter_name=f"{_scout_pfx}_max_vol_liq_ratio",
                                       filter_value=round(_c_vl_ratio, 1), threshold=_c_max_vl, strategy=token_dex,
                                       extra={"liq_usd": _c_liq, "vol_h1": _c_vol_h1})
                    except Exception:
                        pass
                    continue

                if _c_min_vl > 0 and _c_liq > 0 and _c_vl_ratio < _c_min_vl:
                    print(
                        f"[raydium-scout] x {mint[:8]}... vol/liq={_c_vl_ratio:.1f}x < {_c_min_vl:.0f}x min "
                        f"(no momentum, {token_dex}) — skip"
                    )
                    evaluated.add(mint)
                    continue

                # ── Buy ratio filter ────────────────────────────────────────────
                _c_buys  = int(token.get("txns_h1_buys") or 0)
                _c_sells = int(token.get("txns_h1_sells") or 0)
                _c_total_txns = _c_buys + _c_sells
                _c_buy_ratio  = round(_c_buys / _c_total_txns * 100, 1) if _c_total_txns > 0 else 0.0
                _c_min_br     = float(_lc_c_vl.get(f"{_scout_pfx}_min_buy_ratio", 0) or 0)
                if _c_min_br > 0 and _c_total_txns >= 10 and _c_buy_ratio < _c_min_br:
                    print(
                        f"[raydium-scout] x {mint[:8]}... buy_ratio={_c_buy_ratio:.0f}% < {_c_min_br:.0f}% "
                        f"(buys={_c_buys} sells={_c_sells}, {token_dex}) — selling pressure, skip"
                    )
                    evaluated.add(mint)
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_cbr
                        _rt_cbr.record(mint, "buy_ratio_low", filter_name=f"{_scout_pfx}_min_buy_ratio",
                                       filter_value=round(_c_buy_ratio, 1), threshold=_c_min_br, strategy=token_dex,
                                       extra={"buys": _c_buys, "sells": _c_sells, "liq_usd": _c_liq})
                    except Exception:
                        pass
                    continue

                # Monster DNA: buy_ratio > 65% = euphoric top buyers, all 8 monsters were 50-65%.
                # FML=69.9%, apple=79.8% had high ratios and zero monster behaviour.
                _MAX_BUY_RATIO = 65.0
                if _c_total_txns >= 10 and _c_buy_ratio > _MAX_BUY_RATIO:
                    print(
                        f"[raydium-scout] x {mint[:8]}... buy_ratio={_c_buy_ratio:.0f}% > {_MAX_BUY_RATIO:.0f}% "
                        f"(buys={_c_buys} sells={_c_sells}, {token_dex}) — euphoric top, skip"
                    )
                    evaluated.add(mint)
                    continue

                # ── Momentum score gate ─────────────────────────────────────────
                _mom_pfx     = "d" if token_dex == "meteora" else "c"
                _c_mom_min   = float(_lc_c_vl.get(f"{_mom_pfx}_min_momentum_score", 0.0) or 0.0)
                _c_m5        = float(token.get("price_change_m5") or 0)
                _c_h1        = float(token.get("price_change_h1") or 0)
                _c_h6        = float(token.get("price_change_h6") or 0)
                _c_momentum  = round(_c_m5 * 0.5 + _c_h1 * 0.3 + _c_h6 * 0.2, 1)
                if _c_mom_min != 0.0 and _c_momentum < _c_mom_min:
                    print(
                        f"[raydium-scout] x {mint[:8]}... momentum_score={_c_momentum:.1f} < {_c_mom_min:.1f} min "
                        f"(m5={_c_m5:+.1f}% h1={_c_h1:+.1f}% h6={_c_h6:+.1f}%, {token_dex}) — skip"
                    )
                    evaluated.add(mint)
                    continue

                # ── Scout quality caps: m5/h1 overbought + liq/MC + min-age ───────
                from elizaos.plugins.solana import live_config as _lc_caps
                _caps_max_m5  = float(_lc_caps.get("scout_max_m5_pct", 0) or 0)
                _caps_max_h1  = float(_lc_caps.get("scout_max_h1_pct", 0) or 0)
                _caps_liq_mc  = float(_lc_caps.get("scout_min_liq_mc_ratio", 0) or 0)
                _caps_min_age = float(_lc_caps.get("scout_min_age_secs", 0) or 0)

                if _caps_max_m5 > 0 and _c_m5 > _caps_max_m5:
                    print(
                        f"[raydium-scout] x {mint[:8]}... m5={_c_m5:+.1f}% > {_caps_max_m5:.0f}% cap "
                        f"— parabolic top, buying exit liquidity — skip"
                    )
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_m5cap
                        _rt_m5cap.record(mint, "m5_overbought", filter_name="scout_max_m5_pct",
                                         filter_value=round(_c_m5, 1), threshold=_caps_max_m5, strategy=token_dex)
                    except Exception:
                        pass
                    evaluated.add(mint)
                    continue

                if _caps_max_h1 > 0 and _c_h1 > _caps_max_h1:
                    print(
                        f"[raydium-scout] x {mint[:8]}... h1={_c_h1:+.1f}% > {_caps_max_h1:.0f}% cap "
                        f"— pump exhausted, exit liquidity risk — skip"
                    )
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_h1cap
                        _rt_h1cap.record(mint, "h1_overbought", filter_name="scout_max_h1_pct",
                                         filter_value=round(_c_h1, 1), threshold=_caps_max_h1, strategy=token_dex)
                    except Exception:
                        pass
                    evaluated.add(mint)
                    continue

                if _caps_liq_mc > 0 and _dna_mc > 0:
                    _liq_mc_ratio = _dna_liq / _dna_mc if _dna_mc > 0 else 0.0
                    if _liq_mc_ratio < _caps_liq_mc:
                        print(
                            f"[raydium-scout] x {mint[:8]}... liq/MC={_liq_mc_ratio:.2f} < {_caps_liq_mc:.2f} min "
                            f"(liq=${_dna_liq:,.0f} MC=${_dna_mc:,.0f}) — thin liquidity, rug-vulnerable — skip"
                        )
                        try:
                            from elizaos.plugins.solana import rejection_tracker as _rt_liqmc
                            _rt_liqmc.record(mint, "liq_mc_ratio_low", filter_name="scout_min_liq_mc_ratio",
                                             filter_value=round(_liq_mc_ratio, 3), threshold=_caps_liq_mc,
                                             strategy=token_dex, extra={"liq_usd": _dna_liq, "mc_usd": _dna_mc})
                        except Exception:
                            pass
                        evaluated.add(mint)
                        continue

                # Per-DEX minimum age: PumpSwap needs 90 min (graduation shakeout),
                # Raydium/Meteora need 60 min. No monster winner was under 2h old.
                _scout_created_ms = float(token.get("pair_created_at") or 0)
                _scout_age_secs = (now - _scout_created_ms / 1000.0) if _scout_created_ms > 0 else None
                if token_dex == "pumpswap":
                    _age_min_key = "pumpswap_min_age_secs"
                    _age_floor = float(_lc_caps.get("pumpswap_min_age_secs", 5400) or 0)
                else:
                    _age_min_key = "scout_min_age_secs"
                    _age_floor = _caps_min_age  # already read above
                if _age_floor > 0 and _scout_age_secs is not None and _scout_age_secs < _age_floor:
                    _age_mins = _scout_age_secs / 60
                    _floor_mins = _age_floor / 60
                    print(
                        f"[raydium-scout] x {mint[:8]}... age={_age_mins:.0f}min < {_floor_mins:.0f}min min "
                        f"({token_dex}) — graduation shakeout window, skip"
                    )
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_age
                        _rt_age.record(mint, "too_fresh", filter_name=_age_min_key,
                                       filter_value=round(_scout_age_secs, 0), threshold=_age_floor,
                                       strategy=token_dex)
                    except Exception:
                        pass
                    evaluated.add(mint)
                    continue

                print(
                    f"[raydium-scout] {mint[:8]}... score={score}/10 "
                    f"| vol/liq={_c_vl_ratio:.0f}x | buy%={_c_buy_ratio:.0f}% | mom={_c_momentum:.1f} | dex={token_dex} "
                    f"| {' | '.join(reasons)}"
                )

                # ── Grok pre-approval + age gate ─────────────────────────────
                _g_pre_svc = runtime.get_service("social_monitor")
                _g_pre_score = _g_pre_svc.get_confidence(mint) if _g_pre_svc else 0

                # HARD REJECT: token >2.5h old with no Grok social confirmation.
                # ONLY applies when Grok is live and returning real scores.
                # When Grok is disabled (credits gone), _g_pre_score is always 0 for
                # every token — applying this gate would block ALL Raydium tokens >2.5h,
                # including monster targets (NoHat 8h, PISS 6h, LOLA 10h).
                _token_age_h = float(token.get("pair_age_hours", 0))
                _grok_is_live = _g_pre_svc is not None and getattr(_g_pre_svc, "_grok_enabled", False)
                if _token_age_h > 2.5 and _g_pre_score == 0 and _grok_is_live:
                    print(
                        f"[raydium-scout] x {mint[:8]}... age={_token_age_h:.1f}h, "
                        f"Grok=0/10 — old token with no social backing, skip"
                    )
                    evaluated.add(mint)
                    continue

                # Grok pre-confirmed: lower score threshold by 4 points (enter earlier)
                _effective_min = (MIN_SCORE - 4) if _g_pre_score >= 5 else MIN_SCORE

                if score < _effective_min:
                    print(f"[raydium-scout] x score {score} < {_effective_min} -- skip")
                    evaluated.add(mint)
                    continue
                if _g_pre_score >= 5 and score < MIN_SCORE:
                    print(
                        f"[raydium-scout] ⚡ GROK FAST LANE: {mint[:8]}... "
                        f"score={score} (normally {MIN_SCORE}, lowered to {_effective_min} — "
                        f"Grok X confidence={_g_pre_score}/10)"
                    )
                # ─────────────────────────────────────────────────────────────

                # ── Jarvis + Grok: run BOTH in parallel ───────────────────────
                # Grok task starts immediately alongside Jarvis — by the time Jarvis
                # replies (~5s) Grok has been running for 5s already (saves ~5s/trade).
                # If Jarvis says RUG/WEAK we cancel the Grok task immediately.
                from elizaos.types.model import ModelType, GenerateTextOptions
                _age_h  = float(token.get("pair_age_hours", 0))
                _vol    = float(token.get("volume_usd_24h", 0))
                _liq    = float(token.get("liquidity_usd", 0))
                _h1     = float(h1_now) if h1_now is not None else 0.0
                _h24    = float(token.get("price_change_h24", 0))
                _m5     = float(m5_now) if m5_now is not None else 0.0
                _buys_h1  = int(token.get("txns_h1_buys", 0))
                _sells_h1 = int(token.get("txns_h1_sells", 0))
                _socials  = token.get("social_links") or []
                _name     = token.get("base_token_name", mint[:8])
                _symbol   = token.get("base_token_symbol", "?")
                _mkt_c    = _get_market_context(pos_mgr)
                _lessons_c = _load_recent_lessons(3)
                _eliza_c_prompt = (
                    f"{_lessons_c}"
                    f"{_mkt_c}"
                    f"=== STRATEGY ===\n"
                    f"We scalp Solana memecoins on PumpSwap/Raydium. Buy 0.06 SOL. "
                    f"TP at +60% full exit. SL at -9%. "
                    f"We ONLY win if we enter early enough for a +60% move to be realistic.\n\n"
                    f"=== TOKEN ===\n"
                    f"Name: {_name} ({_symbol}) | DEX: {token_dex} | Score: {score}/16\n"
                    f"Age: {_age_h:.1f}h | Liq: ${_liq:,.0f} | Vol 24h: ${_vol:,.0f}\n"
                    f"Price: m5 {_m5:+.0f}% | h1 {_h1:+.0f}% | h24 {_h24:+.0f}%\n"
                    f"H1 txns: {_buys_h1} buys / {_sells_h1} sells | "
                    f"Socials: {', '.join(_socials[:2]) if _socials else 'none'}\n"
                    f"Score reasons: {'; '.join(reasons)}\n\n"
                    f"=== VERDICT GUIDE ===\n"
                    f"  STRONG = fresh token (<2h), momentum still accelerating, "
                    f"high buy/sell ratio, real socials — +60% from here is very plausible\n"
                    f"  SAFE   = momentum looks real, entry is reasonably early, "
                    f"likely to hit +60% TP given time\n"
                    f"  WEAK   = already ran its main wave, m5 slowing, old token >3h, "
                    f"or sell pressure building — slim chance of +60% more\n"
                    f"  RUG    = red flags: sells > buys, no socials, suspicious pattern\n\n"
                    f"ONE WORD ONLY: STRONG, SAFE, WEAK, or RUG."
                )

                # Start Grok targeted buzz check as a background task NOW
                _grok_svc_r = runtime.get_service("social_monitor")
                _grok_task_r = None
                if _grok_svc_r and getattr(_grok_svc_r, "_enabled", False):
                    _grok_task_r = asyncio.create_task(
                        _grok_svc_r.check_token_buzz(mint, _name, _symbol)
                    )

                # Wait for Jarvis verdict (Grok runs concurrently in _grok_task_r (alongside Jarvis))
                _vc = "UNKNOWN"
                try:
                    _vc_result = await asyncio.wait_for(
                        runtime.generate_text(
                            _eliza_c_prompt,
                            GenerateTextOptions(model_type=ModelType.TEXT_SMALL),
                        ),
                        timeout=8.0,
                    )
                    _vc = (_vc_result.text if _vc_result else "").strip().upper()[:60]
                    if "RUG" in _vc or "WEAK" in _vc:
                        print(f"[raydium-scout] Jarvis: {_vc} — skip")
                        if _grok_task_r:
                            _grok_task_r.cancel()
                        evaluated.add(mint)
                        # Near-miss: scored well + passed all objective gates but Jarvis vetoed
                        if score >= max(3, MIN_SCORE):
                            try:
                                from elizaos.plugins.solana import near_miss_tracker as _nmt
                                _nmt.maybe_record(
                                    mint,
                                    gates_passed=["not_blacklisted", "price_available", "rugcheck", "vol_liq_max", "vol_liq_min", "buy_ratio", "score"],
                                    failed_gate="jarvis_quality",
                                    strategy=token_dex,
                                    score=score,
                                    extra={"verdict": _vc, "liq_usd": liq, "vol_liq": _c_vl_ratio, "buy_ratio": _c_buy_ratio},
                                )
                            except Exception:
                                pass
                        continue
                    print(f"[raydium-scout] Jarvis: {_vc} — proceeding")
                except (asyncio.TimeoutError, Exception) as _ec:
                    _ec_msg = "timeout" if isinstance(_ec, asyncio.TimeoutError) else str(_ec)
                    print(f"[raydium-scout] {mint[:8]}... Jarvis {_ec_msg} — proceeding")

                # Collect Grok result (already been running for ~5-8s alongside Eliza)
                if _grok_task_r:
                    try:
                        _g_confirmed, _g_score, _g_reason = await asyncio.wait_for(
                            asyncio.shield(_grok_task_r), timeout=8.0
                        )
                        _tp_note = "TP=200% 🔥" if _g_confirmed else "TP=60%"
                        print(
                            f"[raydium-scout] [grok] {mint[:8]}... score={_g_score}/10 "
                            f"{_g_reason[:60]} → {_tp_note}"
                        )
                    except Exception as _ge:
                        print(f"[raydium-scout] [grok] {mint[:8]}... check skipped ({_ge})")
                # ────────────────────────────────────────────────────────────────────

                # ── Momentum confirmation wait — 5 min before committing ─────
                # After all gates pass, snapshot price+vol, wait 300s, re-check.
                # Prevents buying pumps that are already fading at scan time.
                _mc_wait_secs = MOMENTUM_CONFIRM_SECS
                if _lc.get("momentum_confirm_secs", 0):
                    _mc_wait_secs = int(_lc.get("momentum_confirm_secs", 300))
                if _mc_wait_secs > 0 and not getattr(pos_mgr, "_circuit_level", 0) >= 2:
                    _mc_snap = _momentum_pending.get(mint)
                    if _mc_snap is None:
                        # First pass — snapshot and wait
                        _momentum_pending[mint] = {
                            "ts": time.time(),
                            "price": price_sol,
                            "vol_h1": _c_vol_h1,
                            "score": score,
                        }
                        _remaining_mc = _mc_wait_secs
                        print(
                            f"[raydium-scout] ⏳ {mint[:8]}... MOMENTUM CONFIRMATION WAIT — "
                            f"queued {_remaining_mc}s, score={score} — re-checking later"
                        )
                        continue  # don't add to evaluated; will be re-scanned
                    elif time.time() - _mc_snap["ts"] < _mc_wait_secs:
                        # Still waiting
                        _remaining_mc = _mc_wait_secs - (time.time() - _mc_snap["ts"])
                        print(
                            f"[raydium-scout] ⏳ {mint[:8]}... MOMENTUM WAIT — "
                            f"{_remaining_mc:.0f}s remaining"
                        )
                        continue  # keep waiting
                    else:
                        # Confirmation period elapsed — verify momentum held
                        _mc_age_s = time.time() - _mc_snap["ts"]
                        _mc_prev_price = _mc_snap["price"]
                        _price_chg_mc = ((price_sol - _mc_prev_price) / max(_mc_prev_price, 1e-12)) * 100
                        del _momentum_pending[mint]
                        if _price_chg_mc < -5:
                            print(
                                f"[raydium-scout] x {mint[:8]}... MOMENTUM FAILED confirmation — "
                                f"price {_price_chg_mc:+.1f}% in {_mc_age_s:.0f}s — skip"
                            )
                            evaluated.add(mint)
                            continue
                        print(
                            f"[raydium-scout] ✅ {mint[:8]}... MOMENTUM CONFIRMED — "
                            f"price {_price_chg_mc:+.1f}% over {_mc_age_s:.0f}s — proceeding to buy"
                        )

                evaluated.add(mint)

                # ── Live liquidity verification — prevent buying on stale data ─
                # Fetch a fresh DexScreener read RIGHT NOW, immediately before buying.
                # $PABLO: bot read $43k liq from scan data, actual PumpSwap pool = $6k.
                # This catches liq that drained between scan and buy time.
                _live_liq_usd, _live_pair_addr = await _fetch_live_liq_usd(mint, token_dex)
                if _live_liq_usd >= 0:
                    _scan_liq = _c_liq
                    _liq_change_pct = ((_live_liq_usd - _scan_liq) / max(_scan_liq, 1)) * 100
                    if abs(_liq_change_pct) > 20:
                        print(
                            f"[raydium-scout] ⚠ {mint[:8]}... LIVE liq ${_live_liq_usd:,.0f} "
                            f"vs scan ${_scan_liq:,.0f} ({_liq_change_pct:+.0f}%) — stale data"
                        )
                    _min_liq_live = float(_lc_dna.get(f"{_dna_pfx}_min_liq_usd", 0) or 0) or 10_000
                    if _live_liq_usd < _min_liq_live:
                        print(
                            f"[raydium-scout] x {mint[:8]}... LIVE liq ${_live_liq_usd:,.0f} < "
                            f"${_min_liq_live:,.0f} min (scan said ${_scan_liq:,.0f}) — REJECT"
                        )
                        try:
                            from elizaos.plugins.solana import rejection_tracker as _rt_liveliq
                            _rt_liveliq.record(
                                mint, "live_liq_too_low",
                                filter_name=f"{_dna_pfx}_min_liq_usd",
                                filter_value=round(_live_liq_usd, 0),
                                threshold=_min_liq_live,
                                strategy=token_dex,
                                extra={"scan_liq_usd": _scan_liq},
                            )
                        except Exception:
                            pass
                        continue
                    _c_liq = _live_liq_usd  # use live data for the hard gate below

                # ── HARD BUY GATE — final check before any transaction ────────
                _gate_ok_r, _gate_reason_r = _hard_buy_gate(
                    "[raydium-scout]", mint, _scout_pfx,
                    liq_usd=_c_liq,
                    mc_usd=_dna_mc,
                    vol_h1_usd=_c_vol_h1,
                    buy_txns=_c_buys,
                    sell_txns=_c_sells,
                    safety_passed=safe,
                    safety_reason=safety_reason,
                )
                if not _gate_ok_r:
                    continue
                # ────────────────────────────────────────────────────────────────

                token_amount = int((actual_buy / price_sol) * (10 ** TOKEN_DECIMALS))

                if PAPER_TRADING:
                    sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
                elif raydium_svc is not None:
                    try:
                        # Buy via Jupiter — aggregates PumpSwap, Raydium, Meteora, Orca
                        _ray_last_exc: Exception | None = None
                        for _ray_slip_bps in (1500, 3000, 5000):
                            try:
                                sig = await raydium_svc.swap_jupiter_buy(  # type: ignore[union-attr]
                                    mint, actual_buy, slippage_bps=_ray_slip_bps, priority_fee=0.002
                                )
                                _ray_last_exc = None
                                break
                            except Exception as _jup_exc:
                                _ray_last_exc = _jup_exc
                        if _ray_last_exc is not None:
                            raise _ray_last_exc
                    except Exception as exc:
                        print(
                            f"[raydium-scout] Buy failed for {mint[:8]}...: {exc}",
                            file=sys.stderr,
                        )
                        continue
                else:
                    print("[raydium-scout] RaydiumService not available -- skip")
                    continue

                # ── Mandatory AI gate ──────────────────────────────────────────
                _sm_r = runtime.get_service("social_monitor")
                if _sm_r is not None:
                    _pc_at_r = token.get("pair_created_at", 0) or 0
                    _gate_data_r = {
                        "dex": token_dex, "liq_usd": token.get("liquidity_usd", 0),
                        "age_mins": token.get("age_mins") or ((time.time() - _pc_at_r / 1000.0) / 60.0 if _pc_at_r > 0 else 0),
                        "price_change_m5": token.get("price_change_m5", 0),
                        "price_change_h1": token.get("price_change_h1", None),
                        "price_change_h6": token.get("price_change_h6", None),
                        "price_change_h24": token.get("price_change_h24", None),
                        "vol_h1_usd": token.get("volume_h1_usd", 0),
                        "vol_h24_usd": token.get("volume_h24_usd", 0),
                        "score": score, "has_socials": token.get("_has_social", False),
                    }
                    _gate_allow_r, _gate_reason_r = await _sm_r.pre_trade_ai_gate(  # type: ignore[union-attr]
                        mint, token.get("name", ""), token.get("symbol", ""), _gate_data_r
                    )
                    if not _gate_allow_r:
                        print(f"[raydium-scout] x {mint[:8]}... AI gate BLOCKED — {_gate_reason_r}")
                        continue

                print(
                    f"[raydium-scout] BUYING {mint[:8]}... "
                    f"({actual_buy:.4f} SOL @ {price_sol:.8f} SOL/token, score={score}/10, dex={token_dex}, route=jupiter)"
                )

                # Live: read actual tokens received to get true fill price (not stale DexScreener quote)
                if not PAPER_TRADING and wallet_svc is not None:
                    try:
                        await asyncio.sleep(3)
                        actual_bals = await wallet_svc.get_token_balances()  # type: ignore[union-attr]
                        actual_raw = next((int(t["raw_amount"]) for t in actual_bals if t["mint"] == mint), 0)
                        if actual_raw > 0:
                            actual_dec = next((t.get("decimals", TOKEN_DECIMALS) for t in actual_bals if t["mint"] == mint), TOKEN_DECIMALS)
                            actual_price = actual_buy / (actual_raw / 10 ** actual_dec)
                            print(f"[raydium-scout] Actual fill: {actual_price:.8f} SOL/token (quoted {price_sol:.8f}, diff {(actual_price/price_sol-1)*100:+.1f}%)")
                            price_sol = actual_price
                            token_amount = actual_raw
                    except Exception as _fe:
                        print(f"[raydium-scout] Warning: could not read actual fill ({_fe}), using quoted price")

                # ── IMMEDIATE Telegram alert — fire BEFORE open_position() ───────
                # If the bot crashes inside open_position(), the event-based alert never
                # fires. Sending directly here ensures you always know a buy happened.
                try:
                    from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_imm
                    _pc_ms_imm = float(token.get("pair_created_at") or 0)
                    _age_min_imm = round((now - _pc_ms_imm / 1000.0) / 60) if _pc_ms_imm > 0 else 0
                    _liq_imm = _c_liq
                    _mc_imm = float(token.get("market_cap_usd") or 0)
                    _tp_imm = price_sol * 1.60  # default TP; Position.__post_init__ sets exact value
                    _sl_imm = price_sol * 0.91  # default SL (-9%)
                    _imm_msg = (
                        f"🎯 <b>BUY EXECUTED — {token_dex.upper()}</b>\n"
                        f"<b>{token.get('name','?')}</b> (${token.get('symbol','?')})\n"
                        f"<code>{mint}</code>\n\n"
                        f"Size: <b>{actual_buy:.4f} SOL</b>  |  Score: {score}\n"
                        f"Entry: <code>{price_sol:.2e} SOL</code>\n"
                        f"TP ~+60%: <code>{_tp_imm:.2e}</code>  |  SL ~-9%: <code>{_sl_imm:.2e}</code>\n"
                        f"Liq: ${_liq_imm:,.0f}  |  MC: ${_mc_imm:,.0f}  |  Age: {_age_min_imm}min\n"
                        f"Sig: <code>{sig[:20]}...</code>"
                    )
                    asyncio.create_task(_tg_imm(_imm_msg))
                except Exception:
                    pass

                _sm_ray = runtime.get_service("social_monitor")
                bal = await _effective_wallet_sol(wallet_svc)
                _is_prem_ray, _ = _check_grok_premium(mint, _sm_ray, bal)
                _ray_grok_confirmed = (lambda _s: _s.is_grok_confirmed(mint) if _s else False)(_sm_ray)
                _ray_meta = {
                    "name": token.get("name", ""),
                    "symbol": token.get("symbol", ""),
                    "liq_usd": token.get("liquidity_usd", 0.0),
                    "vol_h24_usd": token.get("volume_h24_usd", 0.0),
                    "vol_h1_usd": token.get("volume_h1_usd", 0.0),
                    "price_change_h1": token.get("price_change_h1", None),
                    "price_change_m5": token.get("price_change_m5", None),
                    "market_cap_usd": token.get("market_cap_usd", 0.0),
                    "has_socials": token.get("_has_social", False),
                    "social_urls": token.get("social_urls", []),
                    "buyers_h1": token.get("buyers_h1", 0),
                    "pair_created_at": token.get("pair_created_at", 0),
                    "score_reasons": reasons,
                    "momentum_score": _c_momentum,
                }
                _ray_label = "A" if _split_enabled_rs and not _is_prem_ray else ""
                pos_mgr.open_position(  # type: ignore[union-attr]
                    mint=mint,
                    dex=token_dex,
                    entry_price_sol=price_sol,
                    entry_sol_spent=actual_buy,
                    token_amount=token_amount,
                    token_decimals=TOKEN_DECIMALS,
                    signature=sig,
                    score=score,
                    grok_confirmed=_ray_grok_confirmed,
                    grok_premium=_is_prem_ray,
                    meta=_ray_meta,
                    label=_ray_label,
                )
                _ray_label_tag = " [HARVESTER]" if _ray_label == "A" else ""
                print(
                    f"[raydium-scout] Position opened{_ray_label_tag}: {mint[:8]}... "
                    f"entry={price_sol:.8f} SOL tokens={token_amount} sig={sig[:20]}"
                )
                # ── Split-buy: open MONSTER HUNTER position B ────────────────
                if _ray_label == "A":
                    await _execute_split_buy_b(
                        runtime, pos_mgr, raydium_svc, wallet_svc,
                        mint=mint, dex=token_dex, entry_price_sol=price_sol,
                        meta=_ray_meta, token_decimals=TOKEN_DECIMALS, score=score,
                        grok_confirmed=_ray_grok_confirmed, grok_premium=False,
                        tag="[raydium-scout]",
                    )
                bought_this_cycle = True
                break  # one trade per cycle

            if not bought_this_cycle:
                print(
                    f"[raydium-scout] Cycle complete -- {len(tokens)} tokens scanned, "
                    f"no qualifying entries"
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[raydium-scout] Unexpected error: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        await asyncio.sleep(SCAN_INTERVAL)


async def social_momentum_loop(runtime: AgentRuntime) -> None:
    """Strategy E: Social Momentum Snipe — established tokens surging on X/Twitter.

    Unlike Strategy B (new pump.fun graduations), this hunts tokens that have been
    trading for 2h–7 days and are suddenly catching fire on X/Twitter.

    The three-brain pipeline:
      Brain 1 — Grok:      scans X/Twitter every 5 min for trending Solana tokens with CAs
      Brain 2 — DexScreener: verifies age, liquidity, h1/h24 momentum
      Brain 3 — GPT-4o:   web safety check (scam/rug reports)
      Synthesis — Score 0-10, log to strategy_e_signals.json

    RESEARCH MODE (default): logs every signal with 'would_buy' flag — NO real trades.
    LIVE MODE (strategy_e_enabled=True + research_mode=False): executes via Jupiter.

    Config keys (all Jarvis-adjustable at runtime):
      strategy_e_enabled       — master on/off
      strategy_e_research_mode — True = research only, False = live
      strategy_e_buy_sol       — SOL per trade (when live)
      strategy_e_min_liq_usd   — min liquidity ($40k default)
      strategy_e_min_age_hours — token must be >2h old
      strategy_e_max_age_hours — token must be <7 days old
      strategy_e_min_h1_pct    — h1 momentum >+3%
      strategy_e_max_h24_pct   — skip if already up >300% today
      strategy_e_min_score     — min score (0-10) to trigger entry
      strategy_e_min_volume_usd — min h24 volume
    """
    import re
    import aiohttp as _aiohttp
    from elizaos.plugins.solana import live_config as _lc_e
    from elizaos.types import ServiceTypeRegistry

    SCAN_INTERVAL   = 300   # 5 minutes
    SIGNALS_FILE    = os.path.join(os.path.dirname(__file__), "packages/python/elizaos/plugins/solana/strategy_e_signals.json")
    # Fix path relative to this file's location
    SIGNALS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elizaos/plugins/solana/strategy_e_signals.json")

    _evaluated_this_session: set[str] = set()

    def _load_signals() -> list:
        try:
            with open(SIGNALS_FILE) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_signal(sig: dict) -> None:
        sigs = _load_signals()
        sigs.append(sig)
        # Keep last 500 signals
        if len(sigs) > 500:
            sigs = sigs[-500:]
        try:
            with open(SIGNALS_FILE, "w") as f:
                json.dump(sigs, f, indent=2, default=str)
        except Exception as exc:
            print(f"[strat-e] Failed to save signal: {exc}", file=sys.stderr)

    async def _query_grok_momentum(session: _aiohttp.ClientSession) -> list[dict]:
        """Ask Grok for established Solana tokens gaining X/Twitter buzz right now.

        Research-backed v2 prompt:
        - KOL detection (accounts >50k followers = stronger signal)
        - Narrative tier classification (Tier1=political/AI, Tier2=animal/gaming, Tier3=generic)
        - Shill/coordinated activity detection
        - Momentum phase (rising/peaked/cooling)
        """
        import re
        grok_key = os.getenv("GROK_API_KEY", "")
        if not grok_key:
            return []
        try:
            import re as _re_inner
            from datetime import datetime as _dt_inner, timezone as _tz_inner, timedelta as _td_inner
            # Rolling 4-hour window for X search — today's date (UTC)
            _today = _dt_inner.now(_tz_inner.utc).strftime("%Y-%m-%d")

            prompt = (
                "You are a Solana memecoin trading signal scanner with LIVE real-time X/Twitter access.\n\n"
                "IMPORTANT: Search X posts from the LAST 2-4 HOURS ONLY. Do NOT use old/cached data.\n"
                "Exclude any token older than 7 days or with market cap above $10M.\n"
                "Exclude permanently established tokens like BONK, WIF, POPCAT, DOGWIFHAT — these always trend.\n\n"
                "TASK: Find Solana tokens with SUDDEN NEW buzz RIGHT NOW (not tokens that have been trending for days).\n\n"
                "Search X for: 'solana gem', 'pump.fun new', 'pumpswap', 'just launched solana', "
                "'solana 100x', 'graduated pump.fun', 'solana contract', and Solana contract addresses being shared.\n\n"
                "REQUIREMENTS:\n"
                "- Token trading on a Solana DEX with a real contract address\n"
                "- At least 2 hours old (past the initial rug window)\n"
                "- SUDDEN INCREASE in X mentions in the LAST 1-2 HOURS specifically\n"
                "- Price looks early — NOT already up 5x today\n\n"
                "SKIP THESE:\n"
                "- Coordinated shills: multiple accounts with identical copy-paste text\n"
                "- No contract address being shared (just a name with price calls)\n"
                "- Already peaked: been trending 6+ hours, h1 price change negative\n"
                "- Newly created bot accounts posting in bursts\n\n"
                "NARRATIVE TIERS:\n"
                "Tier1 = political event, AI agent with real product, celebrity connection → multi-day\n"
                "Tier2 = animal with story, gaming, ecosystem, sports → hours to 2 days\n"
                "Tier3 = generic meme, copy-cat → 15min–2hr only\n\n"
                "For each token, respond with EXACTLY this format (one per block):\n"
                "TOKEN: [name]\n"
                "SYMBOL: [ticker without $]\n"
                "CA: [32-44 char Solana base58 address, or UNKNOWN]\n"
                "NARRATIVE: [Tier1/Tier2/Tier3: description in 2-4 words]\n"
                "HYPE: [1-10]\n"
                "KOL: [Y/N]\n"
                "KOL_NAMES: [names or NONE]\n"
                "SHILL: [Y/N]\n"
                "PHASE: [rising/peaked/cooling]\n"
                "NOTES: [1 sentence on why people are excited RIGHT NOW]\n\n"
                "Maximum 6 tokens, ordered by hype score descending. "
                "Only include tokens discovered in the LAST 2-4 HOURS on X. "
                "If none qualify, reply: NONE"
            )
            # xAI Responses API (replaces deprecated chat/completions + search_parameters)
            # Requires grok-4 family; x_search tool forces live X search with date filter.
            payload = {
                "model": "grok-4-1-fast-non-reasoning",
                "instructions": (
                    "You are a real-time X/Twitter Solana trading signal scanner. "
                    "Use your live X search capability. Only report tokens with fresh buzz "
                    "from the last few hours. Be precise about contract addresses. "
                    "Be honest about shill detection — false signals cost money."
                ),
                "input": [{"role": "user", "content": prompt}],
                "max_output_tokens": 1400,
                "tools": [{"type": "x_search", "from_date": _today}],
            }
            async with session.post(
                "https://api.x.ai/v1/responses",
                json=payload,
                headers={"Authorization": f"Bearer {grok_key}", "Content-Type": "application/json"},
                timeout=_aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status != 200:
                    print(f"[strat-e] Grok API error {resp.status}", file=sys.stderr)
                    return []
                data = await resp.json()
            # Extract text from responses API output array
            text = ""
            for _out in data.get("output", []):
                if _out.get("type") == "message":
                    for _c in _out.get("content", []):
                        if _c.get("type") == "output_text":
                            text += _c.get("text", "")
            if "NONE" in text.upper() and len(text) < 50:
                print("[strat-e] Grok: no trending tokens found this cycle")
                return []

            # Parse TOKEN blocks
            tokens = []
            _SOLANA_ADDR_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
            blocks = re.split(r'\n(?=TOKEN:)', text.strip())
            for block in blocks:
                if "TOKEN:" not in block:
                    continue
                def _field(key: str) -> str:
                    m = re.search(rf'^{key}:\s*(.+)', block, re.MULTILINE | re.IGNORECASE)
                    return m.group(1).strip() if m else ""
                name       = _field("TOKEN")
                symbol     = _field("SYMBOL").lstrip("$")
                ca_raw     = _field("CA")
                narrative  = _field("NARRATIVE")
                hype_raw   = _field("HYPE")
                kol_raw    = _field("KOL").upper()
                kol_names  = _field("KOL_NAMES")
                shill_raw  = _field("SHILL").upper()
                phase      = _field("PHASE").lower()
                notes      = _field("NOTES")

                # Narrative tier from the NARRATIVE field prefix
                narr_lower = narrative.lower()
                if narr_lower.startswith("tier1"):
                    narrative_tier = 1
                elif narr_lower.startswith("tier2"):
                    narrative_tier = 2
                else:
                    narrative_tier = 3
                # Strip the "TierN:" prefix for display
                narrative_clean = re.sub(r'^tier\d[:\s]*', '', narrative, flags=re.IGNORECASE).strip()

                # Extract valid Solana CA
                ca_matches = _SOLANA_ADDR_RE.findall(ca_raw)
                mint = ca_matches[0] if ca_matches else ""
                try:
                    hype = int(re.search(r'\d+', hype_raw).group()) if hype_raw else 5  # type: ignore[union-attr]
                except Exception:
                    hype = 5
                kol_mentioned      = kol_raw.startswith("Y")
                coordinated_shill  = shill_raw.startswith("Y")

                if name:
                    tokens.append({
                        "name": name, "symbol": symbol, "mint": mint,
                        "buzz": True, "narrative": narrative_clean,
                        "narrative_tier": narrative_tier,
                        "hype": hype,
                        "kol_mentioned": kol_mentioned,
                        "kol_names": kol_names if kol_names and kol_names.upper() != "NONE" else "",
                        "coordinated_shill": coordinated_shill,
                        "phase": phase,
                        "notes": notes,
                    })
            # Log shill-filtered count
            total = len(tokens)
            shilled = sum(1 for t in tokens if t.get("coordinated_shill"))
            peaked  = sum(1 for t in tokens if t.get("phase") == "peaked")
            print(f"[strat-e] Grok returned {total} token(s) — {shilled} shill-flagged, {peaked} peaked-phase")
            return tokens
        except asyncio.TimeoutError:
            print("[strat-e] Grok query timed out", file=sys.stderr)
            return []
        except Exception as exc:
            print(f"[strat-e] Grok query error: {exc}", file=sys.stderr)
            return []

    async def _query_grok_kol_calls(session: _aiohttp.ClientSession) -> list[dict]:
        """Secondary Grok query: scan for KOL calls specifically (influencers posting CAs).

        KOL calls are the strongest leading indicator — run this in parallel with
        the background scan so we catch influencer posts the broad scan may miss.
        """
        import re as _re_kol
        from datetime import datetime as _dt_kol, timezone as _tz_kol
        grok_key = os.getenv("GROK_API_KEY", "")
        if not grok_key:
            return []
        try:
            _today_kol = _dt_kol.now(_tz_kol.utc).strftime("%Y-%m-%d")
            prompt = (
                "Search X right now for Solana token CALLS made in the LAST 3 HOURS "
                "by well-known crypto influencers or accounts with large followings.\n\n"
                "Look specifically for:\n"
                "- Accounts with 10,000+ followers posting a Solana contract address\n"
                "- Any influencer saying they 'bought' or 'aping' into a new Solana token TODAY\n"
                "- High-engagement posts (many likes/retweets) about a new Solana launch\n"
                "- Posts where an influencer shares a CA with genuine excitement (not copy-paste)\n\n"
                "Exclude: pure retweets, accounts with <5k followers, posts older than 3 hours.\n\n"
                "For each KOL call found, output EXACTLY:\n"
                "TOKEN: [name]\n"
                "SYMBOL: [symbol]\n"
                "CA: [solana contract address or UNKNOWN]\n"
                "KOL_ACCOUNT: [influencer account name]\n"
                "FOLLOWER_TIER: [MEGA=1M+ / LARGE=100k-1M / MID=10k-100k]\n"
                "ENGAGEMENT: [HIGH/MED/LOW]\n"
                "HYPE: [1-10]\n"
                "NOTES: [1 sentence on what they said]\n\n"
                "Maximum 5 results. If none found, reply: NONE"
            )
            payload = {
                "model": "grok-4-1-fast-non-reasoning",
                "instructions": "You are a real-time X/Twitter KOL call detector for Solana tokens. Use live X search. Only report calls from today.",
                "input": [{"role": "user", "content": prompt}],
                "max_output_tokens": 800,
                "tools": [{"type": "x_search", "from_date": _today_kol}],
            }
            async with session.post(
                "https://api.x.ai/v1/responses",
                json=payload,
                headers={"Authorization": f"Bearer {grok_key}", "Content-Type": "application/json"},
                timeout=_aiohttp.ClientTimeout(total=35),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
            # Extract text from responses API output array
            text = ""
            for _out in data.get("output", []):
                if _out.get("type") == "message":
                    for _c in _out.get("content", []):
                        if _c.get("type") == "output_text":
                            text += _c.get("text", "")
            if "NONE" in text.upper() and len(text) < 50:
                print("[strat-e] KOL scan: no influencer calls found this cycle")
                return []

            tokens = []
            _SOL_RE = _re_kol.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
            blocks = _re_kol.split(r'\n(?=TOKEN:)', text.strip())
            for block in blocks:
                if "TOKEN:" not in block:
                    continue
                def _kf(key: str) -> str:
                    m = _re_kol.search(rf'^{key}:\s*(.+)', block, _re_kol.MULTILINE | _re_kol.IGNORECASE)
                    return m.group(1).strip() if m else ""
                name      = _kf("TOKEN")
                symbol    = _kf("SYMBOL").lstrip("$")
                ca_raw    = _kf("CA")
                kol_acct  = _kf("KOL_ACCOUNT")
                tier      = _kf("FOLLOWER_TIER").upper()
                engage    = _kf("ENGAGEMENT").upper()
                hype_raw  = _kf("HYPE")
                notes     = _kf("NOTES")
                ca_matches = _SOL_RE.findall(ca_raw)
                mint = ca_matches[0] if ca_matches else ""
                try:
                    hype = int(_re_kol.search(r'\d+', hype_raw).group()) if hype_raw else 7  # type: ignore[union-attr]
                except Exception:
                    hype = 7
                # KOL calls always score highly — boost hype based on tier
                if "MEGA" in tier:   hype = max(hype, 9)
                elif "LARGE" in tier: hype = max(hype, 8)
                elif "MID" in tier:   hype = max(hype, 7)
                if name:
                    tokens.append({
                        "name": name, "symbol": symbol, "mint": mint,
                        "buzz": True, "narrative": f"KOL call ({kol_acct})",
                        "narrative_tier": 1 if "MEGA" in tier or "LARGE" in tier else 2,
                        "hype": hype,
                        "kol_mentioned": True,
                        "kol_names": kol_acct,
                        "coordinated_shill": False,
                        "phase": "rising",
                        "notes": f"KOL {kol_acct} ({tier}): {notes}",
                    })
            if tokens:
                print(f"[strat-e] KOL scan: {len(tokens)} influencer call(s) found")
            return tokens
        except Exception as exc:
            print(f"[strat-e] KOL scan error: {exc}", file=sys.stderr)
            return []

    async def _dexscreener_lookup(session: _aiohttp.ClientSession, mint: str, name: str, symbol: str) -> dict | None:
        """Fetch token data from DexScreener by mint or by name search."""
        try:
            # Try by mint first
            if mint:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs") or []
                        # Filter Solana pairs with liquidity, pick highest liquidity
                        sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and (p.get("liquidity") or {}).get("usd", 0) > 0]
                        if sol_pairs:
                            return max(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
            # Fallback: search by symbol
            if symbol:
                url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
                async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs") or []
                        sol_pairs = [
                            p for p in pairs
                            if p.get("chainId") == "solana"
                            and (p.get("liquidity") or {}).get("usd", 0) > 0
                            and (p.get("baseToken") or {}).get("symbol", "").upper() == symbol.upper()
                        ]
                        if sol_pairs:
                            return max(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
            return None
        except Exception as exc:
            print(f"[strat-e] DexScreener lookup failed for {name}: {exc}", file=sys.stderr)
            return None

    async def _gpt_web_check(session: _aiohttp.ClientSession, name: str, symbol: str, mint: str) -> tuple[bool, str]:
        """Quick GPT-4o web safety check — scam/rug reports."""
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if not openai_key:
            return True, "skipped (no key)"
        try:
            payload = {
                "model": "gpt-4o-search-preview",
                "messages": [{"role": "user", "content":
                    f'Quick safety check for Solana token "{name}" (${symbol}) CA: {mint or "unknown"}.\n'
                    f"Search X/Twitter and the web: any rug reports, honeypot alerts, or scam warnings?\n"
                    f"Also: is this a known established project with real community?\n"
                    f"Reply: SAFE / CAUTION / UNSAFE — then one sentence reason."
                }],
                "max_tokens": 100,
            }
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {openai_key}"},
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return True, f"check failed ({resp.status})"
                data = await resp.json()
            answer = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip().upper()
            safe = "UNSAFE" not in answer
            caution = "CAUTION" in answer
            return safe, answer[:120]
        except Exception:
            return True, "skipped (timeout)"

    def _score_token(dex_pair: dict, grok_data: dict, web_safe: bool) -> int:
        """Score token 0-10 based on momentum, liquidity, social, safety.

        Research-backed v2 scoring:
        - Coordinated shill = instant 0 (hard block)
        - Peaked phase = -3 penalty (buying exit liquidity)
        - KOL mentioned = +2 bonus (strongest reliable signal)
        - Narrative tier 1 = +2, tier 2 = +1, tier 3 = +0
        - Vol/liq ratio 2-10x = +1 (real trading vs wash trading)
        - MC sweet spot $200k-$5M = more upside room
        """
        # Hard blocks
        if grok_data.get("coordinated_shill"):
            return 0  # Never trade coordinated shills

        score = 0
        liq   = (dex_pair.get("liquidity") or {}).get("usd", 0)
        vol   = (dex_pair.get("volume") or {}).get("h24", 0)
        h1    = float((dex_pair.get("priceChange") or {}).get("h1", 0) or 0)
        h6    = float((dex_pair.get("priceChange") or {}).get("h6", 0) or 0)
        h24   = float((dex_pair.get("priceChange") or {}).get("h24", 0) or 0)
        fdv   = float((dex_pair.get("fdv") or 0) or 0)  # market cap proxy
        hype  = grok_data.get("hype", 5)
        phase = grok_data.get("phase", "unknown").lower()
        narr_tier = int(grok_data.get("narrative_tier", 3))
        kol   = grok_data.get("kol_mentioned", False)

        # Liquidity (0-2)
        if liq >= 100_000:    score += 2
        elif liq >= 30_000:   score += 1

        # Volume/liquidity ratio — real activity check (0-1)
        # Ratio 2-15x = genuine interest; <1x = dead; >20x = wash trading
        vol_liq_ratio = (vol / liq) if liq > 0 else 0
        if 1.5 <= vol_liq_ratio <= 15:
            score += 1

        # Market cap sweet spot $150k-$5M (0-1)
        # Too low = rug risk, too high = priced in
        if fdv > 0:
            if 150_000 <= fdv <= 5_000_000:
                score += 1
            elif fdv > 15_000_000:
                score = max(0, score - 1)  # already too large

        # h1 momentum — early entry zone (0-2)
        # Research: 0-50% h1 is sweet spot. >50% = buying exit liquidity.
        if 5 <= h1 <= 30:     score += 2
        elif 0 <= h1 < 5:     score += 1
        # h1 > 50 gives 0 (already pumped — blocked in filter pipeline)

        # h24 not overextended (0-1)
        if h24 < 150:         score += 1
        elif h24 > 400:
            score = max(0, score - 1)  # dangerously extended

        # Grok hype quality (0-2)
        if hype >= 8:         score += 2
        elif hype >= 6:       score += 1

        # KOL signal — strongest leading indicator (0-2)
        if kol:               score += 2

        # Web safe (0-1)
        if web_safe:          score += 1

        # Narrative tier bonus — drives hold-time and TP selection
        if narr_tier == 1:    score += 2   # political/AI/celebrity — multi-day potential
        elif narr_tier == 2:  score += 1   # animal/gaming/ecosystem — hours to 1-2 days
        # tier 3 = generic meme, no bonus

        # Momentum phase penalty — peaked = selling into exit liquidity
        if phase == "peaked":
            score = max(0, score - 3)
        elif phase == "cooling":
            score = max(0, score - 2)

        return min(score, 10)

    print("[strat-e] Social Momentum Snipe loop started — RESEARCH MODE "
          "(logging signals, no trades until strategy_e_enabled=True + research_mode=False)")

    await asyncio.sleep(120)  # stagger startup — let other loops settle first

    async with _aiohttp.ClientSession() as session:
        while True:
            try:
                cfg = _lc_e.all_config()
                research_mode   = cfg.get("strategy_e_research_mode", True)
                live_enabled    = cfg.get("strategy_e_enabled", False)
                MIN_LIQ         = cfg.get("strategy_e_min_liq_usd", 25_000)
                MIN_AGE_H       = cfg.get("strategy_e_min_age_hours", 2.0)
                MAX_AGE_H       = cfg.get("strategy_e_max_age_hours", 24.0)   # tightened: 24h sweet spot
                MIN_H1          = cfg.get("strategy_e_min_h1_pct", 0.0)       # allow 0%+ (early momentum)
                MAX_H1          = cfg.get("strategy_e_max_h1_pct", 50.0)      # new: skip >50% h1 (already pumped)
                MAX_H24         = cfg.get("strategy_e_max_h24_pct", 400.0)
                MIN_SCORE       = cfg.get("strategy_e_min_score", 6)
                MIN_VOL         = cfg.get("strategy_e_min_volume_usd", 15_000)
                BUY_SOL         = cfg.get("strategy_e_buy_sol", 0.05)

                # Always run research scanning; only gate live execution
                print(f"[strat-e] Scanning X/Twitter for momentum tokens "
                      f"({'RESEARCH' if research_mode or not live_enabled else 'LIVE'} mode)")

                # Run background scan + KOL call detector in parallel
                bg_task  = asyncio.create_task(_query_grok_momentum(session))
                kol_task = asyncio.create_task(_query_grok_kol_calls(session))
                bg_tokens, kol_tokens = await asyncio.gather(bg_task, kol_task)

                # Merge, dedup by mint (KOL tokens take precedence as they're higher quality)
                seen_mints: set[str] = set()
                grok_tokens: list[dict] = []
                for tok in kol_tokens:   # KOL first — higher priority
                    key = tok.get("mint") or f"{tok.get('name')}_{tok.get('symbol')}"
                    if key not in seen_mints:
                        seen_mints.add(key)
                        grok_tokens.append(tok)
                for tok in bg_tokens:
                    key = tok.get("mint") or f"{tok.get('name')}_{tok.get('symbol')}"
                    if key not in seen_mints:
                        seen_mints.add(key)
                        grok_tokens.append(tok)

                if not grok_tokens:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                pos_mgr    = runtime.get_service("position_manager")
                wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)

                for grok_tok in grok_tokens:
                    name   = grok_tok.get("name", "?")
                    symbol = grok_tok.get("symbol", "?")
                    mint   = grok_tok.get("mint", "")

                    # ── Pre-screen: shill and peaked phase ────────────────────────
                    if grok_tok.get("coordinated_shill"):
                        print(f"[strat-e] x {name} (${symbol}) — SHILL DETECTED by Grok, skipping")
                        _save_signal({
                            "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                            "name": name, "symbol": symbol, "mint": mint,
                            "grok_hype": grok_tok.get("hype", 0), "narrative": grok_tok.get("narrative", ""),
                            "narrative_tier": grok_tok.get("narrative_tier", 3),
                            "decision": "SHILL", "reason": "Grok flagged as coordinated shill",
                        })
                        continue
                    if grok_tok.get("phase") == "peaked":
                        print(f"[strat-e] x {name} (${symbol}) — phase=peaked, buying exit liquidity — skip")
                        _save_signal({
                            "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                            "name": name, "symbol": symbol, "mint": mint,
                            "grok_hype": grok_tok.get("hype", 0), "narrative": grok_tok.get("narrative", ""),
                            "narrative_tier": grok_tok.get("narrative_tier", 3),
                            "decision": "PEAKED", "reason": "momentum phase = peaked",
                        })
                        continue

                    # Skip already evaluated this session
                    cache_key = mint or f"{name}_{symbol}"
                    if cache_key in _evaluated_this_session:
                        continue
                    _evaluated_this_session.add(cache_key)

                    # Skip already traded tokens
                    if mint and pos_mgr and mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
                        print(f"[strat-e] x {name} — already traded this session")
                        continue

                    # DexScreener lookup
                    pair = await _dexscreener_lookup(session, mint, name, symbol)
                    if not pair:
                        print(f"[strat-e] x {name} (${symbol}) — not found on DexScreener")
                        _save_signal({
                            "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                            "name": name, "symbol": symbol, "mint": mint,
                            "grok_hype": grok_tok.get("hype", 0), "narrative": grok_tok.get("narrative", ""),
                            "narrative_tier": grok_tok.get("narrative_tier", 3),
                            "decision": "SKIP", "reason": "not found on DexScreener",
                        })
                        continue

                    # Extract DexScreener metrics
                    liq_usd   = (pair.get("liquidity") or {}).get("usd", 0)
                    vol_h24   = (pair.get("volume") or {}).get("h24", 0)
                    h1_pct    = float((pair.get("priceChange") or {}).get("h1", 0) or 0)
                    h6_pct    = float((pair.get("priceChange") or {}).get("h6", 0) or 0)
                    h24_pct   = float((pair.get("priceChange") or {}).get("h24", 0) or 0)
                    price_sol = float((pair.get("priceNative") or 0) or 0)
                    mc_usd    = float((pair.get("fdv") or pair.get("marketCap") or 0) or 0)
                    vol_liq_ratio = (vol_h24 / liq_usd) if liq_usd > 0 else 0
                    # Age: pairCreatedAt in ms
                    created_ms = pair.get("pairCreatedAt") or 0
                    age_hours  = (time.time() - created_ms / 1000.0) / 3600.0 if created_ms else 9999
                    dex_name   = pair.get("dexId", "?")
                    token_mint = (pair.get("baseToken") or {}).get("address", mint) or mint

                    base_info = (f"{name} (${symbol}) | {dex_name} | "
                                 f"liq=${liq_usd:,.0f} | mc=${mc_usd:,.0f} | age={age_hours:.1f}h | "
                                 f"h1={h1_pct:+.1f}% h24={h24_pct:+.1f}% | "
                                 f"vol=${vol_h24:,.0f} (v/l={vol_liq_ratio:.1f}x)"
                                 f"{' | KOL✓' if grok_tok.get('kol_mentioned') else ''}")

                    # ── Filter pipeline ───────────────────────────────────────────
                    skip_reason = None
                    if liq_usd < MIN_LIQ:
                        skip_reason = f"liq ${liq_usd:,.0f} < ${MIN_LIQ:,.0f} minimum"
                    elif age_hours < MIN_AGE_H:
                        skip_reason = f"too new: {age_hours:.1f}h < {MIN_AGE_H}h minimum"
                    elif age_hours > MAX_AGE_H:
                        skip_reason = f"too old: {age_hours:.0f}h > {MAX_AGE_H:.0f}h maximum"
                    elif h1_pct < MIN_H1:
                        skip_reason = f"no momentum: h1={h1_pct:+.1f}% < {MIN_H1}% minimum"
                    elif h1_pct > MAX_H1:
                        skip_reason = f"already pumped: h1={h1_pct:+.0f}% > {MAX_H1:.0f}% — buying exit liquidity"
                    elif h24_pct > MAX_H24:
                        skip_reason = f"overextended: h24={h24_pct:+.0f}% > {MAX_H24:.0f}%"
                    elif vol_h24 < MIN_VOL:
                        skip_reason = f"low volume: ${vol_h24:,.0f} < ${MIN_VOL:,.0f}"
                    elif vol_liq_ratio > 25:
                        skip_reason = f"vol/liq={vol_liq_ratio:.0f}x — likely wash trading"

                    if skip_reason:
                        print(f"[strat-e] x {base_info} — {skip_reason}")
                        _save_signal({
                            "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                            "name": name, "symbol": symbol, "mint": token_mint,
                            "liq_usd": liq_usd, "vol_h24": vol_h24, "mc_usd": mc_usd,
                            "vol_liq_ratio": round(vol_liq_ratio, 2),
                            "h1_pct": h1_pct, "h6_pct": h6_pct, "h24_pct": h24_pct,
                            "age_hours": age_hours,
                            "grok_hype": grok_tok.get("hype", 0),
                            "narrative": grok_tok.get("narrative", ""),
                            "narrative_tier": grok_tok.get("narrative_tier", 3),
                            "kol_mentioned": grok_tok.get("kol_mentioned", False),
                            "kol_names": grok_tok.get("kol_names", ""),
                            "phase": grok_tok.get("phase", "unknown"),
                            "decision": "FILTERED", "reason": skip_reason,
                        })
                        continue

                    # GPT-4o web safety check
                    web_safe, web_reason = await _gpt_web_check(session, name, symbol, token_mint)
                    if not web_safe:
                        print(f"[strat-e] x {base_info} — WEB UNSAFE: {web_reason[:80]}")
                        _save_signal({
                            "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                            "name": name, "symbol": symbol, "mint": token_mint,
                            "liq_usd": liq_usd, "vol_h24": vol_h24, "mc_usd": mc_usd,
                            "h1_pct": h1_pct, "h6_pct": h6_pct, "h24_pct": h24_pct,
                            "grok_hype": grok_tok.get("hype", 0),
                            "narrative": grok_tok.get("narrative", ""),
                            "narrative_tier": grok_tok.get("narrative_tier", 3),
                            "kol_mentioned": grok_tok.get("kol_mentioned", False),
                            "phase": grok_tok.get("phase", "unknown"),
                            "web_reason": web_reason, "decision": "BLOCKED", "reason": "web unsafe",
                        })
                        continue

                    # Score
                    score = _score_token(pair, grok_tok, web_safe)
                    would_buy = score >= MIN_SCORE

                    # Narrative-aware TP multiplier for position meta
                    narr_tier = grok_tok.get("narrative_tier", 3)
                    kol_hit   = grok_tok.get("kol_mentioned", False)
                    if narr_tier == 1 or kol_hit:
                        # Political/AI/celebrity + KOL: sustained pump potential → +200% TP
                        narrative_tp_mult = 3.00
                        tp_label = "Tier1/KOL (+200%)"
                    elif narr_tier == 2:
                        # Animal/gaming/ecosystem → +100% TP
                        narrative_tp_mult = 2.00
                        tp_label = "Tier2 (+100%)"
                    else:
                        # Generic meme: fast pump-and-dump → +60% exit fast
                        narrative_tp_mult = 1.60
                        tp_label = "Tier3 (+60% quick)"

                    sig = {
                        "ts": time.time(), "datetime": datetime.utcnow().isoformat(),
                        "name": name, "symbol": symbol, "mint": token_mint,
                        "dex": dex_name, "liq_usd": liq_usd, "vol_h24": vol_h24,
                        "mc_usd": mc_usd, "vol_liq_ratio": round(vol_liq_ratio, 2),
                        "h1_pct": h1_pct, "h6_pct": h6_pct, "h24_pct": h24_pct,
                        "age_hours": round(age_hours, 2), "price_sol": price_sol,
                        "score": score,
                        "grok_hype": grok_tok.get("hype", 0),
                        "narrative": grok_tok.get("narrative", ""),
                        "narrative_tier": narr_tier,
                        "kol_mentioned": kol_hit,
                        "kol_names": grok_tok.get("kol_names", ""),
                        "phase": grok_tok.get("phase", "unknown"),
                        "notes": grok_tok.get("notes", ""),
                        "web_safe": web_safe, "web_reason": web_reason,
                        "narrative_tp_mult": narrative_tp_mult, "tp_label": tp_label,
                        "would_buy": would_buy,
                        "decision": "WOULD_BUY" if would_buy else "LOW_SCORE",
                        "reason": f"score={score}/{MIN_SCORE} required" if not would_buy else "passed all filters",
                    }

                    if would_buy:
                        print(f"[strat-e] ✅ SIGNAL: {base_info} | score={score}/10 | "
                              f"tier={narr_tier} | {tp_label} | kol={kol_hit} | {web_reason[:50]}")
                    else:
                        print(f"[strat-e] ~ {base_info} | score={score}/10 (need {MIN_SCORE}) — logged")

                    # ── Execute (only if live mode AND not research) ──────────────
                    if would_buy and live_enabled and not research_mode:
                        # Full trade execution gate
                        if wallet_svc and pos_mgr:
                            try:
                                bal = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
                                actual_buy = _dynamic_buy_sol(BUY_SOL, bal)
                                allowed, gate_reason = pos_mgr.check_trade_allowed(actual_buy, bal)  # type: ignore[union-attr]
                                if not allowed:
                                    sig["decision"] = "GATED"
                                    sig["reason"] = gate_reason
                                    print(f"[strat-e] x gated: {gate_reason}")
                                else:
                                    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                                    if raydium_svc and price_sol > 0:
                                        token_amount = int((actual_buy / price_sol) * (10 ** 6))
                                        print(f"[strat-e] BUYING {name} via Jupiter: "
                                              f"{actual_buy:.4f} SOL @ {price_sol:.2e} | {tp_label}")
                                        try:
                                            sig_tx = await raydium_svc.swap_jupiter_buy(  # type: ignore[union-attr]
                                                token_mint, actual_buy,
                                                slippage_bps=2000, priority_fee=0.001
                                            )
                                            await pos_mgr.open_position(  # type: ignore[union-attr]
                                                mint=token_mint,
                                                sol_spent=actual_buy,
                                                token_amount=token_amount,
                                                entry_price_sol=price_sol,
                                                dex="social_momentum",
                                                meta={
                                                    "strategy": "E",
                                                    "grok_narrative": grok_tok.get("narrative", ""),
                                                    "grok_narrative_tier": narr_tier,
                                                    "grok_hype": grok_tok.get("hype", 0),
                                                    "kol_mentioned": kol_hit,
                                                    "kol_names": grok_tok.get("kol_names", ""),
                                                    "narrative_tp_mult": narrative_tp_mult,
                                                    "score": score,
                                                    "buy_sig": sig_tx,
                                                },
                                            )
                                            sig["decision"] = "BOUGHT"
                                            sig["reason"] = f"executed {actual_buy:.4f} SOL"
                                            print(f"[strat-e] ✅ Position opened: {name} {actual_buy:.4f} SOL | {tp_label}")
                                        except Exception as buy_exc:
                                            sig["decision"] = "BUY_FAILED"
                                            sig["reason"] = str(buy_exc)
                                            print(f"[strat-e] Buy failed for {name}: {buy_exc}", file=sys.stderr)
                            except Exception as exec_exc:
                                print(f"[strat-e] Execution error: {exec_exc}", file=sys.stderr)
                    elif would_buy and research_mode:
                        sig["reason"] = "Research mode — trade not executed (set research_mode=False to go live)"

                    _save_signal(sig)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[strat-e] Unexpected error: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

            await asyncio.sleep(SCAN_INTERVAL)



async def _find_pumpswap_pair(session: Any, mint: str) -> dict | None:
    """Search DexScreener for a PumpSwap pair for a newly graduated token.

    After March 2025, pump.fun graduation routes liquidity to PumpSwap
    (constant product AMM, dexId contains 'pump').
    Returns pair data if found within the last 15 minutes, else None.
    """
    import aiohttp as _aiohttp
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        pairs = data.get("pairs") or []
        # PumpSwap dexId is "pumpswap", "pump-amm", or "pumpfun-amm".
        # Explicitly exclude "pumpfun" (bonding curve) — it also contains "pump" but is
        # the drained bonding curve pool after graduation, NOT the actual AMM pair.
        _PUMPSWAP_DEX_IDS = {"pumpswap", "pump-amm", "pumpfun-amm"}
        pumpswap_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and p.get("dexId", "").lower() in _PUMPSWAP_DEX_IDS
        ]
        if not pumpswap_pairs:
            return None
        # Freshest pair
        best = max(pumpswap_pairs, key=lambda p: p.get("pairCreatedAt") or 0)
        pair_created_ms = best.get("pairCreatedAt") or 0
        age_mins = (time.time() - pair_created_ms / 1000.0) / 60 if pair_created_ms else 999
        if age_mins > 20:
            return None   # too old — graduation sell pressure already over
        price_chg = best.get("priceChange") or {}
        vol = best.get("volume") or {}
        txns = best.get("txns") or {}
        h1_txns = txns.get("h1") or {}
        h1_buys = int(h1_txns.get("buys") or 0)
        h1_sells = int(h1_txns.get("sells") or 0)
        h1_vol = float(vol.get("h1") or 0)
        liq_usd = float((best.get("liquidity") or {}).get("usd") or 0)
        return {
            "mint": mint,
            "name": (best.get("baseToken") or {}).get("name", ""),
            "symbol": (best.get("baseToken") or {}).get("symbol", ""),
            "pair_address": best.get("pairAddress", ""),
            "price_sol": float(best.get("priceNative") or 0),
            "liquidity_usd": liq_usd,
            "vol_h1_usd": h1_vol,
            "vol_liq_ratio": round(h1_vol / liq_usd, 1) if liq_usd > 0 else 0.0,
            "txns_h1_buys": h1_buys,
            "txns_h1_sells": h1_sells,
            "buy_ratio_h1": round(h1_buys / (h1_buys + h1_sells) * 100, 1) if (h1_buys + h1_sells) > 0 else 0.0,
            "pair_created_at_ms": pair_created_ms,
            "age_mins": round(age_mins, 1),
            "dex_id": best.get("dexId", ""),
            "price_change_m5": float(price_chg.get("m5") or 0),
            "price_change_h1": float(price_chg.get("h1") or 0),
        }
    except Exception:
        return None


async def graduation_confirmation_loop(runtime: AgentRuntime) -> None:
    """Re-checks parked Strategy B tokens after graduation_confirmation_wait seconds.

    Buys if momentum is confirmed (price ≥ snap, vol still building, buy_ratio ≥ 52%, liq stable).
    Skips and logs if price dumped during the wait window.
    """
    from elizaos.plugins.solana import live_config as _lc_gcl
    from elizaos.types import ServiceTypeRegistry
    import aiohttp as _gcl_aiohttp

    async with _gcl_aiohttp.ClientSession() as _gcl_session:
        while True:
            try:
                await asyncio.sleep(30)
                if not _grad_confirmation_pending:
                    continue

                _pos_mgr_gcl = runtime.get_service(ServiceTypeRegistry.POSITION_MANAGER)
                now = time.time()

                for mint in list(_grad_confirmation_pending.keys()):
                    snap = _grad_confirmation_pending.get(mint)
                    if snap is None:
                        continue

                    elapsed = now - snap["detected_ts"]
                    conf_wait = snap["conf_wait"]

                    # Discard if too old (3× wait window = max 90 min at default 600s)
                    if elapsed > conf_wait * 3:
                        print(f"[grad-confirm] x {mint[:8]}... EXPIRED (waited {elapsed/60:.0f}m, max {conf_wait*3/60:.0f}m) — discarding")
                        del _grad_confirmation_pending[mint]
                        continue

                    # Not ready yet
                    if elapsed < conf_wait:
                        remaining = conf_wait - elapsed
                        print(f"[grad-confirm] {mint[:8]}... waiting {remaining:.0f}s more ({elapsed:.0f}/{conf_wait:.0f}s elapsed)")
                        continue

                    # Time's up — re-fetch DexScreener and confirm
                    print(f"[grad-confirm] {mint[:8]}... {elapsed:.0f}s elapsed — checking momentum confirmation...")

                    # Skip if already in a position or being bought
                    if _pos_mgr_gcl and mint in _pos_mgr_gcl.positions:
                        print(f"[grad-confirm] x {mint[:8]}... already in position — skip")
                        del _grad_confirmation_pending[mint]
                        continue
                    if mint in _buying_mints:
                        continue  # in-flight, check again next cycle

                    fresh_pair = await _find_pumpswap_pair(_gcl_session, mint)
                    if not fresh_pair or fresh_pair.get("liquidity_usd", 0) <= 0:
                        print(f"[grad-confirm] x {mint[:8]}... no fresh pair data — discarding")
                        del _grad_confirmation_pending[mint]
                        continue

                    price_now     = fresh_pair.get("price_sol", 0.0)
                    liq_now       = fresh_pair.get("liquidity_usd", 0.0)
                    vol_h1_now    = fresh_pair.get("volume_h1_usd", 0.0)
                    buy_ratio_now = fresh_pair.get("buy_ratio_h1", 0.0)
                    price_snap    = snap["price_at_snap"]
                    liq_snap      = snap["liq_at_snap"]
                    vol_snap      = snap["vol_h1_at_snap"]

                    _min_br = float(_lc_gcl.get("b_min_buy_ratio", 52.0) or 52.0)

                    fail_reasons = []
                    if price_snap > 0 and price_now < price_snap * 0.95:
                        fail_reasons.append(f"price dropped {((price_now/price_snap)-1)*100:+.0f}% during wait (snap={price_snap:.2e} now={price_now:.2e})")
                    if vol_snap > 0 and vol_h1_now < vol_snap * 0.7:
                        fail_reasons.append(f"volume dropped {((vol_h1_now/vol_snap)-1)*100:+.0f}% (was ${vol_snap:,.0f} now ${vol_h1_now:,.0f})")
                    if liq_snap > 0 and liq_now < liq_snap * 0.7:
                        fail_reasons.append(f"liquidity dropped {((liq_now/liq_snap)-1)*100:+.0f}% (was ${liq_snap:,.0f} now ${liq_now:,.0f})")
                    txns = fresh_pair.get("txns_h1_buys", 0) + fresh_pair.get("txns_h1_sells", 0)
                    if txns >= 10 and _min_br > 0 and buy_ratio_now < _min_br:
                        fail_reasons.append(f"buy_ratio={buy_ratio_now:.0f}% < {_min_br:.0f}% (sellers winning)")

                    del _grad_confirmation_pending[mint]

                    if fail_reasons:
                        print(f"[grad-confirm] x {mint[:8]}... CONFIRMATION FAILED — {'; '.join(fail_reasons)}")
                        try:
                            from elizaos.plugins.solana import rejection_tracker as _rt_gcl
                            _rt_gcl.record(mint, "confirmation_failed",
                                           filter_name="graduation_confirmation_wait",
                                           filter_value=round(elapsed, 0),
                                           threshold=conf_wait,
                                           strategy="grad",
                                           extra={"price_chg": round((price_now/price_snap-1)*100, 1) if price_snap > 0 else 0,
                                                  "reasons": fail_reasons})
                        except Exception:
                            pass
                        continue

                    # ── Safety check (rugcheck + LP lock) ────────────────────────
                    _gc_safe, _gc_safety_reason = await _pos_mgr_gcl.check_token_safety(mint)  # type: ignore[union-attr]
                    if not _gc_safe:
                        print(f"[grad-confirm] x {mint[:8]}... UNSAFE: {_gc_safety_reason}")
                        del _grad_confirmation_pending[mint]
                        continue

                    # ── HARD BUY GATE — final check before any transaction ────────
                    _gc_buys  = int(fresh_pair.get("txns_h1_buys") or 0)
                    _gc_sells = int(fresh_pair.get("txns_h1_sells") or 0)
                    _gate_ok_gc, _ = _hard_buy_gate(
                        "[grad-confirm]", mint, "b",
                        liq_usd=liq_now,
                        mc_usd=float(fresh_pair.get("market_cap_usd") or 0),
                        vol_h1_usd=vol_h1_now,
                        buy_txns=_gc_buys,
                        sell_txns=_gc_sells,
                        safety_passed=_gc_safe,
                        safety_reason=_gc_safety_reason,
                    )
                    if not _gate_ok_gc:
                        del _grad_confirmation_pending[mint]
                        continue
                    # ────────────────────────────────────────────────────────────────

                    # ── Momentum confirmed — execute buy ────────────────────────
                    price_gain = ((price_now / price_snap) - 1) * 100 if price_snap > 0 else 0
                    print(
                        f"[grad-confirm] ✅ {mint[:8]}... CONFIRMED — price {price_gain:+.0f}% "
                        f"liq=${liq_now:,.0f} vol=${vol_h1_now:,.0f} buy={buy_ratio_now:.0f}% — BUYING"
                    )

                    if mint in _buying_mints or (_pos_mgr_gcl and mint in _pos_mgr_gcl.positions):
                        continue
                    _buying_mints.add(mint)

                    _buy_sol_gcl = snap.get("buy_sol", float(_lc_gcl.get("buy_sol", 0.05)))
                    sig = ""
                    token_amount = 0

                    try:
                        if not PAPER_TRADING:
                            _raydium_gcl = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                            if _raydium_gcl is None:
                                raise RuntimeError("RaydiumService not available")
                            try:
                                sig, token_amount = await _raydium_gcl.buy_jupiter(mint, _buy_sol_gcl, slippage=0.15)  # type: ignore[union-attr]
                            except Exception:
                                _pump_gcl = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
                                if _pump_gcl is None:
                                    raise RuntimeError("PumpFunService not available")
                                sig = await _pump_gcl.buy(mint, _buy_sol_gcl, slippage=0.15, pool="pump-amm")  # type: ignore[union-attr]
                        else:
                            sig = "PAPER_CONFIRM_" + mint[:8]
                            token_amount = int((_buy_sol_gcl / max(price_now, 1e-12)) * 10 ** TOKEN_DECIMALS)

                        _pool_addr = snap.get("pool_addr", "") or fresh_pair.get("pairAddress", "")
                        if _pos_mgr_gcl is not None:
                            _pos_mgr_gcl.open_position(
                                mint=mint, dex="pumpswap",
                                entry_price_sol=price_now,
                                entry_sol_spent=_buy_sol_gcl,
                                token_amount=token_amount,
                                token_decimals=TOKEN_DECIMALS,
                                signature=sig,
                                grok_confirmed=False,
                                grok_premium=False,
                                meta={
                                    "name":             fresh_pair.get("name", ""),
                                    "symbol":           fresh_pair.get("symbol", ""),
                                    "liq_usd":          liq_now,
                                    "vol_h1_usd":       vol_h1_now,
                                    "price_change_m5":  fresh_pair.get("price_change_m5", 0),
                                    "price_change_h1":  fresh_pair.get("price_change_h1", 0),
                                    "strategy":         "grad_confirm",
                                    "confirmation_wait_secs": round(elapsed, 0),
                                    "price_gain_during_wait": round(price_gain, 1),
                                    "pool_address":     _pool_addr,
                                    "moon_mode":        snap.get("is_ultra_fast", False),
                                },
                            )
                        print(f"[grad-confirm] Position opened: {mint[:8]}... entry={price_now:.2e} SOL sig={sig[:20]}")
                    except Exception as _gcl_exc:
                        print(f"[grad-confirm] Buy failed {mint[:8]}...: {_gcl_exc}", file=sys.stderr)
                    finally:
                        _buying_mints.discard(mint)

            except asyncio.CancelledError:
                raise
            except Exception as _gcl_outer:
                print(f"[grad-confirm] Loop error: {_gcl_outer}", file=sys.stderr)


async def graduation_snipe_loop(runtime: AgentRuntime, graduation_queue: asyncio.Queue) -> None:
    """Strategy B: Graduation Snipe — buy the dip when a token migrates to PumpSwap.

    After March 2025, pump.fun graduated tokens route to PumpSwap (not Raydium).
    Entry strategy:
      - Wait for bonding curve complete=True (queued by autonomous_scout_loop)
      - Wait 30-60s for the initial sell-pressure dip from bonding curve holders
      - Verify PumpSwap pool exists and has decent liquidity
      - Buy 0.02 SOL (proven traction = slightly larger position)
      - SL at -15%, TP at +25%/+56% staircase

    Research note: brief dip often occurs at graduation — "buy the graduation dip"
    can work. Target: 1-3 blocks post-migration.
    """
    import aiohttp as _aiohttp
    from elizaos.types import ServiceTypeRegistry

    from elizaos.plugins.solana import live_config as _lc_b
    TOKEN_DECIMALS = 6      # PumpSwap tokens use 6 decimals
    # Note: PAPER_TRADING, BUY_SOL, MIN_LIQUIDITY_USD and DIP_WAIT_SECS are re-read
    # from live_config inside the main loop body so Jarvis can adjust them at runtime.
    PAPER_TRADING     = _lc_b.get("paper_trading", False)
    BUY_SOL           = _lc_b.get("buy_sol", 0.05)
    MIN_LIQUIDITY_USD = _lc_b.get("strategy_b_min_liq_usd", 13500)
    DIP_WAIT_SECS     = _lc_b.get("strategy_b_dip_wait_secs", 20)

    GRAD_USER_ID = "00000000-0000-0000-0000-000000000093"
    GRAD_ROOM_ID = "00000000-0000-0000-0000-000000000092"

    processed: set[str] = set()
    _last_api_poll: float = 0.0  # tracks last pump.fun API graduation poll
    API_POLL_INTERVAL = 10  # poll pump.fun API every 10s — faster detection = earlier entry before token dumps

    # Feed every graduation into Strategy C's 5-min watch queue (in addition to Strategy B)
    async def _on_graduation_strat_c(data: dict) -> None:
        mint_g = data.get("mint", "")
        if mint_g and mint_g not in _strat_c_grad_queue:
            _strat_c_grad_queue[mint_g] = time.time()

    runtime.register_event("token_graduation", _on_graduation_strat_c)

    print(f"[grad-snipe] Graduation snipe loop started — Strategy B (PumpSwap, {BUY_SOL} SOL each, pump.fun API poll)")

    async def _poll_pumpfun_graduations(session: Any) -> list[str]:
        """Poll pump.fun API for tokens that have recently graduated to PumpSwap.

        Uses the official pump.fun v3 API which returns `complete=True` tokens with
        a `pool_address` field (the PumpSwap pool account). This is the most reliable
        graduation source — the WS Withdraw detection generated false positives because
        pump.fun's PumpSwap migration does NOT use Instruction: Withdraw.
        Returns list of new mint addresses (not yet seen) that graduated within
        FAST_GRAD_MAX_SECS.
        """
        try:
            url = "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&complete=true"
            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                coins: list[dict] = await resp.json()

            found: list[str] = []
            for coin in coins:
                mint = coin.get("mint", "")
                if not mint or len(mint) < 32:
                    continue
                if mint in processed:
                    continue
                pool_address = coin.get("pool_address", "")
                if not pool_address:
                    continue  # not yet graduated to PumpSwap
                created_ms = coin.get("created_timestamp") or 0
                age_secs = (time.time() - created_ms / 1000.0) if created_ms else 9999
                if age_secs > FAST_GRAD_MAX_SECS:
                    continue  # too old
                found.append(mint)
                if mint not in _mint_creation_times:
                    _mint_creation_times[mint] = created_ms / 1000.0 if created_ms else time.time()
                # Store pool address for Helius pre-warming — available at detection time
                if pool_address and mint not in _mint_pool_addresses:
                    _mint_pool_addresses[mint] = pool_address
                # Store pump.fun metadata for social signal detection
                if mint not in _mint_pf_metadata:
                    _pf_desc = str(coin.get("description") or "")
                    _pf_meta = {
                        "name":        coin.get("name", ""),
                        "symbol":      coin.get("symbol", ""),
                        "description": _pf_desc,
                        "twitter":     coin.get("twitter") or "",
                        "telegram":    coin.get("telegram") or "",
                        "website":     coin.get("website") or "",
                        "has_discord": "discord.gg/" in _pf_desc.lower(),
                        "has_telegram_link": bool(coin.get("telegram")),
                        "grad_secs":   age_secs,
                    }
                    _mint_pf_metadata[mint] = _pf_meta
                    # Log social signals at detection time
                    _social_tags = []
                    if _pf_meta["has_discord"]:    _social_tags.append("🎮Discord")
                    if _pf_meta["has_telegram_link"]: _social_tags.append("📱Telegram")
                    if _pf_meta["twitter"]:        _social_tags.append("🐦Twitter")
                    _social_str = " ".join(_social_tags) or "no-socials"
                    print(f"[grad-snipe] pump.fun API: fresh grad {mint[:8]}... age={age_secs:.0f}s pool={pool_address[:8]}... {_social_str}")
            return found
        except Exception as exc:
            print(f"[grad-snipe] pump.fun API poll failed: {exc}", file=sys.stderr)
            return []

    async with _aiohttp.ClientSession() as session:

        async def _process_one_grad(mint: str) -> None:
            """Process a single graduation concurrently — runs as an asyncio Task."""
            try:
                # Fast-graduation filter
                creation_time = _mint_creation_times.get(mint, 0.0)
                if creation_time > 0:
                    grad_age_secs = time.time() - creation_time
                    from elizaos.plugins.solana import live_config as _lc_b_age
                    _b_min_age = float(_lc_b_age.get("strategy_b_min_age_secs", 15))
                    if grad_age_secs < _b_min_age:
                        # Quality-gate bypass (Brain 1 + Brain 2 derived from COWARDS +16,737%):
                        # If liquidity is already strong and price is stable, the pool HAS settled —
                        # don't wait the full age floor. Entry window matters more than time.
                        _bypass_age = False
                        try:
                            _qs_pair = await _find_pumpswap_pair(session, mint)
                            if _qs_pair:
                                _qs_liq  = float(_qs_pair.get("liquidity_usd") or 0)
                                _qs_m5   = float(_qs_pair.get("price_change_m5") or 0)
                                # $50K liq + 5m price within ±15% = pool is live and stable
                                if _qs_liq >= 50_000 and abs(_qs_m5) <= 15.0:
                                    _bypass_age = True
                                    print(
                                        f"[grad-snipe] ✅ QUALITY BYPASS: {mint[:8]}... age={grad_age_secs:.0f}s "
                                        f"liq=${_qs_liq:,.0f} m5={_qs_m5:+.1f}% — strong pool, bypassing age floor"
                                    )
                        except Exception:
                            pass
                        if not _bypass_age:
                            print(
                                f"[grad-snipe] x {mint[:8]}... age={grad_age_secs:.0f}s < {_b_min_age:.0f}s — "
                                f"pool not yet settled — skip"
                            )
                            return
                    # macro_event_mode raises max age to 3600s — during news events tokens
                    # take longer to build bonding curve momentum (more competing launches)
                    from elizaos.plugins.solana import live_config as _lc_fgs
                    _fgs_max = 3600.0 if bool(_lc_fgs.get("macro_event_mode", False)) else FAST_GRAD_MAX_SECS
                    if grad_age_secs > _fgs_max:
                        print(
                            f"[grad-snipe] x {mint[:8]}... skipped — graduated too slowly "
                            f"({grad_age_secs:.0f}s > {_fgs_max:.0f}s threshold)"
                        )
                        return
                    # Ultra-fast graduation: < 360s = coordinated capital (fine999.9=178s +11312%, PUPPY=337s +1268%)
                    # Moon mode = trailing stop exit instead of fixed +30% TP
                    _is_ultra_fast_grad = grad_age_secs < 360
                    if _is_ultra_fast_grad:
                        print(f"[grad-snipe] ⚡ ULTRA-FAST GRAD: {mint[:8]}... age={grad_age_secs:.0f}s — MOON MODE ON (trailing stop, no fixed TP)")
                    else:
                        print(f"[grad-snipe] Fast grad confirmed: {mint[:8]}... age={grad_age_secs:.0f}s ✓")
                else:
                    _is_ultra_fast_grad = False
                    print(f"[grad-snipe] pump.fun API grad: {mint[:8]}... (proceeding)")

                # ── Telegram alpha channel cross-reference ────────────────────
                # If this mint was called in a monitored alpha channel, log it as
                # a positive signal. Does NOT block or gate — just intelligence.
                _tg_alpha_sig: dict | None = None
                try:
                    from elizaos.plugins.solana.telegram_alpha_monitor import get_alpha_signal as _tg_get
                    _tg_alpha_sig = _tg_get(mint)
                    if _tg_alpha_sig:
                        _tg_age_mins = int((time.time() - _tg_alpha_sig.get("ts", 0)) / 60)
                        print(
                            f"[grad-snipe] 🔔 TG ALPHA CONFIRMED: {mint[:8]}... called in "
                            f"@{_tg_alpha_sig.get('channel','?')} {_tg_age_mins}m ago — upstream signal active"
                        )
                except Exception:
                    pass

                # ── Trading hours gate ────────────────────────────────────────────
                # Window is live-config controlled — Jarvis can adjust at runtime.
                # Default: 21:00–03:00 UTC (data shows 50-86% WR in this window).
                _gs_utc_hour  = datetime.utcnow().hour
                _gs_win_start = _lc_b.get("trading_window_start_utc", 21)
                _gs_win_end   = _lc_b.get("trading_window_end_utc", 3)
                # start == end (e.g. 0==0) means no restriction — always open
                if _gs_win_start == _gs_win_end:
                    _in_window = True
                elif _gs_win_start > _gs_win_end:
                    # wraps midnight
                    _in_window = (_gs_utc_hour >= _gs_win_start or _gs_utc_hour < _gs_win_end)
                else:
                    _in_window = (_gs_win_start <= _gs_utc_hour < _gs_win_end)
                if not _in_window:
                    print(
                        f"[grad-snipe] x {mint[:8]}... outside trading window "
                        f"({_gs_win_start:02d}:00–{_gs_win_end:02d}:00 UTC) — skip"
                    )
                    return

                # Also check strategy B enabled flag
                if not _lc_b.get("strategy_b_enabled", True):
                    print(f"[grad-snipe] x {mint[:8]}... Strategy B disabled — skip")
                    return

                pos_mgr = runtime.get_service("position_manager")
                wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
                if None in (pos_mgr, wallet_svc):
                    return

                # Check trade gate
                try:
                    bal = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
                    _sm_gs = runtime.get_service("social_monitor")
                    _is_prem_gs, _prem_buy_gs = _check_grok_premium(mint, _sm_gs, bal)
                    _split_enabled_gs = bool(_lc_b.get("split_buy_enabled", False))
                    if _is_prem_gs:
                        actual_buy = _prem_buy_gs
                    elif _split_enabled_gs:
                        actual_buy = float(_lc_b.get("split_buy_a_sol", 0.05))
                    else:
                        actual_buy = _dynamic_buy_sol(BUY_SOL, bal)
                    if _is_prem_gs:
                        print(f"[grok-premium] {mint[:8]}... score≥8 → {actual_buy} SOL position, TP+50%, stall exit @+25%")
                    elif _split_enabled_gs:
                        print(f"[grad-snipe] split-buy: A={actual_buy:.4f} SOL (HARVESTER), B={_lc_b.get('split_buy_b_sol', 0.025):.4f} SOL (MONSTER HUNTER)")
                    allowed, reason = pos_mgr.check_trade_allowed(actual_buy, bal)  # type: ignore[union-attr]
                    if not allowed:
                        print(f"[grad-snipe] x {mint[:8]}... gated: {reason}")
                        return
                    _sl_gap = time.time() - _last_sl_exit_time
                    if _sl_gap < _get_post_sl_cooldown():
                        print(f"[grad-snipe] x {mint[:8]}... post-SL cooldown — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                        return
                except Exception:
                    return

                if mint in pos_mgr.positions or mint in _buying_mints:  # type: ignore[union-attr]
                    return

                if mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
                    _gs_pair_peek = await _find_pumpswap_pair(session, mint)
                    _gs_m5_peek = _gs_pair_peek.get("price_change_m5", None) if _gs_pair_peek else None
                    if _allow_reentry(mint, _gs_m5_peek):
                        pos_mgr._session_traded_mints.discard(mint)  # type: ignore[union-attr]
                        _all_exit_times.pop(mint, None)
                        print(f"[re-entry] {mint[:8]}... grad win-exit + m5={float(_gs_m5_peek):+.0f}% — re-entry ALLOWED")
                    else:
                        print(f"[grad-snipe] x {mint[:8]}... already traded this session — skip")
                        return

                # Quick pre-check: skip dip-wait if already ripping hard.
                # Only use m5 when DexScreener has real pool reserves (liq > 0).
                _early_pair = await _find_pumpswap_pair(session, mint)
                _early_liq = _early_pair.get("liquidity_usd", 0.0) if _early_pair else 0.0
                _early_m5 = _early_pair.get("price_change_m5", 0.0) if (_early_pair and _early_liq > 0) else 0.0
                _m5_val = float(_early_m5) if (_early_pair and _early_liq > 0) else 0.0
                # Data: tokens already pumping at graduation = 0W/6L (avg -27%).
                # All winners came from flat/dipping graduations where we waited.
                # Skip anything already up > 10% — raised to 20% during macro_event_mode
                # (research 2026-04-04: political memes during news events can sustain higher m5 at graduation)
                from elizaos.plugins.solana import live_config as _lc_macro
                _macro_mode = bool(_lc_macro.get("macro_event_mode", False))
                _macro_label = str(_lc_macro.get("macro_event_label", "") or "")
                _m5_grad_max = 20.0 if _macro_mode else 10.0
                if _m5_val > _m5_grad_max:
                    _macro_note = f" [macro_event_mode={_macro_label}]" if _macro_mode else ""
                    print(f"[grad-snipe] x {mint[:8]}... m5={_m5_val:+.0f}% already pumped at graduation — missed dip, skip{_macro_note}")
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_gs1
                        _rt_gs1.record(mint, "grad_m5_overbought", filter_name="grad_m5_max",
                                       filter_value=round(_m5_val, 1), threshold=10.0, strategy="grad",
                                       extra={"liq_usd": _early_liq})
                    except Exception:
                        pass
                    return
                elif _m5_val < -5:
                    # Data: entering a declining token (m5 < -5%) = buying into a dump.
                    # ALL SL exits had m5 ≤ 0 at entry — winners had m5 > 0.
                    # Skip anything already declining at graduation to avoid buying falling knives.
                    print(f"[grad-snipe] x {mint[:8]}... m5={_m5_val:+.0f}% already declining at graduation — skip (buying a dump)")
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_gs2
                        _rt_gs2.record(mint, "grad_m5_declining", filter_name="grad_m5_min",
                                       filter_value=round(_m5_val, 1), threshold=-5.0, strategy="grad",
                                       extra={"liq_usd": _early_liq})
                    except Exception:
                        pass
                    return
                elif _m5_val > 0:
                    print(f"[grad-snipe] {mint[:8]}... m5={_m5_val:+.0f}% rising — 15s wait for dip")
                    await asyncio.sleep(15)
                else:
                    # m5 between -5% and 0%: neutral/slight dip, could be the graduation dip
                    print(f"[grad-snipe] {mint[:8]}... m5={_m5_val:+.0f}% slight dip — 20s wait for bottom")
                    await asyncio.sleep(20)

                # ── Helius pre-warm: resolve vault addresses during dip-wait ─────
                # pump.fun API gives us pool_address at detection time. Pre-warm the
                # Helius vault cache NOW so first monitor cycle uses real-time price
                # instead of DexScreener (eliminates rug blind spot at entry).
                _known_pool = _mint_pool_addresses.get(mint, "")
                if _known_pool:
                    try:
                        _raydium_prewarm = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                        if _raydium_prewarm is not None:
                            await _raydium_prewarm.get_pumpswap_price_helius(_known_pool)  # type: ignore[union-attr]
                            print(f"[grad-snipe] Helius pre-warm OK for {mint[:8]}... pool={_known_pool[:8]}...")
                    except Exception as _pw_exc:
                        print(f"[grad-snipe] Helius pre-warm failed (non-fatal): {_pw_exc}")
                _bought_immediately = False  # always dip-wait now

                # Re-read from live_config each time — never use the stale outer variable
                from elizaos.plugins.solana import live_config as _lc_b_liq
                _b_min_liq = float(_lc_b_liq.get("strategy_b_min_liq_usd", 8000) or 8000)

                # Find PumpSwap pair — retry up to 5× (150s total) for DexScreener indexing lag.
                pair = None
                for _attempt in range(5):
                    pair = await _find_pumpswap_pair(session, mint)
                    if pair and pair["liquidity_usd"] >= _b_min_liq:
                        break
                    if pair and pair["liquidity_usd"] == 0:
                        print(f"[grad-snipe] {mint[:8]}... pool found $0 liq (DexScreener lag), retry {_attempt+1}/5 in 30s")
                        if _attempt < 4:
                            await asyncio.sleep(30)
                    elif not pair:
                        print(f"[grad-snipe] {mint[:8]}... pool not indexed yet, retrying in 30s ({_attempt+1}/5)")
                        if _attempt < 4:
                            await asyncio.sleep(30)
                    else:
                        break  # has liq but below minimum — not worth retrying
                if not pair:
                    print(f"[grad-snipe] x {mint[:8]}... no PumpSwap pair found after 5 attempts")
                    return

                if pair["liquidity_usd"] < _b_min_liq:
                    print(
                        f"[grad-snipe] x {mint[:8]}... liquidity ${pair['liquidity_usd']:,.0f} "
                        f"< ${_b_min_liq:,.0f} minimum"
                    )
                    return

                # ── Vol/Liq ratio filter (wash-trade detection + momentum check) ─
                from elizaos.plugins.solana import live_config as _lc_gs_vl
                _gs_vol_liq = pair.get("vol_liq_ratio", 0.0)
                _gs_h1_vol  = pair.get("vol_h1_usd", 0.0)
                _gs_liq     = pair.get("liquidity_usd", 0.0)
                _b_max_vl   = float(_lc_gs_vl.get("b_max_vol_liq_ratio", 0) or 0)
                _b_min_vl   = float(_lc_gs_vl.get("b_min_vol_liq_ratio", 0) or 0)
                if _b_max_vl > 0 and _gs_vol_liq > _b_max_vl:
                    print(
                        f"[grad-snipe] x {mint[:8]}... vol/liq={_gs_vol_liq:.0f}x > {_b_max_vl:.0f}x max "
                        f"(h1_vol=${_gs_h1_vol:,.0f} / liq=${_gs_liq:,.0f}) — WASH TRADING, skip"
                    )
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_gsvl
                        _rt_gsvl.record(mint, "wash_trade_vol_liq", filter_name="b_max_vol_liq_ratio",
                                        filter_value=round(_gs_vol_liq, 1), threshold=_b_max_vl, strategy="grad",
                                        extra={"liq_usd": _gs_liq, "vol_h1": _gs_h1_vol})
                    except Exception:
                        pass
                    return
                # NOTE: vol/liq MIN check is intentionally skipped for graduation snipe.
                # A newly graduated PumpSwap pool was created seconds/minutes ago — it has
                # virtually zero volume history. vol/liq = 0.4x on an 11-min-old pool does
                # NOT mean "no momentum" — it means the pool just opened. The graduation
                # event itself (fast fill % on pump.fun BC) IS the momentum signal.
                # The MAX check above still protects against wash trading on older pools.
                # Evidence: Pluto the Penguin (+297% h24) was blocked here at 0.4x vol/liq.

                # ── Buy ratio filter ──────────────────────────────────────────────
                _gs_buy_ratio = pair.get("buy_ratio_h1", 0.0)
                _gs_buys      = pair.get("txns_h1_buys", 0)
                _gs_sells     = pair.get("txns_h1_sells", 0)
                _b_min_br     = float(_lc_gs_vl.get("b_min_buy_ratio", 0) or 0)
                if _b_min_br > 0 and (_gs_buys + _gs_sells) >= 10 and _gs_buy_ratio < _b_min_br:
                    print(
                        f"[grad-snipe] x {mint[:8]}... buy_ratio={_gs_buy_ratio:.0f}% < {_b_min_br:.0f}% min "
                        f"(buys={_gs_buys} sells={_gs_sells}) — selling pressure, skip"
                    )
                    try:
                        from elizaos.plugins.solana import rejection_tracker as _rt_gsbr
                        _rt_gsbr.record(mint, "buy_ratio_low", filter_name="b_min_buy_ratio",
                                        filter_value=round(_gs_buy_ratio, 1), threshold=_b_min_br, strategy="grad",
                                        extra={"buys": _gs_buys, "sells": _gs_sells, "liq_usd": _gs_liq})
                    except Exception:
                        pass
                    return

                # ── Momentum score gate ───────────────────────────────────────────
                _b_mom_min = float(_lc_gs_vl.get("b_min_momentum_score", 0.0) or 0.0)
                if _b_mom_min != 0.0:
                    _b_m5 = float(pair.get("price_change_m5") or 0)
                    _b_h1 = float(pair.get("price_change_h1") or 0)
                    _b_h6 = float(pair.get("price_change_h6") or 0)
                    _b_mom = round(_b_m5 * 0.5 + _b_h1 * 0.3 + _b_h6 * 0.2, 1)
                    if _b_mom < _b_mom_min:
                        print(
                            f"[grad-snipe] x {mint[:8]}... momentum_score={_b_mom:.1f} < {_b_mom_min:.1f} min "
                            f"(m5={_b_m5:+.1f}% h1={_b_h1:+.1f}% h6={_b_h6:+.1f}%) — skip"
                        )
                        return

                price_sol = pair.get("price_sol", 0.0)
                if price_sol <= 0:
                    print(f"[grad-snipe] x {mint[:8]}... no price data")
                    return

                # ── Graduation confirmation timer ────────────────────────────────
                # If enabled: park token here and let _graduation_confirmation_loop
                # re-check after graduation_confirmation_wait seconds.
                from elizaos.plugins.solana import live_config as _lc_gct
                _conf_wait = float(_lc_gct.get("graduation_confirmation_wait", 0.0) or 0.0)
                if _conf_wait > 0 and mint not in _grad_confirmation_pending:
                    _grad_confirmation_pending[mint] = {
                        "price_at_snap":    price_sol,
                        "liq_at_snap":      pair.get("liquidity_usd", 0.0),
                        "vol_h1_at_snap":   pair.get("volume_h1_usd", 0.0),
                        "buy_ratio_at_snap": pair.get("buy_ratio_h1", 0.0),
                        "detected_ts":      time.time(),
                        "conf_wait":        _conf_wait,
                        "pair":             pair,
                        "buy_sol":          float(_lc_gct.get("buy_sol", 0.05)),
                        "pool_addr":        _mint_pool_addresses.get(mint, "") or pair.get("pairAddress", ""),
                        "is_ultra_fast":    _is_ultra_fast_grad,
                    }
                    print(
                        f"[grad-confirm] {mint[:8]}... PARKED for {_conf_wait:.0f}s confirmation "
                        f"(price={price_sol:.2e} liq=${pair.get('liquidity_usd',0):,.0f}) "
                        f"— will re-check at {time.strftime('%H:%M:%S', time.gmtime(time.time()+_conf_wait))} UTC"
                    )
                    return   # hand off to _graduation_confirmation_loop

                m5_chg = pair.get("price_change_m5", 0.0)
                # Crash guard: skip tokens still dumping hard after dip wait.
                if m5_chg < -20:
                    print(f"[grad-snipe] x {mint[:8]}... continued dump m5={m5_chg:+.0f}% -- skip (not stabilising)")
                    return
                # Pump guard: skip if m5 > 10% at buy time — tightened from 15% (2026-03-16).
                # All winners had m5 ≈ 0% at entry.
                if m5_chg > 10.0:
                    print(f"[grad-snipe] x {mint[:8]}... already pumped m5={m5_chg:+.0f}% -- skip (exit liquidity risk)")
                    return

                # ── Safety check FIRST (rugcheck + LP lock) — must pass before AI gate ──
                wallet_svc_g = runtime.get_service(ServiceTypeRegistry.WALLET)
                safe, safety_reason = await pos_mgr.check_token_safety(mint)  # type: ignore[union-attr]
                if not safe:
                    print(f"[grad-snipe] x {mint[:8]}... UNSAFE: {safety_reason}")
                    return

                # ── Mandatory AI gate: Grok X rug-check + GPT-4o risk score ────────
                _sm_g = runtime.get_service("social_monitor")
                if _sm_g is not None:
                    _gate_data = {
                        "dex": "pumpswap",
                        "liq_usd": pair.get("liquidity_usd", 0),
                        "age_mins": pair.get("age_mins", 0),
                        "price_change_m5": m5_chg,
                        "price_change_h1": pair.get("price_change_h1", 0),
                        "vol_h1_usd": pair.get("vol_h1_usd", 0),
                        # Graduated tokens: score=8 baseline (passed liquidity/safety checks),
                        # is_fresh_grad=True so GPT skips no-volume/no-socials penalties —
                        # brand new listings legitimately have no H1 volume or indexed socials.
                        "score": 8,
                        "has_socials": True,
                        "is_fresh_grad": True,
                    }
                    _gate_allow, _gate_reason = await _sm_g.pre_trade_ai_gate(  # type: ignore[union-attr]
                        mint, pair.get("name", ""), pair.get("symbol", ""), _gate_data
                    )
                    if not _gate_allow:
                        print(f"[grad-snipe] x {mint[:8]}... AI gate BLOCKED — {_gate_reason}")
                        return

                # ── Web reputation check — live internet scam/rug search ────
                _g_web_safe, _g_web_reason = await _web_reputation_check(
                    pair.get("name", ""), pair.get("symbol", ""), mint
                )
                if not _g_web_safe:
                    print(f"[grad-snipe] x {mint[:8]}... WEB-BLOCKED — {_g_web_reason}")
                    return
                elif "skipped" not in _g_web_reason.lower():
                    print(f"[grad-snipe] web-check OK: {mint[:8]}... — {_g_web_reason[:80]}")

                # ── HARD BUY GATE — final check before any transaction ────────
                _gate_ok_gs, _ = _hard_buy_gate(
                    "[grad-snipe]", mint, "b",
                    liq_usd=float(pair.get("liquidity_usd") or 0),
                    mc_usd=float(pair.get("market_cap_usd") or 0),
                    vol_h1_usd=float(pair.get("volume_h1_usd") or pair.get("vol_h1_usd") or 0),
                    buy_txns=int(pair.get("txns_h1_buys") or 0),
                    sell_txns=int(pair.get("txns_h1_sells") or 0),
                    safety_passed=safe,
                    safety_reason=safety_reason,
                )
                if not _gate_ok_gs:
                    return
                # ────────────────────────────────────────────────────────────────

                # Single full entry — 0.25 SOL in one transaction (2026-03-16)
                token_amount = int((actual_buy / price_sol) * (10 ** TOKEN_DECIMALS))
                print(
                    f"[grad-snipe] BUYING graduation dip: {mint[:8]}... "
                    f"({actual_buy:.4f} SOL @ {price_sol:.8f} SOL/token, "
                    f"liq=${pair['liquidity_usd']:,.0f}, age={pair['age_mins']}min, m5={m5_chg:+.1f}%)"
                )

                _buying_mints.add(mint)
                if PAPER_TRADING:
                    sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
                else:
                    raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                    try:
                        _g_last_exc: Exception | None = None
                        for _g_slip_bps in (2000, 3000, 5000):  # 20%→30%→50% (was 15%→30%→50%)
                            try:
                                sig = await raydium_svc.swap_jupiter_buy(  # type: ignore[union-attr]
                                    mint, actual_buy, slippage_bps=_g_slip_bps, priority_fee=0.002
                                )
                                _g_last_exc = None
                                break
                            except Exception as _g_jup_exc:
                                _g_last_exc = _g_jup_exc
                        if _g_last_exc is not None:
                            raise _g_last_exc
                    except Exception as exc:
                        # Jupiter failed (Custom 6014 = newly-graduated pool in transitional state).
                        # Fallback: PumpPortal native pump-amm — bypasses Jupiter routing entirely.
                        print(f"[grad-snipe] Jupiter failed ({exc}), trying PumpPortal pump-amm fallback...", file=sys.stderr)
                        _pump_svc_g = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
                        if _pump_svc_g is None:
                            print(f"[grad-snipe] Buy failed for {mint[:8]}...: no pump service available", file=sys.stderr)
                            _buying_mints.discard(mint)
                            return
                        try:
                            sig = await _pump_svc_g.buy(mint, actual_buy, slippage=0.15, pool="pump-amm")  # type: ignore[union-attr]
                            print(f"[grad-snipe] PumpPortal pump-amm fallback OK for {mint[:8]}...")
                        except Exception as _pp_exc:
                            print(f"[grad-snipe] Buy failed for {mint[:8]}...: {_pp_exc}", file=sys.stderr)
                            _buying_mints.discard(mint)
                            return

                if not PAPER_TRADING and wallet_svc_g is not None:
                    try:
                        await asyncio.sleep(3)
                        actual_bals_g = await wallet_svc_g.get_token_balances()  # type: ignore[union-attr]
                        actual_raw_g = next((int(t["raw_amount"]) for t in actual_bals_g if t["mint"] == mint), 0)
                        if actual_raw_g > 0:
                            actual_dec_g = next((t.get("decimals", TOKEN_DECIMALS) for t in actual_bals_g if t["mint"] == mint), TOKEN_DECIMALS)
                            actual_price_g = actual_buy / (actual_raw_g / 10 ** actual_dec_g)
                            print(f"[grad-snipe] Actual fill: {actual_price_g:.8f} SOL/token (quoted {price_sol:.8f}, diff {(actual_price_g/price_sol-1)*100:+.1f}%)")
                            price_sol = actual_price_g
                            token_amount = actual_raw_g
                    except Exception as _fe_g:
                        print(f"[grad-snipe] Warning: could not read actual fill ({_fe_g}), using quoted price")

                # Pool address: prefer pump.fun API value (available at detection), fallback to DexScreener pairAddress
                _final_pool_addr = _mint_pool_addresses.get(mint, "") or pair.get("pairAddress", "")

                # Enrich meta with pump.fun social signals (stored at detection time)
                _pf_meta_gs = _mint_pf_metadata.get(mint, {})
                _gs_grad_secs = _pf_meta_gs.get("grad_secs") or (
                    (time.time() - _mint_creation_times[creation_time]) if creation_time else None
                ) if False else _pf_meta_gs.get("grad_secs", None)
                _has_discord_gs   = _pf_meta_gs.get("has_discord", False)
                _has_telegram_gs  = _pf_meta_gs.get("has_telegram_link", False)
                _has_twitter_gs   = bool(_pf_meta_gs.get("twitter"))
                if _has_discord_gs:
                    print(f"[grad-snipe] 🎮 Discord community in description: {mint[:8]}... (coordinated launch signal)")
                if _has_telegram_gs:
                    print(f"[grad-snipe] 📱 Telegram channel linked: {mint[:8]}... {_pf_meta_gs.get('telegram','')[:40]}")
                _gs_meta = {
                    "name": pair.get("name", ""),
                    "symbol": pair.get("symbol", ""),
                    "liq_usd": pair.get("liquidity_usd", 0.0),
                    "vol_h24_usd": pair.get("volume_h24_usd", 0.0),
                    "vol_h1_usd": pair.get("volume_h1_usd", 0.0),
                    "price_change_h1": pair.get("price_change_h1", None),
                    "price_change_m5": m5_chg,
                    "market_cap_usd": pair.get("market_cap_usd", 0.0),
                    "has_socials": pair.get("_has_social", False),
                    "social_urls": pair.get("social_urls", []),
                    "grad_age_mins": pair.get("age_mins", 0),
                    "grad_dip_wait_secs": DIP_WAIT_SECS,
                    "strategy": "grad_snipe",
                    "momentum_score": round(
                        float(pair.get("price_change_m5") or 0) * 0.5
                        + float(pair.get("price_change_h1") or 0) * 0.3
                        + float(pair.get("price_change_h6") or 0) * 0.2,
                        1,
                    ),
                    "pool_address": _final_pool_addr,
                    # Ultra-fast grad (< 6 min): trailing stop instead of fixed +30% TP
                    "moon_mode": _is_ultra_fast_grad,
                    # pump.fun social metadata — for pattern analysis
                    "pf_grad_secs":        _pf_meta_gs.get("grad_secs"),
                    "pf_has_discord":      _has_discord_gs,
                    "pf_has_telegram":     _has_telegram_gs,
                    "pf_has_twitter":      _has_twitter_gs,
                    "pf_telegram_url":     _pf_meta_gs.get("telegram", ""),
                    "pf_twitter_url":      _pf_meta_gs.get("twitter", ""),
                    "pf_description":      _pf_meta_gs.get("description", "")[:200],
                    # Telegram alpha channel signal (from monitored channels)
                    "tg_alpha_channel":    _tg_alpha_sig.get("channel") if _tg_alpha_sig else None,
                    "tg_alpha_ts":         _tg_alpha_sig.get("ts") if _tg_alpha_sig else None,
                    "tg_alpha_snippet":    _tg_alpha_sig.get("snippet", "")[:100] if _tg_alpha_sig else None,
                }
                _gs_label = "A" if _split_enabled_gs and not _is_prem_gs else ""
                _gs_grok_confirmed = (lambda _s: _s.is_grok_confirmed(mint) if _s else False)(_sm_gs)
                pos_mgr.open_position(  # type: ignore[union-attr]
                    mint=mint, dex="pumpswap", entry_price_sol=price_sol,
                    entry_sol_spent=actual_buy, token_amount=token_amount,
                    token_decimals=TOKEN_DECIMALS, signature=sig,
                    grok_confirmed=_gs_grok_confirmed,
                    grok_premium=_is_prem_gs,
                    meta=_gs_meta,
                    label=_gs_label,
                )
                _gs_label_tag = " [HARVESTER]" if _gs_label == "A" else ""
                print(f"[grad-snipe] Position opened{_gs_label_tag}: {mint[:8]}... dex=pumpswap entry={price_sol:.8f} SOL sig={sig[:20]}")
                # ── Split-buy: open MONSTER HUNTER position B ────────────────
                if _gs_label == "A":
                    _gs_raydium = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                    await _execute_split_buy_b(
                        runtime, pos_mgr, _gs_raydium, wallet_svc_g,
                        mint=mint, dex="pumpswap", entry_price_sol=price_sol,
                        meta=_gs_meta, token_decimals=TOKEN_DECIMALS, score=0,
                        grok_confirmed=_gs_grok_confirmed, grok_premium=False,
                        tag="[grad-snipe]",
                    )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[grad-snipe] Unexpected error for {mint[:8]}: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            finally:
                _buying_mints.discard(mint)
                try:
                    graduation_queue.task_done()
                except Exception:
                    pass

        # Dispatcher loop — picks mints from queue and spawns concurrent tasks.
        # Each token is processed in its own asyncio.Task so DexScreener retries
        # on one token do NOT block processing of the next graduation.
        while True:
            try:
                # Re-read live config every cycle — Jarvis can change these at runtime
                PAPER_TRADING     = _lc_b.get("paper_trading", False)
                _b_override       = _lc_b.get("strategy_b_buy_sol", 0.0)
                BUY_SOL           = _b_override if _b_override > 0 else _lc_b.get("buy_sol", 0.05)
                MIN_LIQUIDITY_USD = _lc_b.get("strategy_b_min_liq_usd", 13500)
                DIP_WAIT_SECS     = _lc_b.get("strategy_b_dip_wait_secs", 20)
                try:
                    mint = await asyncio.wait_for(graduation_queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    if time.time() - _last_api_poll >= API_POLL_INTERVAL:
                        _last_api_poll = time.time()
                        polled = await _poll_pumpfun_graduations(session)
                        for m in polled:
                            if m not in processed:
                                await graduation_queue.put(m)
                    continue

                if mint in processed:
                    graduation_queue.task_done()
                    continue

                processed.add(mint)
                asyncio.create_task(_process_one_grad(mint))

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[grad-snipe] Dispatcher error: {exc}", file=sys.stderr)


async def status_log_loop(runtime: AgentRuntime) -> None:
    """Print a compact status report every 30 minutes so you can monitor overnight."""
    await asyncio.sleep(60)  # give services time to start
    while True:
        try:
            pos_mgr = runtime.get_service("position_manager")
            wallet_svc = runtime.get_service("wallet")
            dev_rep = get_reputation()

            bal_str = ""
            if wallet_svc is not None:
                try:
                    bal = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
                    bal_str = f"  Wallet: {bal:.4f} SOL\n"
                except Exception:
                    pass

            if pos_mgr is not None:
                risk = pos_mgr.get_risk_summary()  # type: ignore[union-attr]
                history = pos_mgr.get_trade_history(limit=200)  # type: ignore[union-attr]
                sells = [t for t in history if t.get("side") == "sell"]
                wins = sum(1 for t in sells if (t.get("pnl_pct") or 0) > 0)
                losses = len(sells) - wins
                win_rate = (wins / len(sells) * 100) if sells else 0
                # Cap per-trade pnl at 0.1 SOL to exclude any corrupted records
                total_pnl = sum(max(-0.1, min(0.1, t.get("pnl_sol") or 0)) for t in sells)
                avg_hold = (sum(t.get("hold_secs") or 0 for t in sells) / len(sells)) if sells else 0
                rep = dev_rep.get_stats()

                print(
                    f"\n{'='*60}\n"
                    f"[STATUS] {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
                    f"{bal_str}"
                    f"  Open positions: {risk['open_position_count']}/6\n"
                    f"  Daily P&L: {risk['daily_pnl_sol']:+.4f} SOL\n"
                    f"  All-time trades: {len(sells)} | Win rate: {win_rate:.0f}% ({wins}W/{losses}L)\n"
                    f"  Total realized P&L: {total_pnl:+.4f} SOL\n"
                    f"  Avg hold time: {avg_hold:.0f}s\n"
                    f"  Consecutive losses: {risk['consecutive_losses']}/{risk.get('max_consecutive_losses',10)}\n"
                    f"  Circuit breaker: {'TRIPPED ⛔' if risk['circuit_broken'] else 'OK ✅'}\n"
                    f"  Dev reputation: {rep['blacklisted']} blacklisted | {rep['whitelisted']} whitelisted | "
                    f"{rep['total_rugs_recorded']} rugs recorded\n"
                    f"{'='*60}\n"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[status] Error: {exc}", file=sys.stderr)

        await asyncio.sleep(1800)  # every 30 minutes


async def analysis_loop(runtime: AgentRuntime) -> None:
    """Every 30 minutes Jarvis reviews all trade data and logs pattern insights.

    She acts as an analyst — not making live trading decisions, but identifying
    what's working, what isn't, and what the data suggests about optimal settings.
    Her insights are logged to the activity feed and printed to stdout.
    """
    ANALYSIS_INTERVAL = 1800   # 30 minutes (10 min in paper mode)
    MIN_TRADES_FOR_ANALYSIS = 2   # minimum to START running analysis (show data)
    MIN_TRADES_FOR_ADJUST   = 15  # minimum to ISSUE ADJUST commands — need statistical signal

    ANALYSIS_USER_ID = "00000000-0000-0000-0000-000000000095"
    ANALYSIS_ROOM_ID = "00000000-0000-0000-0000-000000000094"

    # In paper mode: run every 10 min to iterate faster on filter tuning
    from elizaos.plugins.solana import live_config as _lc_al_init
    _paper_mode = bool(_lc_al_init.get("paper_trading", False))
    if _paper_mode:
        ANALYSIS_INTERVAL = 600  # 10 min in paper mode — fast iteration
        await asyncio.sleep(300)  # first run after 5 min in paper mode
        print("[analysis] Jarvis analysis loop started (PAPER MODE — first run in 5 min, then every 10 min)")
    else:
        await asyncio.sleep(900)  # first analysis after 15 min
        print("[analysis] Jarvis analysis loop started (first run in 15 min, then every 30 min)")

    while True:
        try:
            pos_mgr = runtime.get_service("position_manager")
            dev_rep = get_reputation()

            if pos_mgr is None:
                await asyncio.sleep(ANALYSIS_INTERVAL)
                continue

            # ── Fetch live market context (Fear & Greed + Trending) ──────────
            _fng_data, _trending_data = await asyncio.gather(
                _fetch_fear_greed(),
                _fetch_trending_coins(),
                return_exceptions=True,
            )
            if isinstance(_fng_data, Exception):
                _fng_data = _fng_cache
            if isinstance(_trending_data, Exception):
                _trending_data = []
            _fng_val   = _fng_data.get("value", 50) if isinstance(_fng_data, dict) else 50
            _fng_class = _fng_data.get("classification", "Neutral") if isinstance(_fng_data, dict) else "Neutral"
            _trending_str = ", ".join(_trending_data[:6]) if _trending_data else "unavailable"

            # ── Load trade history ────────────────────────────────────────────
            # PAPER MODE: only load current session file. Old backup files contain
            # live trades from weeks ago with different settings — loading them causes
            # Jarvis to fabricate justifications based on stale data ("five consecutive
            # pump_fun losses") when the current session may have 0-2 trades.
            # LIVE MODE: load all backup files for full historical context.
            session_history = pos_mgr.get_trade_history(limit=1000)  # type: ignore[union-attr]
            _solana_dir = os.path.join(os.path.dirname(__file__), "elizaos/plugins/solana")
            _all_by_id: dict = {}
            from elizaos.plugins.solana import live_config as _lc_hist
            _paper_mode_analysis = bool(_lc_hist.get("paper_trading", False))
            if _paper_mode_analysis:
                # Paper mode: current session only — fresh paper run tests current settings
                _hist_files = [os.path.join(_solana_dir, "trade_history.json")]
                print("[analysis] PAPER MODE — loading current session only (no backup files)")
            else:
                # Live mode: all backup files for full historical pattern analysis
                _hist_files = sorted([
                    os.path.join(_solana_dir, f)
                    for f in os.listdir(_solana_dir)
                    if f.startswith("trade_history") and f.endswith(".json")
                ])
            _file_history: list[dict] = []
            for _hpath in _hist_files:
                try:
                    with open(_hpath) as _hf:
                        _batch = json.load(_hf)
                    if isinstance(_batch, list):
                        for t in _batch:
                            if t.get("id"):
                                _all_by_id[t["id"]] = t
                        _file_history.extend(_batch)
                except Exception:
                    pass
            # Session overwrites file data (most up-to-date)
            for t in session_history:
                if t.get("id"):
                    _all_by_id[t["id"]] = t
            history = sorted(_all_by_id.values(), key=lambda t: t.get("timestamp", 0))
            sells = [t for t in history if t.get("side") == "sell"]

            if len(sells) < MIN_TRADES_FOR_ANALYSIS:
                print(f"[analysis] Only {len(sells)} closed trades — skipping analysis (need {MIN_TRADES_FOR_ANALYSIS})")
                await asyncio.sleep(ANALYSIS_INTERVAL)
                continue

            # Compute stats the code knows objectively
            wins = [t for t in sells if (t.get("pnl_pct") or 0) > 0]
            losses = [t for t in sells if (t.get("pnl_pct") or 0) <= 0]
            win_rate = len(wins) / len(sells) * 100

            avg_pnl = sum(t.get("pnl_pct") or 0 for t in sells) / len(sells)
            avg_hold_wins = (sum(t.get("hold_secs") or 0 for t in wins) / len(wins)) if wins else 0
            avg_hold_losses = (sum(t.get("hold_secs") or 0 for t in losses) / len(losses)) if losses else 0

            # Exit reason breakdown
            reasons: dict[str, int] = {}
            for t in sells:
                r = t.get("reason", "unknown")
                reasons[r] = reasons.get(r, 0) + 1
            reason_str = ", ".join(f"{r}={n}" for r, n in sorted(reasons.items(), key=lambda x: -x[1]))

            # DEX breakdown
            dex_groups: dict[str, list] = {}
            for t in sells:
                d = t.get("dex", "unknown")
                dex_groups.setdefault(d, []).append(t)
            dex_lines = []
            for d, dt in sorted(dex_groups.items()):
                d_wins = sum(1 for t in dt if (t.get("pnl_pct") or 0) > 0)
                d_wr = d_wins / len(dt) * 100
                d_avg = sum(t.get("pnl_pct") or 0 for t in dt) / len(dt)
                d_pnl = sum(t.get("pnl_sol") or 0 for t in dt)
                dex_lines.append(f"  {d}: {len(dt)} trades | WR {d_wr:.0f}% | avg {d_avg:+.1f}% | net {d_pnl:+.4f} SOL")
            dex_str = "\n".join(dex_lines) or "  (none)"

            # Winner/loser token profile from meta — compare entry conditions
            def _meta_avg(trade_list: list, key: str) -> float:
                vals = [t.get("meta", {}).get(key) for t in trade_list if t.get("meta", {}).get(key) is not None]
                return sum(float(v) for v in vals) / len(vals) if vals else 0.0

            def _meta_pct(trade_list: list, key: str) -> float:
                vals = [t.get("meta", {}).get(key) for t in trade_list]
                truthy = sum(1 for v in vals if v)
                return truthy / len(vals) * 100 if vals else 0.0

            # Match wins/losses to their buy-side meta via mint
            buy_meta: dict[str, dict] = {}
            for t in history:
                if t.get("side") == "buy" and t.get("meta"):
                    buy_meta[t["mint"]] = t["meta"]

            wins_with_meta = [dict(**t, meta=buy_meta.get(t["mint"], {})) for t in wins]
            losses_with_meta = [dict(**t, meta=buy_meta.get(t["mint"], {})) for t in losses]

            winner_profile = (
                f"  Avg liq: ${_meta_avg(wins_with_meta, 'liq_usd'):,.0f} | "
                f"vol_h1: ${_meta_avg(wins_with_meta, 'vol_h1_usd'):,.0f} | "
                f"buyers_h1: {_meta_avg(wins_with_meta, 'buyers_h1'):.0f} | "
                f"has_socials: {_meta_pct(wins_with_meta, 'has_socials'):.0f}% | "
                f"h1_pct: {_meta_avg(wins_with_meta, 'price_change_h1'):+.1f}% | "
                f"m5_pct: {_meta_avg(wins_with_meta, 'price_change_m5'):+.1f}%"
            ) if wins_with_meta else "  (no wins with meta)"
            loser_profile = (
                f"  Avg liq: ${_meta_avg(losses_with_meta, 'liq_usd'):,.0f} | "
                f"vol_h1: ${_meta_avg(losses_with_meta, 'vol_h1_usd'):,.0f} | "
                f"buyers_h1: {_meta_avg(losses_with_meta, 'buyers_h1'):.0f} | "
                f"has_socials: {_meta_pct(losses_with_meta, 'has_socials'):.0f}% | "
                f"h1_pct: {_meta_avg(losses_with_meta, 'price_change_h1'):+.1f}% | "
                f"m5_pct: {_meta_avg(losses_with_meta, 'price_change_m5'):+.1f}%"
            ) if losses_with_meta else "  (no losses with meta)"

            # Individual loss detail — last 25 losses for pattern analysis
            recent_losses = sorted(losses, key=lambda t: t.get("timestamp", 0), reverse=True)[:25]
            loss_rows = []
            for _lt in recent_losses:
                _m = buy_meta.get(_lt["mint"], {})
                _name = _lt.get("symbol") or _lt["mint"][:8]
                _pnl = _lt.get("pnl_pct") or 0
                _sol = _lt.get("pnl_sol") or 0
                _reason = _lt.get("reason", "?")
                _dex = _lt.get("dex", "?")
                _hold = _lt.get("hold_secs") or 0
                _liq = _m.get("liq_usd") or 0
                _m5 = _m.get("price_change_m5") or 0
                _socials = "socials" if _m.get("has_socials") else "no-socials"
                loss_rows.append(
                    f"  {_name:12s} | {_dex:10s} | {_pnl:+.0f}% ({_sol:+.4f} SOL) | hold {_hold:.0f}s | "
                    f"reason={_reason} | liq=${_liq:,.0f} | m5={_m5:+.1f}% | {_socials}"
                )
            loss_detail_str = "\n".join(loss_rows) or "  (none)"

            rep_stats = dev_rep.get_stats()
            total_pnl = sum(t.get("pnl_sol") or 0 for t in sells)

            # Wallet balance for position sizing context — use live RPC, NOT cached attribute.
            # The cached _sol_balance is often 0.0 at analysis time (not yet refreshed),
            # which was causing Sonnet to issue ADJUST: a2_buy_sol=0.01 thinking we're broke.
            _wallet_svc = runtime.get_service("solana_wallet")
            _wallet_sol = 0.0
            try:
                if _wallet_svc is not None:
                    _wallet_sol = await _effective_wallet_sol(_wallet_svc)
            except Exception:
                pass
            # If wallet still reads ~0 (RPC failure/timing), mark it as unreadable so
            # Sonnet does NOT attempt to "fix" position sizes based on a false 0-balance.
            _wallet_readable = _wallet_sol > 0.005

            from elizaos.plugins.solana import live_config as _analysis_lc
            _cfg = _analysis_lc.all_config()
            _cfg_str = (
                f"  --- EXITS ---\n"
                f"  stop_loss_pct={_cfg.get('stop_loss_pct', 0.09):.3f} ({_cfg.get('stop_loss_pct', 0.09)*100:.1f}%)\n"
                f"  pumpswap_stop_loss_pct={_cfg.get('pumpswap_stop_loss_pct', 0.07):.3f} ({_cfg.get('pumpswap_stop_loss_pct', 0.07)*100:.1f}%)\n"
                f"  early_stop_loss_pct={_cfg.get('early_stop_loss_pct', 0.05):.3f} ({_cfg.get('early_stop_loss_pct', 0.05)*100:.1f}%)\n"
                f"  tp1_mult={_cfg.get('tp1_mult', 1.40):.3f} (+{(_cfg.get('tp1_mult',1.40)-1)*100:.0f}% pumpswap)\n"
                f"  pf_tp1_mult={_cfg.get('pf_tp1_mult', 1.60):.3f} (+{(_cfg.get('pf_tp1_mult',1.60)-1)*100:.0f}% pump.fun BC)\n"
                f"  grok_tp_mult={_cfg.get('grok_tp_mult', 1.80):.3f} (+{(_cfg.get('grok_tp_mult',1.80)-1)*100:.0f}% grok-confirmed)\n"
                f"  --- POSITION SIZES (wallet={_wallet_sol:.3f} SOL) ---\n"
                f"  a2_buy_sol={_cfg.get('a2_buy_sol', 0.15):.3f} SOL (A2 pump.fun BC)\n"
                f"  strategy_b_buy_sol={_cfg.get('strategy_b_buy_sol', 0.0):.3f} SOL (Strategy B grad-snipe, 0=use buy_sol)\n"
                f"  strategy_c_buy_sol={_cfg.get('strategy_c_buy_sol', 0.0):.3f} SOL (Strategy C Raydium, 0=use buy_sol)\n"
                f"  strategy_d_buy_sol={_cfg.get('strategy_d_buy_sol', 0.0):.3f} SOL (Strategy D Meteora, 0=use buy_sol)\n"
                f"  buy_sol={_cfg.get('buy_sol', 0.05):.3f} SOL (global fallback)\n"
                f"  grok_premium_buy_sol={_cfg.get('grok_premium_buy_sol', 0.3):.3f} SOL\n"
                f"  --- STRATEGY ENABLES ---\n"
                f"  strategy_a2_enabled={_cfg.get('strategy_a2_enabled', True)}\n"
                f"  strategy_b_enabled={_cfg.get('strategy_b_enabled', True)}\n"
                f"  strategy_c_enabled={_cfg.get('strategy_c_enabled', False)}\n"
                f"  strategy_d_enabled={_cfg.get('strategy_d_enabled', False)}\n"
                f"  grok_premium_enabled={_cfg.get('grok_premium_enabled', True)}\n"
                f"  smart_wallet_enabled={_cfg.get('smart_wallet_enabled', False)}\n"
                f"  --- FILTERS ---\n"
                f"  strategy_b_min_liq_usd=${_cfg.get('strategy_b_min_liq_usd', 14000):,.0f}\n"
                f"  strategy_b_dip_wait_secs={_cfg.get('strategy_b_dip_wait_secs', 20)}s\n"
                f"  fill_cap_pct={_cfg.get('fill_cap_pct', 68.0):.1f}% (BC fill window cap)\n"
                f"  a2_min_real_sol={_cfg.get('a2_min_real_sol', 1.6):.2f} SOL (BC momentum floor)\n"
                f"  strategy_c_min_score={_cfg.get('strategy_c_min_score', 3)} | strategy_c_min_liq_usd=${_cfg.get('strategy_c_min_liq_usd', 0):,.0f}\n"
                f"  strategy_d_min_score={_cfg.get('strategy_d_min_score', 3)} | strategy_d_min_liq_usd=${_cfg.get('strategy_d_min_liq_usd', 1000):,.0f}\n"
                f"  --- RISK ---\n"
                f"  max_concurrent_positions={_cfg.get('max_concurrent_positions', 1)}\n"
                f"  max_daily_loss_pct={_cfg.get('max_daily_loss_pct', 0.12)*100:.0f}%\n"
                f"  --- GROK PREMIUM ---\n"
                f"  grok_premium_min_score={_cfg.get('grok_premium_min_score', 8)}/10 threshold\n"
                f"  grok_premium_tp_mult={_cfg.get('grok_premium_tp_mult', 1.50):.3f} (+{(_cfg.get('grok_premium_tp_mult',1.50)-1)*100:.0f}%)\n"
                f"  grok_premium_stall_secs={_cfg.get('grok_premium_stall_secs', 60)}s | stall_band={_cfg.get('grok_premium_stall_band', 0.02)*100:.1f}% | stall_min_pct={_cfg.get('grok_premium_stall_min_pct', 0.25)*100:.0f}%\n"
                f"  --- ENTRY FILTERS (Jarvis-adjustable) ---\n"
                f"  rugcheck_max_score={_cfg.get('rugcheck_max_score', 8000)} (lower=stricter; tokens above this score blocked)\n"
                f"  a2_min_holders={_cfg.get('a2_min_holders', 10)} (BC tokens need ≥N holders before entry)\n"
                f"  a2_max_whale_pct={_cfg.get('a2_max_whale_pct', 40.0):.1f}% (reject if top buyer > this % of supply)\n"
                f"  post_sl_cooldown_secs={_cfg.get('post_sl_cooldown_secs', 180.0):.0f}s (all strategies freeze after any SL)"
            )
            # Keys the analysis loop is ALLOWED to tune via ADJUST: commands.
            # Research-locked keys (buy ratios, age floors, h1 cap, keyword score, macro mode)
            # are deliberately EXCLUDED from this list — they are protected in PROTECTED_KEYS
            # and any ADJUST command for them will be silently dropped.
            _adj_keys = (
                "stop_loss_pct, pumpswap_stop_loss_pct, early_stop_loss_pct, "
                "grok_tp_mult, fill_cap_pct, "
                "a2_min_real_sol, max_concurrent_positions, max_daily_loss_pct, "
                "strategy_a2_enabled, strategy_b_enabled, strategy_c_enabled, strategy_d_enabled, "
                "strategy_b_min_liq_usd, strategy_b_dip_wait_secs, "
                "strategy_c_min_score, strategy_c_min_liq_usd, "
                "strategy_d_min_score, strategy_d_min_liq_usd, "
                "grok_premium_enabled, grok_premium_min_score, "
                "grok_premium_tp_mult, grok_premium_stall_secs, grok_premium_stall_band, grok_premium_stall_min_pct, "
                "smart_wallet_enabled, "
                "rugcheck_max_score, a2_min_holders, a2_max_whale_pct, post_sl_cooldown_secs, "
                "trailing_stop_enabled, trailing_stop_pct, momentum_confirm_secs, "
                "b_min_vol_liq_ratio, b_max_vol_liq_ratio, c_min_vol_liq_ratio, c_max_vol_liq_ratio, "
                "d_min_vol_liq_ratio, d_max_vol_liq_ratio, scout_min_liq_mc_ratio"
                # NOTE: buy_sol/*_buy_sol, tp1_mult, b/c/d_min_buy_ratio, scout_max_h1_pct,
                # scout_min_age_secs, pumpswap_min_age_secs, narrative_keyword_score,
                # macro_event_mode are intentionally EXCLUDED — research-locked or trader-hardwired.
            )

            # ── Build Haiku coach audit summary for Sonnet to review ────────
            _recent_haiku = list(_haiku_decisions)[-20:]
            if _recent_haiku:
                _h_exits = [d for d in _recent_haiku if d["verdict"] == "EXIT"]
                _h_holds = [d for d in _recent_haiku if d["verdict"] == "HOLD"]
                _h_executed = [d for d in _h_exits if d.get("executed")]
                _haiku_audit_str = (
                    f"  Total decisions: {len(_recent_haiku)} | "
                    f"EXIT: {len(_h_exits)} ({len(_h_executed)} executed) | "
                    f"HOLD: {len(_h_holds)}\n"
                )
                for _hd in _recent_haiku[-8:]:
                    _exec_tag = " [SOLD]" if _hd.get("executed") else ""
                    _haiku_audit_str += (
                        f"  {_hd['symbol'][:10]} {_hd['verdict']}{_exec_tag} "
                        f"P&L:{_hd['pnl_pct']:+.0f}% m5:{_hd['chg_m5']:+.1f}% "
                        f"B{_hd['buys_m5']}/S{_hd['sells_m5']} — {_hd['reason'][:60]}\n"
                    )
            else:
                _haiku_audit_str = "  No coach decisions yet this session.\n"

            # ── Wire rejection outcomes into analysis context ─────────────────
            _rejection_summary = ""
            try:
                from elizaos.plugins.solana import rejection_tracker as _rt_analysis
                _outcomes = await _rt_analysis.check_outcomes(min_age_hours=2.0)
                _pumped = _outcomes.get("pumped", [])
                _rugged = _outcomes.get("rugged", [])
                _flat   = _outcomes.get("flat", [])
                if _pumped or _rugged:
                    _rejection_summary = (
                        f"REJECTION OUTCOMES (tokens we blocked — checked 2h+ later via DexScreener):\n"
                        f"  Pumped after we rejected: {len(_pumped)} tokens — these are FILTER MISSES\n"
                        f"  Rugged after we rejected: {len(_rugged)} tokens — filters working correctly\n"
                        f"  Flat after we rejected:   {len(_flat)} tokens\n"
                    )
                    if _pumped:
                        _rejection_summary += "  TOP MISSES (tokens we rejected that pumped):\n"
                        for _p in sorted(_pumped, key=lambda x: x.get("h6_pct", 0), reverse=True)[:5]:
                            _rejection_summary += (
                                f"    {_p.get('symbol','?'):10} blocked by {_p.get('filter_name','?'):20} "
                                f"val={str(_p.get('filter_value',''))[:20]:20} → h6={_p.get('h6_pct',0):+.0f}%\n"
                            )
                        _rejection_summary += "  ACTION: If 3+ pumped tokens failed the SAME filter, that filter may be too strict.\n"
            except Exception as _rte:
                pass

            prompt = (
                f"You are Jarvis — the pattern-analysis brain of a Solana trading bot.\n"
                f"You have EXACTLY {len(sells)} closed trades in this session to work with.\n"
                f"CRITICAL RULES:\n"
                f"  1. NEVER invent trade data, token names, or patterns not shown below.\n"
                f"  2. Every ADJUST command MUST cite specific trades (e.g. '7 of 12 losses had m5>15%').\n"
                f"  3. With fewer than {MIN_TRADES_FOR_ADJUST} trades: write NO_CHANGES_NEEDED. You do not have enough data.\n"
                f"  4. Maximum 2 ADJUST commands per cycle. Quality over quantity.\n"
                f"  5. Never disable strategies. Never touch buy sizes or stop losses.\n\n"
                f"LIVE MARKET CONTEXT:\n"
                f"  Crypto Fear & Greed Index: {_fng_val}/100 ({_fng_class})\n"
                f"  {'FEAR market — momentum trades dump fast. Let trailing stop do its job.' if _fng_val < 40 else 'GREED market — momentum runs further. Trailing stop is even more important.' if _fng_val > 65 else 'NEUTRAL market sentiment.'}\n"
                f"  CoinGecko trending: {_trending_str}\n\n"
                f"WALLET: {_wallet_sol:.4f} SOL\n"
                f"  {'(buy sizes are PROTECTED — do not adjust)' if _wallet_readable else '(wallet unreadable — skip all sizing suggestions)'}\n\n"
                f"SESSION PERFORMANCE — {len(sells)} closed trades THIS SESSION ONLY:\n"
                f"  Win rate: {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)\n"
                f"  Total realized P&L: {total_pnl:+.4f} SOL\n"
                f"  Average P&L per trade: {avg_pnl:+.1f}%\n"
                f"  Avg hold — wins: {avg_hold_wins:.0f}s | losses: {avg_hold_losses:.0f}s\n"
                f"  Exit reasons: {reason_str}\n\n"
                f"DEX BREAKDOWN:\n{dex_str}\n\n"
                f"WINNER TOKEN PROFILE (avg at entry):\n{winner_profile}\n"
                f"LOSER TOKEN PROFILE (avg at entry):\n{loser_profile}\n\n"
                f"RECENT LOSSES — STUDY THESE (last {len(recent_losses)}):\n{loss_detail_str}\n\n"
                f"HAIKU COACH DECISIONS (last {min(20, len(_haiku_decisions))}):\n{_haiku_audit_str}\n\n"
                f"CURRENT PARAMETERS:\n{_cfg_str}\n\n"
                f"ALL ADJUSTABLE PARAMETERS:\n  {_adj_keys}\n\n"
                f"  For boolean params use: true or false\n"
                f"  For numeric params use the number directly\n\n"
                f"STRATEGY DESCRIPTIONS:\n"
                f"  A2: pump.fun bonding curve sniper — buys at token creation (55-{_cfg.get('fill_cap_pct',68):.0f}% fill)\n"
                f"      Size: a2_buy_sol={_cfg.get('a2_buy_sol',0.15):.3f} SOL | TP +{(_cfg.get('pf_tp1_mult',1.60)-1)*100:.0f}% | SL -{_cfg.get('stop_loss_pct',0.09)*100:.0f}%\n"
                f"  B:  PumpSwap grad-snipe — buys at pump.fun graduation, liq≥${_cfg.get('strategy_b_min_liq_usd',14000):,.0f}\n"
                f"      Size: strategy_b_buy_sol={_cfg.get('strategy_b_buy_sol',0.0):.3f} SOL | TP +{(_cfg.get('tp1_mult',1.40)-1)*100:.0f}% | SL -{_cfg.get('pumpswap_stop_loss_pct',0.07)*100:.0f}%\n"
                f"  C:  Raydium/PumpSwap momentum scout — native Raydium tokens\n"
                f"  D:  Meteora DLMM scout — Meteora liquidity pool tokens\n"
                f"  Developer blacklist: {rep_stats['blacklisted']} | whitelist: {rep_stats['whitelisted']}\n\n"
                f"HARD LIMITS — research-backed, CANNOT be overridden:\n"
                f"  buy_sol / a2_buy_sol / *_buy_sol: DO NOT CHANGE — position sizes are trader-set.\n"
                f"  tp1_mult / pf_tp1_mult: DO NOT CHANGE — TP is trader-set at 30%.\n"
                f"  b_min_buy_ratio / c_min_buy_ratio / d_min_buy_ratio: LOCKED 48-65%\n"
                f"    (Research: 50-65% buy ratio is iron-clad across ALL 22 monster tokens. Zero exceptions.\n"
                f"     Floor 48% ensures filter is never disabled. Ceiling 65% blocks euphoric top-buyers.)\n"
                f"  scout_max_h1_pct: DO NOT LOWER BELOW 80%\n"
                f"    (Last night's losses ALL entered at h1 +121-264%. Monster entry = h1 quiet, NOT already pumping.)\n"
                f"  scout_min_age_secs / pumpswap_min_age_secs: DO NOT LOWER\n"
                f"    (Monsters were 6-10h old on Raydium, 14-48h on PumpSwap. Entering brand-new tokens = rugs.)\n"
                f"  narrative_keyword_score: HARDWIRED 0 — viral keyword bonus is disabled, DO NOT SET >0.\n"
                f"  macro_event_mode / macro_event_label: NOT your decision — trader-triggered only.\n"
                f"  rugcheck_max_score: range 6000-200000\n"
                f"    (Graduated PumpSwap tokens score 1-11001. Raw BC tokens score 20000-150000 naturally.)\n"
                f"  strategy_b_min_liq_usd: range 5000-30000 (sweet spot 8000-13500)\n"
                f"  DO NOT progressively tighten filters across sessions without data backing the change.\n\n"
                f"== MONSTER TRADE MISSION ==\n"
                f"The trader has provided 9 confirmed monster trades (6x-143x gains, 6-10h holds).\n"
                f"Their common DNA: buy_ratio 50-57%, score 3-10 at entry, quiet entry (no extreme m5 pump),\n"
                f"Raydium/Meteora DEX, hold time 6-10 HOURS (not seconds), rugcheck passing.\n"
                f"CRITICAL: These tokens were held for HOURS — a fixed 30% or 60% TP would have missed 90%+ of gains.\n"
                f"TRAILING STOP is the correct exit for these. Enable it and set it to 12-15%.\n"
                f"Your mission: identify whether current filters are catching or missing monster-type setups.\n\n"
                + (_rejection_summary + "\n" if _rejection_summary else "")
                + f"YOUR JOB THIS CYCLE:\n"
                f"(1) Compare the loss patterns above against the MONSTER TRADE DNA.\n"
                f"    Are we entering tokens that look nothing like the monsters? What's different?\n"
                f"(2) Check the REJECTION OUTCOMES (if shown above).\n"
                f"    If 3+ pumped tokens failed the SAME filter → that filter may be too strict.\n"
                f"    If 3+ rejected tokens rugged → that filter is working, don't loosen it.\n"
                f"(3) Only issue ADJUST commands if you have evidence from {MIN_TRADES_FOR_ADJUST}+ trades.\n"
                f"    With {len(sells)} trades: {'write NO_CHANGES_NEEDED — insufficient data for config changes.' if len(sells) < MIN_TRADES_FOR_ADJUST else 'you MAY suggest up to 2 adjustments with trade-count evidence.'}\n"
                f"    ⛔ DO NOT disable strategies. DO NOT change buy sizes, stop losses, or TP.\n"
                f"    ✅ Secondary params you may tune: strategy_b_min_liq_usd (range $5k-$15k), \n"
                f"       trailing_stop_pct (range 10-20%), momentum_confirm_secs, post_sl_cooldown_secs.\n\n"
                f"FORMAT — brief analysis (under 150 words), then:\n"
                f"ADJUST: key=value reason=<cite specific trade count — e.g. '8 of 12 losses had X'>\n"
                f"If fewer than {MIN_TRADES_FOR_ADJUST} trades or no clear pattern: NO_CHANGES_NEEDED\n\n"
                f"SELF-RESTART — only for genuine runtime failures (crash loop, hung process):\n"
                f"RESTART: reason=<short phrase>"
            )

            _paper_mode_analysis = bool(_analysis_lc.get("paper_trading", False))
            print(f"[analysis] Running {'PAPER MODE ' if _paper_mode_analysis else ''}Jarvis analysis: {len(sells)} closed trades ({len(_hist_files)} history files, {len(session_history)} session records)...")
            try:
                import re as _re_analysis
                insight = ""

                # Brain 0: Claude Sonnet (PRIMARY — best quality analysis, self-tunes with ADJUST commands)
                _ant_key_a = os.getenv("ANTHROPIC_API_KEY", "")
                if _ant_key_a and not insight:
                    try:
                        import anthropic as _anthropic_analysis
                        _sonnet_client = _anthropic_analysis.AsyncAnthropic(api_key=_ant_key_a)
                        _sonnet_resp = await asyncio.wait_for(
                            _sonnet_client.messages.create(
                                model="claude-sonnet-4-6",
                                max_tokens=1200,
                                messages=[{"role": "user", "content": prompt}],
                            ),
                            timeout=60.0,
                        )
                        insight = (_sonnet_resp.content[0].text if _sonnet_resp.content else "").strip()
                    except Exception as _ce:
                        print(f"[analysis] Claude failed: {_ce}", file=sys.stderr)

                # Brain 1: Groq llama-3.3-70b (fallback — free, high TPM)
                _groq_key_a = os.getenv("GROQ_API_KEY", "")
                if _groq_key_a and not insight:
                    try:
                        from openai import AsyncOpenAI as _OAI_a
                        _groq_a = _OAI_a(api_key=_groq_key_a, base_url="https://api.groq.com/openai/v1", max_retries=0)
                        _gr = await asyncio.wait_for(
                            _groq_a.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                max_tokens=1024,
                                messages=[{"role": "user", "content": prompt}],
                            ),
                            timeout=60.0,
                        )
                        insight = (_gr.choices[0].message.content or "").strip()
                    except Exception as _ge:
                        print(f"[analysis] Groq failed: {_ge}", file=sys.stderr)

                # Brain 2: Gemini 2.0 Flash (second fallback — free, Google AI Studio)
                _gemini_key_a = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
                if _gemini_key_a and not insight:
                    try:
                        from google import genai as _gai_a
                        _gc_a = _gai_a.Client(api_key=_gemini_key_a)
                        _gem_r = await asyncio.wait_for(
                            _gc_a.aio.models.generate_content(
                                model="gemini-2.5-flash",
                                contents=prompt,
                                config={"max_output_tokens": 1024},
                            ),
                            timeout=60.0,
                        )
                        insight = (_gem_r.text or "").strip()
                    except Exception as _ge2:
                        print(f"[analysis] Gemini failed: {_ge2}", file=sys.stderr)

                if not insight:
                    raise RuntimeError("All analysis brains failed")
                print(f"\n{'─'*60}\n[JARVIS ANALYSIS @ {time.strftime('%H:%M UTC', time.gmtime())}]\n{insight}\n{'─'*60}\n")

                # ── Write Sonnet's tactical briefing to shared intel for Haiku ──
                # Extract first 2-3 sentences (the strategic summary before ADJUST lines)
                _intel_lines = [l for l in insight.split("\n") if l.strip() and not l.startswith("ADJUST:") and not l.startswith("NO_CHANGES")]
                _intel_summary = " ".join(_intel_lines[:4])[:400]
                _sonnet_intel["summary"] = _intel_summary
                _sonnet_intel["timestamp"] = time.time()
                print(f"[sonnet→haiku] Tactical briefing updated ({len(_intel_summary)} chars) — Haiku will use this context")

                # ── RESTART: tag — Jarvis requests a clean self-restart ──────────
                _restart_match = _re_analysis.search(
                    r"RESTART:\s*reason=(.+)", insight, _re_analysis.IGNORECASE
                )
                if _restart_match:
                    _restart_reason = _restart_match.group(1).strip()
                    try:
                        wd_pid = int(open("/tmp/traderbot-watchdog.pid").read().strip())
                        import signal as _signal_mod
                        os.kill(wd_pid, 0)   # verify watchdog is alive
                        print(f"[jarvis-restart] 🔄 Autonomous restart requested — reason: {_restart_reason}")
                        print(f"[jarvis-restart] Watchdog active — sending SIGTERM in 5s to trigger clean restart")
                        if pos_mgr is not None:
                            pos_mgr._log_activity("warning", f"Jarvis autonomous restart: {_restart_reason}")
                        await asyncio.sleep(5)
                        os.kill(os.getpid(), _signal_mod.SIGTERM)
                    except Exception as _wr_err:
                        print(f"[jarvis-restart] ⚠️  RESTART: tag found but watchdog not active ({_wr_err}) — skipping restart")

                # Parse and apply ADJUST commands — handles numbers AND booleans
                adjust_lines = _re_analysis.findall(
                    r"ADJUST:\s*(\w+)=([^\s]+)\s+reason=(.+)", insight, _re_analysis.IGNORECASE
                )
                # Hard floor/ceiling guardrails — Jarvis cannot override these
                _JARVIS_GUARDRAILS: dict[str, tuple] = {
                    # Trade sizing
                    "buy_sol":                 (0.03, 10.0),
                    "a2_buy_sol":              (0.003, 5.0),
                    # Risk — hard floors prevent Jarvis ratcheting SL to near-zero
                    "stop_loss_pct":           (0.05, 0.15),   # min -5%, max -15%
                    "pumpswap_stop_loss_pct":  (0.05, 0.15),
                    "early_stop_loss_pct":     (0.03, 0.10),
                    # Take-profit — must leave room to actually win
                    "tp1_mult":                (1.25, 5.0),    # min +25% TP
                    "pf_tp1_mult":             (1.25, 3.0),
                    "grok_tp_mult":            (1.50, 10.0),
                    # Position limits — raised to 10 for copy-trade paper test (2026-04-05)
                    "max_concurrent_positions": (1, 10),
                    # Strategy B graduation filters
                    "strategy_b_min_age_secs":  (5, 60),
                    "strategy_b_min_liq_usd":  (5000, 25000),
                    "strategy_b_dip_wait_secs": (0, 60),
                    # A2 filters
                    "a2_min_holders":          (5, 50),
                    "a2_min_liq_usd":          (0, 100_000),
                    "a2_min_score":            (0, 80),
                    # Rugcheck — graduated tokens score 1-11001; BC score 20k-150k; cap at 100k
                    "rugcheck_max_score":       (6000, 200000),
                    # Cooldowns — prevent Jarvis from freezing the bot
                    "post_sl_cooldown_secs":   (30, 600),
                    # Vol/liq ratios — monster DNA: 1-3x at entry, cap at 8x (above = post-pump)
                    "b_min_vol_liq_ratio":     (1.0, 5.0),    # floor: 1x (dead tokens at 0.2x)
                    "b_max_vol_liq_ratio":     (5.0, 15.0),   # cap: 8x (above = already pumped)
                    "c_min_vol_liq_ratio":     (1.0, 5.0),
                    "c_max_vol_liq_ratio":     (5.0, 15.0),   # cap at 8x; above = wash trading
                    "d_min_vol_liq_ratio":     (1.0, 5.0),
                    "d_max_vol_liq_ratio":     (5.0, 15.0),
                    # Liq floors — $25k minimum prevents micro-cap rugs; max $200k (above = already ran)
                    "c_min_liq_usd":           (20000, 100000),
                    "d_min_liq_usd":           (20000, 100000),
                    # Scout age — 1h-3h sweet spot; never 0 (brand new rugs)
                    "scout_min_age_secs":      (1800, 7200),   # 30min-2h range
                    "pumpswap_min_age_secs":   (3600, 14400),  # 1h-4h range for PumpSwap scouts
                    # Scout m5 cap — monster entry is QUIET; 15% max; hard ceiling 25%
                    "scout_max_m5_pct":        (10.0, 25.0),   # 15% default; max 25%; Jarvis cannot set >25%
                    # Scout h1 cap — 80% default; if h1>80% first wave is done
                    "scout_max_h1_pct":        (50.0, 100.0),  # 80% default
                    # Buy ratio floors — 29-monster dataset: 50-65% iron-clad
                    # Floor 50%: NO token with buy ratio < 50% was a true monster
                    # Ceiling 65%: euphoric top buyers (FML=69.9%, apple=79.8% both rugged)
                    "b_min_buy_ratio":         (50.0, 65.0),
                    "c_min_buy_ratio":         (50.0, 65.0),   # raised from 45; 50% is the floor
                    "d_min_buy_ratio":         (50.0, 65.0),
                    # Copy-trade compounding system — 91-trade quant analysis 2026-04-14
                    # Hard TP 15% / SL 10% / 20%-of-wallet compound rule. All locked.
                    "copy_trade_tp_pct":        (14.0, 16.0),   # tight band — 15% hard TP
                    "copy_trade_sl_pct":        (8.0, 12.0),    # tight band — 10% hard SL
                    "copy_trade_compound_pct":  (0.18, 0.22),   # 20% of wallet rule ±2%
                }
                # Keys the analysis loop cannot touch — trader has explicitly locked these
                _PROTECTED_KEYS: set[str] = set(
                    (_analysis_lc.get("_protected_keys") or "").split(",")
                ) - {""}

                # In PAPER MODE — Jarvis has wide authority to tune filters.
                # Real money is not at stake so we let it experiment freely.
                # Only buy sizes (which determine paper equity scale) stay locked.
                _paper_mode_now = bool(_analysis_lc.get("paper_trading", False))
                if _paper_mode_now:
                    # PAPER MODE — same protections as LIVE. These are the research-derived
                    # optimal settings from the 29-monster dataset. Paper trading must test
                    # exactly the same settings we'll use with real money. Jarvis can tune
                    # secondary filters but cannot touch the core monster-hunting parameters.
                    _PROTECTED_KEYS.update({
                        # ── Buy sizes — paper equity scale ──────────────────────────────────
                        "buy_sol", "a2_buy_sol",
                        "strategy_b_buy_sol", "strategy_c_buy_sol", "strategy_d_buy_sol",
                        "grok_premium_buy_sol", "split_buy_a_sol", "split_buy_b_sol",
                        # ── TP / exit structure — quant-optimised ─────────────────────────
                        "tp1_mult", "pf_tp1_mult", "grok_tp_mult",
                        "split_buy_a_tp_mult", "split_buy_b_tp_mult", "grok_premium_tp_mult",
                        # ── Stop loss — 9% hard SL from monster dataset ───────────────────
                        "stop_loss_pct",              # 9% — Jarvis tends to drift this to 5%
                        "pumpswap_stop_loss_pct",     # 9% — same for PumpSwap
                        "early_stop_loss_pct",
                        # ── Trailing stop — primary exit for monster holds ─────────────────
                        "trailing_stop_enabled",      # MUST stay True — fixed TP kills monsters
                        "trailing_stop_pct",          # 12% trail — tested on all 29 monsters
                        # ── Position limits ────────────────────────────────────────────────
                        "max_concurrent_positions",   # 2 max — simulates 5 SOL wallet
                        "graduation_confirmation_wait",  # 30s recheck before committing
                        # ── Strategy enables ──────────────────────────────────────────────
                        "strategy_a2_enabled", "strategy_b_enabled", "strategy_c_enabled",
                        "strategy_d_enabled", "strategy_e_enabled",
                        # ── Buy ratio — THE signal (29-monster iron-clad) ─────────────────
                        "b_min_buy_ratio", "c_min_buy_ratio", "d_min_buy_ratio",
                        # ── Vol/liq ratio — monster entry zone ────────────────────────────
                        "b_min_vol_liq_ratio", "b_max_vol_liq_ratio",
                        "c_min_vol_liq_ratio", "c_max_vol_liq_ratio",
                        "d_min_vol_liq_ratio", "d_max_vol_liq_ratio",
                        # ── Liquidity floors — prevent micro-cap rugs ─────────────────────
                        "c_min_liq_usd", "c_min_mc_usd",    # $25k liq / $50k MC
                        "d_min_liq_usd", "d_min_mc_usd",    # $25k liq / $50k MC
                        # ── Scout quality caps — monster entry is quiet ────────────────────
                        "scout_max_m5_pct",           # 15% — buying mid-pump is the #1 loss driver
                        "scout_max_h1_pct",           # 80% — h1>80% = first wave already done
                        "scout_min_liq_mc_ratio",     # 0.05 minimum ratio
                        "scout_min_age_secs",         # 1h min — brand new tokens are rugs
                        "pumpswap_min_age_secs",      # 2h min for PumpSwap scouts
                        # ── Strategy B graduation filters ─────────────────────────────────
                        # strategy_b_min_liq_usd: $10k — Jarvis WILL raise this to $13.5k
                        # "hold golden formula". 7x7=49 had $11.5k at graduation and went
                        # +1547%. If liq_min > $11.5k we miss these. Lock at $10k.
                        "strategy_b_min_liq_usd",
                        "strategy_b_dip_wait_secs",
                        "strategy_b_min_age_secs",
                        # ── Rugcheck / A2 filters ─────────────────────────────────────────
                        "rugcheck_max_score",
                        "a2_min_real_sol",
                        "a2_min_holders",
                        "a2_max_whale_pct",
                        # ── Research locks ────────────────────────────────────────────────
                        "narrative_keyword_score",    # permanently 0
                        "macro_event_mode", "macro_event_label",
                        # ── Unused / no-op features ───────────────────────────────────────
                        "monster_scanner_enabled", "monster_scanner_buy",
                        "monster_scanner_min_liq_usd", "monster_scanner_min_mc_usd",
                        "monster_scanner_interval_secs", "monster_scanner_min_vol_liq",
                        "monster_scanner_max_vol_liq", "monster_scanner_min_buy_ratio",
                        "monster_scanner_min_age_mins", "monster_scanner_max_age_mins",
                        "a2_min_holder_growth_rate", "a2_holder_growth_window_secs",
                        "b_min_momentum_score", "c_min_momentum_score", "d_min_momentum_score",
                        # ── Copy-trade paper test — Jarvis cannot touch these ─────────────
                        "copy_trade_paper_enabled", "copy_trade_paper_buy_sol",
                        "copy_trade_paper_balance", "copy_trade_min_sol",
                        "copy_trade_consensus", "copy_trade_enabled", "copy_trade_sl_pct",
                        # ── Copy-trade compounding system (2026-04-14 quant-locked) ─────────
                        "copy_trade_tp_pct",           # 15% hard TP — 91-trade simulation proven
                        "copy_trade_compound_pct",     # 20% of wallet rule
                        "copy_trade_compound_max_sol", # 2.0 SOL cap per trade
                        # ── Parked strategies — locked OFF during 24h copy trade test ─────
                        "strategy_a2_enabled", "strategy_b_enabled",
                        "strategy_c_enabled", "strategy_d_enabled", "strategy_e_enabled",
                    })
                else:
                    # LIVE MODE — full protection. Jarvis CANNOT change any of these.
                    # All values are derived from 29-monster dataset + quant analysis.
                    # To change any of these, restart bot after editing bot_config.json manually.
                    _PROTECTED_KEYS.update({
                        # ── Strategy enables ──────────────────────────────────────────────
                        "strategy_a2_enabled", "strategy_b_enabled",
                        "strategy_c_enabled", "strategy_d_enabled",
                        # ── Position sizing — CRITICAL with 5 SOL wallet ─────────────────
                        "buy_sol", "a2_buy_sol",
                        "strategy_b_buy_sol", "strategy_c_buy_sol", "strategy_d_buy_sol",
                        "grok_premium_buy_sol", "smart_wallet_buy_sol",
                        "max_concurrent_positions",   # max 2 live positions
                        # ── Stop loss / TP — quant-optimised, never touch ─────────────────
                        "stop_loss_pct",              # 9% hard SL
                        "pumpswap_stop_loss_pct",     # 9% for PumpSwap
                        "early_stop_loss_pct",
                        "tp1_mult",                   # +100% safety net TP — trailing stop is primary
                        "trailing_stop_enabled",      # MUST stay True
                        "trailing_stop_pct",          # 12% trail — gives monsters room
                        "max_daily_loss_pct",
                        "post_sl_cooldown_secs",
                        "graduation_confirmation_wait",  # 30s recheck before committing
                        # ── Entry quality — monster DNA (29-token research) ───────────────
                        "b_min_buy_ratio",            # 50% iron-clad floor — buy ratio THE signal
                        "b_max_vol_liq_ratio",        # 8x cap — above = wash trade / already pumped
                        "b_min_vol_liq_ratio",        # 1x floor — below = dead token
                        "c_min_buy_ratio",            # 50% iron-clad (was 45% — too loose)
                        "c_max_vol_liq_ratio",        # 8x cap
                        "c_min_vol_liq_ratio",        # 1x floor
                        "d_min_buy_ratio",            # 50% floor
                        "d_max_vol_liq_ratio",        # 8x cap
                        "d_min_vol_liq_ratio",        # 1x floor
                        # ── Liquidity floors — prevent micro-cap rugs ────────────────────
                        "c_min_liq_usd",              # $25k minimum — $3k liq is rug territory
                        "c_min_mc_usd",               # $50k MC floor
                        "d_min_liq_usd",              # $25k minimum
                        "d_min_mc_usd",               # $50k MC floor
                        "a2_max_whale_pct",
                        "rugcheck_max_score",
                        "a2_min_holders",
                        "a2_min_liq_usd", "a2_min_mc_usd",
                        "strategy_b_min_liq_usd", "strategy_b_dip_wait_secs",
                        # ── Scout quality caps — monster entry zone ────────────────────────
                        "scout_max_m5_pct",           # 15% — quiet entry; >15% = mid-pump chasing
                        "scout_max_h1_pct",           # 80% — if h1>80%, first wave is done
                        "scout_min_liq_mc_ratio",     # 0.05 minimum
                        "pumpswap_min_age_secs",      # 2h min for PumpSwap scouts
                        "scout_min_age_secs",         # 1h min for Raydium/Meteora scouts
                        # ── Research locks ───────────────────────────────────────────────
                        "narrative_keyword_score",    # permanently 0 — rewarded meme rugs
                        "macro_event_mode", "macro_event_label",  # only via explicit Jarvis command
                        # ── Monster scanner — only trader activates ───────────────────────
                        "monster_scanner_enabled", "monster_scanner_buy",
                        "monster_scanner_min_liq_usd", "monster_scanner_min_mc_usd",
                        "monster_scanner_interval_secs",
                        "monster_scanner_min_vol_liq", "monster_scanner_max_vol_liq",
                        "monster_scanner_min_buy_ratio",
                        "monster_scanner_min_age_mins", "monster_scanner_max_age_mins",
                        # ── Misc ─────────────────────────────────────────────────────────
                        "a2_min_holder_growth_rate", "a2_holder_growth_window_secs",
                        "b_min_momentum_score", "c_min_momentum_score", "d_min_momentum_score",
                        # ── Copy-trade compounding system (2026-04-14 quant-locked) ─────────
                        # 91-trade simulation: -10% SL / +15% TP / 20%-of-wallet compounding
                        # Result: +1.68 SOL vs +0.015 SOL actual. Never touch without fresh data.
                        "copy_trade_tp_pct",           # 15% hard TP
                        "copy_trade_sl_pct",           # 10% hard SL
                        "copy_trade_paper_buy_sol",    # 0.4 SOL floor (20% of 2 SOL wallet)
                        "copy_trade_compound_pct",     # 20% of wallet per trade
                        "copy_trade_compound_max_sol", # 2.0 SOL max per trade
                    })

                applied: list[str] = []
                for key, val_str, reason in adjust_lines:
                    val_str = val_str.strip().rstrip(".,;")
                    # Skip protected keys
                    if key in _PROTECTED_KEYS:
                        print(f"[jarvis-adjust] 🔒 PROTECTED: {key} — analysis cannot change strategy enables")
                        continue
                    # Coerce value type
                    if val_str.lower() in ("true", "false"):
                        val: bool | int | float = val_str.lower() == "true"
                    elif "." in val_str:
                        try:
                            val = float(val_str)
                        except ValueError:
                            print(f"[jarvis-adjust] ⚠️  Could not parse value '{val_str}' for {key}")
                            continue
                    else:
                        try:
                            val = int(val_str)
                        except ValueError:
                            print(f"[jarvis-adjust] ⚠️  Could not parse value '{val_str}' for {key}")
                            continue
                    # Enforce hard guardrails
                    if key in _JARVIS_GUARDRAILS and isinstance(val, (int, float)):
                        _glo, _ghi = _JARVIS_GUARDRAILS[key]
                        if not (_glo <= val <= _ghi):
                            _clamped = max(_glo, min(_ghi, val))
                            print(f"[jarvis-adjust] 🛡️  GUARDRAIL: {key}={val} rejected (must be {_glo}–{_ghi}), clamped to {_clamped}")
                            val = _clamped
                    ok, msg = _analysis_lc.set_value(key, val, changed_by="jarvis", reason=reason.strip())
                    if ok:
                        applied.append(f"{key}={val} ({reason.strip()})")
                        print(f"[jarvis-adjust] ✅ {msg} — reason: {reason.strip()}")
                        if pos_mgr is not None:
                            pos_mgr._log_activity(  # type: ignore[union-attr]
                                "warning",
                                f"[jarvis-adjust] {msg} — {reason.strip()}"
                            )
                    else:
                        print(f"[jarvis-adjust] ⚠️  {msg}")

                # Store applied adjustments in shared intel so Haiku knows what changed
                if applied:
                    _sonnet_intel["adjustments"] = applied[-8:]

                summary = insight[:200]
                if applied:
                    summary += f" | ADJUSTED: {'; '.join(applied)}"
                # Log to activity feed
                if pos_mgr is not None:
                    pos_mgr._log_activity(  # type: ignore[union-attr]
                        "info",
                        f"Jarvis analysis ({len(sells)} trades, {win_rate:.0f}% WR): {summary}"
                    )
            except Exception as exc:
                print(f"[analysis] Jarvis call failed: {exc}", file=sys.stderr)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[analysis] Unexpected error: {exc}", file=sys.stderr)

        # Re-read paper mode in case it changed; adjust sleep accordingly
        _paper_now = bool(_analysis_lc.get("paper_trading", False))
        await asyncio.sleep(600 if _paper_now else ANALYSIS_INTERVAL)


async def three_brain_review_loop(runtime: AgentRuntime) -> None:
    """Every 2 hours, all 3 AIs collaborate on a deep retrospective trade review.

    The three-brain council:
      Brain 1 — Grok (xAI):      Social signal quality — did our tokens have real X buzz?
      Brain 2 — GPT-4o (OpenAI): Entry pattern analysis — what separates wins from losses?
      Brain 3 — Claude Sonnet:   Synthesis — writes concrete lessons to eliza_lessons.txt

    This runs ALONGSIDE the existing 30-min Sonnet ADJUST loop (analysis_loop).
    The difference: this goes DEEPER per-token using all 3 AIs, focuses on WHY
    we entered, and writes persistent lessons that future Haiku decisions can use.
    """
    REVIEW_INTERVAL = 7200  # every 2 hours
    MIN_TRADES = 10

    # First run: 5 minutes after boot (gives time to load history)
    await asyncio.sleep(300)
    print("[3brain] Three-brain trade review loop started (first run in 5 min, then every 2h)")

    while True:
        try:
            pos_mgr = runtime.get_service("position_manager")
            if pos_mgr is None:
                await asyncio.sleep(REVIEW_INTERVAL)
                continue

            # ── Load trade history (paper=current session only) ───────────────
            _solana_dir = os.path.join(os.path.dirname(__file__), "elizaos/plugins/solana")
            _all_by_id: dict = {}
            from elizaos.plugins.solana import live_config as _lc_3b
            _paper_3b = bool(_lc_3b.get("paper_trading", False))
            if _paper_3b:
                _hist_files_3b = [os.path.join(_solana_dir, "trade_history.json")]
            else:
                _hist_files_3b = sorted([
                    os.path.join(_solana_dir, f)
                    for f in os.listdir(_solana_dir)
                    if f.startswith("trade_history") and f.endswith(".json")
                ])
            for _hpath in _hist_files_3b:
                try:
                    with open(_hpath) as _hf:
                        _batch = json.load(_hf)
                    if isinstance(_batch, list):
                        for t in _batch:
                            if t.get("id"):
                                _all_by_id[t["id"]] = t
                except Exception:
                    pass
            for t in pos_mgr.get_trade_history(limit=1000):  # type: ignore[union-attr]
                if t.get("id"):
                    _all_by_id[t["id"]] = t

            history = sorted(_all_by_id.values(), key=lambda t: t.get("timestamp", 0))
            sells = [t for t in history if t.get("side") == "sell"]
            buys  = [t for t in history if t.get("side") == "buy"]

            if len(sells) < MIN_TRADES:
                print(f"[3brain] Only {len(sells)} closed trades — skipping (need {MIN_TRADES})")
                await asyncio.sleep(REVIEW_INTERVAL)
                continue

            # ── Build buy-side meta index ─────────────────────────────────────
            buy_meta: dict[str, dict] = {}
            buy_info: dict[str, dict] = {}  # name/symbol/score for each mint
            for t in buys:
                m = t.get("mint", "")
                if t.get("meta"):
                    buy_meta[m] = t["meta"]
                buy_info[m] = {
                    "name": t.get("meta", {}).get("name", "") if t.get("meta") else "",
                    "symbol": t.get("meta", {}).get("symbol", "") if t.get("meta") else "",
                    "score": t.get("score", 0),
                    "dex": t.get("dex", "?"),
                    "fill_pct": t.get("fill_pct", 0),
                }

            # ── Aggregate stats for briefing ──────────────────────────────────
            wins   = [t for t in sells if (t.get("pnl_pct") or 0) > 0]
            losses = [t for t in sells if (t.get("pnl_pct") or 0) <= 0]
            win_rate = len(wins) / len(sells) * 100 if sells else 0
            avg_pnl = sum(t.get("pnl_pct") or 0 for t in sells) / len(sells) if sells else 0
            total_pnl_sol = sum(t.get("pnl_sol") or 0 for t in sells)

            from collections import Counter as _Counter
            reasons_ctr = _Counter(t.get("reason", "?") for t in sells)
            reasons_str = ", ".join(f"{r}={n}" for r, n in reasons_ctr.most_common(8))

            from collections import defaultdict as _dd
            dex_groups: dict = _dd(list)
            for t in sells:
                dex_groups[t.get("dex", "?")].append(t)
            dex_summary = "\n".join(
                f"  {d}: {len(ts)} trades | WR {sum(1 for t in ts if (t.get('pnl_pct') or 0)>0)/len(ts)*100:.0f}% | "
                f"avg {sum(t.get('pnl_pct') or 0 for t in ts)/len(ts):+.1f}%"
                for d, ts in sorted(dex_groups.items())
            )

            # Sample for per-token social scan: last 20 unique mints (mix of wins and losses)
            recent_sells_sorted = sorted(sells, key=lambda t: t.get("timestamp", 0), reverse=True)
            seen_mints: set = set()
            sample_sells: list = []
            for t in recent_sells_sorted:
                m = t.get("mint", "")
                if m and m not in seen_mints:
                    seen_mints.add(m)
                    sample_sells.append(t)
                if len(sample_sells) >= 20:
                    break

            # Build token list for Grok social scan
            token_lines = []
            for t in sample_sells:
                m = t.get("mint", "")
                bi = buy_info.get(m, {})
                name   = bi.get("name") or t.get("symbol") or m[:8]
                symbol = bi.get("symbol") or ""
                pnl    = t.get("pnl_pct") or 0
                dex    = t.get("dex", "?")
                reason = t.get("reason", "?")
                token_lines.append(
                    f"  {name} ({symbol}) mint={m[:12]}... | {dex} | {pnl:+.0f}% | exit={reason}"
                )
            token_list_str = "\n".join(token_lines)

            # Recent 25 losses detail for GPT-4o
            recent_losses = sorted(losses, key=lambda t: t.get("timestamp", 0), reverse=True)[:25]
            loss_rows = []
            for _lt in recent_losses:
                _m = buy_meta.get(_lt["mint"], {})
                _bi = buy_info.get(_lt["mint"], {})
                _name   = _bi.get("symbol") or _bi.get("name") or _lt["mint"][:8]
                _pnl    = _lt.get("pnl_pct") or 0
                _sol    = _lt.get("pnl_sol") or 0
                _reason = _lt.get("reason", "?")
                _dex    = _lt.get("dex", "?")
                _hold   = _lt.get("hold_secs") or 0
                _score  = _lt.get("score") or _bi.get("score", 0)
                _fill   = _lt.get("fill_pct") or _bi.get("fill_pct") or 0
                _liq    = _m.get("liq_usd") or 0
                _m5     = _m.get("price_change_m5") or 0
                _socials = "socials" if _m.get("has_socials") else "no-socials"
                loss_rows.append(
                    f"  {_name:12s}| {_dex:10s}| {_pnl:+.0f}% ({_sol:+.4f} SOL) "
                    f"| hold {_hold:.0f}s | score={_score} fill={_fill:.0f}% "
                    f"| reason={_reason} | liq=${_liq:,.0f} m5={_m5:+.1f}% {_socials}"
                )
            loss_detail_str = "\n".join(loss_rows) or "  (none)"

            # Market context
            _fng = await _fetch_fear_greed()
            _fng_val   = _fng.get("value", 50)
            _fng_class = _fng.get("classification", "Neutral")
            _trending  = await _fetch_trending_coins()
            _trending_str = ", ".join(_trending[:5]) if _trending else "unavailable"

            # Existing lessons (so AI doesn't repeat what's already known)
            _existing_lessons = _load_recent_lessons(10)

            print(f"[3brain] Starting three-brain review: {len(sells)} closed trades, "
                  f"{len(sample_sells)} tokens sampled for social scan...")

            # ═══════════════════════════════════════════════════════════════
            # BRAIN 1 — Claude Sonnet: Token narrative & market pattern analysis
            # "What token name/narrative patterns correlate with wins vs losses?
            #  What does the market context (F&G, trending) tell us right now?"
            # (Replaces Grok X social scan — Grok disabled to save API costs)
            # ═══════════════════════════════════════════════════════════════
            grok_report = "(Brain 1 unavailable)"
            _b1_prompt = (
                f"You are analyzing a Solana memecoin trading bot's recent trades.\n\n"
                f"TOKENS WE TRADED (recent {len(sample_sells)}, mix of wins/losses):\n{token_list_str}\n\n"
                f"MARKET CONTEXT RIGHT NOW:\n"
                f"  Fear & Greed Index: {_fng_val}/100 ({_fng_class})\n"
                f"  Trending globally: {_trending_str}\n\n"
                f"OVERALL STATS: {len(sells)} trades | WR {win_rate:.0f}% | Net {total_pnl_sol:+.4f} SOL\n\n"
                f"Analyze:\n"
                f"1. TOKEN NAME PATTERNS: From the token names/symbols above, what narrative categories "
                f"appear (animal, AI, meme, celebrity-derivative, nonsense)? Which correlated with wins vs losses?\n"
                f"2. GRADUATION SPEED PATTERNS: Ultra-fast grads (<6 min) vs slow grads (>30 min) — "
                f"based on the DEX/exit data, which performed better?\n"
                f"3. MARKET TIMING: With Fear & Greed at {_fng_val} ({_fng_class}), "
                f"what does that mean for memecoin momentum right now? Should we be more or less aggressive?\n"
                f"4. ONE ACTIONABLE RULE: Based on the name/narrative patterns visible, "
                f"suggest one filter (e.g. 'avoid tokens with X in name', 'prefer Y narrative').\n\n"
                f"Be concise and specific. Max 250 words."
            )
            # Brain 1: Groq primary
            _groq_key_b1 = os.getenv("GROQ_API_KEY", "")
            if _groq_key_b1:
                try:
                    from openai import AsyncOpenAI as _OAI_b1
                    _b1_client = _OAI_b1(api_key=_groq_key_b1, base_url="https://api.groq.com/openai/v1", max_retries=0)
                    _b1_resp = await asyncio.wait_for(
                        _b1_client.chat.completions.create(
                            model="llama-3.1-8b-instant",
                            max_tokens=600,
                            messages=[{"role": "user", "content": _b1_prompt}],
                        ),
                        timeout=30.0,
                    )
                    grok_report = (_b1_resp.choices[0].message.content or "").strip()
                    print(f"[3brain] Groq B1 pattern analysis: {len(grok_report)} chars")
                except Exception as _ge:
                    print(f"[3brain] Groq B1 failed: {_ge}")
            # Brain 1 Gemini fallback
            if grok_report == "(Brain 1 unavailable)":
                _gemini_key_b1 = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
                if _gemini_key_b1:
                    try:
                        from google import genai as _gai_b1
                        _gc_b1 = _gai_b1.Client(api_key=_gemini_key_b1)
                        _b1_gem = await asyncio.wait_for(
                            _gc_b1.aio.models.generate_content(
                                model="gemini-2.5-flash",
                                contents=_b1_prompt,
                                config={"max_output_tokens": 600},
                            ),
                            timeout=30.0,
                        )
                        grok_report = (_b1_gem.text or "").strip()
                        print(f"[3brain] Gemini B1 pattern analysis: {len(grok_report)} chars")
                    except Exception as _ge2:
                        print(f"[3brain] Gemini B1 failed: {_ge2}")

            # ═══════════════════════════════════════════════════════════════
            # BRAIN 2 — Groq/Llama (free): Entry quality pattern analysis
            # "What entry conditions separate our winners from losers?"
            # ═══════════════════════════════════════════════════════════════
            gpt_report = "(Groq unavailable)"
            try:
                import openai as _oai_gpt
                _groq_key_b2 = os.getenv("GROQ_API_KEY", "")
                if not _groq_key_b2:
                    raise ValueError("GROQ_API_KEY not set")
                _gpt_client = _oai_gpt.AsyncOpenAI(
                    api_key=_groq_key_b2,
                    base_url="https://api.groq.com/openai/v1",
                    max_retries=0,
                )
                _gpt_prompt = (
                    f"You are analyzing trading data for a Solana memecoin bot to improve its entry strategy.\n\n"
                    f"OVERALL STATS ({len(sells)} closed trades):\n"
                    f"  Win rate: {win_rate:.0f}% | Avg P&L: {avg_pnl:+.1f}% | Net: {total_pnl_sol:+.4f} SOL\n"
                    f"  Exit reasons: {reasons_str}\n\n"
                    f"DEX BREAKDOWN:\n{dex_summary}\n\n"
                    f"RECENT LOSSES (worst 25) — entry conditions at purchase time:\n"
                    f"{loss_detail_str}\n\n"
                    f"MARKET CONTEXT:\n"
                    f"  Fear & Greed: {_fng_val}/100 ({_fng_class})\n"
                    f"  Trending globally: {_trending_str}\n\n"
                    f"KEY QUESTION: 'early_stop_loss' is our #1 exit ({reasons_ctr.get('early_stop_loss', 0)} trades = "
                    f"{reasons_ctr.get('early_stop_loss', 0)/len(sells)*100:.0f}% of all exits).\n"
                    f"This means tokens dump immediately after we buy. WHY?\n\n"
                    f"Analyze:\n"
                    f"1. ENTRY TIMING: Are we buying too late (overextended m5) or too early (no momentum)?\n"
                    f"2. TOKEN QUALITY: What score/liquidity/fill patterns appear in early_stop_loss trades?\n"
                    f"3. DEX PATTERNS: Which DEX has the worst early exit rate and why?\n"
                    f"4. MARKET TIMING: How does Fear & Greed affect our win rate?\n"
                    f"5. WHAT WOULD YOU DO DIFFERENTLY: Give 3 specific entry rule changes.\n\n"
                    f"Be specific and data-driven. Max 300 words."
                )
                _gpt_resp = await asyncio.wait_for(
                    _gpt_client.chat.completions.create(
                        model="llama-3.1-8b-instant",
                        messages=[
                            {"role": "system", "content": "You are a quantitative trading analyst. Be direct, specific, and data-driven. No hedging."},
                            {"role": "user", "content": _gpt_prompt},
                        ],
                        max_tokens=600,
                        temperature=0.2,
                    ),
                    timeout=30.0,
                )
                gpt_report = (_gpt_resp.choices[0].message.content or "").strip()
                print(f"[3brain] Groq/Llama entry analysis: {len(gpt_report)} chars")
            except Exception as _ge:
                print(f"[3brain] Groq call failed: {_ge}")

            # ═══════════════════════════════════════════════════════════════
            # BRAIN 3 — Claude Sonnet: Synthesis → lessons.txt
            # "Given what Claude B1 and GPT-4o found, what lessons should we lock in?"
            # ═══════════════════════════════════════════════════════════════
            try:
                _synth_prompt = (
                    f"You are Jarvis — the AI brain of a Solana memecoin trading bot.\n"
                    f"Two specialist analysts have reviewed our {len(sells)} closed trades.\n"
                    f"Your job: synthesize their findings into 5-8 CONCRETE LESSONS for the lessons file.\n\n"
                    f"=== ANALYST 1: MARKET PATTERN & NARRATIVE ANALYSIS ===\n{grok_report}\n\n"
                    f"=== ANALYST 2: ENTRY QUALITY ANALYSIS ===\n{gpt_report}\n\n"
                    f"=== LESSONS WE ALREADY KNOW (don't repeat) ===\n"
                    f"{_existing_lessons or '(none yet)'}\n\n"
                    f"=== OUR STATS ===\n"
                    f"  {len(sells)} trades | WR {win_rate:.0f}% | Net {total_pnl_sol:+.4f} SOL\n"
                    f"  Top exit reason: early_stop_loss ({reasons_ctr.get('early_stop_loss',0)} trades)\n\n"
                    f"Write 5-8 NEW lessons in this format (one per line):\n"
                    f"LESSON: [specific rule or pattern — 1-2 sentences max]\n\n"
                    f"Requirements:\n"
                    f"- Each lesson must be ACTIONABLE (a specific filter, condition, or rule to apply)\n"
                    f"- Must NOT duplicate existing lessons above\n"
                    f"- Must be grounded in the data from the two analysts above\n"
                    f"- After the lessons, write 1 paragraph: JARVIS VERDICT — what is the single\n"
                    f"  most important change we should make to improve profitability?"
                )
                synthesis = ""
                # Brain 3a: Groq synthesis
                _groq_key_b3 = os.getenv("GROQ_API_KEY", "")
                if _groq_key_b3 and not synthesis:
                    try:
                        from openai import AsyncOpenAI as _OAI_b3
                        _b3_gc = _OAI_b3(api_key=_groq_key_b3, base_url="https://api.groq.com/openai/v1", max_retries=0)
                        _b3_resp = await asyncio.wait_for(
                            _b3_gc.chat.completions.create(
                                model="llama-3.1-8b-instant",
                                max_tokens=1000,
                                messages=[{"role": "user", "content": _synth_prompt}],
                            ),
                            timeout=45.0,
                        )
                        synthesis = (_b3_resp.choices[0].message.content or "").strip()
                        print(f"[3brain] Groq synthesis: {len(synthesis)} chars")
                    except Exception as _b3e:
                        print(f"[3brain] Groq synthesis failed: {_b3e}")
                # Brain 3b: Gemini fallback
                _gemini_key_b3 = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
                if _gemini_key_b3 and not synthesis:
                    try:
                        from google import genai as _gai_b3
                        _gc_b3 = _gai_b3.Client(api_key=_gemini_key_b3)
                        _b3_gem = await asyncio.wait_for(
                            _gc_b3.aio.models.generate_content(
                                model="gemini-2.5-flash",
                                contents=_synth_prompt,
                                config={"max_output_tokens": 1000},
                            ),
                            timeout=45.0,
                        )
                        synthesis = (_b3_gem.text or "").strip()
                        print(f"[3brain] Gemini synthesis: {len(synthesis)} chars")
                    except Exception as _b3e2:
                        print(f"[3brain] Gemini synthesis failed: {_b3e2}")
                # Brain 3c: Claude bonus
                if not synthesis:
                    try:
                        import anthropic as _anthropic_3b
                        _sonnet_3b = _anthropic_3b.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
                        _sonnet_3b_resp = await asyncio.wait_for(
                            _sonnet_3b.messages.create(
                                model="claude-sonnet-4-6",
                                max_tokens=1000,
                                messages=[{"role": "user", "content": _synth_prompt}],
                            ),
                            timeout=45.0,
                        )
                        synthesis = (_sonnet_3b_resp.content[0].text if _sonnet_3b_resp.content else "").strip()
                        print(f"[3brain] Claude synthesis: {len(synthesis)} chars")
                    except Exception as _ce3:
                        print(f"[3brain] Claude synthesis failed: {_ce3}")

                # Extract LESSON: lines and write to lessons file
                import re as _re_3b
                lesson_lines = _re_3b.findall(r"LESSON:\s*(.+)", synthesis)
                saved = 0
                for lesson in lesson_lines:
                    lesson = lesson.strip()
                    if lesson and len(lesson) > 20:
                        _append_lesson(f"[3brain] {lesson}")
                        saved += 1

                # Log the Jarvis verdict to activity feed
                verdict_match = _re_3b.search(r"JARVIS VERDICT[:\s—-]+(.+?)(?:\n\n|$)", synthesis, _re_3b.DOTALL)
                verdict = verdict_match.group(1).strip()[:300] if verdict_match else synthesis[-300:]

                if pos_mgr is not None:
                    pos_mgr._log_activity(  # type: ignore[union-attr]
                        "info",
                        f"[3brain] {len(sells)} trades reviewed | WR {win_rate:.0f}% | "
                        f"{saved} lessons saved | Verdict: {verdict[:200]}"
                    )
                print(f"[3brain] Review complete: {saved} new lessons saved to {_LESSONS_FILE}")
                print(f"[3brain] Jarvis verdict: {verdict[:200]}")

            except Exception as _se:
                print(f"[3brain] Sonnet synthesis failed: {_se}", file=sys.stderr)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[3brain] Unexpected error: {exc}", file=sys.stderr)

        await asyncio.sleep(REVIEW_INTERVAL)


async def bc_social_sniper_loop(runtime: AgentRuntime, graduation_queue: asyncio.Queue) -> None:
    """Strategy A2: Bonding curve early entry — buy at token CREATION when creator supplied socials.

    This is the early-entry path that catches mega-runners like 8616Po7A at 09:45
    (launch) instead of 10:24 (after a 36-minute pump and recovery).

    Filter pipeline (ALL must pass):
      1. Website present AND (Twitter OR Telegram present)
      2. Description >= 20 chars (shows creator effort)
      3. No obvious rug words in name/symbol/description
      4. Standard gates: circuit breaker, cooldowns, position slots
      5. Token safety: mint + freeze authority burned
      6. Bonding curve fill 5–65% (sweet spot — not dead, not over-sniped)
      7. Jarvis SAFE/RUG assessment (4s timeout — optimistic on timeout)

    On graduation: position_manager auto-transitions dex='pump_fun' → 'pumpswap'.
    SL/TP/trailing stop logic applies identically once the token is on PumpSwap.

    Enabled via STRATEGY_A2_ENABLED=true in .env.
    Position size: BUY_SOL_BC env var (default 0.06 SOL — same as other strategies).
    """
    from elizaos.types import ServiceTypeRegistry

    PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    BUY_SOL_BC = float(os.getenv("BUY_SOL_BC", "0.08"))
    TOKEN_DECIMALS = 6

    _bc_evaluated: set[str] = set()

    _RUG_WORDS: set[str] = {
        "test", "scam", "rug", "honeypot", "ponzi", "fake", "hack", "exploit",
        "drain", "steal", "phish", "malware",
        "uxento",  # Uxento bot-launched tokens — instant dump pattern
        "rizzbot",  # Rizzbot — bought 2026-03-16, lost -15.2% — factory dump token
    }

    print(
        f"[a2-bc] Strategy A2 (bonding curve social sniper) ACTIVE — "
        f"{'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'} | "
        f"buy_sol={BUY_SOL_BC}"
    )

    async def _on_token_launch(launch: dict) -> None:
        asyncio.create_task(_process_bc_launch(launch))

    async def _process_bc_launch(launch: dict) -> None:
        # Respect live_config toggle — Jarvis can pause/resume A2 at runtime
        from elizaos.plugins.solana import live_config as _lc_a2_gate
        if not _lc_a2_gate.get("strategy_a2_enabled", True):
            return

        mint = launch.get("mint", "")
        if not mint or not mint.lower().endswith("pump"):
            return
        if mint in _bc_evaluated:
            return

        # Trading hours gate DISABLED — running 24/7 per user instruction 2026-03-16

        # ── GATE 0.5: Token age gate — skip old bonding curves ────────────────
        # Jarvis insight (2026-03-20): only trade tokens created < a2_max_age_secs ago.
        # Old bonding curves are already pumped or dumped — we'd be buying exit liquidity.
        _token_created = launch.get("created_timestamp") or launch.get("created_at") or 0
        if _token_created > 0:
            from elizaos.plugins.solana import live_config as _lc_age
            _max_age = int(_lc_age.get("a2_max_age_secs", 300))
            _token_age = time.time() - _token_created
            if _token_age > _max_age:
                return  # skip quietly — old token

        # ── GATE 1: Social metadata check (from pump.fun API at creation time) ─
        pf_website  = (launch.get("pf_website") or "").strip()
        pf_twitter  = (launch.get("pf_twitter") or "").strip()
        pf_telegram = (launch.get("pf_telegram") or "").strip()
        pf_name     = (launch.get("pf_name") or launch.get("name") or "").strip()
        pf_symbol   = (launch.get("pf_symbol") or launch.get("symbol") or "").strip()
        pf_desc     = (launch.get("pf_description") or "").strip()

        # ── GATE 1: Social presence filter ────────────────────────────────────────
        # Policy (updated 2026-03-20):
        #   Website is OPTIONAL — twitter + telegram together are sufficient community signal.
        #   Acceptable social profiles (live_config-tunable):
        #   A) FULL PROJECT:    website (real domain) + twitter or telegram
        #   B) VIRAL MEME:      real twitter + description ≥ 30 chars
        #   C) TELEGRAM COMM:   telegram + description ≥ 20 chars
        #   D) COMMUNITY DUO:   real twitter AND telegram (website optional) — NEW 2026-03-20
        #      → website not mandatory once both community channels are present
        #      → GPT-4o + Eliza safety checks still run on all tokens (secondary protection)
        #
        _SOCIAL_PLATFORM_DOMAINS = (
            "tiktok.com", "instagram.com", "facebook.com", "youtube.com",
            "youtu.be", "reddit.com", "twitch.tv", "discord.gg", "discord.com",
            "linktr.ee",
        )
        _ws_lower = pf_website.lower() if pf_website else ""

        # Twitter is real if it's a specific account handle (not a bare domain, status tweet, or x.com/i/* link)
        _twitter_is_real = (
            bool(pf_twitter)
            and pf_twitter.rstrip("/").lower() not in (
                "https://twitter.com", "https://x.com", "http://twitter.com", "http://x.com"
            )
            and "/status/" not in pf_twitter.lower()
            and "/i/trending/" not in pf_twitter.lower()
        )

        # Website is real if it's a genuine project domain (not a social platform or x.com internal link)
        _website_is_real = (
            bool(pf_website)
            and not any(d in _ws_lower for d in _SOCIAL_PLATFORM_DOMAINS)
            and "/status/" not in _ws_lower
            and "/i/trending/" not in _ws_lower
            and "/i/communities/" not in _ws_lower   # x.com communities exploit — blocked 2026-03-16
            and "x.com/i/" not in _ws_lower
            and "twitter.com/i/" not in _ws_lower
        )

        _has_telegram = bool(pf_telegram)

        from elizaos.plugins.solana import live_config as _lc_gate1
        _req_twitter  = bool(_lc_gate1.get("a2_require_twitter",  True))
        _req_telegram = bool(_lc_gate1.get("a2_require_telegram", True))
        _req_website  = bool(_lc_gate1.get("a2_require_website",  False))

        # Path A: full project (website + twitter or telegram)
        _path_a = _website_is_real and (_twitter_is_real or _has_telegram)
        # Path B: viral meme (real twitter + meaningful description)
        _path_b = _twitter_is_real and len(pf_desc) >= 30
        # Path C: telegram community + description
        _path_c = _has_telegram and len(pf_desc) >= 20
        # Path D: community duo — twitter + telegram, website optional (NEW 2026-03-20)
        #   If a2_require_website is True, this path is bypassed and website stays mandatory.
        _path_d = (not _req_website) and (_twitter_is_real if _req_twitter else True) and (_has_telegram if _req_telegram else True)

        if not (_path_a or _path_b or _path_c or _path_d):
            return  # insufficient social presence — skip quietly

        # NOTE: do NOT add to _bc_evaluated here.
        # Tokens are detected at creation (fill ~0%) and re-fired on every trade event.
        # Adding to _bc_evaluated at the social gate would permanently block a token seen
        # at 0% fill from being re-evaluated when it later reaches the 55-68% window.
        # We only mark as evaluated when we commit to the expensive API calls (after fill check).

        # ── GATE 2: Non-trivial description ───────────────────────────────────
        # Path A tokens need ≥ 10 chars; Path B/C already enforce longer descriptions above
        if len(pf_desc) < 10:
            return  # skip quietly — token may just have no desc yet

        # ── GATE 3: Rug word filter — hard reject (mark evaluated immediately) ──
        combined = (pf_name + " " + pf_symbol + " " + pf_desc).lower()
        if any(w in combined for w in _RUG_WORDS):
            _bc_evaluated.add(mint)  # name/symbol won't change — permanent reject
            print(f"[a2-bc] x {mint[:8]}... rug-word filter: {pf_name[:25]}")
            return

        # ── GATE 4: Standard bot gates ─────────────────────────────────────────
        pos_mgr    = runtime.get_service("position_manager")
        wallet_svc = runtime.get_service("wallet")
        pump_svc   = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
        if pos_mgr is None or wallet_svc is None or pump_svc is None:
            return

        if mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
            return
        if mint in _all_exit_times or mint in _loss_exit_times:
            return
        if mint in _buying_mints or mint in pos_mgr.positions:  # type: ignore[union-attr]
            return
        if time.time() < _rug_streak_pause_until:
            return

        try:
            bal        = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
            _sm_a2bc   = runtime.get_service("social_monitor")
            _is_prem_a2bc, _prem_buy_a2bc = _check_grok_premium(mint, _sm_a2bc, bal)
            from elizaos.plugins.solana import live_config as _lc_a2bc_sz
            _a2bc_size = _lc_a2bc_sz.get("a2_buy_sol", BUY_SOL_BC)
            actual_buy = _prem_buy_a2bc if _is_prem_a2bc else _compound_buy_sol(bal, _a2bc_size)
            if _is_prem_a2bc:
                print(f"[grok-premium] {mint[:8]}... score≥8 → {actual_buy} SOL position, TP+50%, stall exit @+25%")
            allowed, reason = pos_mgr.check_trade_allowed(actual_buy, bal)  # type: ignore[union-attr]
            if not allowed:
                print(f"[a2-bc] x {mint[:8]}... trade gated: {reason}")
                return
            _sl_gap = time.time() - _last_sl_exit_time
            if _sl_gap < _get_post_sl_cooldown():
                print(f"[a2-bc] x {mint[:8]}... post-SL cooldown — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                return
        except Exception:
            return

        # Slot limit: 1 BC position at a time — focused 0.25 SOL pump.fun test session
        bc_count = sum(
            1 for p in pos_mgr.positions.values()  # type: ignore[union-attr]
            if getattr(p, "dex", "") == "pump_fun"
        )
        if bc_count >= 1 or len(pos_mgr.positions) >= 1:  # type: ignore[union-attr]
            return

        # ── GATE 4.5: Creator reputation — early blacklist / proven fast-track ─
        _creator_wallet_early = (launch.get("creator_wallet") or "").strip()
        _is_proven_creator = False
        _proven_buy_override = None   # SOL override for proven creators
        if _creator_wallet_early:
            _dev_rep_early = get_reputation()
            _cr_status_early, _cr_reason_early = _dev_rep_early.check(_creator_wallet_early)
            if _cr_status_early == "blacklisted":
                print(f"[a2-bc] x {mint[:8]}... BLACKLISTED CREATOR {_creator_wallet_early[:12]}... — skip")
                _bc_evaluated.add(mint)  # permanent block
                return
            if _cr_status_early == "proven":
                _is_proven_creator = True
                # PROVEN creator → 75% of wallet balance
                from elizaos.plugins.solana import live_config as _lc_proven
                _proven_pct = _lc_proven.get("proven_creator_pct", 0.75)
                try:
                    _bal_proven = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
                    _proven_buy_override = max(0.01, _bal_proven * _proven_pct)
                except Exception:
                    _proven_buy_override = None
                print(
                    f"[a2-bc] ⭐ PROVEN CREATOR {_creator_wallet_early[:12]}... "
                    f"({_cr_reason_early[:60]}) — "
                    f"boosting to {_proven_buy_override:.3f} SOL ({_proven_pct*100:.0f}% wallet) for {pf_name}"
                )

        # ── GATES 5 + 6 + 6.5: Run in parallel to minimise pipeline latency ────
        # Previously sequential (~3.5s total). Now concurrent (~1.5s = longest single call).
        # Gate 5 = token safety, Gate 6 = bonding curve, Gate 6.5 = holder check.
        _rpc_client = getattr(wallet_svc, "rpc", None)

        async def _fetch_safety():
            return await pos_mgr.check_token_safety(mint)  # type: ignore[union-attr]

        async def _fetch_bc():
            try:
                return await pump_svc.get_bonding_curve(mint)  # type: ignore[attr-defined]
            except Exception:
                return None

        async def _fetch_holders():
            if _rpc_client is None:
                return []
            try:
                return await asyncio.wait_for(
                    _rpc_client.get_token_largest_accounts(mint), timeout=4.0
                )
            except Exception:
                return []

        _safety_res, _bc_res, _holder_res = await asyncio.gather(
            _fetch_safety(), _fetch_bc(), _fetch_holders()
        )

        # ── Gate 5 result: token safety ───────────────────────────────────────
        safe, safety_reason = _safety_res if isinstance(_safety_res, tuple) else (False, "error")
        if not safe:
            _bc_evaluated.add(mint)  # contract flags won't change — permanent reject
            print(f"[a2-bc] x {mint[:8]}... UNSAFE: {safety_reason}")
            return

        # ── Gate 6 result: bonding curve state ───────────────────────────────
        bc = _bc_res
        if not bc:
            if graduation_queue is not None:
                try:
                    graduation_queue.put_nowait(mint)
                    print(f"[a2-bc] {mint[:8]}... already graduated → Strategy B queue")
                except asyncio.QueueFull:
                    pass
            return
        if bc.get("complete", False):
            return

        progress  = bc.get("progress_pct", 0.0)
        price_sol = bc.get("price_sol", 0.0)
        real_sol  = bc.get("real_sol", 0.0)

        from elizaos.plugins.solana import live_config as _lc_a2_mom
        _a2_min_sol = _lc_a2_mom.get("a2_min_real_sol", 1.6)
        if real_sol < _a2_min_sol:
            # Log all available data at rejection so we can tune the threshold later
            _rej_holders = len([a for a in (_holder_res or []) if float(a.get("uiAmount") or 0) > 0])
            _rej_name    = launch.get("symbol") or launch.get("name") or "?"
            _rej_age     = int(time.time() - launch.get("created_timestamp", time.time()))
            print(
                f"[a2-bc] x {mint[:8]}... REJECT real_sol={real_sol:.3f}/{_a2_min_sol:.1f} SOL"
                f" | fill={progress:.1f}% | holders={_rej_holders} | age={_rej_age}s | {_rej_name}"
            )
            try:
                from elizaos.plugins.solana import rejection_tracker as _rt_a2
                _rt_a2.record(mint, "real_sol_low", filter_name="a2_min_real_sol",
                              filter_value=round(real_sol, 4), threshold=float(_a2_min_sol),
                              strategy="a2", score=None,
                              extra={"fill": round(progress, 1), "holders": _rej_holders,
                                     "age_secs": _rej_age, "mc": launch.get("usd_market_cap")})
            except Exception:
                pass
            return

        # ── A2 liquidity filter (real_sol → USD proxy) ─────────────────────────
        # Jarvis finding: winners had $59K-$162K equivalent; losers $3K-$3.7K.
        # Proxy: real_sol * SOL_USD_PRICE (no DEX pair exists yet for BC tokens).
        from elizaos.plugins.solana import live_config as _lc_a2_liq
        _a2_min_liq = float(_lc_a2_liq.get("a2_min_liq_usd", 0) or 0)
        if _a2_min_liq > 0:
            _sol_usd = float(os.getenv("SOL_PRICE_USD", "140"))
            _real_sol_usd = real_sol * _sol_usd
            if _real_sol_usd < _a2_min_liq:
                print(
                    f"[a2-bc] x {mint[:8]}... liq_proxy=${_real_sol_usd:,.0f} "
                    f"(real_sol={real_sol:.3f} × ${_sol_usd:.0f}) < min ${_a2_min_liq:,.0f} — skip"
                )
                try:
                    from elizaos.plugins.solana import rejection_tracker as _rt_a2liq
                    _rt_a2liq.record(mint, "liq_too_low", filter_name="a2_min_liq_usd",
                                     filter_value=round(_real_sol_usd, 0), threshold=_a2_min_liq,
                                     strategy="a2", extra={"real_sol": round(real_sol, 4),
                                                           "fill": round(progress, 1)})
                except Exception:
                    pass
                return

        from elizaos.plugins.solana import live_config as _lc_a2
        _live_fill_cap = _lc_a2.get("fill_cap_pct", FILL_CAP_PCT)
        if progress < 55.0 or progress > _live_fill_cap:
            print(f"[a2-bc] x {mint[:8]}... fill={progress:.1f}% outside 55-{_live_fill_cap:.0f}% Ghost Rider window — skip")
            return

        # Fill window confirmed — commit to expensive API calls; mark evaluated so we
        # don't repeat Rugcheck/holder/Grok/Jarvis for this same in-window token.
        _bc_evaluated.add(mint)

        # ── Gate 6.5 result: holder traction + whale check ───────────────────
        holder_count = 0
        top_holder_pct = 100.0
        _nonzero = [a for a in (_holder_res or []) if float(a.get("uiAmount") or 0) > 0]
        holder_count = len(_nonzero)
        if len(_nonzero) >= 3:
            real_buyers = _nonzero[1:]  # exclude bonding curve (always account[0])
            top_ui = float(real_buyers[0].get("uiAmount") or 0)
            total_ui = sum(float(a.get("uiAmount") or 0) for a in real_buyers)
            top_holder_pct = (top_ui / total_ui * 100) if total_ui > 0 else 100.0

        # Record holder snapshot for growth rate tracking (before any gate checks)
        try:
            from elizaos.plugins.solana import holder_tracker as _ht
            if holder_count > 0:
                _ht.record_snapshot(mint, holder_count)
        except Exception:
            pass

        from elizaos.plugins.solana import live_config as _lc_a2_filt
        _min_holders   = int(_lc_a2_filt.get("a2_min_holders", 10))
        _max_whale_pct = float(_lc_a2_filt.get("a2_max_whale_pct", 40.0))
        _watch_thresh  = max(1, _min_holders - 3)

        if holder_count > 0 and holder_count < _min_holders:
            if holder_count >= _watch_thresh and mint not in _holder_watch_pending:
                _holder_watch_pending[mint] = {
                    "launch": launch,
                    "progress": progress,
                    "price_sol": price_sol,
                    "first_seen": time.time(),
                    "holders_at_detect": holder_count,
                }
                print(f"[a2-bc] {mint[:8]}... {holder_count} holders — WATCHING (need ≥{_min_holders}, check every 20s)")
            else:
                print(f"[a2-bc] x {mint[:8]}... only {holder_count} holders (need ≥{_min_holders}) — no traction, skip")
                try:
                    from elizaos.plugins.solana import rejection_tracker as _rt_h
                    _rt_h.record(mint, "holders_low", filter_name="a2_min_holders",
                                 filter_value=holder_count, threshold=_min_holders, strategy="a2",
                                 extra={"fill": round(progress, 1), "mc": launch.get("usd_market_cap")})
                except Exception:
                    pass
            return

        if top_holder_pct > _max_whale_pct:
            print(f"[a2-bc] x {mint[:8]}... top buyer holds {top_holder_pct:.0f}% of buyer supply — whale concentration, skip")
            try:
                from elizaos.plugins.solana import rejection_tracker as _rt_w
                _rt_w.record(mint, "whale_concentration", filter_name="a2_max_whale_pct",
                             filter_value=round(top_holder_pct, 1), threshold=_max_whale_pct, strategy="a2",
                             extra={"holders": holder_count, "fill": round(progress, 1)})
            except Exception:
                pass
            return

        # ── GATE: Holder growth rate (0 = disabled) ──────────────────────────────
        _min_growth = float(_lc_a2_filt.get("a2_min_holder_growth_rate", 0.0))
        if _min_growth > 0 and holder_count > 0:
            try:
                from elizaos.plugins.solana import holder_tracker as _ht_gate
                _growth_window = float(_lc_a2_filt.get("a2_holder_growth_window_secs", 300.0))
                _growth_rate = _ht_gate.get_growth_rate(mint, window_secs=_growth_window)
                _change = _ht_gate.get_holder_change(mint, window_secs=_growth_window)
                if _growth_rate is not None and _growth_rate < _min_growth:
                    _old_h, _new_h = (_change if _change else (holder_count, holder_count))
                    print(
                        f"[a2-bc] x {mint[:8]}... holder growth {_growth_rate:.1f}/min "
                        f"({_old_h}→{_new_h} in {_growth_window/60:.0f}m) "
                        f"< min {_min_growth:.1f}/min — stagnant, skip"
                    )
                    return
                elif _growth_rate is not None:
                    print(
                        f"[a2-bc] ✓ {mint[:8]}... holder growth {_growth_rate:.1f}/min "
                        f"≥ {_min_growth:.1f}/min — active accumulation"
                    )
            except Exception:
                pass

        # ── GATE: Market cap filter (Jarvis lesson: tiny MC < $50K = traps) ─────────
        _min_mc = float(_lc_a2_filt.get("a2_min_mc_usd", 0))
        if _min_mc > 0:
            _token_mc = float(launch.get("usd_market_cap") or 0)
            if _token_mc > 0 and _token_mc < _min_mc:
                print(f"[a2-bc] x {mint[:8]}... MC ${_token_mc:,.0f} < min ${_min_mc:,.0f} — tiny cap trap, skip")
                try:
                    from elizaos.plugins.solana import rejection_tracker as _rt_mc
                    _rt_mc.record(mint, "mc_too_low", filter_name="a2_min_mc_usd",
                                  filter_value=_token_mc, threshold=_min_mc, strategy="a2",
                                  extra={"fill": round(progress, 1), "holders": holder_count})
                except Exception:
                    pass
                return
            elif _token_mc == 0:
                print(f"[a2-bc] {mint[:8]}... MC data unavailable, skipping MC filter")

        # ── Smart money detection: resolve token account owners → check watchlist ──
        # Runs BEFORE Jarvis gate so smart money presence can influence the verdict.
        # Non-blocking — if RPC call fails, proceeds without smart money data.
        _holder_owner_wallets: list[str] = []
        _smart_money_hits: list[str] = []
        _token_acct_addrs = [a.get("address", "") for a in _nonzero if a.get("address")]
        if _token_acct_addrs:
            try:
                from elizaos.plugins.solana.smart_money_tracker import (
                    get_smart_money_set as _get_sm_set,
                    resolve_token_account_owners as _resolve_owners,
                )
                _sm_set = _get_sm_set()
                # Only pay the RPC cost if we have smart money wallets to check against
                _rpc_svc = runtime.get_service("lp_pool")
                _rpc_obj = getattr(_rpc_svc, "_rpc", None) if _rpc_svc else None
                if _rpc_obj:
                    _owner_map = await asyncio.wait_for(
                        _resolve_owners(_token_acct_addrs, _rpc_obj), timeout=5.0
                    )
                    _holder_owner_wallets = list(_owner_map.values())
                    if _sm_set:
                        _smart_money_hits = [w for w in _holder_owner_wallets if w in _sm_set]
                        if _smart_money_hits:
                            print(
                                f"[a2-bc] 💰 SMART MONEY detected in {mint[:8]}...: "
                                + ", ".join(h[:8] + "..." for h in _smart_money_hits)
                            )
                            # Push alert to Jarvis chat
                            try:
                                from elizaos.plugins.solana.dashboard_api import push_system_alert as _push_sm
                                _push_sm(
                                    f"🧠 SMART MONEY SIGNAL: {pf_name or mint[:8]} — "
                                    + f"{len(_smart_money_hits)} known smart wallet(s) already holding! "
                                    + "High conviction entry incoming."
                                )
                            except Exception:
                                pass
            except Exception:
                pass

        # ── GATE 7a: Mandatory AI gate (Grok X rug-check + GPT-4o risk score) ─────
        _sm_a2 = runtime.get_service("social_monitor")
        if _sm_a2 is not None:
            _gate_data_a2 = {
                "dex": "pump_fun",
                "liq_usd": real_sol * float(os.getenv("SOL_PRICE_USD", "140")),
                "age_mins": 0,  # BC tokens are brand new at entry
                "price_change_m5": 0,
                "price_change_h1": None,
                "vol_h1_usd": 0,
                "score": 0,
                "has_socials": bool(pf_website or pf_twitter or pf_telegram),
                "is_bonding_curve": True,  # Skip GPT liquidity check — no DEX pair exists yet
            }
            # require_buzz unless token has Twitter or Telegram — real community channels
            # signal a legitimate project; website-only or no socials = cheap to fake,
            # needs X presence to have any pump catalyst.
            _has_community = bool(pf_twitter or pf_telegram)
            _gate_allow_a2, _gate_reason_a2 = await _sm_a2.pre_trade_ai_gate(  # type: ignore[union-attr]
                mint, pf_name, pf_symbol, _gate_data_a2,
                require_buzz=not _has_community,
            )
            if not _gate_allow_a2:
                print(f"[a2-bc] x {mint[:8]}... AI gate BLOCKED — {_gate_reason_a2}")
                return

        # ── GATE 7a.5: GPT-4o web search — live internet scam/rug check ──────────
        # Search the live web for reports of this token being a scam, rug, or honeypot.
        # Runs in parallel with other gates but fires AFTER Grok gate (only for passing tokens).
        _web_safe, _web_reason = await _web_reputation_check(pf_name, pf_symbol, mint)
        if not _web_safe:
            print(f"[a2-bc] x {mint[:8]}... WEB-BLOCKED — {_web_reason}")
            return
        elif "skipped" not in _web_reason.lower():
            print(f"[a2-bc] web-check OK: {mint[:8]}... — {_web_reason[:80]}")

        # ── GATE 7b: Jarvis pre-buy assessment ─────────────────────────────────────
        # Jarvis is our narrative/quality filter. Blocks WEAK + RUG. SAFE + STRONG both proceed.
        # Data: biggest wins (Gkd6PP5s +105%, 4vZiHhYD +65%, CHS2L1Yr +65%) were all SAFE.
        # STRONG-only killed trade flow without improving results — SAFE is allowed.
        # Current strategy: 0.020 SOL, TP1 +40%, TP1.5 +80%, TP2 +180%, TP3 +400%, SL -12%.
        eliza_ok = True
        eliza_strong = False

        # ── PROVEN creator shortcut: trust the creator, skip Eliza gate ──────
        if _is_proven_creator:
            eliza_strong = True  # treat as STRONG for score / TP purposes
            print(f"[a2-bc] ⭐ {mint[:8]}... PROVEN creator — skipping Eliza gate, auto-STRONG")
        else:
          try:
            from elizaos.types.model import ModelType, GenerateTextOptions

            _mkt = _get_market_context(pos_mgr)
            _lessons = _load_recent_lessons(3)

            holder_ctx = (
                f"{holder_count} distinct holder accounts (top buyer: {top_holder_pct:.0f}% of buyer supply)"
                if holder_count > 0 else "holder count unavailable"
            )

            _creator_wallet = (launch.get("creator_wallet") or "").strip()
            _dev_rep = get_reputation()
            _creator_status, _creator_reason = _dev_rep.check(_creator_wallet) if _creator_wallet else ("unknown", "")
            _creator_ctx = ""
            if _creator_wallet:
                if _creator_status == "blacklisted":
                    _creator_ctx = f"Creator wallet {_creator_wallet[:12]}...: BLACKLISTED — {_creator_reason}\n"
                elif _creator_status == "proven":
                    _creator_ctx = f"Creator wallet {_creator_wallet[:12]}...: ⭐ PROVEN — {_creator_reason}\n"
                else:
                    _creator_ctx = f"Creator wallet {_creator_wallet[:12]}...: {_creator_status}\n"

            eliza_prompt = (
                f"{_lessons}"
                f"{_mkt}"
                f"You are an elite pump.fun sniper with the mindset of a quantitative trader and the instincts of a veteran meme coin degen. "
                f"You have traded thousands of bonding curve launches and you understand exactly what separates the 10x runners from the instant rugs.\n\n"
                f"=== YOUR EDGE ===\n"
                f"You are looking for ONE thing: a token that can move +40% within 30 minutes of launch. "
                f"That's all. Not a moonshot. Not a 10x. Just a clean +40% move so we lock profit at TP1, "
                f"then ride the rest with house money. Our SL is -12% — so the risk/reward at TP1 is 3.3:1. "
                f"You only need to be right 16% of the time to break even (R:R = 30%/9% ≈ 3.3:1). Be selective but not paranoid.\n\n"
                f"=== WHAT MAKES A TOKEN GO +40% IN 30 MINUTES ===\n"
                f"1. NARRATIVE VELOCITY: It taps into something people are already talking about RIGHT NOW. "
                f"Elon tweet, viral meme, breaking news, trending crypto drama, AI hype, celebrity moment. "
                f"The story needs to be shareable — someone sees the name and immediately wants to tell their group chat.\n"
                f"2. NEAR-GRADUATION ENTRY: Ghost Rider targets 55-80%. Check if Grok social brain has flagged this token. We're at {progress:.1f}% BC fill. The main wave is IN PROGRESS "
                f"(55%+ fill = real demand proven) and graduation catalyst is close. Above 70% = exit liquidity risk — early snipers who entered at 5-20% are already up 3-4x and looking to dump.\n"
                f"3. ORGANIC COMMUNITY: Real buyers, not bots. "
                f"Telegram with actual name (not just 't.me') = people are coordinating. Website with real domain = someone invested time.\n"
                f"4. HOLDER SPREAD: {holder_ctx}. More holders = more distributed = harder to rug. "
                f"One whale controlling >40% of buyer supply = one sell order wipes us out.\n"
                f"5. REAL BUYING PRESSURE: {real_sol:.2f} SOL deposited by actual buyers (≥1.5 SOL required). "
                f"This is real money, not virtual. It shows genuine demand exists right now.\n\n"
                f"=== PATTERN RECOGNITION — WHAT TO REJECT ===\n"
                f"INSTANT RUG PATTERNS (→ RUG verdict):\n"
                f"• Website is x.com/i/communities/*, x.com/i/trending/*, or any x.com/i/* path — rug operators exploit this as a fake social signal, these are NOT real project websites\n"
                f"• Twitter field is a tweet status URL (x.com/*/status/*) — someone else's tweet, not a real project account\n"
                f"• Token name/symbol is obviously disposable: 'test', 'scam', 'rug', 'drug', 'cure for cancer'\n"
                f"• Creator wallet flagged/blacklisted\n"
                f"• Zero social proof despite claiming to have socials\n\n"
                f"LOW QUALITY PATTERNS (→ WEAK verdict):\n"
                f"• Generic AI agent / crypto bot boilerplate description — hundreds of these launch daily, zero stand out\n"
                f"• Fill already >63% — early snipers (entered at 5-20%) are up 3-4x and actively looking to exit on new buyers like us\n"
                f"• Description reads like it was written by GPT in 10 seconds with no specific meme hook\n"
                f"• No Twitter AND no Telegram — no community = no buyers = slow bleed to SL\n"
                f"• Name/symbol has no memetic value — you couldn't explain it to a friend in 5 words\n\n"
                f"SOLID OPPORTUNITY PATTERNS (→ SAFE or STRONG verdict):\n"
                f"• Clear viral hook: references a current meme, event, or cultural moment you know is trending\n"
                f"• Has ALL THREE: website (real domain, not x.com/*) + Twitter (real account, not tweet link) + Telegram — full social stack = serious launch\n"
                f"• 15+ holders already at this fill level = strong early organic demand\n"
                f"• Animal mascot / simple funny concept with a clean ticker people will want to post\n"
                f"• Description has personality and specific details — not copy-paste\n\n"
                f"=== TOKEN TO ASSESS ===\n"
                f"Name: {pf_name} | Symbol: ${pf_symbol}\n"
                f"Description: {pf_desc[:400]}\n"
                f"Website: {pf_website or 'none'}\n"
                f"Twitter: {pf_twitter or 'none'} | Telegram: {pf_telegram or 'none'}\n"
                f"{_creator_ctx}"
                f"\n=== ON-CHAIN SNAPSHOT ===\n"
                f"BC fill: {progress:.1f}% | Real SOL in: {real_sol:.2f} | Holders: {holder_ctx}\n\n"
                f"=== VERDICT (one word only) ===\n"
                f"  STRONG = strong viral narrative + full socials + healthy holders + sweet-spot fill — high conviction, fast +40% very plausible\n"
                f"  SAFE   = plausible narrative, decent socials, entry viable — acceptable risk/reward, will buy\n"
                f"  WEAK   = generic/boilerplate, already pumped, thin socials, no real narrative — skip\n"
                f"  RUG    = rug pattern detected, scam signals, blacklisted creator, zero real project — hard skip\n\n"
                f"Respond with ONE WORD: STRONG, SAFE, WEAK, or RUG."
            )
            verdict_result = await asyncio.wait_for(
                runtime.generate_text(
                    eliza_prompt,
                    GenerateTextOptions(model_type=ModelType.TEXT_SMALL),
                ),
                timeout=6.0,
            )
            verdict_upper = (verdict_result.text if verdict_result else "").strip().upper()[:80]
            if "RUG" in verdict_upper:
                print(f"[a2-bc] x {mint[:8]}... Eliza: RUG — {verdict_upper}")
                eliza_ok = False
            elif "WEAK" in verdict_upper:
                print(f"[a2-bc] x {mint[:8]}... Eliza: WEAK — skip")
                eliza_ok = False
            elif "STRONG" in verdict_upper:
                eliza_strong = True
                print(f"[a2-bc] Eliza: {mint[:8]}... STRONG — high conviction, proceeding")
            else:
                # SAFE is allowed — our biggest wins (Gkd6PP5s +105%, 4vZiHhYD +65%) were SAFE
                # STRONG is no more reliable than SAFE in practice; gate filters WEAK/RUG only
                print(f"[a2-bc] Eliza: {mint[:8]}... SAFE — proceeding")
          except asyncio.TimeoutError:
            print(f"[a2-bc] Eliza timeout {mint[:8]}... — proceeding optimistically")
          except Exception as exc:
            print(f"[a2-bc] Eliza error {mint[:8]}...: {exc} — proceeding")

        if not eliza_ok:
            return

        # ── GATE 7c: Minimum score gate ────────────────────────────────────────────
        # Jarvis (2026-03-20): previous BC trades all scored 12 (= STRONG) yet most were losers.
        # a2_min_score ≥ 50 → require STRONG verdict (eliza_strong=True), block SAFE entries.
        # Proven creator tokens always bypass this gate (they already have auto-STRONG).
        if not _is_proven_creator:
            from elizaos.plugins.solana import live_config as _lc_score_gate
            _min_score_cfg = int(_lc_score_gate.get("a2_min_score", 0))
            if _min_score_cfg >= 50 and not eliza_strong:
                print(f"[a2-bc] x {mint[:8]}... score gate: SAFE verdict blocked — a2_min_score={_min_score_cfg} requires STRONG")
                return

        # ── GATE 8: A2 stability wait ──────────────────────────────────────────────
        # Before buying, wait 25s and re-check bonding curve.
        # PROVEN creators: reduced to 5s — we trust the creator, speed is the edge.
        # Rugs happen 12-108 seconds after buy (ClawPad 12s, SpiralTerminal 16s, Lucid 108s).
        # A stability wait catches tokens where the creator pumped to our fill window,
        # then immediately dumps on the first bot buyers. Data: all 3 rugs would have failed this.
        # Only applied if position slots still open after wait (might have been taken).
        A2_STABILITY_WAIT = 5 if _is_proven_creator else 25
        print(f"[a2-bc] {mint[:8]}... stability check: waiting {A2_STABILITY_WAIT}s (fill={progress:.1f}% price={price_sol:.2e}{'  ⭐PROVEN' if _is_proven_creator else ''})", flush=True)
        await asyncio.sleep(A2_STABILITY_WAIT)

        # Re-check gates that can change during the wait
        if mint in _buying_mints or mint in pos_mgr.positions or mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
            return
        if time.time() < _rug_streak_pause_until:
            return
        if len(pos_mgr.positions) >= 1:  # type: ignore[union-attr]
            return

        try:
            bc_recheck = await pump_svc.get_bonding_curve(mint)  # type: ignore[attr-defined]
        except Exception:
            bc_recheck = None

        if bc_recheck is None:
            print(f"[a2-bc] x {mint[:8]}... graduated during stability wait — skip")
            return
        if bc_recheck.get("complete", False):
            print(f"[a2-bc] x {mint[:8]}... graduated during stability wait — skip")
            return

        new_fill  = bc_recheck.get("progress_pct", progress)
        new_price = bc_recheck.get("price_sol", price_sol)

        # Fill dropped >1pp: sell pressure hit during our wait — rug or early exits
        if progress - new_fill > 1.0:
            print(f"[a2-bc] x {mint[:8]}... fill {progress:.1f}%→{new_fill:.1f}% (−{progress-new_fill:.1f}pp) in {A2_STABILITY_WAIT}s — sell pressure, skip")
            return
        # Fill pushed above cap: snipers piled in during wait — over-sniped
        if new_fill > FILL_CAP_PCT:
            print(f"[a2-bc] x {mint[:8]}... fill rose {progress:.1f}%→{new_fill:.1f}% (above {FILL_CAP_PCT:.0f}% cap) — over-sniped, skip")
            return
        # Price crashed >5%: someone sold during our wait
        if price_sol > 0 and new_price > 0:
            price_delta = (new_price - price_sol) / price_sol
            if price_delta < -0.05:
                print(f"[a2-bc] x {mint[:8]}... price {price_delta:+.1%} in {A2_STABILITY_WAIT}s — dump started, skip")
                return
            # Price spiked >8%: overbought, likely to correct on us
            if price_delta > 0.08:
                print(f"[a2-bc] x {mint[:8]}... price {price_delta:+.1%} in {A2_STABILITY_WAIT}s — overbought pump, skip")
                return
            price_sol = new_price
        progress = new_fill
        print(f"[a2-bc] {mint[:8]}... stability OK fill={new_fill:.1f}% price_Δ={price_delta:+.1%} — buying")

        # Final concurrent-buy guard
        if mint in _buying_mints or mint in pos_mgr.positions or mint in pos_mgr._session_traded_mints:  # type: ignore[union-attr]
            return

        # ── HARD BUY GATE — final check before any transaction ────────────────
        _gate_ok_a2, _ = _hard_buy_gate(
            "[a2-bc]", mint, "a2",
            # BC tokens have no DEX liq/vol data — skip those steps (thresholds are 0)
            liq_usd=0.0,
            mc_usd=0.0,
            vol_h1_usd=0.0,
            buy_txns=0,
            sell_txns=0,
            safety_passed=True,   # check_token_safety passed in _bc_token_gate earlier
            holders=holder_count,
        )
        if not _gate_ok_a2:
            return
        # ────────────────────────────────────────────────────────────────────────

        # Apply proven creator buy override (75% wallet) if set
        if _proven_buy_override is not None:
            actual_buy = _proven_buy_override

        print(
            f"[a2-bc] {'⭐ PROVEN ' if _is_proven_creator else ''}BUYING {mint[:8]}... {pf_name} ({pf_symbol}) "
            f"fill={progress:.1f}% price={price_sol:.2e} SOL {actual_buy:.3f} SOL"
            + (f" web={pf_website[:30]}" if pf_website else "")
        )

        _buying_mints.add(mint)
        try:
            if PAPER_TRADING:
                import uuid as _uuid_bc
                sig = f"PAPER_BC_{_uuid_bc.uuid4().hex[:10].upper()}"
                entry_price = price_sol if price_sol > 0 else 1e-7
                token_amount = int((actual_buy / entry_price) * 10 ** TOKEN_DECIMALS)
            else:
                sig = await pump_svc.buy(mint, actual_buy, slippage=0.20)  # type: ignore[attr-defined]
                # Read actual tokens received to get true fill price
                await asyncio.sleep(3)
                actual_bals = await wallet_svc.get_token_balances()  # type: ignore[union-attr]
                actual_raw = next(
                    (int(t["raw_amount"]) for t in actual_bals if t["mint"] == mint), 0
                )
                if actual_raw > 0:
                    decimals = next(
                        (t.get("decimals", TOKEN_DECIMALS) for t in actual_bals if t["mint"] == mint),
                        TOKEN_DECIMALS,
                    )
                    entry_price = actual_buy / (actual_raw / 10 ** decimals)
                    token_amount = actual_raw
                else:
                    entry_price = price_sol if price_sol > 0 else 1e-7
                    token_amount = int((actual_buy / entry_price) * 10 ** TOKEN_DECIMALS)

            # Score: PROVEN creator = 15, STRONG = 12, SAFE = 9
            _a2_score = 15 if _is_proven_creator else (12 if eliza_strong else 9)
            pos_mgr.open_position(  # type: ignore[union-attr]
                mint=mint,
                dex="pump_fun",  # position_manager uses bonding curve for price; auto-upgrades to pumpswap on graduation
                entry_price_sol=entry_price,
                entry_sol_spent=actual_buy,
                token_amount=token_amount,
                token_decimals=TOKEN_DECIMALS,
                signature=sig,
                creator_wallet=_creator_wallet_early,
                score=_a2_score,
                grok_confirmed=(lambda _s: _s.is_grok_confirmed(mint) if _s else False)(_sm_a2bc),
                grok_premium=_is_prem_a2bc,
                meta={"holder_wallets": _holder_owner_wallets},
            )
            pos_mgr._session_traded_mints.add(mint)  # type: ignore[union-attr]
            try:
                from elizaos.plugins.solana import holder_tracker as _ht_buy
                _ht_buy.purge(mint)
            except Exception:
                pass
            _label = "⭐ PROVEN" if _is_proven_creator else ("STRONG" if eliza_strong else "SAFE")
            print(
                f"[a2-bc] Position opened: {mint[:8]}... {pf_name} "
                f"entry={entry_price:.2e} SOL spent={actual_buy:.4f} SOL "
                f"score={_a2_score} ({_label}) sig={sig[:20]}"
            )
        except Exception as exc:
            import traceback as _tb
            print(f"[a2-bc] Buy failed {mint[:8]}...: {exc}", file=sys.stderr)
            _tb.print_exc(file=sys.stderr)
        finally:
            _buying_mints.discard(mint)

    runtime.register_event("token_launch", _on_token_launch)

    # Keep loop alive — all work is event-driven
    while True:
        await asyncio.sleep(3600)


async def holder_watch_loop(runtime: AgentRuntime) -> None:
    """Poll bonding curve tokens that narrowly missed the 30-holder gate.

    When a token passes ALL other gates (social, Eliza, fill%, safety) but has only
    20-29 holders, it enters _holder_watch_pending. This loop checks every 20 seconds.
    The moment a token hits 30 holders we buy immediately — this is the earliest
    possible entry: right as genuine traction starts forming, before price moves.

    Expires watchers after 4 minutes — if a token hasn't grown past 30 holders in
    that time it has stalled and isn't worth buying.
    """
    from elizaos.types import ServiceTypeRegistry

    POLL_INTERVAL   = 20    # seconds between holder checks
    MAX_WATCH_SECS  = 240   # abandon watcher after 4 min
    MAX_FILL_PCT    = 70.0  # if bonding curve already >70% filled by the time we check, skip
    PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    BUY_SOL_BC = float(os.getenv("BUY_SOL_BC", "0.08"))
    TOKEN_DECIMALS = 6

    print("[holder-watch] Holder threshold watcher started — polling every 20s")

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        if not _holder_watch_pending:
            continue

        pos_mgr    = runtime.get_service("position_manager")
        wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
        pump_svc   = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)

        if None in (pos_mgr, wallet_svc, pump_svc):
            continue

        for mint in list(_holder_watch_pending.keys()):
            entry = _holder_watch_pending.get(mint)
            if entry is None:
                continue

            # Expire stale watchers
            if time.time() - entry["first_seen"] > MAX_WATCH_SECS:
                print(f"[holder-watch] {mint[:8]}... watcher expired (4min, no traction)")
                del _holder_watch_pending[mint]
                continue

            # Already bought / blacklisted
            if (mint in (pos_mgr.positions or {})  # type: ignore[union-attr]
                    or mint in _buying_mints
                    or mint in pos_mgr._session_traded_mints):  # type: ignore[union-attr]
                _holder_watch_pending.pop(mint, None)
                continue

            # Re-check holder count
            try:
                _rpc = getattr(wallet_svc, "rpc", None)
                if _rpc is None:
                    continue
                _accounts = await asyncio.wait_for(
                    _rpc.get_token_largest_accounts(mint), timeout=4.0
                )
                _nonzero = [a for a in _accounts if float(a.get("uiAmount") or 0) > 0]
                holder_count = len(_nonzero)

                top_holder_pct = 100.0
                if len(_nonzero) >= 3:
                    real_buyers = _nonzero[1:]
                    top_ui = float(real_buyers[0].get("uiAmount") or 0)
                    total_ui = sum(float(a.get("uiAmount") or 0) for a in real_buyers)
                    top_holder_pct = (top_ui / total_ui * 100) if total_ui > 0 else 100.0
            except Exception:
                continue

            print(f"[holder-watch] {mint[:8]}... {entry['holders_at_detect']}→{holder_count} holders")

            # Update holder growth tracker with this poll result
            try:
                from elizaos.plugins.solana import holder_tracker as _ht_hw
                _ht_hw.record_snapshot(mint, holder_count)
            except Exception:
                pass

            from elizaos.plugins.solana import live_config as _lc_hw_filt
            _hw_min_holders   = int(_lc_hw_filt.get("a2_min_holders", 10))
            _hw_max_whale_pct = float(_lc_hw_filt.get("a2_max_whale_pct", 40.0))

            if holder_count < _hw_min_holders:
                continue  # not there yet

            if top_holder_pct > _hw_max_whale_pct:
                print(f"[holder-watch] x {mint[:8]}... {holder_count} holders but whale {top_holder_pct:.0f}% — skip")
                del _holder_watch_pending[mint]
                continue

            # Threshold reached — verify current fill% and price haven't moved too much
            try:
                from elizaos.plugins.solana.services.pump_fun import PumpFunService
                if isinstance(pump_svc, PumpFunService):
                    bc = await asyncio.wait_for(pump_svc.get_bonding_curve(mint), timeout=5.0)
                    if bc:
                        current_fill = float(bc.get("fill_pct") or 0) * 100
                        current_price = float(bc.get("price_sol") or 0)
                        if current_fill > MAX_FILL_PCT:
                            print(f"[holder-watch] x {mint[:8]}... fill {current_fill:.0f}% > {MAX_FILL_PCT:.0f}% — already pumped, skip")
                            del _holder_watch_pending[mint]
                            continue
                        if current_price > 0:
                            entry["price_sol"] = current_price
            except Exception:
                pass  # use cached price if check fails

            price_sol = entry.get("price_sol", 0.0)
            if price_sol <= 0:
                del _holder_watch_pending[mint]
                continue

            # Check wallet balance and position limits
            try:
                bal = await _effective_wallet_sol(wallet_svc)  # type: ignore[union-attr]
                _sm_hw = runtime.get_service("social_monitor")
                _is_prem_hw, _prem_buy_hw = _check_grok_premium(mint, _sm_hw, bal)
                from elizaos.plugins.solana import live_config as _lc_hw_sz
                _hw_size = _lc_hw_sz.get("a2_buy_sol", BUY_SOL_BC)
                actual_buy = _prem_buy_hw if _is_prem_hw else _compound_buy_sol(bal, _hw_size)
                if _is_prem_hw:
                    print(f"[grok-premium] {mint[:8]}... score≥8 → {actual_buy} SOL position, TP+50%, stall exit @+25%")
                allowed, reason = pos_mgr.check_trade_allowed(actual_buy, bal)  # type: ignore[union-attr]
                if not allowed:
                    print(f"[holder-watch] x {mint[:8]}... gated: {reason}")
                    del _holder_watch_pending[mint]
                    continue
                _sl_gap = time.time() - _last_sl_exit_time
                if _sl_gap < _get_post_sl_cooldown():
                    print(f"[holder-watch] x {mint[:8]}... post-SL cooldown — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                    continue
            except Exception:
                continue

            del _holder_watch_pending[mint]
            launch = entry["launch"]
            pf_name   = (launch.get("name") or mint[:8])
            pf_symbol = (launch.get("symbol") or "?")

            print(
                f"[holder-watch] THRESHOLD HIT {holder_count} holders — BUYING {mint[:8]}... "
                f"{pf_name} ({pf_symbol}) @ {price_sol:.2e} SOL  [watched {(time.time()-entry['first_seen']):.0f}s]"
            )

            _buying_mints.add(mint)
            try:
                if PAPER_TRADING:
                    sig = f"PAPER_HW_{uuid.uuid4().hex[:10].upper()}"
                    entry_price = price_sol
                    token_amount = int((actual_buy / entry_price) * 10 ** TOKEN_DECIMALS)
                else:
                    from elizaos.plugins.solana.services.pump_fun import PumpFunService
                    if not isinstance(pump_svc, PumpFunService):
                        continue
                    sig = await pump_svc.buy(mint, actual_buy, slippage=0.20)
                    await asyncio.sleep(3)
                    actual_bals = await wallet_svc.get_token_balances()  # type: ignore[union-attr]
                    actual_raw = next((int(t["raw_amount"]) for t in actual_bals if t["mint"] == mint), 0)
                    if actual_raw > 0:
                        decimals = next((t.get("decimals", TOKEN_DECIMALS) for t in actual_bals if t["mint"] == mint), TOKEN_DECIMALS)
                        entry_price = actual_buy / (actual_raw / 10 ** decimals)
                        token_amount = actual_raw
                    else:
                        entry_price = price_sol
                        token_amount = int((actual_buy / entry_price) * 10 ** TOKEN_DECIMALS)

                pos_mgr.open_position(  # type: ignore[union-attr]
                    mint=mint,
                    dex="pump_fun",
                    entry_price_sol=entry_price,
                    entry_sol_spent=actual_buy,
                    token_amount=token_amount,
                    token_decimals=TOKEN_DECIMALS,
                    signature=sig,
                    score=0,
                    grok_confirmed=(lambda _s: _s.is_grok_confirmed(mint) if _s else False)(_sm_hw),
                    grok_premium=_is_prem_hw,
                )
                pos_mgr._session_traded_mints.add(mint)  # type: ignore[union-attr]
                print(f"[holder-watch] Position opened: {mint[:8]}... entry={entry_price:.2e} SOL sig={sig[:20]}")
            except Exception as exc:
                print(f"[holder-watch] Buy failed {mint[:8]}...: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            finally:
                _buying_mints.discard(mint)


async def monster_scanner_loop(runtime: AgentRuntime) -> None:
    """Dedicated high-quality monster scanner.

    Runs every monster_scanner_interval_secs (default 15 min).
    Looks for tokens 30 min–6 hrs old on all platforms that match full monster DNA:
      - Liq ≥ min_liq_usd, MC ≥ min_mc_usd
      - Vol/liq between min_vol_liq and max_vol_liq
      - Buy ratio ≥ min_buy_ratio
      - Passed rugcheck safety

    If monster_scanner_buy=True: executes trades like Strategy C/D.
    If monster_scanner_buy=False: logs signals only and pushes alert to Jarvis.
    """
    from elizaos.plugins.solana import live_config as _lc_ms
    import aiohttp as _ms_aiohttp
    import json as _ms_json

    _MS_LOG = os.path.join(os.path.dirname(__file__), "packages/python/elizaos/plugins/solana/monster_scanner_signals.json")
    _MS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elizaos/plugins/solana/monster_scanner_signals.json")

    def _ms_load() -> list:
        try:
            with open(_MS_LOG) as _f:
                return _ms_json.load(_f)
        except Exception:
            return []

    def _ms_save(records: list) -> None:
        try:
            with open(_MS_LOG, "w") as _f:
                _ms_json.dump(records[-500:], _f, indent=2)
        except Exception:
            pass

    async with _ms_aiohttp.ClientSession() as _ms_session:
        while True:
            try:
                _enabled = _lc_ms.get("monster_scanner_enabled", False)
                _interval = float(_lc_ms.get("monster_scanner_interval_secs", 900) or 900)
                await asyncio.sleep(_interval)

                if not _enabled:
                    continue

                _min_liq  = float(_lc_ms.get("monster_scanner_min_liq_usd", 50_000) or 50_000)
                _min_mc   = float(_lc_ms.get("monster_scanner_min_mc_usd",  50_000) or 50_000)
                _min_age  = float(_lc_ms.get("monster_scanner_min_age_mins", 30)    or 30)
                _max_age  = float(_lc_ms.get("monster_scanner_max_age_mins", 360)   or 360)
                _min_vl   = float(_lc_ms.get("monster_scanner_min_vol_liq",  5.0)   or 5.0)
                _max_vl   = float(_lc_ms.get("monster_scanner_max_vol_liq",  100.0) or 100.0)
                _min_br   = float(_lc_ms.get("monster_scanner_min_buy_ratio",52.0)  or 52.0)
                _do_buy   = bool(_lc_ms.get("monster_scanner_buy", False))

                print(f"[monster-scan] Running — targets: liq≥${_min_liq:,.0f} mc≥${_min_mc:,.0f} age {_min_age:.0f}-{_max_age:.0f}min")

                # Collect candidates from all platforms
                _candidates: list[dict] = []
                try:
                    _grad_res = await _fetch_graduated_raydium_tokens(_ms_session, limit=30)
                    _candidates.extend(_grad_res)
                except Exception as _e:
                    print(f"[monster-scan] Grad fetch error: {_e}")
                try:
                    _ray_res = await _fetch_native_raydium_tokens(_ms_session, limit=20)
                    _candidates.extend(_ray_res)
                except Exception as _e:
                    print(f"[monster-scan] Raydium fetch error: {_e}")
                try:
                    _met_res = await _fetch_meteora_tokens(_ms_session, limit=10)
                    _candidates.extend(_met_res)
                except Exception as _e:
                    print(f"[monster-scan] Meteora fetch error: {_e}")

                # Deduplicate
                _seen_ms: set[str] = set()
                _deduped: list[dict] = []
                for _t in _candidates:
                    _m = _t.get("mint", "")
                    if _m and _m not in _seen_ms:
                        _seen_ms.add(_m)
                        _deduped.append(_t)

                _pos_mgr_ms = runtime.get_service(ServiceTypeRegistry.POSITION_MANAGER)
                _now_ms = time.time()
                _signals: list[dict] = []

                for _tok in _deduped:
                    _mint_ms  = _tok.get("mint", "")
                    if not _mint_ms:
                        continue

                    # Skip already traded / in position / blacklisted
                    if _pos_mgr_ms and _mint_ms in _pos_mgr_ms.positions:
                        continue
                    if _pos_mgr_ms and _mint_ms in _pos_mgr_ms._session_traded_mints:
                        continue
                    if _mint_ms in _buying_mints:
                        continue

                    # Age filter
                    _age_mins = float(_tok.get("age_mins") or 0)
                    if _age_mins < _min_age or _age_mins > _max_age:
                        continue

                    # Liquidity filter
                    _liq_ms = float(_tok.get("liquidity_usd") or 0)
                    if _liq_ms < _min_liq:
                        continue

                    # Market cap filter
                    _mc_ms = float(_tok.get("market_cap_usd") or _tok.get("mc_usd") or 0)
                    if _min_mc > 0 and _mc_ms > 0 and _mc_ms < _min_mc:
                        continue

                    # Vol/liq ratio
                    _vol_ms   = float(_tok.get("volume_h1_usd") or _tok.get("volume_usd_h1") or 0)
                    _vl_ratio = (_vol_ms / _liq_ms) if _liq_ms > 0 else 0.0
                    if _min_vl > 0 and _vl_ratio < _min_vl:
                        continue
                    if _max_vl > 0 and _vl_ratio > _max_vl:
                        continue

                    # Buy ratio
                    _br_ms = float(_tok.get("buy_ratio_h1") or 0)
                    if _min_br > 0 and _br_ms > 0 and _br_ms < _min_br:
                        continue

                    # Safety check
                    try:
                        _safe_ms, _safe_reason_ms = await _pos_mgr_ms.check_token_safety(_mint_ms)  # type: ignore[union-attr]
                        if not _safe_ms:
                            continue
                    except Exception:
                        continue

                    _m5_ms = float(_tok.get("price_change_m5") or 0)
                    _h1_ms = float(_tok.get("price_change_h1") or 0)
                    _momentum_ms = round(_m5_ms * 0.5 + _h1_ms * 0.3, 1)
                    _dex_ms  = _tok.get("dex", "unknown")
                    _name_ms = _tok.get("name", _mint_ms[:8])
                    _sym_ms  = _tok.get("symbol", "???")

                    _signal = {
                        "ts":            _now_ms,
                        "utc":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now_ms)),
                        "mint":          _mint_ms,
                        "name":          _name_ms,
                        "symbol":        _sym_ms,
                        "dex":           _dex_ms,
                        "age_mins":      round(_age_mins, 1),
                        "liq_usd":       round(_liq_ms, 0),
                        "mc_usd":        round(_mc_ms, 0),
                        "vol_liq_ratio": round(_vl_ratio, 1),
                        "buy_ratio":     round(_br_ms, 1),
                        "m5_pct":        round(_m5_ms, 1),
                        "h1_pct":        round(_h1_ms, 1),
                        "momentum_score": _momentum_ms,
                        "action":        "BUY" if _do_buy else "SIGNAL",
                    }
                    _signals.append(_signal)
                    print(
                        f"[monster-scan] 🔥 {_sym_ms} ({_mint_ms[:8]}...) {_dex_ms} "
                        f"age={_age_mins:.0f}m liq=${_liq_ms:,.0f} mc=${_mc_ms:,.0f} "
                        f"v/l={_vl_ratio:.1f}x br={_br_ms:.0f}% m5={_m5_ms:+.0f}% h1={_h1_ms:+.0f}%"
                        + (" → BUYING" if _do_buy else " → SIGNAL LOGGED")
                    )

                    # Push alert to Jarvis dashboard
                    try:
                        from elizaos.plugins.solana.dashboard_api import push_system_alert
                        await push_system_alert(
                            f"🔥 Monster Signal: {_sym_ms} ({_dex_ms}) "
                            f"age={_age_mins:.0f}m liq=${_liq_ms:,.0f} mc=${_mc_ms:,.0f} "
                            f"v/l={_vl_ratio:.1f}x br={_br_ms:.0f}% m5={_m5_ms:+.0f}%"
                            + (" — BUYING NOW" if _do_buy else " — Research mode, not buying")
                        )
                    except Exception:
                        pass

                    if _do_buy:
                        _buy_ms     = float(_lc_ms.get("buy_sol", 0.05))
                        _price_ms   = float(_tok.get("price_sol") or 0)
                        _raydium_ms = runtime.get_service(ServiceTypeRegistry.LP_POOL)
                        if _raydium_ms is None or _price_ms <= 0:
                            print(f"[monster-scan] x {_mint_ms[:8]}... buy skipped (no price or raydium svc)")
                        else:
                            _buying_mints.add(_mint_ms)
                            try:
                                _sig_ms, _amt_ms = await _raydium_ms.buy_jupiter(_mint_ms, _buy_ms, slippage=0.15)  # type: ignore[union-attr]
                                if _pos_mgr_ms is not None:
                                    _pos_mgr_ms.open_position(
                                        mint=_mint_ms, dex=_dex_ms,
                                        entry_price_sol=_price_ms,
                                        entry_sol_spent=_buy_ms,
                                        token_amount=_amt_ms,
                                        token_decimals=TOKEN_DECIMALS,
                                        signature=_sig_ms,
                                        grok_confirmed=False, grok_premium=False,
                                        meta={
                                            "name": _name_ms, "symbol": _sym_ms,
                                            "liq_usd": _liq_ms, "mc_usd": _mc_ms,
                                            "strategy": "monster_scanner",
                                            "age_mins": round(_age_mins, 1),
                                            "vol_liq_ratio": round(_vl_ratio, 1),
                                        },
                                    )
                                    print(f"[monster-scan] ✅ Bought {_sym_ms} {_mint_ms[:8]}...")
                            except Exception as _buy_ms_exc:
                                print(f"[monster-scan] Buy failed {_mint_ms[:8]}...: {_buy_ms_exc}")
                            finally:
                                _buying_mints.discard(_mint_ms)

                # Persist signals
                if _signals:
                    _existing = _ms_load()
                    _ms_save(_existing + _signals)
                    print(f"[monster-scan] Scan complete — {len(_signals)} monster signals found")
                else:
                    print(f"[monster-scan] Scan complete — no monsters found this cycle")

            except asyncio.CancelledError:
                raise
            except Exception as _ms_err:
                print(f"[monster-scan] Loop error: {_ms_err}", file=sys.stderr)


async def smart_reset_loop(runtime: AgentRuntime) -> None:
    """After circuit break, wait for cooldown then ask Eliza to analyse and decide whether to reset.

    Eliza reads the recent closed trades, assesses whether the losses were avoidable
    (rug patterns, already-improved filters) or structural, and either resets the
    circuit breaker or recommends leaving it paused.

    Cooldown: 60 minutes after circuit break before first analysis attempt.
    Re-checks every 30 minutes while still broken.
    """
    COOLDOWN_SECS = 300      # 5 min minimum before analysis (was 3600; shorter for active testing)
    RETRY_INTERVAL = 1800    # 30 min between re-checks when still broken
    CHECK_INTERVAL = 60      # poll interval when not broken

    RESET_USER_ID = "00000000-0000-0000-0000-000000000097"
    RESET_ROOM_ID = "00000000-0000-0000-0000-000000000096"

    print("[smart-reset] Smart circuit-reset loop started")

    while True:
        try:
            pos_mgr = runtime.get_service("position_manager")
            if pos_mgr is None or not pos_mgr.circuit_broken:  # type: ignore[union-attr]
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Check cooldown
            broken_secs = time.time() - pos_mgr.circuit_break_at  # type: ignore[union-attr]
            if broken_secs < COOLDOWN_SECS:
                remaining = int(COOLDOWN_SECS - broken_secs)
                print(f"[smart-reset] Circuit broken — cooldown {remaining}s remaining")
                await asyncio.sleep(min(remaining + 1, RETRY_INTERVAL))
                continue

            # Auto-reset immediately if the trigger was trivial (no real losses)
            # This prevents the circuit from staying stuck all day on transaction fees
            _risk = pos_mgr.get_risk_summary()  # type: ignore[union-attr]
            _consec = _risk.get("consecutive_losses", 0)
            _daily_loss = _risk.get("daily_pnl_sol", 0)
            # Auto-reset if: zero consecutive losses AND less than 0.05 SOL daily loss
            if _consec == 0 and _daily_loss > -0.05:
                pos_mgr.force_reset_circuit_breaker(  # type: ignore[union-attr]
                    f"Auto-reset: trivial trigger (consec={_consec}, daily_pnl={_daily_loss:+.4f} SOL > -0.05)"
                )
                print(f"[smart-reset] AUTO-RESET — trivial trigger (consec={_consec}, daily_pnl={_daily_loss:+.4f} SOL), resuming immediately")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Hard overnight safety net: never stay paused longer than 45 minutes.
            # If the LLM call keeps failing or returns STAY PAUSED, the bot would be dead
            # for hours overnight. After 45min the risk from whatever caused the break has
            # passed (positions closed, rug resolved) — resume and keep collecting data.
            if broken_secs > 2700:  # 45 minutes
                pos_mgr.force_reset_circuit_breaker(  # type: ignore[union-attr]
                    f"Auto-reset: 45min overnight safety net (consec={_consec}, daily_pnl={_daily_loss:+.4f} SOL)"
                )
                print(f"[smart-reset] AUTO-RESET — 45min safety net fired, resuming trading (consec={_consec}, daily_pnl={_daily_loss:+.4f} SOL)")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Build prompt: recent closed trades for Eliza to analyse
            recent = pos_mgr.get_trade_history(limit=10)  # type: ignore[union-attr]
            sells = [t for t in recent if t.get("side") == "sell"]
            trade_lines = []
            for t in sells[:6]:
                trade_lines.append(
                    f"  - {t.get('mint','?')[:8]}... dex={t.get('dex','?')} "
                    f"pnl={t.get('pnl_pct',0):+.1f}% hold={t.get('hold_secs',0):.0f}s "
                    f"reason={t.get('reason','?')}"
                )
            trade_summary = "\n".join(trade_lines) if trade_lines else "  (no recent closed trades)"

            risk = pos_mgr.get_risk_summary()  # type: ignore[union-attr]
            dev_stats = get_reputation().get_stats()

            prompt = (
                f"The trading circuit breaker fired {int(broken_secs/60)} minutes ago after "
                f"{risk['consecutive_losses']} consecutive stop-loss exits.\n\n"
                f"Recent closed trades:\n{trade_summary}\n\n"
                f"Developer reputation stats: {dev_stats}\n\n"
                f"Current risk parameters:\n"
                f"  1. Fill cap: reject bonding curve fill >75% (data: snipers fill to 55-80%)\n"
                f"  2. Price sanity: reject entry price >2.5e-7 SOL\n"
                f"  3. Early SL: -10% in first 3 minutes\n"
                f"  4. Hard SL: -15% (tightened from -25%)\n"
                f"  5. Capital-recovery staircase: TP1 +40% sell 50%, TP2 +56% sell 75% of rest — "
                f"TP1+TP2 fully recoup the 0.011 SOL entry cost; moonbag is free profit\n"
                f"  6. Developer blacklist: auto-blacklist after 2 rugs\n"
                f"  7. Strategy B: dynamic dip wait (15-45s), Strategy C: 30s scans, "
                f"holder watcher fires at exactly 30 holders on bonding curve\n\n"
                f"Analyse the losses: were they fast rug patterns the new filters would now block? "
                f"Is it safe to resume trading? "
                f"Reply with RESUME or STAY PAUSED and a brief explanation."
            )

            print(f"[smart-reset] Asking Jarvis to analyse circuit break after {int(broken_secs/60)}min...")
            _reset_system = (
                "You are Jarvis, the AI risk manager for a Solana trading bot. "
                "After a circuit breaker fires, you review recent losses and decide whether to resume. "
                "Reply RESUME if it is safe to resume trading, or STAY PAUSED if the risk is too high. "
                "Include a one-sentence explanation. Be decisive — hedging is not acceptable."
            )
            try:
                reply = ""
                # Groq primary
                _groq_key_sr = os.getenv("GROQ_API_KEY", "")
                if _groq_key_sr and not reply:
                    try:
                        from openai import AsyncOpenAI as _OAI_sr
                        _sr_gc = _OAI_sr(api_key=_groq_key_sr, base_url="https://api.groq.com/openai/v1", max_retries=0)
                        _sr_resp = await asyncio.wait_for(
                            _sr_gc.chat.completions.create(
                                model="llama-3.1-8b-instant",
                                max_tokens=300,
                                messages=[
                                    {"role": "system", "content": _reset_system},
                                    {"role": "user", "content": prompt},
                                ],
                            ),
                            timeout=20.0,
                        )
                        reply = (_sr_resp.choices[0].message.content or "").strip()
                    except Exception as _sre:
                        print(f"[smart-reset] Groq failed: {_sre}", file=sys.stderr)
                # Gemini fallback
                _gemini_key_sr = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
                if _gemini_key_sr and not reply:
                    try:
                        from google import genai as _gai_sr
                        _gc_sr = _gai_sr.Client(api_key=_gemini_key_sr)
                        _sr_gem = await asyncio.wait_for(
                            _gc_sr.aio.models.generate_content(
                                model="gemini-2.5-flash",
                                contents=f"{_reset_system}\n\n{prompt}",
                                config={"max_output_tokens": 300},
                            ),
                            timeout=20.0,
                        )
                        reply = (_sr_gem.text or "").strip()
                    except Exception as _sre2:
                        print(f"[smart-reset] Gemini failed: {_sre2}", file=sys.stderr)
                # Claude bonus
                if not reply:
                    import anthropic as _anthropic_reset
                    _reset_client = _anthropic_reset.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
                    _reset_resp = await _reset_client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=300,
                        system=_reset_system,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    reply = (_reset_resp.content[0].text if _reset_resp.content else "").strip()
                print(f"[smart-reset] Jarvis says: {reply[:300]}")

                if "RESUME" in reply.upper():
                    pos_mgr.force_reset_circuit_breaker(  # type: ignore[union-attr]
                        f"Jarvis analysis after {int(broken_secs/60)}min cooldown: {reply[:120]}"
                    )
                    print("[smart-reset] Circuit breaker RESET — trading resumed by Jarvis decision")
                elif "STAY PAUSED" in reply.upper() or "STAY_PAUSED" in reply.upper():
                    print("[smart-reset] Jarvis recommends staying paused — will re-check in 30min")
                    await asyncio.sleep(RETRY_INTERVAL)
                    continue
                else:
                    # Ambiguous response — treat as RESUME after 5min (don't stay dark forever)
                    print(f"[smart-reset] ⚠️  Jarvis response ambiguous ('{reply[:60]}') — auto-resuming to prevent deadlock")
                    pos_mgr.force_reset_circuit_breaker(  # type: ignore[union-attr]
                        f"Auto-resume: ambiguous Jarvis response after {int(broken_secs/60)}min"
                    )
            except Exception as exc:
                print(f"[smart-reset] Jarvis call failed: {exc} — auto-resuming to prevent overnight deadlock", file=sys.stderr)
                pos_mgr.force_reset_circuit_breaker(  # type: ignore[union-attr]
                    f"Auto-resume: Jarvis unavailable after {int(broken_secs/60)}min ({exc})"
                )
                print("[smart-reset] Circuit breaker RESET — Jarvis unavailable, safety auto-resume applied")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[smart-reset] Unexpected error: {exc}", file=sys.stderr)

        await asyncio.sleep(CHECK_INTERVAL)


async def grok_bc_sniper_loop(runtime: AgentRuntime) -> None:
    """Strategy E: Grok-first pump.fun BC sniper.

    Grok scans X every 60s for NEW pump.fun launches being hyped.
    For each Grok-discovered token:
      1. pump.fun API confirms fill < 45%, real_sol > 0.3, age < 10min
      2. Rugcheck safety gate
      3. Eliza narrative quality check
      4. Buy 0.06 SOL immediately — we're on first block of traction
      5. TP = +200% (grok_confirmed=True) — high conviction early entry

    This is the fastest path to a position: Grok finds it → validate → buy.
    No waiting for DexScreener metrics to accumulate.
    """
    from elizaos.types import ServiceTypeRegistry

    PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")
    TOKEN_DECIMALS = 6
    POLL_INTERVAL = 15  # check discovery queue every 15s

    print("[grok-sniper] Strategy E (Grok-first BC sniper) ACTIVE — "
          f"{'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'}")

    # Wait for services to start
    await asyncio.sleep(90)

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)

            from elizaos.plugins.solana import live_config as _lc_e
            if not _lc_e.get("strategy_b_enabled", True):
                continue  # respect strategy B toggle (same BC ecosystem)

            # Check trading window
            _e_win_start = _lc_e.get("trading_window_start_utc", 8)
            _e_win_end   = _lc_e.get("trading_window_end_utc", 21)
            _e_utc_hour  = datetime.utcnow().hour
            if _e_win_start == _e_win_end:
                _e_in_window = True
            elif _e_win_end > _e_win_start:
                _e_in_window = _e_win_start <= _e_utc_hour < _e_win_end
            else:
                _e_in_window = _e_utc_hour >= _e_win_start or _e_utc_hour < _e_win_end
            if not _e_in_window:
                continue

            grok_svc = runtime.get_service("social_monitor")
            if not grok_svc or not getattr(grok_svc, "_enabled", False):
                continue

            queue = grok_svc.get_discovery_queue()
            if not queue:
                continue

            pos_mgr    = runtime.get_service("position_manager")
            wallet_svc = runtime.get_service(ServiceTypeRegistry.WALLET)
            pump_svc   = runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
            raydium_svc = runtime.get_service(ServiceTypeRegistry.LP_POOL)

            if not pos_mgr or not wallet_svc:
                continue

            for token in queue:
                mint = token.get("mint", "")
                if not mint:
                    continue

                # Mark processed immediately to prevent double-buy
                grok_svc.mark_discovery_processed(mint)

                # Session blacklist check
                if mint in getattr(pos_mgr, "_session_traded_mints", set()):
                    print(f"[grok-sniper] x {mint[:8]}... already traded this session — skip")
                    continue

                # Already holding
                if mint in pos_mgr.positions:
                    continue

                # Position cap check
                active = sum(1 for p in pos_mgr.positions.values() if not p.is_reconciled)
                if active >= 3:
                    print(f"[grok-sniper] x {mint[:8]}... position cap (3) reached — skip")
                    continue

                name        = token.get("name", mint[:8])
                symbol      = token.get("symbol", "?")
                fill_pct    = token.get("fill_pct", 0.0)
                real_sol    = token.get("real_sol", 0.0)
                holder_count = token.get("holder_count", 0)
                age_secs    = token.get("age_secs", 999)
                confidence  = token.get("confidence", 0)
                hype        = token.get("hype", "")

                print(
                    f"[grok-sniper] Grok-discovered: {name} ({symbol}) {mint[:8]}... "
                    f"fill={fill_pct:.0f}% sol={real_sol:.2f} holders={holder_count} "
                    f"age={age_secs:.0f}s confidence={confidence}/10 hype={hype}"
                )

                # Re-validate fill % is still acceptable (token keeps moving)
                if fill_pct > 50:
                    print(f"[grok-sniper] x {mint[:8]}... fill {fill_pct:.0f}% > 50% — too late")
                    continue
                if real_sol < 0.3:
                    print(f"[grok-sniper] x {mint[:8]}... only {real_sol:.2f} SOL raised — skip")
                    continue
                if confidence < 5:
                    print(f"[grok-sniper] x {mint[:8]}... Grok confidence {confidence}/10 < 5 — skip")
                    continue

                # Safety check
                try:
                    safe, safety_reason = await pos_mgr.check_token_safety(mint)
                    if not safe:
                        print(f"[grok-sniper] x {mint[:8]}... UNSAFE: {safety_reason} — skip")
                        continue
                except Exception as _se:
                    print(f"[grok-sniper] x {mint[:8]}... safety check error ({_se}) — skip")
                    continue

                # Wallet + position sizing
                try:
                    bal = await _effective_wallet_sol(wallet_svc)
                    BUY_SOL_E = float(os.getenv("BUY_SOL", "0.06"))
                    _sm_gsnipe = runtime.get_service("social_monitor")
                    _is_prem_gsnipe, _prem_buy_gsnipe = _check_grok_premium(mint, _sm_gsnipe, bal)
                    actual_buy = _prem_buy_gsnipe if _is_prem_gsnipe else _dynamic_buy_sol(BUY_SOL_E, bal)
                    if _is_prem_gsnipe:
                        print(f"[grok-premium] {mint[:8]}... score≥8 → {actual_buy} SOL position, TP+50%, stall exit @+25%")
                    allowed, reason = pos_mgr.check_trade_allowed(actual_buy, bal)
                    if not allowed:
                        print(f"[grok-sniper] x {mint[:8]}... trade gated: {reason}")
                        continue
                    _sl_gap = time.time() - _last_sl_exit_time
                    if _sl_gap < _get_post_sl_cooldown():
                        print(f"[grok-sniper] x {mint[:8]}... post-SL cooldown — {_get_post_sl_cooldown() - _sl_gap:.0f}s remaining")
                        continue
                except Exception:
                    continue

                # Eliza narrative check (non-blocking — fast timeout, optimistic fallback)
                try:
                    from elizaos.types.model import ModelType, GenerateTextOptions
                    _eliza_e_prompt = (
                        f"Grok found this token being hyped on X: {name} ({symbol})\n"
                        f"pump.fun BC fill: {fill_pct:.0f}% | SOL raised: {real_sol:.2f} | "
                        f"holders: {holder_count} | age: {age_secs:.0f}s | Grok hype: {hype}\n\n"
                        f"Is this a GENUINE meme launch with community backing (STRONG/SAFE), "
                        f"or does it show rug patterns (WEAK/RUG)?\n"
                        f"ONE WORD ONLY: STRONG, SAFE, WEAK, or RUG."
                    )
                    _e_vc_result = await asyncio.wait_for(
                        runtime.generate_text(
                            _eliza_e_prompt,
                            GenerateTextOptions(model_type=ModelType.TEXT_SMALL),
                        ),
                        timeout=6.0,
                    )
                    _e_vc = (_e_vc_result.text if _e_vc_result else "").strip().upper()[:10]
                    if "RUG" in _e_vc or "WEAK" in _e_vc:
                        print(f"[grok-sniper] x {mint[:8]}... Eliza: {_e_vc} — skip")
                        continue
                    print(f"[grok-sniper] {mint[:8]}... Eliza: {_e_vc} -> proceeding")
                except Exception as _ee:
                    print(f"[grok-sniper] {mint[:8]}... Eliza skip ({_ee}) — proceeding")

                # Execute buy via PumpPortal (BC tokens, pre-graduation)
                print(
                    f"[grok-sniper] BUYING {mint[:8]}... "
                    f"({actual_buy:.4f} SOL, fill={fill_pct:.0f}%, confidence={confidence}/10)"
                )

                sig = ""
                if PAPER_TRADING:
                    sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
                elif pump_svc is not None:
                    try:
                        sig = await pump_svc.buy(
                            mint,
                            sol_amount=actual_buy,
                            slippage_bps=2500,
                            priority_fee=0.002,
                        )
                    except Exception as _buy_exc:
                        print(f"[grok-sniper] Buy failed for {mint[:8]}: {_buy_exc}")
                        continue
                else:
                    print(f"[grok-sniper] x {mint[:8]}... no pump service — skip")
                    continue

                # Get token amount
                token_amount = int((actual_buy / 0.000000035) * (10 ** TOKEN_DECIMALS))  # rough estimate
                if not PAPER_TRADING and wallet_svc is not None:
                    try:
                        await asyncio.sleep(3)
                        actual_bals = await wallet_svc.get_token_balances()
                        actual_raw = next(
                            (int(t["raw_amount"]) for t in actual_bals if t["mint"] == mint), 0
                        )
                        if actual_raw > 0:
                            token_amount = actual_raw
                    except Exception:
                        pass

                price_sol = actual_buy / (token_amount / 10 ** TOKEN_DECIMALS) if token_amount > 0 else 0.000000035

                try:
                    pos_mgr.open_position(
                        mint=mint,
                        dex="pump_fun",
                        entry_price_sol=price_sol,
                        entry_sol_spent=actual_buy,
                        token_amount=token_amount,
                        token_decimals=TOKEN_DECIMALS,
                        signature=sig,
                        grok_confirmed=True,  # always True — this IS the Grok discovery path
                        grok_premium=_is_prem_gsnipe,
                        meta={
                            "name": name,
                            "symbol": symbol,
                            "fill_pct": fill_pct,
                            "real_sol": real_sol,
                            "holder_count": holder_count,
                            "grok_hype": hype,
                            "grok_confidence": confidence,
                            "source": "grok_discovery",
                        },
                    )
                    print(
                        f"[grok-sniper] Position opened: {mint[:8]}... "
                        f"entry={price_sol:.8f} SOL sig={sig[:20]} "
                        f"[TP=+200% — Grok-first entry]"
                    )
                except ValueError as ve:
                    print(f"[grok-sniper] x {mint[:8]}... gated: {ve}")

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[grok-sniper] Unexpected error: {exc}", file=sys.stderr)


async def run_startup_health_check(runtime: Any) -> None:
    """Run a full health check on startup and post any failures to the Jarvis chat window.

    Checks:
    1. API keys present and reachable (Grok, OpenAI, Anthropic)
    2. Solana RPC reachable
    3. Wallet balance readable and > 0 SOL
    4. At least one strategy enabled
    5. Config sanity (stop_loss sensible, TP > 30%, max_concurrent >= 2)
    6. Grok API credits not exhausted
    7. Trading window sanity (start != end)

    Results are printed to stdout AND pushed to the Jarvis chat window as soon as
    the WebSocket has a connected client (alerts are queued until then).
    """
    from elizaos.plugins.solana.dashboard_api import push_system_alert
    from elizaos.plugins.solana import live_config  # type: ignore[attr-defined]
    from elizaos.types import ServiceTypeRegistry as _SvcReg

    issues: list[str] = []
    ok_items: list[str] = []

    print("[health-check] Running startup health check...")

    # ── 1. API key presence ───────────────────────────────────────────────────
    grok_key    = os.getenv("GROK_API_KEY", "")
    openai_key  = os.getenv("OPENAI_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    rpc_url     = os.getenv("SOLANA_RPC_URL", "")

    gemini_key  = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")
    groq_key    = os.getenv("GROQ_API_KEY", "")
    if not grok_key and not gemini_key:
        issues.append("GROK_API_KEY and GOOGLE_GENERATIVE_AI_API_KEY both missing — social signals disabled")
    elif not grok_key and gemini_key:
        pass  # Gemini is active replacement — not an issue
    if not openai_key:
        issues.append("OPENAI_API_KEY missing — GPT-4o pre-trade gate disabled")
    if not anthropic_key:
        issues.append("ANTHROPIC_API_KEY missing — Claude analysis disabled")
    if not rpc_url:
        issues.append("SOLANA_RPC_URL missing — cannot reach Solana RPC")

    # ── 2. Solana RPC reachability ────────────────────────────────────────────
    if rpc_url:
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _sess:
                async with _sess.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
                    timeout=_aio.ClientTimeout(total=5),
                ) as _resp:
                    if _resp.status == 200:
                        ok_items.append("Solana RPC reachable ✓")
                    else:
                        issues.append(f"Solana RPC returned HTTP {_resp.status}")
        except Exception as _rpc_exc:
            issues.append(f"Solana RPC unreachable: {_rpc_exc}")

    # ── 3. Wallet balance ─────────────────────────────────────────────────────
    try:
        _bal = 0.0
        _wallet_svc = runtime.get_service(_SvcReg.WALLET)
        if _wallet_svc is not None:
            try:
                _bal = await _wallet_svc.get_sol_balance()
            except Exception:
                pass
        # Wallet service may return 0 when in read-only mode — always verify via RPC
        if _bal == 0.0:
            try:
                import aiohttp as _aio_hc
                _rpc_hc = os.getenv("SOLANA_RPC_URL", "")
                _pk_hc  = os.getenv("WALLET_PUBLIC_KEY") or os.getenv("SOLANA_PUBLIC_KEY", "")
                if _rpc_hc and _pk_hc:
                    async with _aio_hc.ClientSession() as _s_hc:
                        async with _s_hc.post(_rpc_hc,
                            json={"jsonrpc":"2.0","id":1,"method":"getBalance",
                                  "params":[_pk_hc,{"commitment":"confirmed"}]},
                            timeout=_aio_hc.ClientTimeout(total=5)) as _r_hc:
                            if _r_hc.status == 200:
                                _d_hc = await _r_hc.json()
                                _bal = (_d_hc.get("result") or {}).get("value", 0) / 1e9
            except Exception:
                pass
        _min_trade = live_config.get("buy_sol", 0.05)
        if _bal <= 0:
            issues.append(f"Wallet balance is 0 SOL — insufficient funds")
        elif _bal < _min_trade:
            issues.append(f"Wallet balance {_bal:.4f} SOL is below minimum trade size {_min_trade} SOL")
        else:
            ok_items.append(f"Wallet balance: {_bal:.4f} SOL ✓")
    except Exception as _bal_exc:
        issues.append(f"Wallet balance check failed: {_bal_exc}")

    # ── 4. Strategy enables ───────────────────────────────────────────────────
    _strats_on = []
    for _env_key, _label in [
        ("MONSTER_STRATEGY_ENABLED", "Monster"),
        ("CREATOR_ALPHA_ENABLED",    "Creator-Alpha"),
        ("STRATEGY_A_ENABLED",       "A"),
        ("STRATEGY_A2_ENABLED",      "A2"),
        ("STRATEGY_B_ENABLED",       "B"),
        ("STRATEGY_C_ENABLED",       "C"),
        ("STRATEGY_D_ENABLED",       "D"),
    ]:
        if os.getenv(_env_key, "false").lower() not in ("false", "0", "no"):
            _strats_on.append(_label)

    _copy_live = bool(live_config.get("copy_trade_enabled", False))
    _copy_paused = bool(live_config.get("copy_trade_paused", False))
    if not _strats_on and not _copy_live:
        issues.append("ALL strategies are OFF and copy trade is OFF — bot will not trade")
    elif not _strats_on and _copy_live:
        _copy_status = "PAUSED" if _copy_paused else "LIVE"
        ok_items.append(f"Copy trade: {_copy_status} (strategies off — copy trade only) ✓")
    else:
        ok_items.append(f"Active strategies: {', '.join(_strats_on)} ✓")
        if _copy_live:
            ok_items.append(f"Copy trade: {'PAUSED' if _copy_paused else 'LIVE'} ✓")

    # ── 5. Config sanity ──────────────────────────────────────────────────────
    _sl  = live_config.get("stop_loss_pct", 0.09)
    _tp  = live_config.get("tp1_mult", 1.40)
    _max = live_config.get("max_concurrent_positions", 3)
    _buy = live_config.get("buy_sol", 0.06)

    _config_issues = []
    if _sl < 0.04:
        _config_issues.append(f"stop_loss={_sl*100:.0f}% (too tight — Jarvis may have degraded this; expected ≥5%)")
    if _sl > 0.20:
        _config_issues.append(f"stop_loss={_sl*100:.0f}% (dangerously wide)")
    if _tp < 1.20:
        _config_issues.append(f"tp1_mult={_tp:.2f} (too conservative — expected ≥1.30; Jarvis may have set this)")
    if _max < 2:
        _config_issues.append(f"max_concurrent_positions={_max} (should be ≥2 for diversification)")
    if _buy < 0.03:
        _config_issues.append(f"buy_sol={_buy} SOL (very small — may be bot_config.json override)")

    if _config_issues:
        for _ci in _config_issues:
            issues.append(f"Config: {_ci}")
    else:
        ok_items.append(f"Config sane: SL={_sl*100:.0f}% TP={(_tp-1)*100:.0f}% max={_max} buy={_buy}SOL ✓")

    # ── 6. Grok API credits (quick test call) ────────────────────────────────
    if grok_key:
        try:
            import openai as _oai_hc
            _grok_hc = _oai_hc.AsyncOpenAI(
                api_key=grok_key,
                base_url="https://api.x.ai/v1",
            )
            _hc_resp = await asyncio.wait_for(
                _grok_hc.chat.completions.create(
                    model="grok-3-mini",
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                ),
                timeout=10,
            )
            ok_items.append("Grok API (grok-3-mini) responding ✓")
        except Exception as _grok_exc:
            _grok_msg = str(_grok_exc)
            if "429" in _grok_msg or "credits" in _grok_msg.lower() or "quota" in _grok_msg.lower():
                issues.append("Grok API credits EXHAUSTED (HTTP 429) — social signals dead. Top up at x.ai/api")
            elif "401" in _grok_msg or "unauthorized" in _grok_msg.lower():
                issues.append("Grok API key INVALID (401) — check GROK_API_KEY in .env")
            else:
                issues.append(f"Grok API unreachable: {_grok_exc}")

    # ── 7. Trading window ─────────────────────────────────────────────────────
    _tw_start = live_config.get("trading_window_start_utc", 21)
    _tw_end   = live_config.get("trading_window_end_utc",   3)
    if _tw_start == _tw_end and _tw_start != 0:
        issues.append(
            f"Trading window start ({_tw_start}h UTC) == end — bot will NEVER trade. "
            "Set both to 0 to trade 24/7 or choose a valid window."
        )
    elif _tw_start == 0 and _tw_end == 0:
        ok_items.append("Trading window: 24/7 (no restriction) ✓")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"[health-check] OK: {len(ok_items)}  Issues: {len(issues)}")
    for _ok in ok_items:
        print(f"[health-check] ✓ {_ok}")
    for _issue in issues:
        print(f"[health-check] ⚠ {_issue}", file=sys.stderr)

    # Push to Jarvis chat window (queued until a WebSocket client connects)
    if not issues:
        push_system_alert(
            f"Startup health check PASSED — {len(ok_items)} checks OK. Bot is ready to trade.",
            level="info",
        )
    else:
        push_system_alert(
            f"Startup health check: {len(issues)} issue(s) detected — bot may not trade correctly.",
            level="error",
        )
        for _issue in issues:
            push_system_alert(_issue, level="error")
        if ok_items:
            push_system_alert(
                f"Passing checks ({len(ok_items)}): " + " | ".join(ok_items),
                level="info",
            )

    # Also send to Telegram if available
    if issues:
        try:
            from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_alert
            _tg_lines = "\n".join(f"• {i}" for i in issues)
            await _tg_alert(
                f"🚨 <b>TraderBot startup issues ({len(issues)})</b>\n{_tg_lines}"
            )
        except Exception:
            pass  # Telegram may not be configured yet


async def main():
    character = Character(
        name="TraderBot",
        username="traderbot",
        bio=[
            "You are TraderBot — a quantitative Solana trading bot running two active strategies. "
            "Strategy B — PumpSwap graduation snipes: when a pump.fun token completes its bonding curve "
            "within 5 minutes (fast-grad signals pre-built community), buy the post-graduation dip on "
            "PumpSwap. Staged entry: 0.025 SOL immediately, add second 0.025 SOL at T+3 min if token is "
            "still alive (eliminates rug exposure — all 3 observed rugs happened within 58-124s). "
            "Dynamic dip wait: 15s if rising, 30s neutral, 45s dipping. Liq ≥$12k required. "
            "Strategy A2 'Ghost Rider' — pump.fun bonding curve near-graduation snipes: buys tokens at "
            "55-70% BC fill (momentum proven, graduation imminent, not yet exit-liquidity). "
            "0.06 SOL position — same size as all other strategies. "
            "3-minute stall: if no +7% in 3 min, exit before SL. "
            "Risk management: max 3 concurrent positions (max 2 BC), trading window 08:00-21:00 UTC. "
            "SL -9% hard cut | Early SL -5% within first 3 min (grace period 60s). "
            "Exit — PumpSwap (B): TP +60% full exit. "
            "Exit — pump.fun BC (A2): TP1 +30% sell 50%, TP2 +150% full exit. "
            "All execution via Jupiter aggregator (routes PumpSwap, Raydium, Meteora, Orca). "
            "Max daily loss -15% of wallet → circuit breaker halts all trading."
        ],
        system=(
            "You are TraderBot — a quantitative Solana meme-coin trading bot.\n\n"
            "ACTIVE STRATEGIES (as of 2026-03-13):\n"
            "  B. PumpSwap graduation snipes\n"
            "     — fast-grad ≤5 min after bonding curve completion\n"
            "     — liq ≥$12k at graduation, age ≥60s, m5 ≤15% (not already pumped)\n"
            "     — STAGED ENTRY: buy 0.025 SOL at dip, add 0.025 SOL at T+3 min if position alive\n"
            "     — total position up to 0.05 SOL (0.025 if rug within 3 min)\n"
            "  A2 'Ghost Rider'. pump.fun bonding curve near-graduation snipes\n"
            "     — fill window 55-70% (momentum proven, not exit-liquidity)\n"
            "     — real_sol ≥2.0 SOL deposited (genuine demand)\n"
            "     — Eliza quality gate: STRONG/SAFE = buy, WEAK/RUG = skip\n"
            "     — 0.06 SOL position (same as all strategies)\n"
            "     — 3-min stall timer: exit if no +7% in 3 minutes\n\n"
            "POSITION SIZES:\n"
            "  Strategy B: 0.025 SOL tranche-1 + 0.025 SOL tranche-2 = 0.05 SOL max\n"
            "  All strategies (B, A2, C, D): 0.06 SOL per trade\n\n"
            "EXIT RULES:\n"
            "  PumpSwap (B):    TP +50% → full exit | SL -9% | early SL -5% in first 3 min\n"
            "  pump.fun BC (A2): TP1 +30% sell 50% | TP2 +150% full exit | SL -9% | stall 3 min\n"
            "  Stall timer: pumpswap 15 min, pump_fun 3 min, raydium 8 min (no +7% → exit)\n\n"
            "BUY/SELL ROUTING:\n"
            "  All tokens: Jupiter aggregator with escalating slippage (15% → 30% → 50%)\n"
            "  Jupiter auto-routes through PumpSwap, Raydium, Meteora, Orca\n"
            "  pump.fun BC buys: PumpPortal (bonding curve pre-graduation only)\n\n"
            "RISK CONTROLS:\n"
            "  Max 4 concurrent positions\n"
            "  Trading window: 21:00–03:00 UTC only\n"
            "  Max daily loss: -15% wallet → circuit breaker halts all trading\n"
            "  Loss streak: 3 consecutive losses → pause 30 min\n"
            "  Rugcheck: score ≥8000 required, LP unlock warnings block entry\n\n"
            "ACTIONS YOU CAN TAKE:\n"
            "  GET_TOKEN_PRICE  — look up current price of any Solana token\n"
            "  BUY_TOKEN        — buy a specific token\n"
            "  SELL_TOKEN       — sell a specific token\n"
            "  solana_wallet    — check wallet balance and open positions\n"
            "  pump_fun_launches — see recent pump.fun launches being tracked\n\n"
            "Always state which strategy a position came from, current P&L, and nearest SL/TP level."
        ),
    )

    # Bootstrap plugin (disable extended to avoid duplicate solana_wallet provider)
    bootstrap = create_bootstrap_plugin(CapabilityConfig(enable_extended=False))
    solana = create_solana_plugin()
    openai_plugin = create_openai_plugin()

    agent_id = uuid.UUID("63adb390-3424-0d07-bf74-e7d049dfc2dc")
    runtime = AgentRuntime(
        character=character,
        agent_id=agent_id,
        plugins=[bootstrap, solana, openai_plugin],
        settings={"VALIDATION_LEVEL": "trusted"},
    )
    await runtime.initialize()

    print(f"TraderBot runtime started agentId={runtime.agent_id}")
    print(f"Providers registered: {[p.name for p in runtime.providers]}")
    print(f"Actions registered:   {[a.name for a in runtime.actions]}")

    # Start HTTP bridge in background
    await start_http_bridge(runtime)

    # Start Telegram bot (push alerts + command polling)
    _tg_wallet_sol = 0.0
    try:
        _tg_wallet_svc = runtime.get_service("wallet")
        if _tg_wallet_svc:
            _tg_wallet_sol = await asyncio.wait_for(_tg_wallet_svc.get_sol_balance(), timeout=8)
    except Exception:
        pass
    # Wallet service may be in read-only mode (SOLANA_PRIVATE_KEY error) and return 0 — fall back to RPC
    if _tg_wallet_sol == 0.0:
        try:
            import aiohttp as _aio_tg
            _rpc_tg = os.getenv("SOLANA_RPC_URL", "")
            _pk_tg  = os.getenv("WALLET_PUBLIC_KEY") or os.getenv("SOLANA_PUBLIC_KEY", "")
            if _rpc_tg and _pk_tg:
                async with _aio_tg.ClientSession() as _s_tg:
                    async with _s_tg.post(_rpc_tg,
                        json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[_pk_tg,{"commitment":"confirmed"}]},
                        timeout=_aio_tg.ClientTimeout(total=5)) as _r_tg:
                        if _r_tg.status == 200:
                            _d_tg = await _r_tg.json()
                            _tg_wallet_sol = (_d_tg.get("result") or {}).get("value", 0) / 1e9
        except Exception:
            pass
    try:
        from elizaos.plugins.solana.telegram_alerts import start_telegram_bot
        _tg_task = await start_telegram_bot(runtime, wallet_sol=_tg_wallet_sol)
        print(f"[telegram] Bot started — alerts + commands active (wallet={_tg_wallet_sol:.4f} SOL)")
    except Exception as _tg_start_err:
        print(f"[telegram] Failed to start (non-fatal): {_tg_start_err}")

    # Shared graduation queue — filled by scout_loop when bonding curve complete=True.
    # graduation_snipe_loop DISABLED: pumpswap grad-snipe WR=31% vs 44% break-even (recent data).
    # Catastrophic losses (-62%, -66%) from post-graduation instant dumps outweigh wins.
    # Re-enable once market conditions improve or entry criteria tightened.
    graduation_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    # Reconcile any orphaned on-chain positions that position manager doesn't know about.
    # Always run — even on a fresh trade_history.json, wallet may have running positions
    # from a previous session that need TP/SL management.
    try:
        from elizaos.plugins.solana.services.position_manager import PositionManagerService
        pos_mgr_svc = runtime.get_service("position_manager")
        if isinstance(pos_mgr_svc, PositionManagerService):
            reconciled = await pos_mgr_svc.reconcile_wallet_positions()
            if reconciled:
                print(f"[startup] Wallet reconciliation: imported {reconciled} orphaned position(s)")
            else:
                print("[startup] Wallet reconciliation: no orphaned positions found")
    except Exception as _rec_exc:
        print(f"[startup] Wallet reconciliation skipped: {_rec_exc}")

    # Load persisted cooldowns first (these survive trade history wipes)
    _load_cooldowns()
    print(f"[startup] Loaded persisted cooldowns: {len(_all_exit_times)} all-exit, {len(_loss_exit_times)} loss-exit")

    # Seed exit cooldown dicts from persisted trade history (must run regardless of strategy toggles)
    _pos_mgr_seed = runtime.get_service("position_manager")
    if _pos_mgr_seed is not None:
        _seed_now = time.time()
        for _t in _pos_mgr_seed.get_trade_history(limit=500):  # type: ignore[union-attr]
            if _t.get("side") != "sell":
                continue
            _mint_h = _t.get("mint", "")
            _ts = _t.get("timestamp", 0.0)
            if not _mint_h or _seed_now - _ts > max(LOSS_COOLDOWN_SECS, ALL_EXIT_COOLDOWN_SECS):
                continue
            _all_exit_times[_mint_h] = max(_all_exit_times.get(_mint_h, 0.0), _ts)
            _pnl = _t.get("pnl_pct", 0.0) or 0.0
            _reason_h = _t.get("reason", "")
            if _reason_h in ("stop_loss", "early_stop_loss", "stall_exit") or _pnl < -5:
                _loss_exit_times[_mint_h] = max(_loss_exit_times.get(_mint_h, 0.0), _ts)
        print(f"[startup] Seeded cooldowns from history: {len(_all_exit_times)} all-exit, {len(_loss_exit_times)} loss-exit")

    # ── Eliza LLM handlers — always registered regardless of strategy toggles ──
    async def _jarvis_assess_position_main(trade: dict) -> None:
        """After a position opens, Eliza gives a quick post-entry read on timing and risk."""
        mint = trade.get("mint", "")
        dex = trade.get("dex", "?")
        entry = trade.get("entry_price_sol", 0.0)
        score = trade.get("score", 0)
        sol_spent = trade.get("entry_sol_spent", 0.0)
        meta = trade.get("meta", {})
        name = meta.get("name") or trade.get("name", mint[:8])
        symbol = meta.get("symbol") or trade.get("symbol", "?")
        liq = meta.get("liq_usd", 0.0)
        vol_h1 = meta.get("vol_h1_usd", 0.0)
        buyers_h1 = meta.get("buyers_h1", 0)
        h1_pct = meta.get("price_change_h1", None)
        m5_pct = meta.get("price_change_m5", None)
        has_socials = meta.get("has_socials", False)
        social_urls = meta.get("social_urls", [])
        market_cap = meta.get("market_cap_usd", 0.0)
        score_reasons = meta.get("score_reasons", [])
        _lessons = _load_recent_lessons(2)
        prompt = (
            f"{_lessons}"
            f"=== CONTEXT ===\n"
            f"We scalp Solana memecoins on live {dex.upper()} with 0.05 SOL positions. "
            f"SL at -9%, TP at +50% (full exit). We win by entering EARLY on real momentum. "
            f"Rugs are the main killer — LP lock, concentrated supply, fake volume are red flags.\n\n"
            f"=== JUST ENTERED ===\n"
            f"Token: {name} ({symbol}) | DEX: {dex} | Mint: {mint[:16]}\n"
            f"Entry: {entry:.2e} SOL | Score: {score} | Spent: {sol_spent:.4f} SOL\n"
            f"Liquidity: ${liq:,.0f} | Vol h1: ${vol_h1:,.0f} | Buyers h1: {buyers_h1}\n"
            f"Price h1: {f'{h1_pct:+.1f}%' if h1_pct is not None else 'n/a'} | "
            f"m5: {f'{m5_pct:+.1f}%' if m5_pct is not None else 'n/a'} | "
            f"Market cap: ${market_cap:,.0f}\n"
            f"Socials: {', '.join(social_urls[:2]) if social_urls else 'NONE'}\n"
            f"Score reasons: {'; '.join(score_reasons[:4]) if score_reasons else 'n/a'}\n\n"
            f"In 2 sentences max: "
            f"(1) Rug risk assessment — name/socials/liquidity/buyer count signal? "
            f"(2) Entry timing — is this early momentum or are we chasing a pump? "
            f"Start with HOLD or CAUTION."
        )
        try:
            from elizaos.types.model import ModelType, GenerateTextOptions
            result = await asyncio.wait_for(
                runtime.generate_text(prompt, GenerateTextOptions(model_type=ModelType.TEXT_SMALL)),
                timeout=8.0,
            )
            verdict = (result.text if result else "").strip()[:200]
            pm = runtime.get_service("position_manager")
            if pm and verdict:
                pm._log_activity(  # type: ignore[union-attr]
                    "info" if verdict.upper().startswith("HOLD") else "warning",
                    f"Eliza [{symbol or mint[:8]}]: {verdict}"
                )
            print(f"[jarvis] {mint[:8]}... {verdict[:120]}")
        except Exception as exc:
            print(f"[jarvis] Assessment skipped {mint[:8]}...: {exc}")

    async def _jarvis_moonbag_advice_main(data: dict) -> None:
        """After TP2 hit, Eliza advises whether to hold or tighten the moonbag stop."""
        if not (data.get("tp2_hit") and not data.get("tp3_hit")):
            return
        mint = data.get("mint", "")
        cp = data.get("current_price_sol", 0.0)
        ep = data.get("entry_price_sol", 0.0)
        pk = data.get("peak_price", cp)
        if not mint or ep <= 0:
            return
        gain_from_entry = ((cp / ep) - 1) * 100 if ep > 0 else 0
        drawdown_from_peak = ((cp / pk) - 1) * 100 if pk > 0 else 0
        prompt = (
            f"=== CONTEXT ===\n"
            f"We scalp Solana memecoins. TP1+TP2 have already fired — our original 0.006 SOL cost is "
            f"FULLY recovered. The remaining 12.5% moonbag is 100% house money — pure profit if it runs.\n"
            f"The question is whether to hold the moonbag for a potential TP3 (+200%) or tighten the "
            f"trailing stop to lock in what's already a bonus win.\n\n"
            f"=== MOONBAG STATUS ===\n"
            f"Mint: {mint[:16]}\n"
            f"Entry: {ep:.2e} SOL | Current: {cp:.2e} SOL | Peak: {pk:.2e} SOL\n"
            f"Gain from entry: +{gain_from_entry:.1f}% | Drawback from peak: {drawdown_from_peak:+.1f}%\n"
            f"Remaining position: 12.5% of original (all house money)\n\n"
            f"In 1 sentence: should we HOLD the moonbag for continuation, or TIGHTEN the stop now? "
            f"Give one concrete reason based on the price action above. Start with HOLD or TIGHTEN."
        )
        try:
            from elizaos.types.model import ModelType, GenerateTextOptions
            result = await asyncio.wait_for(
                runtime.generate_text(prompt, GenerateTextOptions(model_type=ModelType.TEXT_SMALL)),
                timeout=8.0,
            )
            advice = (result.text if result else "").strip()[:200]
            pm = runtime.get_service("position_manager")
            if pm and advice:
                pm._log_activity(  # type: ignore[union-attr]
                    "success" if "HOLD" in advice.upper() else "warning",
                    f"Eliza moonbag [{mint[:8]}]: {advice}"
                )
            print(f"[jarvis-moonbag] {mint[:8]}... {advice[:120]}")
        except Exception as exc:
            print(f"[jarvis-moonbag] Skipped {mint[:8]}...: {exc}")

    runtime.register_event("position_opened", lambda t: asyncio.create_task(_jarvis_assess_position_main(t)))
    runtime.register_event("position_update", lambda d: asyncio.create_task(_jarvis_moonbag_advice_main(d)))
    _social_brain = "Gemini (Google Search)" if os.getenv("GOOGLE_GENERATIVE_AI_API_KEY") and not os.getenv("GROK_API_KEY") else ("Grok-3 (X)" if os.getenv("GROK_API_KEY") else "DISABLED")
    _analysis_brain = "Groq (Llama) → Gemini → Claude" if os.getenv("GROQ_API_KEY") else "Gemini → Claude"
    print("[jarvis] Multi-model AI stack active:")
    print(f"[jarvis]   {_social_brain}  → X/web social scanning + token discovery (every 5 min)")
    print("[jarvis]   GPT-4o       → per-token entry quality gate (pre-buy risk scoring)")
    print("[jarvis]   Claude Haiku → position coach EXIT/HOLD verdicts (every 5s, live DexScreener)")
    print(f"[jarvis]   {_analysis_brain} → 30-min strategy analysis + ADJUST parameter tuning")

    # ── Telegram alert handlers (always active regardless of strategy toggles) ─
    try:
        from elizaos.plugins.solana.telegram_alerts import (
            send_alert,
            fmt_position_opened,
            fmt_position_closed,
            fmt_partial_sell,
            fmt_circuit_breaker_on,
            fmt_circuit_breaker_off,
        )

        async def _tg_on_position_opened(trade: dict) -> None:
            await send_alert(fmt_position_opened(trade))

        async def _tg_on_position_closed(trade: dict) -> None:
            await send_alert(fmt_position_closed(trade))

        async def _tg_on_partial_sell(trade: dict) -> None:
            await send_alert(fmt_partial_sell(trade))

        async def _tg_on_circuit_breaker_on(data: dict) -> None:
            await send_alert(fmt_circuit_breaker_on(data.get("reason", "")))

        async def _tg_on_circuit_breaker_off(data: dict) -> None:
            await send_alert(fmt_circuit_breaker_off(data.get("reason", "")))

        runtime.register_event("position_opened",    _tg_on_position_opened)
        runtime.register_event("position_closed",    _tg_on_position_closed)
        runtime.register_event("partial_sell",       _tg_on_partial_sell)
        runtime.register_event("circuit_breaker_on", _tg_on_circuit_breaker_on)
        runtime.register_event("circuit_breaker_off", _tg_on_circuit_breaker_off)
        print("[telegram] Alert handlers registered (position opened/closed/partial + circuit breaker)")
    except Exception as _tg_reg_err:
        print(f"[telegram] Alert handler registration failed: {_tg_reg_err}")

    # Strategy toggles — controlled via .env
    _strategy_a  = os.getenv("STRATEGY_A_ENABLED",  "false").lower() not in ("false", "0", "no")
    _strategy_a2 = os.getenv("STRATEGY_A2_ENABLED", "false").lower() not in ("false", "0", "no")
    _strategy_b  = os.getenv("STRATEGY_B_ENABLED",  "false").lower() not in ("false", "0", "no")  # grad-snipe
    _strategy_c  = os.getenv("STRATEGY_C_ENABLED",  "true").lower()  not in ("false", "0", "no")  # raydium scout
    _strategy_d  = os.getenv("STRATEGY_D_ENABLED",  "false").lower() not in ("false", "0", "no")

    print(f"[startup] Strategies: A={'ON' if _strategy_a else 'OFF'} A2={'ON' if _strategy_a2 else 'OFF'} B={'ON' if _strategy_b else 'OFF'} C={'ON' if _strategy_c else 'OFF'} D={'ON' if _strategy_d else 'OFF'}")

    # Sync .env strategy flags into live_config — env is authoritative at startup,
    # overrides any stale bot_config.json values so Jarvis status display is accurate
    from elizaos.plugins.solana import live_config as _lc_startup_sync
    _lc_startup_sync.set_value("strategy_a_enabled",  _strategy_a,  changed_by="startup-env-sync")
    _lc_startup_sync.set_value("strategy_a2_enabled", _strategy_a2, changed_by="startup-env-sync")
    _lc_startup_sync.set_value("strategy_b_enabled",  _strategy_b,  changed_by="startup-env-sync")
    _lc_startup_sync.set_value("strategy_c_enabled",  _strategy_c,  changed_by="startup-env-sync")
    _lc_startup_sync.set_value("strategy_d_enabled",  _strategy_d,  changed_by="startup-env-sync")

    # ── Startup health check — runs async, results appear in Jarvis chat window ──
    asyncio.create_task(run_startup_health_check(runtime))

    # ── Hourly internal audit — verifies all settings, gates, APIs, playbook ──
    try:
        from elizaos.plugins.solana.bot_auditor import audit_loop
        asyncio.create_task(audit_loop())
    except Exception as _audit_err:
        print(f"[audit] Failed to start audit loop: {_audit_err}")

    # ── Creator Whitelist Fast-Buy — ALWAYS ON regardless of strategy toggles ──
    # When a whitelisted creator deploys a new token, buy immediately at the
    # bonding curve before the community even notices. We skip normal score/social
    # filters — the creator's track record IS the filter.
    # Registered directly here so it works even with A/A2/B all disabled.
    from elizaos.types import ServiceTypeRegistry as _SvcReg
    _cwl_dev_rep = get_reputation()
    _cwl_pos_mgr = runtime.get_service("position_manager")
    _cwl_pump_svc = runtime.get_service(_SvcReg.TOKEN_DATA)
    _cwl_wallet_svc = runtime.get_service(_SvcReg.WALLET)
    _cwl_buying: set[str] = set()

    async def _creator_whitelist_buy(launch: dict) -> None:
        """Immediate buy when a whitelisted creator's token appears on the WS."""
        mint = launch.get("mint", "")
        creator_wallet = launch.get("creator_wallet", "")
        if not mint or not creator_wallet:
            return
        if not mint.lower().endswith("pump"):
            return
        if not _cwl_dev_rep.is_whitelisted(creator_wallet):
            return

        # Don't double-buy or re-enter session blacklist
        if mint in _cwl_buying:
            return
        if _cwl_pos_mgr is not None and (
            mint in _cwl_pos_mgr.positions  # type: ignore[union-attr]
            or mint in _cwl_pos_mgr._session_traded_mints  # type: ignore[union-attr]
        ):
            return

        _cwl_status, _cwl_reason = _cwl_dev_rep.check(creator_wallet)
        print(
            f"[creator-wl] 🎯 WHITELISTED creator: {creator_wallet[:16]}... "
            f"→ new token {mint[:8]}... ({_cwl_reason})"
        )

        if _cwl_wallet_svc is None or _cwl_pos_mgr is None or _cwl_pump_svc is None:
            return

        try:
            _cwl_bal = await _effective_wallet_sol(_cwl_wallet_svc)  # type: ignore[union-attr]
            _cwl_buy_sol = _dynamic_buy_sol(BUY_SOL, _cwl_bal)
            _allowed, _gate_reason = _cwl_pos_mgr.check_trade_allowed(_cwl_buy_sol, _cwl_bal)  # type: ignore[union-attr]
            if not _allowed:
                print(f"[creator-wl] x {mint[:8]}... gated: {_gate_reason}")
                return
        except Exception as _exc:
            print(f"[creator-wl] x {mint[:8]}... balance check failed: {_exc}")
            return

        # Minimal safety: just check mint/freeze authority (no social/fill filter)
        safe, safety_reason = await _cwl_pos_mgr.check_token_safety(mint)  # type: ignore[union-attr]
        if not safe:
            print(f"[creator-wl] x {mint[:8]}... UNSAFE: {safety_reason}")
            return

        # Get live bonding curve data for entry price
        try:
            _bc = await _cwl_pump_svc.get_bonding_curve(mint)  # type: ignore[union-attr]
            if not _bc:
                print(f"[creator-wl] x {mint[:8]}... bonding curve not found")
                return
            if _bc.get("complete"):
                print(f"[creator-wl] x {mint[:8]}... already graduated — skip bonding curve buy")
                return
            _entry_price = _bc.get("price_sol", 0.0)
            if _entry_price <= 0:
                print(f"[creator-wl] x {mint[:8]}... no price data")
                return
            _fill = _bc.get("progress_pct", 0.0)
        except Exception as _exc:
            print(f"[creator-wl] x {mint[:8]}... BC fetch failed: {_exc}")
            return

        _cwl_buying.add(mint)
        try:
            print(
                f"[creator-wl] BUYING {mint[:8]}... "
                f"fill={_fill:.1f}% price={_entry_price:.2e} SOL "
                f"amount={_cwl_buy_sol:.4f} SOL"
            )
            if PAPER_TRADING:
                import uuid as _uuid_cwl
                sig = f"PAPER_CWL_{_uuid_cwl.uuid4().hex[:10].upper()}"
                token_amount = int((_cwl_buy_sol / _entry_price) * 10 ** TOKEN_DECIMALS)
                actual_price = _entry_price
            else:
                sig = await _cwl_pump_svc.buy(mint, _cwl_buy_sol, slippage=0.25, pool="pump")  # type: ignore[union-attr]
                # Read actual fill
                await asyncio.sleep(3)
                _actual_bals = await _cwl_wallet_svc.get_token_balances()  # type: ignore[union-attr]
                _actual_raw = next((int(t["raw_amount"]) for t in _actual_bals if t["mint"] == mint), 0)
                if _actual_raw > 0:
                    _actual_dec = next((t.get("decimals", TOKEN_DECIMALS) for t in _actual_bals if t["mint"] == mint), TOKEN_DECIMALS)
                    actual_price = _cwl_buy_sol / (_actual_raw / 10 ** _actual_dec)
                    token_amount = _actual_raw
                else:
                    actual_price = _entry_price
                    token_amount = int((_cwl_buy_sol / _entry_price) * 10 ** TOKEN_DECIMALS)

            _cwl_pos_mgr.open_position(  # type: ignore[union-attr]
                mint=mint,
                dex="pump_fun",
                entry_price_sol=actual_price,
                entry_sol_spent=_cwl_buy_sol,
                token_amount=token_amount,
                token_decimals=TOKEN_DECIMALS,
                signature=sig,
                score=99,  # special score to mark creator-whitelist entries
                grok_confirmed=(lambda _s: _s.is_grok_confirmed(mint) if _s else False)(runtime.get_service("social_monitor")),
            )
            _cwl_pos_mgr._session_traded_mints.add(mint)  # type: ignore[union-attr]
            print(
                f"[creator-wl] ✅ Position opened: {mint[:8]}... "
                f"entry={actual_price:.2e} SOL spent={_cwl_buy_sol:.4f} SOL sig={sig[:20]}"
            )
        except Exception as _exc:
            import traceback as _tb_cwl
            print(f"[creator-wl] Buy failed {mint[:8]}...: {_exc}", file=sys.stderr)
            _tb_cwl.print_exc(file=sys.stderr)
        finally:
            _cwl_buying.discard(mint)

    async def _cwl_event_handler(launch: dict) -> None:
        asyncio.create_task(_creator_whitelist_buy(launch))

    runtime.register_event("token_launch", _cwl_event_handler)
    _cwl_stats = _cwl_dev_rep.get_stats()
    print(
        f"[creator-wl] Creator whitelist fast-buy ACTIVE — "
        f"{_cwl_stats['whitelisted']} whitelisted devs | "
        f"{_cwl_stats['blacklisted']} blacklisted"
    )

    # ── Strategy SW: Smart Wallet shadow-buy loop ─────────────────────────────
    async def smart_wallet_loop(rt) -> None:
        """Poll Helius for recent SWAP transactions from tracked smart wallets.

        When a watched wallet buys a token < max_age_secs ago:
          1. Run safety check (mint/freeze authority + rugcheck LP lock)
          2. Check position limits + circuit breaker
          3. Buy via PumpSwap → pump.fun BC → Jupiter (first that succeeds)
          4. Open position tagged as dex='pumpswap' (most smart wallet plays are PumpSwap)

        Wallets configured in elizaos/plugins/solana/smart_wallets.json.
        Enable via bot_config.json: "smart_wallet_enabled": true
        """
        import json as _sw_json
        _sw_file = os.path.join(os.path.dirname(__file__), "elizaos/plugins/solana/smart_wallets.json")
        _sw_seen: dict[str, set] = {}   # wallet_addr → set of tx signatures already processed
        _sw_evaluated: set[str] = set() # mints already evaluated this session (avoid re-checking)

        # Extract Helius API key from RPC URL (format: https://.../?api-key=XYZ)
        _rpc_url = os.getenv("SOLANA_RPC_URL", "")
        _helius_key = ""
        if "api-key=" in _rpc_url:
            _helius_key = _rpc_url.split("api-key=")[-1].split("&")[0].strip()

        # Stables we never want to treat as "bought token"
        _STABLES = {
            "So11111111111111111111111111111111111111112",  # WSOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        }

        _sw_pos_mgr = rt.get_service("position_manager")
        _sw_wallet_svc = rt.get_service("solana_wallet")
        _sw_pump_svc = rt.get_service("token_data")
        _sw_ray_svc = rt.get_service("lp_pool")

        while True:
            try:
                from elizaos.plugins.solana import live_config as _lc_sw
                if not _lc_sw.get("smart_wallet_enabled", False):
                    await asyncio.sleep(30)
                    continue

                if not _helius_key:
                    print("[smart-wallet] No Helius API key found in SOLANA_RPC_URL — strategy disabled")
                    await asyncio.sleep(60)
                    continue

                # Load wallet list fresh each cycle (hot-reloadable by editing the JSON)
                try:
                    with open(_sw_file) as _f:
                        _sw_data = _sw_json.load(_f)
                        _wallets: list[str] = (
                            _sw_data if isinstance(_sw_data, list)
                            else _sw_data.get("wallets", [])
                        )
                except Exception:
                    await asyncio.sleep(30)
                    continue

                if not _wallets:
                    await asyncio.sleep(30)
                    continue

                _poll_secs = int(_lc_sw.get("smart_wallet_poll_secs", 30))
                _max_age   = int(_lc_sw.get("smart_wallet_max_age_secs", 120))
                _sw_buy_sol = float(_lc_sw.get("smart_wallet_buy_sol", 0.08))
                _now = time.time()

                import aiohttp as _aio_sw
                for _wallet in _wallets:
                    if _wallet not in _sw_seen:
                        _sw_seen[_wallet] = set()
                    try:
                        _url = f"https://api.helius.xyz/v0/addresses/{_wallet}/transactions"
                        _params = {"api-key": _helius_key, "type": "SWAP", "limit": "10"}
                        async with _aio_sw.ClientSession() as _sess:
                            async with _sess.get(_url, params=_params, timeout=_aio_sw.ClientTimeout(sock_connect=10, sock_read=25)) as _resp:
                                if _resp.status != 200:
                                    continue
                                _txs = await _resp.json(content_type=None)
                    except Exception as _sw_e:
                        print(f"[smart-wallet] Helius fetch failed for {_wallet[:8]}...: {type(_sw_e).__name__}: {_sw_e}")
                        continue

                    for _tx in _txs:
                        _sig = _tx.get("signature", "")
                        if not _sig or _sig in _sw_seen[_wallet]:
                            continue
                        _sw_seen[_wallet].add(_sig)

                        # Only act on fresh buys
                        _tx_time = _tx.get("timestamp", 0)
                        if _now - _tx_time > _max_age:
                            continue

                        # Find the token received (bought) — non-stable token going TO this wallet
                        _mint = None
                        for _tf in _tx.get("tokenTransfers", []):
                            _t_mint = _tf.get("mint", "")
                            _to     = _tf.get("toUserAccount", "")
                            if _t_mint not in _STABLES and _to == _wallet:
                                _mint = _t_mint
                                break

                        if not _mint or _mint in _sw_evaluated:
                            continue
                        _sw_evaluated.add(_mint)

                        if _sw_pos_mgr is None:
                            continue
                        if _mint in _sw_pos_mgr._session_traded_mints:
                            continue
                        if _mint in _sw_pos_mgr.positions:
                            continue

                        print(
                            f"[smart-wallet] {_wallet[:8]}... bought {_mint[:8]}... "
                            f"{int(_now - _tx_time)}s ago — evaluating"
                        )

                        # Safety check
                        try:
                            _sw_safe, _sw_reason = await _sw_pos_mgr.check_token_safety(_mint)
                            if not _sw_safe:
                                print(f"[smart-wallet] x {_mint[:8]}... UNSAFE: {_sw_reason} — skip")
                                continue
                        except Exception as _se:
                            print(f"[smart-wallet] x {_mint[:8]}... safety error ({_se}) — skip")
                            continue

                        # Circuit breaker + position limit check
                        try:
                            _sw_bal = await _effective_wallet_sol(_sw_wallet_svc)
                            _sm_sw  = rt.get_service("social_monitor")
                            _is_prem_sw, _prem_buy_sw = _check_grok_premium(_mint, _sm_sw, _sw_bal)
                            _actual_sw_buy = _prem_buy_sw if _is_prem_sw else _dynamic_buy_sol(_sw_buy_sol, _sw_bal)
                            if _is_prem_sw:
                                print(f"[grok-premium] {_mint[:8]}... score≥8 → {_actual_sw_buy} SOL (smart-wallet)")
                            _sw_allowed, _sw_gate_reason = _sw_pos_mgr.check_trade_allowed(_actual_sw_buy, _sw_bal)
                            if not _sw_allowed:
                                print(f"[smart-wallet] x {_mint[:8]}... gated: {_sw_gate_reason}")
                                continue
                            _sl_gap = time.time() - _last_sl_exit_time
                            if _sl_gap < _get_post_sl_cooldown():
                                print(f"[smart-wallet] x {_mint[:8]}... post-SL cooldown ({_get_post_sl_cooldown() - _sl_gap:.0f}s)")
                                continue
                        except Exception:
                            continue

                        # Get entry price from DexScreener (best-effort — use 0 fallback)
                        _sw_price = 0.0
                        try:
                            if _sw_ray_svc is not None:
                                _sw_pair = await _sw_ray_svc.get_token_price(_mint)  # type: ignore[union-attr]
                                _sw_price = float(_sw_pair or 0)
                        except Exception:
                            pass

                        # Execute buy: try PumpSwap → pump.fun BC → Jupiter
                        _sw_sig = None
                        _sw_dex = "pumpswap"
                        print(
                            f"[smart-wallet] BUYING {_mint[:8]}... "
                            f"({_actual_sw_buy} SOL, shadow of {_wallet[:8]}...)"
                        )
                        if PAPER_TRADING:
                            _sw_sig = f"PAPER_SW_{uuid.uuid4().hex[:10].upper()}"
                        else:
                            for _pool, _dex_label in [("pump-amm", "pumpswap"), ("pump", "pump_fun")]:
                                try:
                                    if _sw_pump_svc is None:
                                        break
                                    _sw_sig = await _sw_pump_svc.buy(  # type: ignore[union-attr]
                                        _mint, _actual_sw_buy, slippage=0.30, pool=_pool
                                    )
                                    _sw_dex = _dex_label
                                    break
                                except Exception:
                                    continue
                            if not _sw_sig:
                                # Jupiter fallback
                                try:
                                    if _sw_ray_svc is not None:
                                        _sw_sig = await _sw_ray_svc.swap_jupiter_buy(  # type: ignore[union-attr]
                                            _mint, _actual_sw_buy, _sw_wallet_svc, slippage=0.30
                                        )
                                        _sw_dex = "pumpswap"
                                except Exception as _je:
                                    print(f"[smart-wallet] All buy methods failed for {_mint[:8]}...: {_je}")
                                    continue

                        if not _sw_sig:
                            continue

                        # Read actual fill
                        await asyncio.sleep(3)
                        _sw_entry_price = _sw_price
                        _sw_token_amount = 0
                        try:
                            _sw_bals = await _sw_wallet_svc.get_token_balances()  # type: ignore[union-attr]
                            _sw_raw  = next((int(t["raw_amount"]) for t in _sw_bals if t["mint"] == _mint), 0)
                            if _sw_raw > 0:
                                _sw_dec = next((t.get("decimals", TOKEN_DECIMALS) for t in _sw_bals if t["mint"] == _mint), TOKEN_DECIMALS)
                                _sw_entry_price  = _actual_sw_buy / (_sw_raw / 10 ** _sw_dec)
                                _sw_token_amount = _sw_raw
                        except Exception:
                            pass

                        if _sw_token_amount == 0:
                            if _sw_entry_price > 0:
                                _sw_token_amount = int((_actual_sw_buy / _sw_entry_price) * 10 ** TOKEN_DECIMALS)
                            else:
                                _sw_token_amount = int((_actual_sw_buy / 1e-7) * 10 ** TOKEN_DECIMALS)
                                _sw_entry_price  = 1e-7

                        try:
                            _sw_pos_mgr.open_position(
                                mint=_mint,
                                dex=_sw_dex,
                                entry_price_sol=_sw_entry_price,
                                entry_sol_spent=_actual_sw_buy,
                                token_amount=_sw_token_amount,
                                token_decimals=TOKEN_DECIMALS,
                                signature=_sw_sig,
                                grok_confirmed=(lambda _s: _s.is_grok_confirmed(_mint) if _s else False)(_sm_sw),
                                grok_premium=_is_prem_sw,
                                meta={"source": "smart_wallet", "shadow_wallet": _wallet},
                            )
                            print(
                                f"[smart-wallet] ✅ Position opened: {_mint[:8]}... "
                                f"entry={_sw_entry_price:.2e} SOL spent={_actual_sw_buy:.4f} SOL "
                                f"dex={_sw_dex} sig={_sw_sig[:20]}"
                            )
                        except Exception as _oe:
                            print(f"[smart-wallet] open_position failed {_mint[:8]}...: {_oe}")

            except Exception as _sw_outer:
                print(f"[smart-wallet] Loop error: {_sw_outer}", file=sys.stderr)

            await asyncio.sleep(_poll_secs if "_poll_secs" in dir() else 30)

    # Start all background loops
    scout_task    = asyncio.create_task(autonomous_scout_loop(runtime, graduation_queue)) if _strategy_a else None
    bc_task       = asyncio.create_task(bc_social_sniper_loop(runtime, graduation_queue)) if _strategy_a2 else None  # Strategy A2
    watch_task    = None  # holder-watch OFF — buys unfiltered tokens (no website/description/Eliza check), poor WR overnight
    grad_task     = asyncio.create_task(graduation_snipe_loop(runtime, graduation_queue)) if _strategy_b else None  # Strategy B
    grad_confirm_task = asyncio.create_task(graduation_confirmation_loop(runtime))  # Strategy B confirmation timer
    raydium_task  = asyncio.create_task(raydium_scout_loop(runtime)) if (_strategy_c or _strategy_d) else None  # Strategy C + D (Meteora)
    monster_task  = asyncio.create_task(monster_scanner_loop(runtime))  # Monster scanner (logs when disabled)
    # BC strategy (pre-graduation bonding curve lane) — gated by BC_STRATEGY_ENABLED.
    # Scaffold only at present; loop refuses to start unless infra (Geyser
    # gRPC URL) is configured. Import is lazy so the dep isn't loaded when
    # the lane is off, matching the copy-trade pattern below.
    bc_strategy_task = None
    if os.getenv("BC_STRATEGY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from elizaos.plugins.solana.bc_strategy.loop import bc_strategy_loop as _run_bc
            bc_strategy_task = asyncio.create_task(_run_bc(runtime))
        except Exception as _bc_exc:
            print(f"[warn] BC strategy failed to start: {_bc_exc}")
    social_task   = asyncio.create_task(social_momentum_loop(runtime))  # Strategy E — always runs in research mode, trades when enabled
    _grok_key_present = bool(os.getenv("GROK_API_KEY", "").strip())
    grok_sniper_task   = asyncio.create_task(grok_bc_sniper_loop(runtime)) if _grok_key_present else None  # Grok BC sniper — only when API key set
    smart_wallet_task  = asyncio.create_task(smart_wallet_loop(runtime))    # Strategy SW
    # Copy-trade monitor — gated by COPY_TRADE_ENABLED (default false; monster-only focus 2026-04-20)
    copy_trade_task = None
    if os.getenv("COPY_TRADE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from elizaos.plugins.solana.axiom_copy_trader import run_copy_trade_monitor as _run_ct
            copy_trade_task = asyncio.create_task(_run_ct(runtime))
        except Exception as _ct_exc:
            print(f"[warn] Copy-trade monitor failed to start: {_ct_exc}")
            copy_trade_task = None
    else:
        print("[copy-trade] disabled via COPY_TRADE_ENABLED=false — monster strategy only")
    reset_task    = asyncio.create_task(smart_reset_loop(runtime))
    status_task   = asyncio.create_task(status_log_loop(runtime))
    analysis_task    = asyncio.create_task(analysis_loop(runtime))
    three_brain_task = asyncio.create_task(three_brain_review_loop(runtime))

    # ── Pre-warm real-time market context caches at startup ──────────────────
    asyncio.create_task(_fetch_fear_greed())
    asyncio.create_task(_fetch_trending_coins())

    # ── Watchdog: pings systemd every 30s + monitors critical tasks ─────────
    _monitored_tasks: dict[str, "asyncio.Task | None"] = {
        "copy_trade": copy_trade_task,
        "grad_snipe":  grad_task,
        "raydium_scout": raydium_task,
        "status_log":  status_task,
    }
    watchdog_task = asyncio.create_task(_watchdog_and_task_monitor(_monitored_tasks, runtime))

    all_tasks = [t for t in [scout_task, bc_task, watch_task, grad_task, grad_confirm_task, raydium_task, monster_task, social_task, grok_sniper_task, smart_wallet_task, copy_trade_task, reset_task, status_task, analysis_task, three_brain_task, watchdog_task] if t is not None]

    _active = (
        ("Strategy A (pump.fun bonding curve)" if _strategy_a else "") +
        (" + Strategy A2 (BC social sniper)" if _strategy_a2 else "") +
        (" + Strategy B (PumpSwap grad)" if _strategy_b else "") +
        (" + Strategy C (Raydium/Orca)" if _strategy_c else "") +
        (" + Strategy E (Social Momentum — research)" if True else "")
    ).lstrip(" + ")

    # Run interactive REPL if stdin is a TTY, otherwise keep the event loop running
    if sys.stdin.isatty():
        await interactive_repl(runtime)
        for t in all_tasks:
            t.cancel()
    else:
        print(
            f"[info] Non-interactive mode — {_active} + smart-reset + 30min status + 2h analysis + 2h three-brain review"
        )
        try:
            await asyncio.gather(*all_tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nShutting down.")
        except Exception as exc:
            print(f"\n[FATAL] Task crashed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        finally:
            for t in all_tasks:
                t.cancel()


def _sd_notify(message: str) -> None:
    """Send a notification to systemd via NOTIFY_SOCKET (no external dependencies)."""
    import socket as _socket
    notify_socket = os.environ.get("NOTIFY_SOCKET", "")
    if not notify_socket:
        return
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        addr = ("\0" + notify_socket[1:]) if notify_socket.startswith("@") else notify_socket
        sock.connect(addr)
        sock.sendall(message.encode())
        sock.close()
    except Exception:
        pass


async def _watchdog_and_task_monitor(
    named_tasks: dict[str, "asyncio.Task | None"],
    runtime,
) -> None:
    """
    Two jobs in one coroutine:
    1. Ping systemd WATCHDOG every 30s — proves the event loop is alive.
       If this stops (frozen loop), systemd kills + restarts after WatchdogSec.
    2. Monitor critical tasks for silent death. If one dies unexpectedly,
       send a Telegram alert and SIGTERM self so systemd does a clean restart.
    """
    import signal as _sig

    PING_INTERVAL   = 30     # seconds between systemd pings
    RESTART_DELAY   = 3      # seconds to wait before SIGTERM (lets Telegram send)
    _last_tg_alert  = 0.0
    _restarts: dict[str, int] = {}   # task_name → restart count

    print("[watchdog] started — pinging systemd every 30s, monitoring critical tasks")
    _sd_notify("READY=1")  # signal systemd we're fully up

    while True:
        await asyncio.sleep(PING_INTERVAL)
        _sd_notify("WATCHDOG=1")  # keep systemd watchdog happy

        for task_name, task in list(named_tasks.items()):
            if task is None or not task.done():
                continue  # task is alive — all good

            # Task has exited — was it cancelled (expected) or a real death?
            was_cancelled = task.cancelled()
            exc = None if was_cancelled else task.exception()

            if was_cancelled:
                # Normal cancellation (e.g. strategy disabled) — remove from monitoring
                named_tasks[task_name] = None
                continue

            # Unexpected death — log it
            err_str = f"[watchdog] ⚠️ '{task_name}' task died: {exc!r}"
            print(err_str, file=sys.stderr)

            # Telegram alert (throttled: max one every 5 minutes)
            now = time.time()
            if now - _last_tg_alert > 300:
                _last_tg_alert = now
                try:
                    from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                    asyncio.create_task(_tg(
                        f"🚨 <b>Bot watchdog alert</b>\n"
                        f"Task <code>{task_name}</code> crashed: {exc!r}\n"
                        f"Restarting in {RESTART_DELAY}s…"
                    ))
                except Exception:
                    pass

            # Attempt to restart the copy trade task in-place (avoid full restart)
            if task_name == "copy_trade" and _restarts.get(task_name, 0) < 3:
                _restarts[task_name] = _restarts.get(task_name, 0) + 1
                await asyncio.sleep(5)  # brief pause before re-launch
                try:
                    from elizaos.plugins.solana.axiom_copy_trader import run_copy_trade_monitor as _run_ct
                    new_task = asyncio.create_task(_run_ct(runtime))
                    named_tasks[task_name] = new_task
                    print(f"[watchdog] ♻️ '{task_name}' restarted in-place (attempt {_restarts[task_name]})")
                    try:
                        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg
                        asyncio.create_task(_tg(f"♻️ <b>Copy trade monitor restarted</b> (in-place, attempt {_restarts[task_name]})"))
                    except Exception:
                        pass
                    continue
                except Exception as _restart_exc:
                    print(f"[watchdog] In-place restart failed: {_restart_exc}", file=sys.stderr)

            # All other tasks: trigger a full bot restart via SIGTERM
            # systemd will restart the process automatically (Restart=always)
            await asyncio.sleep(RESTART_DELAY)
            print(f"[watchdog] Triggering full restart via SIGTERM", file=sys.stderr)
            os.kill(os.getpid(), _sig.SIGTERM)
            return


if __name__ == "__main__":
    asyncio.run(main())
