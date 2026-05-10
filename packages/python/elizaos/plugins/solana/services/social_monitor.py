"""GrokSocialMonitorService — X (Twitter) social intelligence via xAI Grok API.

Replaces the Nitter RSS scraper with direct xAI API calls. Uses Grok's live X
search capability to scan for Solana meme token launches and hype signals.

Every GROK_POLL_INTERVAL_SECS (5 min), asks Grok to search X for:
  - New Solana meme token launches (pump.fun contract addresses)
  - Trending crypto tokens with viral narrative
  - Upcoming token launches being hyped

When Grok finds a high-confidence token:
  - Emits "social_signal" event (same interface as before)
  - Stores mint/name in _grok_signals dict for is_grok_confirmed() checks
  - Grok-confirmed tokens get TP=200% in the trading bot instead of TP=60%

Brain communication:
  run_traderbot.py calls svc.is_grok_confirmed(mint) before any buy.
  If True → open_position(..., grok_confirmed=True) → TP set to 3.00x (+200%).

API: xAI Grok API (OpenAI-compatible)
  Base URL: https://api.x.ai/v1
  Model: grok-3-latest (has live X search built-in)
  Key: GROK_API_KEY env var
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from elizaos.types import Service

if TYPE_CHECKING:
    from elizaos.types import IAgentRuntime

SOCIAL_MONITOR_SERVICE_TYPE = "social_monitor"

# How often to query Grok for fresh X signals (seconds)
GROK_POLL_INTERVAL_SECS: int = 300  # 5 minutes — cost-controlled; was 90s but burned credits at ~2400 calls/day

# How long a Grok signal stays "hot" (seconds)
SIGNAL_TTL_SECS: int = 1800  # 30 minutes

# Solana mint address pattern: 32-44 base58 chars
_MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")

# Narrative keywords that suggest a viral meme token
_NARRATIVE_KEYWORDS = frozenset({
    "ca:", "contract:", "mint:", "launch", "launching", "just launched",
    "pump.fun", "pumpfun", "solana", "$sol", "bonding", "100x", "1000x",
    "gem", "moon", "ape in", "early", "new token", "stealth launch",
})

# Financial narrative keywords — tokens like INCOME, MEFAI, BAGWORKOOR
# These are "story tokens" with real missions, not just meme launches
_FINANCIAL_NARRATIVE_KEYWORDS = frozenset({
    "universal income", "ubi", "financial freedom", "passive income",
    "financial ai", "meta financial", "financial revolution", "bag work",
    "earn while", "defi narrative", "ai finance", "economic freedom",
    "financial independence", "money revolution", "ai financial",
    "universal basic", "income protocol", "freedom finance",
    "financial meta", "meta finance", "workoor", "bagworkoor",
    "mefai", "financial agent", "ai economy", "universal wage",
})

# Confidence threshold for "Grok confirmed" (0-10 scale we assign internally)
GROK_CONFIRM_THRESHOLD: int = 7


class SocialMonitorService(Service):
    service_type = SOCIAL_MONITOR_SERVICE_TYPE

    @property
    def capability_description(self) -> str:
        return "Monitor X (Twitter) via Grok API for Solana meme token pre-launch signals."

    def __init__(self) -> None:
        self._runtime: IAgentRuntime | None = None
        self._task: asyncio.Task[None] | None = None
        self._signals: deque[dict[str, Any]] = deque(maxlen=200)

        # mint_address → {confidence, name, text, detected_at}
        self._grok_signals: dict[str, dict[str, Any]] = {}

        # Token name/ticker → mint (for name-based lookups)
        self._name_to_mint: dict[str, str] = {}

        self._grok_api_key: str = ""
        self._gemini_api_key: str = ""
        self._enabled: bool = False
        self._last_query_at: float = 0.0

        # Discovery queue — found tokens waiting for on-chain validation
        self._discovery_queue: list[dict[str, Any]] = []
        self._discovery_processed: set[str] = set()  # mints already acted on

        # Focus directive — can change what we search for at runtime
        self._grok_focus: str = ""

    @classmethod
    async def start(cls, runtime: IAgentRuntime) -> SocialMonitorService:
        service = cls()
        service._runtime = runtime
        service._grok_api_key = os.getenv("GROK_API_KEY", "")
        service._gemini_api_key = os.getenv("GOOGLE_GENERATIVE_AI_API_KEY", "")

        if os.getenv("SOCIAL_MONITOR_ENABLED", "true").lower() in ("false", "0", "no"):
            runtime.logger.info(
                "SocialMonitorService disabled via SOCIAL_MONITOR_ENABLED=false",
                src="service:social_monitor",
            )
            service._enabled = False
            service._task = asyncio.create_task(asyncio.sleep(0))
            return service

        if service._grok_api_key:
            service._enabled = True
            runtime.logger.info(
                "SocialMonitorService started — Grok (X/Twitter) ACTIVE, "
                "scanning every 5 minutes for token launches",
                src="service:social_monitor",
            )
        elif service._gemini_api_key:
            service._enabled = True
            runtime.logger.info(
                "SocialMonitorService started — Gemini (Google Search) ACTIVE, "
                "replacing Grok for social signal detection",
                src="service:social_monitor",
            )
        else:
            runtime.logger.warning(
                "GROK_API_KEY and GOOGLE_GENERATIVE_AI_API_KEY both missing — "
                "SocialMonitorService will be inactive",
                src="service:social_monitor",
            )

        service._task = asyncio.create_task(service._monitor_loop())
        return service

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ──────────────────────────────────────── public API ──────────────────────

    async def _gemini_search(self, prompt: str, timeout: float = 15.0) -> str:
        """Run a Gemini query with Google Search grounding. Returns raw text."""
        try:
            from google import genai as _gai
            gc = _gai.Client(api_key=self._gemini_api_key)
            resp = await asyncio.wait_for(
                gc.aio.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=prompt,
                    config={"tools": [{"google_search": {}}], "max_output_tokens": 600},
                ),
                timeout=timeout,
            )
            return (resp.text or "").strip()
        except Exception as exc:
            return f"GEMINI_ERROR: {exc}"

    def is_grok_confirmed(self, mint: str) -> bool:
        """Return True if Grok/Gemini has flagged this mint as a high-confidence signal."""
        self._evict_expired()
        entry = self._grok_signals.get(mint)
        if entry is None:
            return False
        return entry.get("confidence", 0) >= GROK_CONFIRM_THRESHOLD

    def get_confidence(self, mint: str) -> int:
        """Return Grok confidence score for mint (0-10). 0 = not seen."""
        self._evict_expired()
        return self._grok_signals.get(mint, {}).get("confidence", 0)

    async def check_token_buzz(self, mint: str, name: str, symbol: str) -> tuple[bool, int, str]:
        """Targeted Grok X search for a SPECIFIC token before buying.

        Called by each strategy loop immediately before executing a buy.
        Returns (grok_confirmed, confidence_score, reason_string).

        Uses a fast timeout (12s) so it never delays a time-critical grad-snipe.
        If Grok API is unavailable, returns (False, 0, 'api_unavailable') — trade
        proceeds normally with default TP.
        """
        if not self._enabled:
            return False, 0, "social_monitor_disabled"

        # Cache hit — already confirmed by background scanner
        self._evict_expired()
        cached = self._grok_signals.get(mint)
        if cached:
            score = cached.get("confidence", 0)
            return score >= GROK_CONFIRM_THRESHOLD, score, "cached"

        token_ref = f"{name} ({symbol})" if name and symbol else (name or symbol or mint[:8])
        prompt = (
            f"Search for recent mentions of this Solana token: {token_ref}\n"
            f"Contract address: {mint}\n\n"
            "Use Google Search to find the latest information. Answer:\n"
            "1. Is this token being actively discussed online in the last hour? (yes/no)\n"
            "2. How many posts/tweets mention it? (rough count)\n"
            "3. What is the overall sentiment? (bullish/neutral/bearish/not_found)\n"
            "4. Are there any influencers or large accounts posting about it? (yes/no)\n"
            "5. Are there any scam warnings, rug reports, or red flags? (yes/no)\n\n"
            "Reply in this EXACT format:\n"
            "BUZZ: active=<yes|no> tweets=<count> sentiment=<bullish|neutral|bearish|not_found> "
            "influencer=<yes|no> redflag=<yes|no>"
        )

        raw = ""
        # Try Grok first (real-time X access)
        if self._grok_api_key:
            try:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=self._grok_api_key, base_url="https://api.x.ai/v1")
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model="grok-3-mini",
                        messages=[
                            {"role": "system", "content":
                                "You are a real-time X (Twitter) social scanner for Solana meme tokens. "
                                "Search X right now for the specific token asked about."},
                            {"role": "user", "content": prompt},
                        ],
                    ),
                    timeout=12.0,
                )
                raw = (response.choices[0].message.content or "").strip()
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(f"[grok] buzz check failed: {exc}", src="service:social_monitor")

        # Gemini fallback (Google Search grounding)
        if not raw and self._gemini_api_key:
            try:
                raw = await asyncio.wait_for(self._gemini_search(prompt, timeout=12.0), timeout=14.0)
                if raw.startswith("GEMINI_ERROR"):
                    raw = ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(f"[gemini] buzz check failed: {exc}", src="service:social_monitor")

        if not raw:
            return False, 0, "api_error: all sources failed"

        # Parse BUZZ: line
        buzz_match = re.search(r"BUZZ:(.+)", raw)
        if not buzz_match:
            return False, 0, "parse_error"

        parts = buzz_match.group(1)

        def _val(key: str) -> str:
            m = re.search(rf"{key}=(\S+)", parts)
            return m.group(1).lower() if m else ""

        active = _val("active") == "yes"
        sentiment = _val("sentiment")
        influencer = _val("influencer") == "yes"
        redflag = _val("redflag") == "yes"
        try:
            tweet_count = int(_val("tweets"))
        except ValueError:
            tweet_count = 0

        # Score the buzz — red flag overrides everything
        if redflag:
            score = 0
            confirmed = False
            reason = f"RED FLAG DETECTED — scam/rug warning found online"
            self._grok_signals[mint] = {
                "name": name, "symbol": symbol, "confidence": 0,
                "hype": "bearish", "detected_at": time.time(), "text": reason,
            }
            return False, 0, reason

        score = 0
        if active:
            score += 2
        if sentiment == "bullish":
            score += 3
        elif sentiment == "neutral":
            score += 1
        if influencer:
            score += 3
        if tweet_count >= 20:
            score += 2
        elif tweet_count >= 5:
            score += 1

        # Bonus: financial narrative tokens tend to have more sustained pumps
        _name_lower = f"{name} {symbol}".lower()
        if any(kw in _name_lower for kw in _FINANCIAL_NARRATIVE_KEYWORDS):
            score += 1

        score = min(score, 10)

        confirmed = score >= GROK_CONFIRM_THRESHOLD
        reason = f"active={active} tweets={tweet_count} sentiment={sentiment} influencer={influencer}"

        # Cache the result
        self._grok_signals[mint] = {
            "name": name,
            "symbol": symbol,
            "confidence": score,
            "hype": sentiment,
            "detected_at": time.time(),
            "text": reason,
        }

        if self._runtime:
            flag = "🔥 SOCIAL-CONFIRMED" if confirmed else "❌ no buzz"
            self._runtime.logger.info(
                f"[grok] {flag} {token_ref} — score={score}/10 {reason}",
                src="service:social_monitor",
            )

        return confirmed, score, reason

    async def pre_trade_ai_gate(
        self,
        mint: str,
        name: str,
        symbol: str,
        token_data: dict[str, Any],
        require_buzz: bool = False,
    ) -> tuple[bool, str]:
        """Mandatory pre-trade AI gate — runs before EVERY buy across all strategies.

        Runs two parallel checks:
          1. Grok X sentiment scan  — blocks if bearish/rug/honeypot signals found on X
          2. GPT-4o-mini risk score — blocks if rug-risk signals detected in on-chain data

        Returns (allow_trade: bool, reason: str).
        Fails OPEN (allow) on timeout/API error so valid trades are never missed.

        Decision logic:
          BLOCK  → Grok finds rug/honeypot/scam warning on X
          BLOCK  → Grok finds bearish sentiment (active sellers, dev dump talk)
          BLOCK  → GPT rates this SKIP with ≥70% confidence
          ALLOW  → everything else, including "not_found" (new tokens often have no X yet)

        Timeout: 10s per check (parallel), so total gate delay ≤ 10s.
        """
        _openai_key = os.getenv("OPENAI_API_KEY", "")
        _grok_key   = self._grok_api_key

        _gemini_key = self._gemini_api_key

        # ── Check 1: Social rug-warning scan (Grok → Gemini fallback) ────────
        async def _grok_check() -> tuple[bool, str]:
            """Ask Grok/Gemini to search for rug/honeypot/scam signals on this specific token."""
            if not _grok_key and not _gemini_key:
                return True, "social_scan_disabled"
            # Build token reference and prompt (shared by Grok + Gemini)
            token_ref = f"{name} ({symbol})" if name and symbol else (name or symbol or mint[:8])
            _h24_ctx = token_data.get("price_change_h24", None)
            _age_ctx  = token_data.get("age_mins", 0) or 0
            _trend_ctx = ""
            if _h24_ctx is not None and _h24_ctx <= -25:
                _trend_ctx = f"\nIMPORTANT: Price is down {abs(_h24_ctx):.0f}% in the last 24h. This token may already be dead."
            elif _age_ctx > 1440:  # > 24h
                _trend_ctx = f"\nIMPORTANT: This token is {_age_ctx/60:.0f}h old. Look for evidence it has already had its pump and is declining."
            _buzz_required_note = (
                "\n\nIMPORTANT: This is a bonding-curve token. I need ACTIVE X buzz about its "
                "theme or meme RIGHT NOW. Search for the token name/meme (not just the CA). "
                "If you find zero tweets or community discussion about this theme in the last 24h, "
                "use reason=not_found. A real meme token will have people talking about the CHARACTER "
                "or CONCEPT — if nobody on X has heard of it, that's a red flag."
            ) if require_buzz else ""
            prompt = (
                f"Search for this Solana token: {token_ref}\n"
                f"Contract: {mint}{_trend_ctx}{_buzz_required_note}\n\n"
                "I need to know ONE thing only — are there any WARNING signals?\n"
                "Look for: rug pull warnings, honeypot alerts, dev dump warnings, "
                "'avoid this token', 'scam', 'honeypot', 'dev sold', 'rugged', "
                "'already pumped', 'dead token', 'exit liquidity'.\n\n"
                "ALSO note if this token has a genuine FINANCIAL NARRATIVE (universal income, UBI, "
                "financial AI, economic freedom, passive income themes) or an organized X community "
                "(x.com/i/communities/ link) — these are POSITIVE signals, not warnings.\n\n"
                "Also check: is sentiment overwhelmingly bearish (people warning others to sell/avoid)?\n"
                "Also check: is the token's hype clearly PAST (people saying it already ran, missed the pump)?\n\n"
                "Reply in EXACTLY this format:\n"
                "GATE: verdict=<ALLOW|BLOCK> reason=<one short phrase>\n\n"
                "Use BLOCK for: rug/scam/honeypot warnings, strong bearish dump talk, "
                "OR clear signals the pump already happened days ago (everyone missed it).\n"
                "Use ALLOW for: neutral, bullish, or mixed sentiment.\n"
                "Use reason=not_found if you cannot find any discussion about this token or its theme."
            )

            raw = ""
            _src = "social"

            # Try Grok first (real-time X search)
            if _grok_key:
                try:
                    from openai import AsyncOpenAI as _OAI
                    _gc = _OAI(api_key=_grok_key, base_url="https://api.x.ai/v1")
                    resp = await asyncio.wait_for(
                        _gc.chat.completions.create(
                            model="grok-3-mini",
                            messages=[
                                {"role": "system", "content": (
                                    "You are a real-time Solana rug-warning scanner with live X search. "
                                    "Search X right now for the token. Reply BLOCK only when you see clear "
                                    "rug/scam/honeypot warnings or developer dump alerts. "
                                    "If token is not found on X at all, reply ALLOW — new tokens rarely have X buzz yet."
                                )},
                                {"role": "user", "content": prompt},
                            ],
                        ),
                        timeout=10.0,
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    _src = "grok"
                except Exception as exc:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"[grok-gate] Grok unavailable: {exc}",
                            src="service:social_monitor",
                        )

            # Gemini fallback (Google Search grounding)
            if not raw and _gemini_key:
                try:
                    raw = await asyncio.wait_for(self._gemini_search(prompt, timeout=10.0), timeout=12.0)
                    if raw.startswith("GEMINI_ERROR"):
                        raw = ""
                    else:
                        _src = "gemini"
                except Exception as exc:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"[grok-gate] Gemini fallback failed: {exc}",
                            src="service:social_monitor",
                        )

            if not raw:
                return True, "social_gate_error_fail_open"

            m = re.search(r"GATE:\s*verdict=(\w+)\s+reason=(.+)", raw, re.IGNORECASE)
            if not m:
                return True, f"{_src}_parse_error"
            verdict = m.group(1).upper()
            reason  = m.group(2).strip()
            allow   = verdict != "BLOCK"
            # require_buzz=True: BC tokens must have ACTIVE X presence.
            # not_found means no organic community — high rug risk at 55-68% fill.
            if allow and require_buzz and "not_found" in reason.lower():
                allow = False
                reason = "no_x_buzz (require_buzz=True — BC token needs active X narrative)"
            if self._runtime:
                flag = "✅ ALLOW" if allow else "🛑 BLOCK"
                self._runtime.logger.info(
                    f"[social-gate] {flag} {token_ref[:30]} [{_src}] — {reason}",
                    src="service:social_monitor",
                )
            return allow, f"{_src}:{reason}"

        # ── Check 2: GPT-4o-mini rug risk assessment (Groq fallback) ─────────
        async def _gpt_check() -> tuple[bool, str]:
            """Use GPT-4o-mini (or Groq llama fallback) to assess rug risk from on-chain token metrics."""
            _groq_key = os.getenv("GROQ_API_KEY", "")
            # Skip entirely for bonding curve tokens — rugcheck + Grok X-check cover rug risk there.
            if token_data.get("is_bonding_curve"):
                return True, "gpt_skipped_bc"
            # Need at least one LLM key to run this check
            if not _openai_key and not _groq_key:
                return True, "risk_gate_disabled"

            is_fresh_grad = token_data.get("is_fresh_grad", False)
            liq      = token_data.get("liquidity_usd", token_data.get("liq_usd", 0))
            age_min  = token_data.get("age_mins", token_data.get("grad_age_mins", 0))
            m5       = token_data.get("price_change_m5", 0) or 0
            h1       = token_data.get("price_change_h1", None)
            h6       = token_data.get("price_change_h6", None)
            h24      = token_data.get("price_change_h24", None)
            score    = token_data.get("score", 0)
            dex      = token_data.get("dex", "pumpswap")
            vol_h1   = token_data.get("vol_h1_usd", 0)
            vol_h24  = token_data.get("vol_h24_usd", 0)
            has_soc  = token_data.get("has_socials", False)

            age_h   = age_min / 60.0
            h1_str  = f"{h1:+.1f}%" if h1 is not None else "N/A"
            h6_str  = f"{h6:+.1f}%" if h6 is not None else "N/A"
            h24_str = f"{h24:+.1f}%" if h24 is not None else "N/A"
            vol_trend = ""
            if vol_h24 > 0 and vol_h1 > 0:
                _vt = vol_h1 / (vol_h24 / 24)
                vol_trend = f"{_vt:.1f}x expected hourly"
            elif vol_h1 > 0:
                vol_trend = f"${vol_h1:,.0f} (no h24 baseline)"

            _system_msg = (
                "You are a Solana meme token rug-risk assessor. "
                "Be conservative — only SKIP on clear quantitative red flags, "
                "not on subjective quality. Most tokens you see will be TRADE."
            )
            if is_fresh_grad:
                prompt = (
                    f"This token JUST graduated from the pump.fun bonding curve onto PumpSwap DEX.\n"
                    f"It has been trading for {age_min:.0f} minutes — DexScreener has not indexed H1/H6/H24 data yet.\n"
                    f"Liquidity: ${liq:,.0f} (graduation deposit — expected ~$11,000-$15,000)\n"
                    f"Price change M5: {m5:+.1f}%\n\n"
                    "IMPORTANT: Do NOT penalise this token for:\n"
                    "  - Low or zero H1/H6/H24 price history (not indexed yet)\n"
                    "  - Zero volume history (no baseline exists)\n"
                    "  - Low social score (socials not yet linked on DexScreener)\n"
                    "  - Liquidity ~$11k-$15k (this is normal graduation liquidity)\n\n"
                    "SKIP only if:\n"
                    "  • M5 < -15%: token is already dumping hard at launch\n"
                    "  • M5 < -8% AND liquidity < $10,000: dump + thin pool\n\n"
                    "TRADE if M5 is flat or positive — this is a fresh graduation entry.\n"
                    "Reply ONLY in this format:\n"
                    "RISK: decision=<TRADE|SKIP> confidence=<0-100> reason=<one short phrase>"
                )
            else:
                prompt = (
                    f"Assess whether this Solana meme token is in structural decline or fresh momentum:\n\n"
                    f"DEX: {dex}\n"
                    f"Liquidity: ${liq:,.0f}\n"
                    f"Token age: {age_min:.0f} min ({age_h:.1f}h)\n"
                    f"Price change M5: {m5:+.1f}%\n"
                    f"Price change H1: {h1_str}\n"
                    f"Price change H6: {h6_str}\n"
                    f"Price change H24: {h24_str}\n"
                    f"1-hour volume: ${vol_h1:,.0f} | 24h volume: ${vol_h24:,.0f}\n"
                    f"Volume trend vs expected: {vol_trend}\n"
                    f"Scout quality score: {score}/16\n"
                    f"Has socials (website/twitter): {has_soc}\n\n"
                    "SKIP signals (clear quantitative red flags only):\n"
                    "  • H24 < -25%: structural downtrend — token already pumped and is dying\n"
                    "  • H6 < -20%: active collapse in last 6 hours\n"
                    "  • H6 < -15% AND H24 < -15%: consistent multi-timeframe decline\n"
                    "  • Age > 24h AND H24 < -15%: old declining token — no alpha left\n"
                    "  • Volume trend < 0.2x expected: trading is drying up (dying token)\n"
                    "  • Liquidity < $10,000: thin pool, easy to manipulate\n"
                    "  • M5 < -15%: already in freefall right now\n"
                    "  • Score < 6 AND no socials AND liq < $8k: multiple simultaneous red flags\n\n"
                    "TRADE if: token is fresh (<6h), trending up on H1/H6, or strong H24 with momentum now.\n"
                    "Reply ONLY in this format:\n"
                    "RISK: decision=<TRADE|SKIP> confidence=<0-100> reason=<one short phrase>"
                )

            raw = ""
            _src = "risk_gate"

            # Try OpenAI GPT-4o-mini first
            if _openai_key:
                try:
                    from openai import AsyncOpenAI as _OAI2
                    _resp = await asyncio.wait_for(
                        _OAI2(api_key=_openai_key).chat.completions.create(
                            model="gpt-4o-mini",
                            messages=[
                                {"role": "system", "content": _system_msg},
                                {"role": "user", "content": prompt},
                            ],
                            max_tokens=80,
                        ),
                        timeout=8.0,
                    )
                    raw = (_resp.choices[0].message.content or "").strip()
                    _src = "gpt"
                except Exception as exc:
                    _exc_str = str(exc)
                    if "429" in _exc_str or "quota" in _exc_str.lower() or "insufficient" in _exc_str.lower():
                        if self._runtime:
                            self._runtime.logger.warning(
                                f"[gpt-gate] OpenAI quota exceeded — trying Groq fallback",
                                src="service:social_monitor",
                            )
                    # Fall through to Groq

            # Groq fallback (llama-3.3-70b-versatile — fast, free tier, OpenAI-compatible)
            if not raw and _groq_key:
                try:
                    from openai import AsyncOpenAI as _OAI3
                    _resp = await asyncio.wait_for(
                        _OAI3(
                            api_key=_groq_key,
                            base_url="https://api.groq.com/openai/v1",
                        ).chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=[
                                {"role": "system", "content": _system_msg},
                                {"role": "user", "content": prompt},
                            ],
                            max_tokens=80,
                        ),
                        timeout=8.0,
                    )
                    raw = (_resp.choices[0].message.content or "").strip()
                    _src = "groq"
                except Exception as exc:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"[gpt-gate] Groq fallback failed: {exc}",
                            src="service:social_monitor",
                        )

            if not raw:
                return True, "risk_gate_fail_open"

            m = re.search(r"RISK:\s*decision=(\w+)\s+confidence=(\d+)\s+reason=(.+)", raw, re.IGNORECASE)
            if not m:
                return True, f"{_src}_parse_error"
            decision   = m.group(1).upper()
            confidence = int(m.group(2))
            reason     = m.group(3).strip()
            # Only block if model says SKIP with ≥70% confidence
            allow = not (decision == "SKIP" and confidence >= 70)
            if self._runtime:
                flag = "✅ TRADE" if allow else f"🛑 SKIP({confidence}%)"
                self._runtime.logger.info(
                    f"[{_src}-gate] {flag} {name or mint[:8]} — {reason}",
                    src="service:social_monitor",
                )
            return allow, f"{_src}:{reason}({confidence}%)"

        # ── Run both checks in parallel ───────────────────────────────────────
        try:
            (grok_allow, grok_reason), (gpt_allow, gpt_reason) = await asyncio.gather(
                _grok_check(), _gpt_check(), return_exceptions=False
            )
        except Exception:
            return True, "gate_error_fail_open"

        if not grok_allow:
            return False, f"GROK BLOCK — {grok_reason}"
        if not gpt_allow:
            return False, f"GPT BLOCK — {gpt_reason}"
        return True, f"gate_ok ({grok_reason} | {gpt_reason})"

    def get_recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest-first list of recent social signals."""
        items = list(self._signals)
        items.reverse()
        return items[:limit]

    def get_discovery_queue(self) -> list[dict[str, Any]]:
        """Return Grok-discovered tokens awaiting on-chain validation + purchase.

        Each entry: {mint, name, symbol, confidence, fill_pct, real_sol,
                     holder_count, age_secs, detected_at, x_posts, hype}
        Caller should call mark_discovery_processed(mint) after acting on it.
        """
        cutoff = time.time() - 600  # 10-minute TTL
        self._discovery_queue = [
            t for t in self._discovery_queue
            if t.get("detected_at", 0) > cutoff
            and t.get("mint", "") not in self._discovery_processed
        ]
        return list(self._discovery_queue)

    def mark_discovery_processed(self, mint: str) -> None:
        """Remove a mint from the discovery queue after buy attempt."""
        self._discovery_processed.add(mint)
        self._discovery_queue = [t for t in self._discovery_queue if t.get("mint") != mint]

    def set_focus(self, focus: str) -> None:
        """Set a Grok focus directive. Eliza can change this at runtime to redirect
        what Grok searches for on X. Empty string = default general scan.
        Examples: 'AI tokens', 'gaming', 'Elon mentions', 'TRUMP related', 'Meteora launches'
        """
        self._grok_focus = focus.strip()
        if self._runtime:
            self._runtime.logger.info(
                f"[grok] Focus updated → '{self._grok_focus or 'default (all Solana meme tokens)'}'"
                " — new directive takes effect on next scan cycle",
                src="service:social_monitor",
            )

    def get_focus(self) -> str:
        """Return current Grok focus directive (empty string = default)."""
        return self._grok_focus

    async def scan_new_pumpfun_launches(self) -> list[dict[str, Any]]:
        """Ask Grok to search X for NEW pump.fun token launches in the last 2 minutes.

        For each CA found, validates current on-chain state via pump.fun API.
        Tokens passing validation are added to _discovery_queue for the grok_bc_sniper_loop.
        Returns list of newly queued tokens.
        """
        if not self._enabled:
            return []

        try:
            import aiohttp
        except ImportError:
            return []

        prompt = (
            "Search for Solana pump.fun token launches posted in the LAST 3 MINUTES.\n\n"
            "Pump.fun contract addresses are 32-44 character base58 strings, most end in 'pump'.\n"
            "Look for: tweets containing a CA (contract address), ticker symbol, and launch announcement.\n\n"
            "PRIORITY 1 — KOL DERIVATIVES (highest move potential — can do +1000%–+10000%):\n"
            "  Check if @elonmusk, @VitalikButerin, @cz_binance, @kanyewest, or any major celebrity/influencer posted ANYTHING in the last 5 minutes.\n"
            "  If they did, immediately look for pump.fun tokens launched in response — people launch tokens within 2-3 minutes of any Elon/celebrity post.\n"
            "  Example: Elon posts about Grok chibi → 'CHIBI' token launched 2 min later. Elon posts about monkeys → 'PUPPY' launched.\n"
            "  These are the highest-priority finds — mark them hype=viral and type=kol_derivative\n\n"
            "PRIORITY 2 — OTHER HIGH-MOVE TYPES:\n"
            "  • VIRAL MEME NAMES — absurd, funny, or unexpected names\n"
            "  • CULTURAL REFERENCES — tokens tied to current events, trending topics, breaking news\n"
            "  • FINANCIAL NARRATIVE — income, wealth, freedom, AI finance themes\n"
            "  • COORDINATED LAUNCHES — tokens with pre-built website + Twitter + Discord + DexScreener boost appearing together\n\n"
            "Focus on: early tweets with the CA posted before the token is widely known. New launches only — posted < 3 min ago.\n\n"
            "For EACH new token found, respond on a separate line in this EXACT format:\n"
            "LAUNCH: ca=<full_contract_address> ticker=<TICKER> name=<TokenName> "
            "tweets=<count_mentioning_it> hype=<low|medium|high|viral> "
            "type=<kol_derivative|viral_meme|cultural|financial_narrative|coordinated|other>\n\n"
            "If no new launches found in last 3 minutes: respond NO_LAUNCHES\n\n"
            "ONLY include tokens with a real contract address from actual tweets. Do not guess or make up addresses."
        )

        raw = ""
        if self._grok_api_key:
            try:
                from openai import AsyncOpenAI
                _gc = AsyncOpenAI(api_key=self._grok_api_key, base_url="https://api.x.ai/v1")
                response = await asyncio.wait_for(
                    _gc.chat.completions.create(
                        model="grok-3-mini",
                        messages=[
                            {"role": "system", "content": (
                                "You are a real-time Solana meme token scanner with live X search access. "
                                "Find pump.fun token CAs being posted on X RIGHT NOW. "
                                "Be precise — only report tokens with actual verifiable contract addresses from real tweets."
                            )},
                            {"role": "user", "content": prompt},
                        ],
                    ),
                    timeout=20.0,
                )
                raw = response.choices[0].message.content or ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-discovery] Grok scan failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw and self._gemini_api_key:
            try:
                raw = await asyncio.wait_for(self._gemini_search(prompt, timeout=18.0), timeout=20.0)
                if raw.startswith("GEMINI_ERROR"):
                    raw = ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-discovery] Gemini scan failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw:
            return []

        if "NO_LAUNCHES" in raw:
            if self._runtime:
                self._runtime.logger.info(
                    "[grok-discovery] No new pump.fun launches on X in last 2 minutes",
                    src="service:social_monitor",
                )
            return []

        # Parse LAUNCH: lines
        found_cas: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("LAUNCH:"):
                continue
            parts = line[len("LAUNCH:"):].strip()

            def _val(key: str) -> str:
                m = re.search(rf"{key}=(\S+)", parts)
                return m.group(1) if m else ""

            ca = _val("ca")
            ticker = _val("ticker")
            name = _val("name")
            hype = _val("hype").lower()
            token_type = _val("type").lower()
            try:
                tweet_count = int(_val("tweets"))
            except ValueError:
                tweet_count = 1

            # Basic CA validation: 32-44 base58 chars
            if not ca or not re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', ca):
                continue
            if ca in self._discovery_processed:
                continue

            # Score confidence
            score = 3  # base: Grok found it on X
            hype_bonus = {"low": 0, "medium": 1, "high": 2, "viral": 3}.get(hype, 0)
            score += hype_bonus
            if tweet_count >= 5:
                score += 2
            elif tweet_count >= 2:
                score += 1
            # Type bonus — KOL derivatives and viral memes have the highest move potential
            # kol_derivative: CHIBI +6,936%, PUPPY +1,268% — direct Elon/celebrity tweet spawns
            type_bonus = {"kol_derivative": 3, "viral_meme": 2, "cultural": 2, "coordinated": 2, "financial_narrative": 2, "commodity": 1}.get(token_type, 0)
            score += type_bonus
            score = min(score, 10)

            found_cas.append({
                "mint": ca,
                "ticker": ticker,
                "name": name,
                "hype": hype,
                "tweet_count": tweet_count,
                "confidence": score,
            })

        if not found_cas:
            return []

        if self._runtime:
            self._runtime.logger.info(
                f"[grok-discovery] Found {len(found_cas)} new launch(es) on X — validating on-chain...",
                src="service:social_monitor",
            )

        # Validate each CA against pump.fun API
        queued: list[dict[str, Any]] = []
        async with aiohttp.ClientSession() as session:
            for token in found_cas:
                ca = token["mint"]
                try:
                    validated = await self._validate_pumpfun_token(session, token)
                    if validated:
                        queued.append(validated)
                        self._discovery_queue.append(validated)
                        # Also cache in grok_signals for is_grok_confirmed() checks
                        self._grok_signals[ca] = {
                            "name": token["name"],
                            "symbol": token["ticker"],
                            "confidence": token["confidence"],
                            "hype": token["hype"],
                            "detected_at": time.time(),
                            "text": f"X launch: {token['tweet_count']} tweets, hype={token['hype']}",
                        }
                        if self._runtime:
                            self._runtime.logger.info(
                                f"[grok-discovery] QUEUED {token['name']} ({token['ticker']}) "
                                f"ca={ca[:8]}... confidence={token['confidence']}/10 "
                                f"fill={validated.get('fill_pct', 0):.0f}% "
                                f"sol={validated.get('real_sol', 0):.2f}",
                                src="service:social_monitor",
                            )
                except Exception as exc:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"[grok-discovery] Validation failed for {ca[:8]}: {exc}",
                            src="service:social_monitor",
                        )

        return queued

    async def _validate_pumpfun_token(
        self, session: Any, token: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fetch pump.fun API to validate CA exists and get current bonding curve state.

        Returns enriched token dict if valid, None if token not found or already too far along.
        """
        ca = token["mint"]
        url = f"https://frontend-api-v3.pump.fun/coins/{ca}"
        try:
            async with session.get(url, timeout=8, ssl=False) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception:
            return None

        if not data or not isinstance(data, dict):
            return None

        # Extract bonding curve state
        virt_sol = float(data.get("virtual_sol_reserves", 0) or 0)
        virt_tok = float(data.get("virtual_token_reserves", 0) or 1)
        real_sol = float(data.get("real_sol_reserves", 0) or 0) / 1e9  # lamports → SOL
        complete = bool(data.get("complete", False))
        holder_count = int(data.get("holder_count", 0) or 0)
        created_ts = int(data.get("created_timestamp", 0) or 0) / 1000  # ms → s
        age_secs = time.time() - created_ts if created_ts > 0 else 999999
        name = data.get("name", token.get("name", ""))
        symbol = data.get("symbol", token.get("ticker", ""))

        # Bonding curve fill % (0-100)
        TOKEN_TOTAL = 1_000_000_000 * 1e6  # 1B tokens, 6 decimals
        fill_pct = max(0.0, min(100.0, (1 - virt_tok / TOKEN_TOTAL) * 100)) if virt_tok > 0 else 0.0

        # Reject if already graduated, too old, or fill too high
        if complete:
            return None  # already graduated — use grad-snipe instead
        if age_secs > 600:  # > 10 minutes old — missed the window
            return None
        if fill_pct > 45:  # > 45% fill — too late for early entry
            return None
        if real_sol < 0.3:  # < 0.3 SOL raised — no traction yet
            return None

        return {
            "mint": ca,
            "name": name,
            "symbol": symbol,
            "confidence": token["confidence"],
            "hype": token["hype"],
            "tweet_count": token.get("tweet_count", 0),
            "fill_pct": fill_pct,
            "real_sol": real_sol,
            "holder_count": holder_count,
            "age_secs": age_secs,
            "detected_at": time.time(),
            "x_posts": token.get("tweet_count", 0),
        }

    async def scan_financial_narrative_launches(self) -> list[dict[str, Any]]:
        """Ask Grok to search X for pump.fun tokens with financial/AI narrative themes.

        Targets tokens like INCOME (Universal High Income), MEFAI (Meta Financial AI),
        BAGWORKOOR — tokens with real missions, X communities, and financial story angles.
        These are high-conviction plays with genuine communities behind them.

        Looks for:
          - Financial/UBI/income freedom narratives
          - AI + finance crossover tokens
          - Tokens with X community links (x.com/i/communities/)
          - Real project signals: website + telegram + active X presence
          - Tokens trending due to a financial narrative (not just "100x gem")

        Returns list of newly queued tokens (same format as scan_new_pumpfun_launches).
        """
        if not self._enabled:
            return []

        try:
            import aiohttp
        except ImportError:
            return []

        prompt = (
            "Search for Solana pump.fun tokens that have a FINANCIAL NARRATIVE "
            "or REAL COMMUNITY behind them — NOT just generic '100x gem' meme tokens.\n\n"
            "I'm looking for tokens like these examples:\n"
            "  - 'Universal High Income' (INCOME) — UBI/financial freedom narrative\n"
            "  - 'Meta Financial AI' (MEFAI) — AI disrupting finance narrative\n"
            "  - 'THE BAGWORKOOR' — unique cultural/financial identity narrative\n\n"
            "Search for pump.fun tokens posted in the LAST 5 MINUTES on X that match ANY of these patterns:\n"
            "  1. FINANCIAL/INCOME NARRATIVE: tokens about universal income, UBI, passive income, "
            "financial freedom, financial AI, economic revolution, earning protocols\n"
            "  2. AI + FINANCE crossover: tokens combining AI with finance/money/economy themes\n"
            "  3. X COMMUNITY tokens: tokens where the tweet includes a link to x.com/i/communities/ "
            "(real organized community behind the token)\n"
            "  4. REAL PROJECT tokens: tokens posted with website URL + Telegram + real description "
            "(not just a CA and ticker)\n"
            "  5. TRENDING NARRATIVE: any token going viral because of a story/mission people believe in\n\n"
            "For EACH token found, respond on a separate line in this EXACT format:\n"
            "NARRATIVE: ca=<full_contract_address> ticker=<TICKER> name=<TokenName> "
            "theme=<financial|ai_finance|community|real_project|trending_narrative> "
            "tweets=<count> hype=<low|medium|high|viral> "
            "has_community=<yes|no> has_website=<yes|no> has_telegram=<yes|no>\n\n"
            "If no such tokens found in the last 5 minutes: respond NO_NARRATIVE_TOKENS\n\n"
            "CRITICAL: Only include tokens with a real contract address from actual tweets. "
            "Pump.fun CAs are 32-44 character base58 strings, most end in 'pump'. "
            "Do not guess or make up addresses."
        )

        raw = ""
        if self._grok_api_key:
            try:
                from openai import AsyncOpenAI
                _gc = AsyncOpenAI(api_key=self._grok_api_key, base_url="https://api.x.ai/v1")
                response = await asyncio.wait_for(
                    _gc.chat.completions.create(
                        model="grok-3-mini",
                        messages=[
                            {"role": "system", "content": (
                                "You are a real-time Solana meme token scanner with live X search. "
                                "Find tokens with genuine communities and financial narratives. "
                                "Be precise — only report tokens with verifiable contract addresses from real tweets."
                            )},
                            {"role": "user", "content": prompt},
                        ],
                    ),
                    timeout=25.0,
                )
                raw = response.choices[0].message.content or ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-narrative] Grok scan failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw and self._gemini_api_key:
            try:
                raw = await asyncio.wait_for(self._gemini_search(prompt, timeout=22.0), timeout=25.0)
                if raw.startswith("GEMINI_ERROR"):
                    raw = ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-narrative] Gemini scan failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw:
            return []

        if "NO_NARRATIVE_TOKENS" in raw:
            if self._runtime:
                self._runtime.logger.info(
                    "[grok-narrative] No financial narrative tokens on X in last 5 minutes",
                    src="service:social_monitor",
                )
            return []

        # Parse NARRATIVE: lines
        found_cas: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("NARRATIVE:"):
                continue
            parts = line[len("NARRATIVE:"):].strip()

            def _val(key: str) -> str:
                m = re.search(rf"{key}=(\S+)", parts)
                return m.group(1) if m else ""

            ca = _val("ca")
            ticker = _val("ticker")
            name = _val("name")
            theme = _val("theme").lower()
            hype = _val("hype").lower()
            has_community = _val("has_community") == "yes"
            has_website = _val("has_website") == "yes"
            has_telegram = _val("has_telegram") == "yes"
            try:
                tweet_count = int(_val("tweets"))
            except ValueError:
                tweet_count = 1

            # Basic CA validation
            if not ca or not re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', ca):
                continue
            if ca in self._discovery_processed:
                continue

            # Score — narrative tokens start higher because they have real communities
            score = 4  # base: Grok found a narrative token on X (higher than generic launch base=3)
            hype_bonus = {"low": 0, "medium": 1, "high": 2, "viral": 3}.get(hype, 0)
            score += hype_bonus
            if has_community:
                score += 2  # X community = organized buyers = sustained pump
            if has_website:
                score += 1
            if has_telegram:
                score += 1
            if tweet_count >= 5:
                score += 1
            score = min(score, 10)

            social_flags = []
            if has_community:
                social_flags.append("X-community")
            if has_website:
                social_flags.append("website")
            if has_telegram:
                social_flags.append("telegram")

            found_cas.append({
                "mint": ca,
                "ticker": ticker,
                "name": name,
                "hype": hype,
                "theme": theme,
                "tweet_count": tweet_count,
                "confidence": score,
                "has_community": has_community,
                "has_website": has_website,
                "has_telegram": has_telegram,
                "social_flags": social_flags,
                "source": "grok_narrative",
            })

        if not found_cas:
            return []

        if self._runtime:
            self._runtime.logger.info(
                f"[grok-narrative] Found {len(found_cas)} financial narrative token(s) on X — validating...",
                src="service:social_monitor",
            )

        # Validate each CA against pump.fun API
        queued: list[dict[str, Any]] = []
        async with aiohttp.ClientSession() as session:
            for token in found_cas:
                ca = token["mint"]
                try:
                    validated = await self._validate_pumpfun_token(session, token)
                    if validated:
                        # Boost confidence for tokens with X community
                        if token.get("has_community"):
                            validated["confidence"] = min(validated.get("confidence", 5) + 1, 10)
                        validated["source"] = "grok_narrative"
                        validated["theme"] = token.get("theme", "")
                        validated["social_flags"] = token.get("social_flags", [])
                        queued.append(validated)
                        self._discovery_queue.append(validated)
                        self._grok_signals[ca] = {
                            "name": token["name"],
                            "symbol": token["ticker"],
                            "confidence": token["confidence"],
                            "hype": token["hype"],
                            "detected_at": time.time(),
                            "text": (
                                f"narrative:{token.get('theme','?')} "
                                f"community={token['has_community']} "
                                f"website={token['has_website']} "
                                f"telegram={token['has_telegram']}"
                            ),
                        }
                        socials = ", ".join(token.get("social_flags", [])) or "none"
                        if self._runtime:
                            self._runtime.logger.info(
                                f"[grok-narrative] 🎯 QUEUED {token['name']} ({token['ticker']}) "
                                f"theme={token.get('theme','?')} socials=[{socials}] "
                                f"ca={ca[:8]}... confidence={token['confidence']}/10 "
                                f"fill={validated.get('fill_pct', 0):.0f}% "
                                f"sol={validated.get('real_sol', 0):.2f}",
                                src="service:social_monitor",
                            )
                except Exception as exc:
                    if self._runtime:
                        self._runtime.logger.warning(
                            f"[grok-narrative] Validation failed for {ca[:8]}: {exc}",
                            src="service:social_monitor",
                        )

        return queued

    def add_watch_account(self, account: str) -> None:
        """No-op kept for interface compatibility."""
        pass

    # ──────────────────────────────────────── internals ───────────────────────

    def _evict_expired(self) -> None:
        cutoff = time.time() - SIGNAL_TTL_SECS
        expired = [m for m, v in self._grok_signals.items() if v.get("detected_at", 0) < cutoff]
        for m in expired:
            del self._grok_signals[m]

    def _extract_mints(self, text: str) -> list[str]:
        candidates = _MINT_RE.findall(text)
        return [c for c in candidates if len(c) >= 32]

    def _score_token(self, name: str, symbol: str, description: str, tweet_count: int) -> int:
        """Score a token 0-10 based on social signal strength."""
        score = 0
        text = f"{name} {symbol} {description}".lower()

        # Narrative keywords
        kw_hits = sum(1 for kw in _NARRATIVE_KEYWORDS if kw in text)
        score += min(kw_hits * 2, 4)

        # Financial narrative keywords — bonus for story tokens (INCOME/MEFAI type)
        fin_hits = sum(1 for kw in _FINANCIAL_NARRATIVE_KEYWORDS if kw in text)
        if fin_hits > 0:
            score += min(fin_hits + 1, 3)  # +2 for 1 hit, +3 max

        # Tweet volume signal
        if tweet_count >= 10:
            score += 3
        elif tweet_count >= 5:
            score += 2
        elif tweet_count >= 2:
            score += 1

        # Mint address present = explicit CA shared
        if _MINT_RE.search(description):
            score += 3

        return min(score, 10)

    async def _query_grok(self) -> list[dict[str, Any]]:
        """Query Grok/Gemini for trending Solana meme tokens.

        Returns list of token dicts: {mint, name, symbol, confidence, text, source_tweets}
        """
        if not self._grok_api_key and not self._gemini_api_key:
            return []

        focus_directive = (
            f"\n\nFOCUS DIRECTIVE (from Jarvis): {self._grok_focus}"
            if self._grok_focus
            else ""
        )
        prompt = (
            "Search X (Twitter) right now for the latest Solana meme token launches.\n\n"
            "I need to know:\n"
            "1. Any new pump.fun token launches posted in the last 10 minutes with their "
            "contract address (CA) — these look like long base58 strings ending in 'pump'\n"
            "2. Any tokens being heavily hyped with words like '100x', 'gem', 'early', "
            "'just launched', 'stealth launch'\n"
            "3. Any Solana token CAs being shared by crypto influencers in the last 30 minutes\n\n"
            "For each token found, respond in this EXACT format on separate lines:\n"
            "TOKEN: name=<name> symbol=<TICKER> ca=<contract_address_or_none> "
            "tweets=<count> hype=<low|medium|high|viral>\n\n"
            "If no relevant tokens found in the last 30 minutes, respond: NO_SIGNALS\n\n"
            "Only include tokens launched or heavily discussed in the LAST 30 MINUTES on X."
            f"{focus_directive}"
        )

        raw = ""
        if self._grok_api_key:
            try:
                from openai import AsyncOpenAI
                _gc = AsyncOpenAI(api_key=self._grok_api_key, base_url="https://api.x.ai/v1")
                response = await asyncio.wait_for(
                    _gc.chat.completions.create(
                        model="grok-3-mini",
                        messages=[
                            {"role": "system", "content": (
                                "You are a real-time Solana meme token scanner with live X search. "
                                "Find tokens being launched or hyped RIGHT NOW on X. "
                                "Only report tokens with actual contract addresses. No speculation."
                            )},
                            {"role": "user", "content": prompt},
                        ],
                    ),
                    timeout=30.0,
                )
                raw = response.choices[0].message.content or ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-scan] Grok failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw and self._gemini_api_key:
            try:
                raw = await asyncio.wait_for(self._gemini_search(prompt, timeout=25.0), timeout=28.0)
                if raw.startswith("GEMINI_ERROR"):
                    raw = ""
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[social-scan] Gemini failed: {exc}",
                        src="service:social_monitor",
                    )

        if not raw:
            return []

        if "NO_SIGNALS" in raw:
            if self._runtime:
                self._runtime.logger.info(
                    "[grok] No new signals from X in last 30 minutes",
                    src="service:social_monitor",
                )
            return []

        tokens: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("TOKEN:"):
                continue
            parts_str = line[len("TOKEN:"):].strip()

            def _extract(key: str) -> str:
                m = re.search(rf"{key}=(\S+)", parts_str)
                return m.group(1) if m else ""

            name = _extract("name")
            symbol = _extract("symbol")
            ca = _extract("ca")
            hype_str = _extract("hype")
            tweets_str = _extract("tweets")

            if ca.lower() in ("none", ""):
                ca = ""

            try:
                tweet_count = int(tweets_str)
            except ValueError:
                tweet_count = 1

            # Map hype level to confidence modifier
            hype_bonus = {"low": 0, "medium": 1, "high": 2, "viral": 3}.get(hype_str.lower(), 0)
            confidence = self._score_token(name, symbol, parts_str, tweet_count) + hype_bonus
            confidence = min(confidence, 10)

            mints = self._extract_mints(ca) if ca else []

            tokens.append({
                "name": name,
                "symbol": symbol,
                "mint": mints[0] if mints else "",
                "confidence": confidence,
                "hype": hype_str,
                "tweet_count": tweet_count,
                "text": parts_str,
                "raw_line": line,
            })

        return tokens

    async def _process_grok_results(self, tokens: list[dict[str, Any]]) -> None:
        if not tokens:
            return

        now = time.time()
        for token in tokens:
            mint = token.get("mint", "")
            name = token.get("name", "unknown")
            symbol = token.get("symbol", "")
            confidence = token.get("confidence", 0)
            hype = token.get("hype", "")

            # For confirmed tokens with a CA, validate against pump.fun before trusting.
            # This catches hallucinated mint addresses (Grok inventing plausible-looking base58
            # strings for tokens it sees on X that have no real pump.fun presence).
            validated = None
            if mint and confidence >= GROK_CONFIRM_THRESHOLD:
                try:
                    import aiohttp as _aiohttp
                    async with _aiohttp.ClientSession() as _sess:
                        validated = await self._validate_pumpfun_token(_sess, token)
                except Exception:
                    validated = None

                if validated is None:
                    # CA doesn't exist on pump.fun (or already graduated/too old) — skip
                    if self._runtime:
                        self._runtime.logger.info(
                            f"[grok] HALLUCINATED CA rejected: {name} ({symbol}) "
                            f"confidence={confidence}/10 mint={mint[:8]}... — "
                            "not found on pump.fun, ignoring",
                            src="service:social_monitor",
                        )
                    continue

            if self._runtime:
                flag = "GROK-CONFIRMED" if confidence >= GROK_CONFIRM_THRESHOLD else "watching"
                self._runtime.logger.info(
                    f"[grok] {flag} {name} ({symbol}) — "
                    f"confidence={confidence}/10 hype={hype} "
                    f"{'mint=' + mint[:8] + '...' if mint else 'no CA yet'}",
                    src="service:social_monitor",
                )

            # Store signal
            if mint:
                self._grok_signals[mint] = {
                    "name": name,
                    "symbol": symbol,
                    "confidence": confidence,
                    "hype": hype,
                    "detected_at": now,
                    "text": token.get("text", ""),
                }

            # For high-confidence confirmed tokens, also queue for Strategy E (grok_bc_sniper).
            # Previously the narrative scanner only stored to _grok_signals (TP upgrade only)
            # but never triggered a buy. Now validated tokens ≥ threshold get queued.
            if validated is not None and confidence >= GROK_CONFIRM_THRESHOLD:
                already_queued = any(t.get("mint") == mint for t in self._discovery_queue)
                already_processed = mint in self._discovery_processed
                if not already_queued and not already_processed:
                    self._discovery_queue.append(validated)
                    if self._runtime:
                        self._runtime.logger.info(
                            f"[grok] → queued for Strategy E: {name} ({symbol}) "
                            f"fill={validated['fill_pct']:.1f}% "
                            f"real_sol={validated['real_sol']:.2f} "
                            f"age={validated['age_secs']:.0f}s",
                            src="service:social_monitor",
                        )

            if symbol:
                self._name_to_mint[symbol.upper()] = mint

            # Build social_signal event (same format as old Nitter integration)
            signal: dict[str, Any] = {
                "type": "social_signal",
                "author": "grok_scanner",
                "text": f"{name} ({symbol}) — {hype} hype on X — {token.get('text', '')}"[:280],
                "url": "",
                "timestamp": now,
                "pump_mints": [mint] if mint else [],
                "other_mints": [],
                "has_mint": bool(mint),
                "confidence": confidence,
                "grok_confirmed": confidence >= GROK_CONFIRM_THRESHOLD,
            }
            self._signals.append(signal)

            if self._runtime:
                try:
                    await self._runtime.emit_event("social_signal", signal)
                except Exception:
                    pass

    async def _monitor_loop(self) -> None:
        """Three concurrent scan loops:

        FAST loop (180s) — pump.fun launch discovery via Grok X search.
            Viral meme tokens peak in 2-5 min; 180s loop catches them within 1-2 cycles.

        SLOW loop (300s, alternating) — general market scan + financial narrative scan.
            Less time-critical. Trending tokens, narrative tokens, KOL calls.

        BOOST loop (60s, FREE) — DexScreener token boosts. Zero API cost. Runs every 60s
            regardless of Grok credit status. Boosted tokens show team commitment.
        """
        # Short initial delay to let bot fully start
        await asyncio.sleep(30)

        # Launch three concurrent tasks inside the single _monitor_loop coroutine
        asyncio.create_task(self._fast_launch_loop())
        asyncio.create_task(self._slow_scan_loop())
        asyncio.create_task(self._dexscreener_boost_loop())

        # Keep this task alive (the subtasks run independently)
        while True:
            await asyncio.sleep(3600)

    async def _dexscreener_boost_loop(self) -> None:
        """FREE signal source: poll DexScreener token-boosts every 60s.

        Teams pay DexScreener to boost their token — it's a genuine commitment signal.
        This loop runs regardless of Grok credit status and feeds _grok_signals
        so Strategy E / is_grok_confirmed() can act on boosted tokens.
        """
        BOOST_INTERVAL = 60  # seconds — free endpoint, no API cost
        BOOST_MIN_AMOUNT = 50   # minimum boost amount (ignore micro-boosts)
        BOOST_MIN_LIQ    = 20000  # minimum liquidity USD to consider
        BOOST_CONFIDENCE = 6    # just below GROK_CONFIRM_THRESHOLD=7 — boosts alone don't confirm

        try:
            import aiohttp
        except ImportError:
            return

        seen_boosts: set[str] = set()

        while True:
            await asyncio.sleep(BOOST_INTERVAL)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.dexscreener.com/token-boosts/latest/v1",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        boosts = await resp.json()

                # Filter to Solana tokens with meaningful boost amounts
                sol_boosts = [
                    b for b in boosts
                    if b.get("chainId") == "solana"
                    and b.get("totalAmount", 0) >= BOOST_MIN_AMOUNT
                    and b.get("tokenAddress")
                ]

                for boost in sol_boosts:
                    mint = boost["tokenAddress"]
                    boost_key = f"{mint}:{boost.get('totalAmount', 0)}"
                    if boost_key in seen_boosts:
                        continue
                    seen_boosts.add(boost_key)

                    # Skip if already confirmed at higher confidence
                    existing = self._grok_signals.get(mint, {})
                    if existing.get("confidence", 0) >= 7:
                        continue

                    # Fetch DexScreener pair data to validate liquidity/momentum
                    try:
                        async with session.get(
                            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as pr:
                            if pr.status != 200:
                                continue
                            pair_data = await pr.json()
                    except Exception:
                        continue

                    pairs = pair_data.get("pairs") or []
                    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    if not sol_pairs:
                        continue

                    # Use highest-liquidity pair
                    best = max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    liq = float(best.get("liquidity", {}).get("usd", 0) or 0)
                    h1 = float((best.get("priceChange") or {}).get("h1", 0) or 0)
                    name = best.get("baseToken", {}).get("name", mint[:8])
                    symbol = best.get("baseToken", {}).get("symbol", "?")

                    if liq < BOOST_MIN_LIQ:
                        continue
                    if h1 < -20:  # skip tokens already in freefall
                        continue

                    self._grok_signals[mint] = {
                        "name": name,
                        "symbol": symbol,
                        "confidence": BOOST_CONFIDENCE,
                        "hype": "medium",
                        "detected_at": time.time(),
                        "text": f"DexScreener boost amount={boost.get('totalAmount', 0)} liq=${liq:.0f} h1={h1:+.1f}%",
                        "source": "dexscreener_boost",
                    }
                    if self._runtime:
                        self._runtime.logger.info(
                            f"[boost] {symbol} ({name}) boost={boost.get('totalAmount', 0)} "
                            f"liq=${liq:.0f} h1={h1:+.1f}% — added to signals",
                            src="service:social_monitor",
                        )

                # Keep seen_boosts from growing forever
                if len(seen_boosts) > 2000:
                    seen_boosts = set(list(seen_boosts)[-1000:])

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.debug(
                        f"[boost] DexScreener boost loop error: {exc}",
                        src="service:social_monitor",
                    )

    async def _fast_launch_loop(self) -> None:
        """Run pump.fun X launch discovery every 180 seconds (was 60s, slowed to control API costs)."""
        FAST_INTERVAL = 180  # seconds — 480 calls/day at grok-3-mini vs 1440/day previously

        while True:
            try:
                if self._enabled:
                    await self.scan_new_pumpfun_launches()
                    self._last_query_at = time.time()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[grok-fast] Launch scan error: {exc}",
                        src="service:social_monitor",
                    )
            await asyncio.sleep(FAST_INTERVAL)

    async def _slow_scan_loop(self) -> None:
        """Alternate between general market scan and financial narrative scan every 90s."""
        await asyncio.sleep(45)  # offset from fast loop so they don't both hit API at once

        while True:
            try:
                if self._enabled:
                    _cycle = int(time.time() / GROK_POLL_INTERVAL_SECS) % 2
                    if _cycle == 0:
                        tokens = await self._query_grok()
                        await self._process_grok_results(tokens)
                    else:
                        await self.scan_financial_narrative_launches()
                    self._last_query_at = time.time()
                else:
                    if self._runtime:
                        self._runtime.logger.debug(
                            "[grok] Scanner disabled (no API key) — sleeping",
                            src="service:social_monitor",
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._runtime:
                    self._runtime.logger.warning(
                        f"[grok] Monitor loop error: {exc}",
                        src="service:social_monitor",
                    )
            await asyncio.sleep(GROK_POLL_INTERVAL_SECS)
