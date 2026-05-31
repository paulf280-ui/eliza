"""
trade_monitor.py — Per-position intelligent exit monitor with 4-tier AI cascade.

PRIMARY exit handler for all monitored positions. The hard SL in the price
refresh task is demoted to a safety-net backstop (-20%) for monitor failures.

Architecture per position:
  TradeMonitor
    ├── DataFeed: snapshots every 2s (DexScreener + Helius)
    ├── AICascade: 4-tier AI evaluation on a rolling schedule
    │     Tier 1: Groq llama-3.3-70b  — gut-check every 30s (PRIMARY exit signal)
    │     Tier 2: Gemini 2.0-flash     — confirmation / pattern recog every 2min
    │     Tier 3: Claude Sonnet 4.6    — deep analysis every 5min
    │     Tier 4: Claude Opus 4.6      — emergency escalation only
    ├── AdaptiveExitEngine: ATR trailing stop + AI override
    └── DecisionLog: records every AI decision for debrief reports

Exit criteria (AI is PRIMARY for monitored positions):
  - Groq SELL + confidence ≥ 0.75 → execute exit immediately
  - Gemini SELL + confidence ≥ 0.70 → execute exit
  - Claude Sonnet SELL + confidence ≥ 0.65 → execute exit
  - BSR emergency (< 0.25) or liq rug (50% drop) → immediate exit
  - ATR trail stop breach (moonbag phase) → exit
  - Opus emergency → always execute

Decision log (per mint) is accessible via get_decision_log(mint) for
the DebriefReporter to include brain activity in post-trade reports.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from elizaos.plugins.solana import brain_memory as _bm

# ── Active monitor registry ───────────────────────────────────────────────────
_active_monitors: dict[str, "TradeMonitor"] = {}

# Decision logs preserved after monitor stops (for debrief reporter).
# Keyed by mint, contains full decision history for each position.
_closed_logs: dict[str, dict] = {}

_DIR = os.path.dirname(__file__)
_MONITOR_STATE_PATH = os.path.join(_DIR, "monitor_state.json")


def is_monitored(mint: str) -> bool:
    return mint in _active_monitors


# ── 1m candle helpers (Jarvis signs) ─────────────────────────────────────────
# Kept local — importing from monster_signals would create a circular import.

async def _fetch_monitor_candles(session: aiohttp.ClientSession, mint: str) -> list:
    """Fetch last 15 BirdEye 1m candles for an active position.
    Returns [[ts, o, h, l, c, vol], ...] oldest-first, or [] on failure.
    """
    api_key = os.getenv("BIRDEYE_API_KEY", "")
    if not api_key:
        return []
    try:
        now = int(time.time())
        async with session.get(
            "https://public-api.birdeye.so/defi/ohlcv",
            headers={"X-API-KEY": api_key, "x-chain": "solana"},
            params={"address": mint, "type": "1m",
                    "time_from": now - 15 * 60, "time_to": now},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status != 200:
                return []
            data = await r.json()
            items = (data.get("data") or {}).get("items") or []
            candles = [
                [int(c["unixTime"]), float(c.get("open") or 0),
                 float(c.get("high") or 0), float(c.get("low") or 0),
                 float(c.get("close") or 0), float(c.get("volume") or 0)]
                for c in items if c.get("unixTime")
            ]
            return sorted(candles, key=lambda x: x[0])
    except Exception:
        return []


def _compute_monitor_signs(candles: list) -> dict:
    """Compute the three Jarvis entry signs from 1m candles.

    Sign 1: current volume ≥ 3× MA(10 prior candles)
    Sign 2: close > max_high(last 3 candles) AND body ≥ 1.5× avg_body(10)
    Sign 3: not computable here (needs wallet-level GMGN data) — omitted
    Returns compact string for memory + verbose summary for Groq prompt.
    """
    if len(candles) < 12:
        return {}
    cur = candles[-1]
    cur_vol, cur_close, cur_open = cur[5], cur[4], cur[1]
    cur_body = abs(cur_close - cur_open)

    vol_ma10   = sum(c[5] for c in candles[-11:-1]) / 10.0
    vol_ratio  = (cur_vol / vol_ma10) if vol_ma10 > 0 else 0.0
    s1         = vol_ratio >= 3.0

    max_high_3  = max(c[2] for c in candles[-4:-1])
    avg_body_10 = sum(abs(c[4] - c[1]) for c in candles[-11:-1]) / 10.0
    range_break = cur_close > max_high_3
    body_ok     = cur_body >= 1.5 * avg_body_10 if avg_body_10 > 0 else False
    s2          = range_break and body_ok

    shape = "".join(
        "🟢" if c[4] > c[1] else ("🔴" if c[4] < c[1] else "➖")
        for c in candles[-5:]
    )
    compact = (
        f"S1={'✅' if s1 else '❌'}({vol_ratio:.1f}x) "
        f"S2={'✅' if s2 else '❌'}(brk={range_break} body={body_ok}) "
        f"shape={shape}"
    )
    summary = (
        f"=== 1M CANDLE SIGNALS (Jarvis) ===\n"
        f"Sign 1 Volume Spike: {vol_ratio:.1f}x MA10  {'✅ SPIKE' if s1 else '❌ flat'}\n"
        f"Sign 2 Breakout:     close {'ABOVE' if range_break else 'below'} 3c-high, "
        f"body {'sig' if body_ok else 'weak'}  {'✅ BREAKOUT' if s2 else '❌'}\n"
        f"Last 5 candles: {shape}  |  "
        f"{'🚀 BOTH GREEN' if (s1 and s2) else '⚠️ partial' if (s1 or s2) else '❌ no signal'}"
    )
    return {
        "s1": s1, "s2": s2, "vol_ratio": round(vol_ratio, 2),
        "range_break": range_break, "body_ok": body_ok,
        "compact": compact, "summary": summary,
    }


def get_decision_log(mint: str) -> dict | None:
    """Return the decision log for a position (active or recently closed).

    Used by DebriefReporter to show what each brain did during the trade.
    Cleaned up after the debrief report consumes it.
    """
    mon = _active_monitors.get(mint)
    if mon:
        return mon.get_log_snapshot()
    return _closed_logs.get(mint)


def consume_decision_log(mint: str) -> dict | None:
    """Retrieve and remove a closed position's decision log."""
    return _closed_logs.pop(mint, None)


def stop_monitor(mint: str) -> None:
    """Cancel and remove the monitor for a closed position.
    The decision log is preserved in _closed_logs for the debrief reporter.
    """
    mon = _active_monitors.pop(mint, None)
    if mon:
        # Preserve the decision log before cancelling
        log = mon.get_log_snapshot()
        if log:
            _closed_logs[mint] = log
            # Keep at most 100 closed logs in memory
            if len(_closed_logs) > 100:
                oldest = next(iter(_closed_logs))
                del _closed_logs[oldest]
        mon.cancel()


async def ask_moonbag_hold(
    mint: str,
    current_price: float,
    pnl_pct: float,
    drop_from_peak: float,
    session: aiohttp.ClientSession,
) -> bool:
    """Ask Groq whether to hold through a moonbag trail-stop trigger.

    Called the FIRST TIME the price crosses the -30% from peak threshold.
    Returns True (AI says HOLD — grant 45s grace) or False (fire trail stop now).

    Only one consultation per moonbag high — the AI gets a single chance.
    After the 45s grace expires the trail stop fires unconditionally.
    """
    mon = _active_monitors.get(mint)
    if not mon or not mon._ai._groq_key:
        return False  # no monitor or no Groq key → rule fires immediately

    try:
        from elizaos.plugins.solana.axiom_copy_trader import _paper_positions  # no circular: lazy
        pos = _paper_positions.get(mint, {})

        ctx = json.dumps({
            "mint":               mint[:8],
            "token":              pos.get("token_name", mint[:8]),
            "whale_wallet":       pos.get("wallet_name", "?"),
            "age_min":            round((time.time() - pos.get("entry_ts", time.time())) / 60, 1),
            "pnl_pct":            round(pnl_pct, 1),
            "peak_pnl_pct":       round(pos.get("peak_pnl_pct", 0.0) or 0.0, 1),
            "drop_from_peak_pct": round(drop_from_peak, 1),
            "tp1_hit":            True,
            "locked_sol":         round(pos.get("locked_sol") or 0.0, 4),
            "narrative":          pos.get("narrative", "unknown"),
            "situation": (
                "MOONBAG TRAIL STOP TRIGGERED: price has dropped past -30% from moonbag peak. "
                "Cost basis is already recovered (TP1 already fired). Locked SOL is safe regardless. "
                "HOLD = delay exit 45s for potential recovery. SELL = exit moonbag now."
            ),
        })

        decision = await mon._ai._call_groq(ctx, session)
        if decision is None:
            return False

        # Record in monitor log so it appears in debrief report
        mon._ai._record(decision)

        if decision.action == "HOLD" and decision.confidence >= 0.60:
            print(
                f"[monitor] 🤖 MOONBAG TRAIL: Groq HOLD "
                f"({decision.confidence:.2f}) — {decision.reason[:80]} — 45s grace granted"
            )
            return True

        print(
            f"[monitor] 🤖 MOONBAG TRAIL: Groq {decision.action} "
            f"({decision.confidence:.2f}) — trail stop fires now"
        )
        return False

    except Exception as _e:
        print(f"[monitor] ask_moonbag_hold error: {_e}")
        return False


async def entry_scan_position(
    mint: str,
    session: aiohttp.ClientSession,
) -> None:
    """Run a quick entry-quality scan immediately when a position opens.

    Uses Groq (fast/free) to rate the token as RUNNER/NORMAL/RUG_RISK based on
    entry conditions. Stores verdict on the position dict so the ongoing monitor
    and exit decisions can reference it.
    """
    from elizaos.plugins.solana.axiom_copy_trader import _paper_positions

    pos = _paper_positions.get(mint)
    if not pos:
        return

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        return

    ctx = json.dumps({
        "token":        pos.get("token_name", mint[:8]),
        "whale_wallet": pos.get("wallet_name", "?"),
        "narrative":    pos.get("narrative", "unknown"),
        "sol_spent":    round(pos.get("sol_spent", 0.0), 3),
        "liq_usd":      pos.get("_learning_entry_liq"),
        "mc_usd":       pos.get("_learning_entry_mc"),
        "holders":      pos.get("_learning_entry_holders"),
        "entry_price":  pos.get("entry_price"),
    })

    patterns = _get_learned_patterns()
    system_note = "You assess new Solana meme-coin positions for rug risk.\n" + patterns if patterns else "You assess new Solana meme-coin positions for rug risk."
    prompt = _ENTRY_SCAN_PROMPT.format(context=ctx)

    try:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": system_note},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return
            data = await r.json()
            raw = data["choices"][0]["message"]["content"]
            verdict_data = json.loads(raw)
            verdict = verdict_data.get("verdict", "NORMAL")
            confidence = float(verdict_data.get("confidence", 0.5))
            reason = str(verdict_data.get("reason", ""))[:150]
            risks = verdict_data.get("key_risks", [])

            _paper_positions[mint]["ai_entry_verdict"] = verdict.lower()
            _paper_positions[mint]["ai_entry_confidence"] = confidence
            _paper_positions[mint]["ai_entry_reason"] = reason

            # Record the entry scan as a Groq decision so dashboard stats reflect
            # brain activity the moment a trade opens (fixes 0 calls/hold/exit).
            try:
                _bm.record_decision(
                    "groq", mint, pos.get("token_name", mint[:8]),
                    action=verdict.upper(),        # RUNNER | NORMAL | RUG_RISK
                    reason=f"entry_scan: {reason}",
                    pnl_pct=0.0,                   # position just opened
                    confidence=confidence,
                )
            except Exception as _bm_err:
                print(f"[monitor/entry-scan] brain memory record failed: {_bm_err}")

            emoji = "🚀" if verdict == "RUNNER" else ("🚨" if verdict == "RUG_RISK" else "✅")
            print(
                f"[monitor/entry-scan] {emoji} {pos.get('token_name', mint[:8])} → "
                f"{verdict} ({confidence:.0%}) — {reason}"
                + (f" | risks: {', '.join(risks[:2])}" if risks else "")
            )

            # Push rug risk alert to dashboard
            if verdict == "RUG_RISK" and confidence >= 0.65:
                try:
                    from elizaos.plugins.solana.axiom_copy_trader import _push_alert
                    _push_alert(
                        f"🚨 <b>RUG RISK SIGNAL</b> — {pos.get('token_name', mint[:8])}\n"
                        f"Entry scan: {reason}\n"
                        f"Confidence: {confidence:.0%} | Watching for exit trigger"
                    )
                except Exception:
                    pass

    except Exception as exc:
        print(f"[monitor/entry-scan] {mint[:8]} error: {exc}")


async def entry_scan_gemini(
    mint: str,
    session: aiohttp.ClientSession,
) -> None:
    """Parallel Gemini entry-quality scan so Gemini's panel shows activity at
    trade open. Uses a different lens from Groq (cheap Gemini Flash, slightly
    deeper context) so the two brains bring independent views.
    """
    from elizaos.plugins.solana.axiom_copy_trader import _paper_positions

    pos = _paper_positions.get(mint)
    if not pos:
        return

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        return

    ctx = json.dumps({
        "token":        pos.get("token_name", mint[:8]),
        "whale_wallet": pos.get("wallet_name", "?"),
        "narrative":    pos.get("narrative", "unknown"),
        "sol_spent":    round(pos.get("sol_spent", 0.0), 3),
        "liq_usd":      pos.get("_learning_entry_liq"),
        "mc_usd":       pos.get("_learning_entry_mc"),
        "holders":      pos.get("_learning_entry_holders"),
        "entry_price":  pos.get("entry_price"),
    })
    brain_ctx = _bm.brain_memory_as_prompt("gemini")
    system_note = (
        "You assess new Solana meme-coin positions for rug risk. "
        "Reply JSON {verdict, confidence, reason} where verdict is "
        "RUNNER|NORMAL|RUG_RISK."
        + brain_ctx
    )
    prompt = _ENTRY_SCAN_PROMPT.format(context=ctx)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash-lite:generateContent?key={gemini_key}"
    )
    body = {
        "contents": [{"parts": [{"text": f"{system_note}\n\n{prompt}"}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 200,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                err = (await r.text())[:160]
                print(f"[monitor/entry-scan-gemini] HTTP {r.status}: {err}")
                return
            data = await r.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            verdict_data = json.loads(raw)
            verdict    = str(verdict_data.get("verdict", "NORMAL")).upper()
            confidence = float(verdict_data.get("confidence", 0.5))
            reason     = str(verdict_data.get("reason", ""))[:150]

            try:
                _bm.record_decision(
                    "gemini", mint, pos.get("token_name", mint[:8]),
                    action=verdict,
                    reason=f"entry_scan: {reason}",
                    pnl_pct=0.0,
                    confidence=confidence,
                )
            except Exception as _bm_err:
                print(f"[monitor/entry-scan-gemini] brain memory record failed: {_bm_err}")

            emoji = "🚀" if verdict == "RUNNER" else ("🚨" if verdict == "RUG_RISK" else "✅")
            print(
                f"[monitor/entry-scan-gemini] {emoji} {pos.get('token_name', mint[:8])} → "
                f"{verdict} ({confidence:.0%}) — {reason}"
            )
    except Exception as exc:
        print(f"[monitor/entry-scan-gemini] {mint[:8]} error: {exc}")


async def spawn_monitor(
    mint: str,
    session: aiohttp.ClientSession,
    runtime: Any,
) -> None:
    """Spawn a TradeMonitor for a newly opened position.

    Called after position is saved to _paper_positions.
    The monitor reads position state from the shared _paper_positions dict.
    """
    # Avoid importing at module level to prevent circular dependency
    from elizaos.plugins.solana.axiom_copy_trader import (
        _paper_positions,
        _close_paper_position,
        _get_market_data,
        _resolve_pool_address,
    )

    pos = _paper_positions.get(mint)
    if not pos:
        return

    if mint in _active_monitors:
        return  # already watching

    monitor = TradeMonitor(
        mint=mint,
        pos_ref=_paper_positions,
        close_fn=_close_paper_position,
        market_data_fn=_get_market_data,
        pool_resolve_fn=_resolve_pool_address,
        runtime=runtime,
    )
    _active_monitors[mint] = monitor
    asyncio.get_event_loop().create_task(monitor.run(session))
    # Fire entry scans immediately (non-blocking — result stored on pos within ~3s).
    # Two brains get an independent first look so the dashboard shows activity from
    # the moment a trade opens (was 0/0/0 because fast scalps closed before the
    # 60s/180s Gemini/Claude cascade could reach them).
    asyncio.get_event_loop().create_task(entry_scan_position(mint, session))
    asyncio.get_event_loop().create_task(entry_scan_gemini(mint, session))
    # Ensure brain memory refresh loop is running (no-op if already started)
    _bm.ensure_memory_loop_started()
    print(f"[monitor] 🚀 TradeMonitor spawned for {pos.get('token_name', mint[:8])} ({mint[:8]})")


# ── Price snapshot ─────────────────────────────────────────────────────────────

@dataclass
class PriceSnapshot:
    ts: float
    price: float
    mc: float | None
    liq: float | None
    vol_h1: float
    buys_h1: int
    sells_h1: int
    buy_sell_ratio: float | None
    vol_mc_ratio: float | None
    holders: int | None
    chg_h1: float = 0.0
    chg_h6: float = 0.0
    chg_h24: float = 0.0


# ── DataFeed ──────────────────────────────────────────────────────────────────

class DataFeed:
    """Collects market data snapshots every FEED_INTERVAL seconds."""

    FEED_INTERVAL = 2.0
    MAX_SNAPSHOTS = 300   # 10 minutes of 2s snapshots

    def __init__(self, mint: str, market_data_fn, pool_resolve_fn, runtime: Any) -> None:
        self.mint = mint
        self._market_data_fn = market_data_fn
        self._pool_resolve_fn = pool_resolve_fn
        self._runtime = runtime
        self.snapshots: deque[PriceSnapshot] = deque(maxlen=self.MAX_SNAPSHOTS)
        self._running = False

    def stop(self) -> None:
        self._running = False

    async def run(self, session: aiohttp.ClientSession) -> None:
        self._running = True
        while self._running:
            try:
                await self._collect(session)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[monitor/feed] {self.mint[:8]} error: {exc}")
            await asyncio.sleep(self.FEED_INTERVAL)

    async def _collect(self, session: aiohttp.ClientSession) -> None:
        # Try Helius real-time price first for PumpSwap positions
        from elizaos.plugins.solana.axiom_copy_trader import _paper_positions
        pos = _paper_positions.get(self.mint, {})
        pool_addr = pos.get("pool_address")
        helius_price: float | None = None

        if pool_addr and self._runtime:
            try:
                ray_svc = self._runtime.get_service("lp_pool")
                if ray_svc:
                    helius_price = await asyncio.wait_for(
                        ray_svc.get_pumpswap_price_helius(pool_addr), timeout=2.5
                    )
            except Exception:
                pass

        # DexScreener for full market data
        mdata = await self._market_data_fn(self.mint, session)
        if not mdata:
            return

        price = helius_price if (helius_price and helius_price > 0) else mdata.get("price")
        if not price or price <= 0:
            return

        snap = PriceSnapshot(
            ts=time.time(),
            price=price,
            mc=mdata.get("mc"),
            liq=mdata.get("liq"),
            vol_h1=float(mdata.get("vol_h1") or 0),
            buys_h1=int(mdata.get("buys_h1") or 0),
            sells_h1=int(mdata.get("sells_h1") or 0),
            buy_sell_ratio=mdata.get("buy_sell_ratio"),
            vol_mc_ratio=mdata.get("vol_mc_ratio"),
            holders=mdata.get("holders"),
            chg_h1=float(mdata.get("chg_h1") or 0),
            chg_h6=float(mdata.get("chg_h6") or 0),
            chg_h24=float(mdata.get("chg_h24") or 0),
        )
        self.snapshots.append(snap)

    def latest_price(self) -> float | None:
        return self.snapshots[-1].price if self.snapshots else None

    def price_window(self, secs: float) -> list[PriceSnapshot]:
        cutoff = time.time() - secs
        return [s for s in self.snapshots if s.ts >= cutoff]

    def compute_atr(self, periods: int = 20) -> float | None:
        """Average True Range over the last N snapshots (2s each → 40s window at N=20).

        For crypto with no OHLCV we use abs(price[i] - price[i-1]) as the range.
        Returns None if insufficient data.
        """
        recent = list(self.snapshots)[-periods - 1:]
        if len(recent) < 5:
            return None
        ranges = [abs(recent[i].price - recent[i - 1].price) for i in range(1, len(recent))]
        return sum(ranges) / len(ranges) if ranges else None


# ── AICascade ─────────────────────────────────────────────────────────────────

@dataclass
class AIDecision:
    action: str        # "HOLD" | "SELL" | "REDUCE" | "WATCH"
    confidence: float  # 0.0 – 1.0
    reason: str
    urgency: str       # "low" | "medium" | "high"
    tier: str          # "groq" | "gemini" | "sonnet" | "opus"
    risk_level: str = "MEDIUM"  # "LOW" | "MEDIUM" | "HIGH" — code-driven exit on HIGH
    ts: float = field(default_factory=time.time)
    triggered_exit: bool = False  # True if this decision caused the position to close


# ── Per-tier health surface ──────────────────────────────────────────────────
# Tracks last call outcome per tier so the dashboard can show "DOWN — reason"
# instead of an empty panel that the user reads as "no decisions". Updated by
# AICascade on every API call (success or failure). Read by dashboard_api.
_tier_health: dict[str, dict] = {
    "groq":   {"last_ok_ts": 0.0, "last_err_ts": 0.0, "last_err_msg": ""},
    "gemini": {"last_ok_ts": 0.0, "last_err_ts": 0.0, "last_err_msg": ""},
    "claude": {"last_ok_ts": 0.0, "last_err_ts": 0.0, "last_err_msg": ""},
}


def _mark_tier_ok(tier: str) -> None:
    h = _tier_health.get(tier)
    if h is not None:
        h["last_ok_ts"] = time.time()


def _mark_tier_err(tier: str, msg: str) -> None:
    h = _tier_health.get(tier)
    if h is not None:
        h["last_err_ts"] = time.time()
        h["last_err_msg"] = msg[:200]


def get_tier_health() -> dict:
    """Return a snapshot of per-tier health for the dashboard. A tier is
    "down" if its last error was more recent than its last success and within
    the last 15 minutes."""
    now = time.time()
    out = {}
    for tier, h in _tier_health.items():
        last_ok = float(h.get("last_ok_ts") or 0)
        last_err = float(h.get("last_err_ts") or 0)
        is_down = last_err > last_ok and (now - last_err) < 900
        out[tier] = {
            "status": "down" if is_down else ("idle" if last_ok == 0 else "ok"),
            "last_ok_secs_ago": int(now - last_ok) if last_ok > 0 else None,
            "last_err_secs_ago": int(now - last_err) if last_err > 0 else None,
            "last_err_msg": str(h.get("last_err_msg") or "") if is_down else "",
        }
    return out


class AICascade:
    """Tiered AI evaluation of position health.

    Tier schedule (wall-clock since last call):
      Groq:         every 30s  (fast, cheap)
      Gemini Flash: every 2min
      Claude Sonnet: every 5min
      Claude Opus:  escalation only (called when Groq/Gemini both signal SELL
                    or when emergency conditions detected)
    """

    GROQ_INTERVAL   = 30
    GEMINI_INTERVAL = 120
    SONNET_INTERVAL = 180   # Opus 4.7 every 3min — lowered from 300s so depth brain fires more on runners

    def __init__(self) -> None:
        self._last_groq   = 0.0
        self._last_gemini = 0.0
        self._last_sonnet = 0.0
        self._last_opus   = 0.0
        # Skip-until: skip API calls until this wall-clock time. Set on
        # transient (5xx/429 → +60s) or hard (4xx → +1h) failures so we
        # stop hammering an overloaded or capped endpoint at every tick.
        self._gemini_skip_until = 0.0
        self._claude_skip_until = 0.0
        # Set by TradeMonitor before each evaluate() call
        self._mint:        str   = ""
        self._token_name:  str   = ""
        self._last_pnl_pct: float | None = None
        self.decisions:   list[AIDecision] = []
        self._pending_escalation = False

        # Load API keys once
        self._groq_key     = os.getenv("GROQ_API_KEY", "")
        self._gemini_key   = os.getenv("GEMINI_API_KEY", "")
        self._anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    def last_decision(self) -> AIDecision | None:
        return self.decisions[-1] if self.decisions else None

    def _record(self, d: AIDecision) -> None:
        self.decisions.append(d)
        if len(self.decisions) > 50:
            self.decisions = self.decisions[-50:]

    async def evaluate(
        self,
        mint: str,
        pos: dict,
        feed: DataFeed,
        session: aiohttp.ClientSession,
    ) -> AIDecision | None:
        """Run the appropriate AI tier for 30-40% range tokens only.

        New strategy (2026-05-03): AI evaluates ONLY tokens in 30-40% PnL range.
        Outside this range, let hard TP (+100%) and floor (-25%) handle exits.
        For 30-40% tokens: Groq checks MC/holders/liq trends and decides.

        Exit thresholds:
          Groq:   SELL + confidence ≥ 0.75 → immediate exit (30-40% range only)
          Outside 30-40%: return None (skip AI)
        """
        now = time.time()
        age_secs = now - pos.get("entry_ts", now)

        # Guard: evaluate AI from entry (0%) up to 80% PnL.
        # Below 0%: Groq watches for early dump signals — can exit before -25% floor.
        # Above 80%: 20% from 100% TP — let it fly, don't interrupt a runner.
        current_price = (feed.latest_price() or 0.0) if feed else 0.0
        entry_price = pos.get("entry_price", 0.0)
        if entry_price > 0 and current_price > 0:
            pnl_pct = ((current_price / entry_price) - 1.0) * 100
            if pnl_pct > 80.0:
                # Runner — let hard TP handle it, don't interrupt
                return None

        # Probe single-whale concentration from on-chain swap history (cached
        # 10s, so multiple brain tiers within a window share one fetch). Only
        # for positions with a known pool address — older legacy positions
        # without it just skip the G7 gate.
        whale_data: dict | None = None
        pool_addr = pos.get("pool_address")
        if pool_addr:
            try:
                whale_data = await _whale_concentration(session, mint, pool_addr)
            except Exception as _w_err:
                print(f"[monitor/whale] {mint[:8]} probe error: {_w_err}")

        ctx = _build_context(mint, pos, feed, age_secs, whale=whale_data)
        if not ctx:
            return None

        decision: AIDecision | None = None

        # ── Tier 1: Groq — OBSERVE ONLY mode ─────────────────────────────────
        # Groq's exit authority has been removed. The volume-based distribution
        # exit (sell_ratio > 65% + 3 consecutive lower lows) handles TP.
        # Groq now ONLY observes and logs to the learning file so it builds
        # pattern memory over time. Once the data-driven system is validated
        # (2-3 weeks), Groq's observations can inform parameter tuning.
        if now - self._last_groq >= self.GROQ_INTERVAL and self._groq_key:
            d = await self._call_groq(ctx, session)
            if d:
                self._last_groq = now
                self._record(d)
                # LOG the recommendation but do NOT execute it
                if d.action in ("SELL", "HOLD"):
                    print(f"[monitor] 🧠 Groq OBSERVING {ctx.get('token','?')} — "
                          f"{d.action} conf={d.confidence:.0%} | {(d.reason or '')[:80]}")
                    # Write to Groq learning log
                    try:
                        import json as _json, os as _os
                        _log_path = _os.path.join(_os.path.dirname(__file__), "groq_learning_log.json")
                        _entry = {
                            "ts": int(now), "mint": ctx.get("mint"), "token": ctx.get("token"),
                            "action": d.action, "confidence": d.confidence,
                            "reason": d.reason, "pnl_pct": ctx.get("pnl_pct"),
                            "peak_pnl_pct": ctx.get("peak_pnl_pct"),
                            "bsr": ctx.get("bsr"), "liq_usd": ctx.get("liq_usd"),
                            "sell_ratio_m5": pos.get("_current_sell_ratio_m5"),
                            "consecutive_lower_lows": pos.get("_consecutive_lower_lows"),
                            "outcome": None,  # filled in when trade closes
                        }
                        _log = []
                        if _os.path.exists(_log_path):
                            try: _log = _json.load(open(_log_path))[-2000:]
                            except: pass
                        _log.append(_entry)
                        with open(_log_path, "w") as _f:
                            _json.dump(_log[-2000:], _f)
                    except Exception:
                        pass
                if d.action == "HOLD" and not decision:
                    decision = d  # propagate HOLD for logging only

        # ── Tier 2: Gemini Flash — DISABLED (2026-05-03)
        # Paused for fresh data gathering with Groq-only setup
        # Re-enable by setting this to True when needed
        GEMINI_ENABLED = False
        if (GEMINI_ENABLED and now - self._last_gemini >= self.GEMINI_INTERVAL
                and self._gemini_key and age_secs >= 60
                and now >= self._gemini_skip_until):
            d = await self._call_gemini(ctx, session)
            if d:
                self._last_gemini = now
                self._record(d)
                if d.action == "SELL" and d.confidence >= 0.70:
                    decision = d

        # ── Tier 3: Claude Sonnet — DISABLED (2026-05-03)
        # Paused for fresh data gathering with Groq-only setup
        # Re-enable by setting this to True when needed
        CLAUDE_ENABLED = False
        if (CLAUDE_ENABLED and now - self._last_sonnet >= self.SONNET_INTERVAL
                and self._anthropic_key and age_secs >= 60
                and now >= self._claude_skip_until):
            d = await self._call_claude(ctx, session, model="claude-opus-4-7", tier="sonnet")
            if d:
                self._last_sonnet = now
                self._record(d)
                if d.action == "SELL" and d.confidence >= 0.65:
                    decision = d

        # ── Tier 4: Claude Opus (emergency) — DISABLED (2026-05-03)
        # Paused for fresh data gathering with Groq-only setup
        CLAUDE_EMERGENCY_ENABLED = False
        _drawdown = _calc_drawdown(pos)
        _is_emergency = (
            (_drawdown is not None and _drawdown <= -12.0) or
            self._pending_escalation
        ) and (now - self._last_opus >= 120)

        if (CLAUDE_EMERGENCY_ENABLED and _is_emergency and self._anthropic_key
                and now >= self._claude_skip_until):
            d = await self._call_claude(ctx, session, model="claude-opus-4-7", tier="opus")
            if d:
                self._last_opus = now
                self._pending_escalation = False
                self._record(d)
                if d.action == "SELL":
                    decision = d

        return decision

    def _is_meteora(self, ctx: str) -> bool:
        """Return True if the context is for a Meteora DLMM position."""
        try:
            return json.loads(ctx).get("dex") == "meteora"
        except Exception:
            return False

    def _strategy_hint(self, ctx: str) -> str:
        """Pull the strategy tag from ctx so learned patterns get filtered per lane."""
        try:
            d = json.loads(ctx)
            if d.get("dex") == "meteora":
                return "meteora"
            return d.get("strategy") or "copy_trade"
        except Exception:
            return "copy_trade"

    async def _call_groq(self, ctx: str, session: aiohttp.ClientSession) -> AIDecision | None:
        is_meteora = self._is_meteora(ctx)
        patterns = _get_learned_patterns(self._strategy_hint(ctx))
        brain_ctx = _bm.brain_memory_as_prompt("groq")
        system_note = (
            "You are a Solana meme-coin trading exit advisor. Respond with a single JSON object only — no prose, no markdown fences. The JSON must contain action (HOLD|SELL|WATCH), confidence (0.0-1.0), reason, and urgency (low|medium|high).\n"
            + patterns + brain_ctx
        )
        prompt_tmpl = _METEORA_HOLD_SELL_PROMPT if is_meteora else _HOLD_SELL_PROMPT
        prompt = prompt_tmpl.format(context=ctx)
        full_prompt = f"{system_note}\n\n{prompt}"

        # Fetch 1m candles + compute Jarvis signs for this position.
        # Appended to the prompt so Groq sees the live chart structure.
        # The compact descriptor is stored in memory for pattern learning.
        candle_shape: str | None = None
        try:
            candles = await _fetch_monitor_candles(session, self._mint)
            if len(candles) >= 12:
                signs = _compute_monitor_signs(candles)
                if signs:
                    full_prompt = full_prompt + "\n\n" + signs["summary"]
                    candle_shape = signs["compact"]
        except Exception:
            pass

        try:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._groq_key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "temperature": 0.1,
                    "max_tokens": 300,
                    # Dropped response_format=json_object: Groq's strict mode
                    # was rejecting valid model outputs with HTTP 400
                    # "json_validate_failed" — happened on every call once
                    # the prompt grew (G6 PULLBACK_SHAPE + 3 few-shot
                    # examples). The lenient _parse_ai_decision regex-
                    # extracts JSON from prose-wrapped output as a backstop.
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    err = (await r.text())[:200]
                    print(f"[monitor/groq] HTTP {r.status}: {err}")
                    _mark_tier_err("groq", f"HTTP {r.status}: {err}")
                    return None
                data = await r.json()
                raw = data["choices"][0]["message"]["content"]
                dec = _parse_ai_decision(raw, tier="groq")
                if dec:
                    _mark_tier_ok("groq")
                    _bm.record_decision("groq", self._mint, self._token_name,
                                        dec.action, dec.reason, self._last_pnl_pct, dec.confidence,
                                        candle_shape=candle_shape)
                return dec
        except Exception as exc:
            print(f"[monitor/groq] {exc}")
            _mark_tier_err("groq", str(exc))
            return None

    async def _call_gemini(self, ctx: str, session: aiohttp.ClientSession) -> AIDecision | None:
        is_meteora = self._is_meteora(ctx)
        patterns = _get_learned_patterns(self._strategy_hint(ctx))
        brain_ctx = _bm.brain_memory_as_prompt("gemini")
        system_note = (
            "You are a Solana meme-coin trading exit advisor. Respond with JSON only.\n"
            + patterns + brain_ctx
        )
        prompt_tmpl = _METEORA_HOLD_SELL_PROMPT if is_meteora else _HOLD_SELL_PROMPT
        prompt = prompt_tmpl.format(context=ctx)
        full_prompt = f"{system_note}\n\n{prompt}"
        body = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 200,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        # Model fallback ladder: try the capable model first, fall back to the
        # lighter one if Google returns 5xx/429 (gemini-2.5-flash sees demand
        # spikes that gemini-2.5-flash-lite usually rides through). 4xx errors
        # are persistent — bail out of the ladder immediately.
        last_status = None
        last_err = ""
        for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={self._gemini_key}"
            )
            try:
                async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        data = await r.json()
                        raw = data["candidates"][0]["content"]["parts"][0]["text"]
                        dec = _parse_ai_decision(raw, tier="gemini")
                        if dec:
                            _mark_tier_ok("gemini")
                            self._gemini_skip_until = 0.0
                            _bm.record_decision("gemini", self._mint, self._token_name,
                                                dec.action, dec.reason, self._last_pnl_pct, dec.confidence)
                        return dec
                    last_status = r.status
                    last_err = (await r.text())[:200]
                    print(f"[monitor/gemini/{model}] HTTP {r.status}: {last_err}")
                    if r.status not in (429, 500, 502, 503, 504):
                        break  # persistent 4xx — don't waste the fallback
            except Exception as exc:
                last_err = str(exc)
                print(f"[monitor/gemini/{model}] {exc}")
                continue
        # All ladder attempts failed.
        _mark_tier_err("gemini", f"HTTP {last_status}: {last_err}" if last_status else last_err)
        self._gemini_skip_until = time.time() + (
            60 if last_status in (429, 500, 502, 503, 504, None) else 3600
        )
        return None

    async def _call_claude(
        self,
        ctx: str,
        session: aiohttp.ClientSession,
        model: str,
        tier: str,
    ) -> AIDecision | None:
        brain_key = "claude"
        is_meteora = self._is_meteora(ctx)
        brain_ctx = _bm.brain_memory_as_prompt(brain_key)
        system = _CLAUDE_SYSTEM + _get_learned_patterns(self._strategy_hint(ctx)) + brain_ctx
        prompt_tmpl = _METEORA_HOLD_SELL_PROMPT if is_meteora else _HOLD_SELL_PROMPT
        prompt = prompt_tmpl.format(context=ctx)
        try:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 300,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    err = (await r.text())[:200]
                    print(f"[monitor/{tier}] HTTP {r.status}: {err}")
                    _mark_tier_err("claude", f"HTTP {r.status}: {err}")
                    # Anthropic 400 with "API usage limits" = spending cap;
                    # back off 1h. Other 4xx also 1h. 5xx/429 = 60s.
                    self._claude_skip_until = time.time() + (
                        60 if r.status in (429, 500, 502, 503, 504) else 3600
                    )
                    return None
                data = await r.json()
                raw = data["content"][0]["text"]
                dec = _parse_ai_decision(raw, tier=tier)
                if dec:
                    _mark_tier_ok("claude")
                    self._claude_skip_until = 0.0
                    _bm.record_decision(brain_key, self._mint, self._token_name,
                                        dec.action, dec.reason, self._last_pnl_pct, dec.confidence)
                return dec
        except Exception as exc:
            print(f"[monitor/{tier}] {exc}")
            _mark_tier_err("claude", str(exc))
            self._claude_skip_until = time.time() + 60
            return None


# ── AdaptiveExitEngine ────────────────────────────────────────────────────────

class AdaptiveExitEngine:
    """Combines ATR trailing stop and AI override into exit signals.

    Priority order:
    1. Emergency override (rug signal: liq drop > 50% in 60s, BSR < 0.3)
    2. ATR trailing stop (dynamic — widens in volatility, tightens in trend)
    3. AI SELL signal with confidence ≥ 0.80
    """

    ATR_MULTIPLIER  = 2.5   # stop = peak - 2.5 * ATR
    ATR_MIN_FRAC    = 0.05  # minimum stop distance (5% of price)
    BSR_EMERGENCY   = 0.25  # buy/sell ratio below this = mass exit
    LIQ_DROP_FRAC   = 0.50  # liquidity drops 50%+ in 60s = rug

    def __init__(self) -> None:
        self._entry_liq: float | None = None
        self._atr_peak: float | None  = None

    def evaluate(
        self,
        pos: dict,
        feed: DataFeed,
        ai_decision: AIDecision | None,
    ) -> tuple[bool, str]:
        """Return (should_exit, reason). Called every price tick."""
        if not feed.snapshots:
            return False, ""

        snap = feed.snapshots[-1]
        price = snap.price
        entry = pos.get("entry_price", 0.0)
        if not entry or entry <= 0:
            return False, ""

        # Only run after TP1 has fired (before TP1 the hard SL handles things)
        tp1_hit = pos.get("tp1_hit", False)

        # ── Emergency: BSR collapse (mass exit signal) ─────────────────────
        bsr = snap.buy_sell_ratio
        if bsr is not None and bsr < self.BSR_EMERGENCY:
            return True, f"emergency_bsr_{bsr:.2f}"

        # ── Emergency: liquidity rug (only if we have baseline) ──────────
        liq = snap.liq
        if liq and liq > 0:
            if self._entry_liq is None:
                self._entry_liq = liq
            elif liq < self._entry_liq * (1.0 - self.LIQ_DROP_FRAC):
                drop_pct = (1.0 - liq / self._entry_liq) * 100
                return True, f"emergency_liq_rug_{drop_pct:.0f}pct"

        # ── ATR trailing stop (moonbag phase only) ─────────────────────────
        if tp1_hit:
            atr = feed.compute_atr(periods=20)
            if atr is not None and atr > 0:
                # Track ATR-based peak
                if self._atr_peak is None or price > self._atr_peak:
                    self._atr_peak = price

                # Dynamic stop: peak - max(2.5*ATR, 5% of price)
                min_distance = price * self.ATR_MIN_FRAC
                stop_dist = max(self.ATR_MULTIPLIER * atr, min_distance)
                atr_stop = self._atr_peak - stop_dist

                if price <= atr_stop:
                    drop_pct = (price - self._atr_peak) / self._atr_peak * 100
                    return True, f"atr_trail_{abs(drop_pct):.1f}pct"

        # ── AI SELL signal ────────────────────────────────────────────────
        if ai_decision and ai_decision.action == "SELL" and ai_decision.confidence >= 0.80:
            return True, f"ai_{ai_decision.tier}_sell_conf{ai_decision.confidence:.0%}"

        return False, ""


# ── TradeMonitor ──────────────────────────────────────────────────────────────

class TradeMonitor:
    """Orchestrates DataFeed + AICascade + AdaptiveExitEngine for one position."""

    EVAL_INTERVAL = 2.0   # seconds between engine evaluations

    def __init__(
        self,
        mint: str,
        pos_ref: dict,
        close_fn,
        market_data_fn,
        pool_resolve_fn,
        runtime: Any,
    ) -> None:
        self.mint = mint
        self._pos_ref = pos_ref
        self._close_fn = close_fn
        self._runtime = runtime
        self._task: asyncio.Task | None = None
        self._running = False
        self._start_ts = time.time()
        self._exit_reason: str | None = None

        self._feed   = DataFeed(mint, market_data_fn, pool_resolve_fn, runtime)
        self._ai     = AICascade()
        self._engine = AdaptiveExitEngine()

    def get_log_snapshot(self) -> dict:
        """Return a structured log of all AI decisions for the debrief reporter."""
        decisions = self._ai.decisions
        all_d = [
            {
                "ts":        d.ts,
                "tier":      d.tier,
                "action":    d.action,
                "confidence": d.confidence,
                "reason":    d.reason,
                "urgency":   d.urgency,
                "triggered_exit": d.triggered_exit,
            }
            for d in decisions
        ]

        # Tier breakdown counts
        def _tier_stats(tier_name: str) -> dict:
            td = [d for d in decisions if d.tier == tier_name]
            return {
                "total_calls":  len(td),
                "hold_count":   sum(1 for d in td if d.action == "HOLD"),
                "watch_count":  sum(1 for d in td if d.action == "WATCH"),
                "sell_count":   sum(1 for d in td if d.action == "SELL"),
                "alert_count":  sum(1 for d in td if d.urgency in ("high", "critical")),
                "last_action":  td[-1].action    if td else None,
                "last_confidence": td[-1].confidence if td else None,
                "last_reason":  td[-1].reason    if td else None,
            }

        groq_d   = _tier_stats("groq")
        gemini_d = _tier_stats("gemini")
        sonnet_d = _tier_stats("sonnet")
        opus_d   = _tier_stats("opus")

        # Last alert reason from Groq
        groq_alerts = [d for d in decisions if d.tier == "groq" and d.urgency in ("high", "critical")]
        groq_d["last_alert_reason"] = groq_alerts[-1].reason if groq_alerts else None

        return {
            "mint":          self.mint,
            "start_ts":      self._start_ts,
            "exit_reason":   self._exit_reason,
            "all_decisions": all_d,
            "groq":          groq_d,
            "gemini":        gemini_d,
            "claude":        sonnet_d,
            "opus":          opus_d,
            "total_decisions": len(decisions),
        }

    def cancel(self) -> None:
        self._running = False
        self._feed.stop()
        if self._task and not self._task.done():
            self._task.cancel()

    async def run(self, session: aiohttp.ClientSession) -> None:
        self._running = True

        # Start data feed as a sibling task
        feed_task = asyncio.get_event_loop().create_task(self._feed.run(session))
        self._task = feed_task

        # Give the feed a few seconds to warm up
        await asyncio.sleep(6)

        try:
            while self._running:
                pos = self._pos_ref.get(self.mint)
                if not pos:
                    break  # position closed externally

                # Run AI cascade on schedule — give cascade current position context
                ai_dec: AIDecision | None = None
                try:
                    _entry = pos.get("entry_price", 0)
                    _cur   = pos.get("current_price") or _entry
                    _pnl   = round((_cur / _entry - 1) * 100, 1) if _entry else None
                    self._ai._mint        = self.mint
                    self._ai._token_name  = pos.get("token_name", self.mint[:8])
                    self._ai._last_pnl_pct = _pnl
                    ai_dec = await self._ai.evaluate(self.mint, pos, self._feed, session)
                except Exception as exc:
                    print(f"[monitor/ai] {self.mint[:8]} cascade error: {exc}")

                # Log notable AI decisions
                if ai_dec and ai_dec.action != "HOLD":
                    tok = pos.get("token_name", self.mint[:8])
                    pnl = pos.get("pnl_pct", 0.0) or 0.0
                    print(
                        f"[monitor] 🤖 {ai_dec.tier.upper()} → {ai_dec.action} "
                        f"conf={ai_dec.confidence:.0%} [{tok} {pnl:+.1f}%] — {ai_dec.reason[:80]}"
                    )

                # Exit engine evaluation
                should_exit, exit_reason = self._engine.evaluate(pos, self._feed, ai_dec)
                if should_exit and self.mint in self._pos_ref:
                    tok = pos.get("token_name", self.mint[:8])
                    print(f"[monitor] ⚡ EXIT signal: {tok} ({self.mint[:8]}) — {exit_reason}")
                    self._exit_reason = exit_reason
                    # Mark the triggering AI decision
                    if ai_dec and "ai_" in exit_reason:
                        ai_dec.triggered_exit = True
                    # Notify learning engine before close
                    try:
                        from elizaos.plugins.solana.learning_engine import (
                            on_monitor_exit_signal,
                        )
                        on_monitor_exit_signal(self.mint, exit_reason, self._feed)
                    except Exception:
                        pass
                    # Resolve brain memory outcomes
                    try:
                        _final_pnl = pos.get("pnl_pct") or 0.0
                        _outcome = (
                            "TP_HIT"      if "tp" in exit_reason.lower() else
                            "SL_HIT"      if "sl" in exit_reason.lower() or "stop" in exit_reason.lower() else
                            "WALLET_EXIT" if "wallet" in exit_reason.lower() else
                            "MANUAL"
                        )
                        for _b in ("groq", "gemini", "claude"):
                            _bm.resolve_outcome(_b, self.mint, _outcome, _final_pnl)
                        # For Meteora positions, record detailed signal state so brains learn
                        if pos.get("dex") == "meteora":
                            _snap = self._feed.snapshots[-1] if self._feed.snapshots else None
                            _w5m  = self._feed.price_window(300)
                            _liq_chg: float | None = None
                            _liq_trend = "stable"
                            _holder_trend = "stable"
                            _h_delta: int | None = None
                            if _snap and len(_w5m) >= 2:
                                _l5 = _w5m[0].liq
                                _ln = _snap.liq
                                if _l5 and _ln and _l5 > 0:
                                    _liq_chg = round((_ln - _l5) / _l5 * 100, 1)
                                    _liq_trend = "growing" if _liq_chg > 5 else "draining" if _liq_chg < -5 else "stable"
                                _h5 = _w5m[0].holders
                                _hn = _snap.holders
                                if _h5 and _hn:
                                    _h_delta = _hn - _h5
                                    _holder_trend = "growing" if _h_delta > 5 else "declining" if _h_delta < -5 else "stable"
                            _tok_name = pos.get("token_name", self.mint[:8])
                            for _b in ("groq", "gemini", "claude"):
                                _bm.record_meteora_exit(
                                    _b, self.mint, _tok_name,
                                    _liq_trend, _holder_trend, _final_pnl,
                                    exit_reason, _liq_chg, _h_delta,
                                )
                    except Exception:
                        pass
                    # Execute close via the shared close function
                    try:
                        await self._close_fn(self.mint, exit_reason, session, self._runtime)
                    except Exception as exc:
                        print(f"[monitor] Close error for {self.mint[:8]}: {exc}")
                    break  # position is gone

                await asyncio.sleep(self.EVAL_INTERVAL)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[monitor] {self.mint[:8]} monitor crashed: {exc}")
        finally:
            self._feed.stop()
            feed_task.cancel()
            _active_monitors.pop(self.mint, None)


# ── Context builder ───────────────────────────────────────────────────────────

# ── Whale-concentration probe ────────────────────────────────────────────
# Detects "single-whale exit" vs "broad distribution" on dips. PETS 2026-05-01
# went from -2% to -19% on what may have been one large sell — without this
# signal, brains see only aggregate "price dropping" and react.
_whale_cache: dict[str, tuple[float, dict]] = {}
_WHALE_CACHE_TTL = 10.0  # seconds — multi-brain calls within this window share data


def _parse_swap_simple(tx_resp: dict | None, mint: str) -> tuple[str, float] | None:
    """Lightweight pump-amm swap parser → ('BUY'|'SELL', sol_amount). None if not a swap."""
    if not tx_resp or not tx_resp.get("meta") or tx_resp.get("meta", {}).get("err"):
        return None
    meta = tx_resp["meta"]
    msg = (tx_resp.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    pre_sol = meta.get("preBalances") or []
    post_sol = meta.get("postBalances") or []
    pre_tok = meta.get("preTokenBalances") or []
    post_tok = meta.get("postTokenBalances") or []
    pre_map = {b["accountIndex"]: b for b in pre_tok if b.get("mint") == mint}
    post_map = {b["accountIndex"]: b for b in post_tok if b.get("mint") == mint}
    user: dict[str, float] = {}
    for idx in set(pre_map) | set(post_map):
        pre = float((pre_map.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post = float((post_map.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
        owner = (post_map.get(idx, {}) or pre_map.get(idx, {})).get("owner")
        d = post - pre
        if owner and abs(d) > 0.01:
            user[owner] = user.get(owner, 0) + d
    if not keys: return None
    signer = keys[0] if isinstance(keys[0], str) else (keys[0].get("pubkey") if isinstance(keys[0], dict) else None)
    if not signer or not pre_sol: return None
    sol_swap = -(post_sol[0] - pre_sol[0]) / 1e9 - (meta.get("fee") or 0) / 1e9
    md = user.get(signer, 0)
    if abs(md) < 0.01: return None
    if sol_swap > 0.001 and md > 0: return ("BUY", sol_swap)
    if sol_swap < -0.001 and md < 0: return ("SELL", -sol_swap)
    return None


async def _whale_concentration(session: aiohttp.ClientSession, mint: str, pool_addr: str) -> dict:
    """Last-30s pool swap concentration: was a dip from 1 whale or broad distribution?

    Returns dict with: largest_sell_sol, sell_concentration, largest_buy_sol,
    buy_concentration, m30s_swap_count. concentration ∈ [0,1] where 1.0 = single
    wallet did all the volume.
    """
    now = time.time()
    cached = _whale_cache.get(mint)
    if cached and (now - cached[0]) < _WHALE_CACHE_TTL:
        return cached[1]
    blank = {"largest_sell_sol": 0.0, "sell_concentration": 0.0,
             "largest_buy_sol": 0.0, "buy_concentration": 0.0,
             "m30s_swap_count": 0}
    try:
        # 1. Last 30 sigs on the pool — cap fetched sigs at 20 to limit cost
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                   "params": [pool_addr, {"limit": 30}]}
        async with session.post(HELIUS_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=4)) as r:
            if r.status != 200:
                _whale_cache[mint] = (now, blank); return blank
            data = await r.json()
        sigs = data.get("result") or []
        cutoff = now - 30
        recent = [s["signature"] for s in sigs
                  if (s.get("blockTime") or 0) >= cutoff and not s.get("err")][:15]
        # 2. Parse each into BUY/SELL with SOL amount
        buys: list[float] = []
        sells: list[float] = []
        for sig in recent:
            try:
                tp = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                      "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
                async with session.post(HELIUS_RPC, json=tp, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status != 200: continue
                    td = await r.json()
                p = _parse_swap_simple(td.get("result"), mint)
                if p:
                    (buys if p[0] == "BUY" else sells).append(p[1])
            except Exception:
                continue
        result = dict(blank)
        if buys:
            result["largest_buy_sol"] = round(max(buys), 3)
            tot = sum(buys)
            result["buy_concentration"] = round(max(buys) / tot, 2) if tot > 0 else 0
        if sells:
            result["largest_sell_sol"] = round(max(sells), 3)
            tot = sum(sells)
            result["sell_concentration"] = round(max(sells) / tot, 2) if tot > 0 else 0
        result["m30s_swap_count"] = len(buys) + len(sells)
        _whale_cache[mint] = (now, result)
        return result
    except Exception:
        _whale_cache[mint] = (now, blank)
        return blank


def _build_context(mint: str, pos: dict, feed: DataFeed, age_secs: float,
                   whale: dict | None = None) -> str | None:
    """Build a concise JSON context string for the AI prompt.

    whale: optional dict from _whale_concentration() — when provided, its fields
    are merged into the context so brains can evaluate G7 WHALE_PATTERN.
    """
    if not feed.snapshots:
        return None

    snap = feed.snapshots[-1]
    entry = pos.get("entry_price", 0.0)
    if not entry or entry <= 0:
        return None

    pnl_pct   = (snap.price - entry) / entry * 100.0
    peak_pnl  = pos.get("peak_pnl_pct", 0.0) or 0.0
    tp1_hit   = pos.get("tp1_hit", False)
    locked    = pos.get("locked_sol", 0.0) or 0.0

    # Price changes
    w30  = feed.price_window(30)
    w5m  = feed.price_window(300)
    chg_30s  = ((snap.price - w30[0].price) / w30[0].price * 100)  if len(w30) >= 2 else 0.0
    chg_5min = ((snap.price - w5m[0].price) / w5m[0].price * 100)  if len(w5m) >= 2 else 0.0

    atr = feed.compute_atr()

    # Holder count delta (key rug signal: falling holders = distribution/dump)
    holder_delta: int | None = None
    w5m_snaps = feed.price_window(300)
    if len(w5m_snaps) >= 2:
        h_now  = snap.holders
        h_5ago = w5m_snaps[0].holders
        if h_now is not None and h_5ago is not None and h_5ago > 0:
            holder_delta = h_now - h_5ago

    # 30s holder delta — fast distribution/accumulation read
    holder_delta_30s: int | None = None
    if len(w30) >= 2:
        h_now    = snap.holders
        h_30ago  = w30[0].holders
        if h_now is not None and h_30ago is not None and h_30ago > 0:
            holder_delta_30s = h_now - h_30ago

    # Liquidity change (key rug signal: liq draining fast)
    liq_chg_5min_pct: float | None = None
    if len(w5m_snaps) >= 2:
        l_now  = snap.liq
        l_5ago = w5m_snaps[0].liq
        if l_now and l_5ago and l_5ago > 0:
            liq_chg_5min_pct = round((l_now - l_5ago) / l_5ago * 100, 1)

    # 30s liquidity change — catches sudden LP pulls before 5min window does
    liq_chg_30s_pct: float | None = None
    if len(w30) >= 2:
        l_now   = snap.liq
        l_30ago = w30[0].liq
        if l_now and l_30ago and l_30ago > 0:
            liq_chg_30s_pct = round((l_now - l_30ago) / l_30ago * 100, 1)

    # Pullback-shape metrics: lowest pnl in last 5min and how far we've bounced.
    # Lets brains tell "still falling" from "already bouncing off the dip".
    local_low_pnl_pct: float | None = None
    bounce_pct_from_local_low: float | None = None
    if w5m_snaps and entry > 0:
        prices_5m = [s.price for s in w5m_snaps if s.price > 0]
        if prices_5m:
            low_5m = min(prices_5m)
            local_low_pnl_pct = round((low_5m - entry) / entry * 100, 1)
            if low_5m > 0:
                bounce_pct_from_local_low = round((snap.price - low_5m) / low_5m * 100, 1)

    # Drawdown from peak (our OWN peak — how far we've given back since best P&L)
    pct_off_peak = round(pnl_pct - peak_pnl, 1) if peak_pnl > 0 else None

    # Momentum-ratio: "is the pump dying?" — h6 >> h1 means peak was hours ago,
    # we're in the rollback. Ratio > 5 is strong dying signal; < 2 is fresh.
    momentum_ratio: float | None = None
    if snap.chg_h1 > 1.0 and snap.chg_h6 > 0:
        momentum_ratio = round(snap.chg_h6 / snap.chg_h1, 1)

    ctx = {
        "mint":              mint[:8],
        "token":             pos.get("token_name", mint[:8]),
        "whale":             pos.get("wallet_name", "?"),
        "age_min":           round(age_secs / 60, 1),
        "pnl_pct":           round(pnl_pct, 1),
        "peak_pnl_pct":      round(peak_pnl, 1),
        "pct_off_peak":      pct_off_peak,
        "chg_30s_pct":       round(chg_30s, 1),
        "chg_5min_pct":      round(chg_5min, 1),
        "chg_h1_pct":        round(snap.chg_h1, 1),
        "chg_h6_pct":        round(snap.chg_h6, 1),
        "chg_h24_pct":       round(snap.chg_h24, 1),
        "momentum_ratio":    momentum_ratio,
        "tp1_hit":           tp1_hit,
        "locked_sol":        round(locked, 4),
        "bsr":               round(snap.buy_sell_ratio, 2) if snap.buy_sell_ratio else None,
        "vol_mc":            round(snap.vol_mc_ratio, 3)   if snap.vol_mc_ratio   else None,
        "liq_usd":           round(snap.liq or 0),
        "liq_chg_5min_pct":  liq_chg_5min_pct,
        "mc_usd":            round(snap.mc  or 0),
        "holders":           snap.holders,
        "holder_delta_5min": holder_delta,
        "holder_delta_30s":  holder_delta_30s,
        "liq_chg_30s_pct":   liq_chg_30s_pct,
        "local_low_pnl_pct": local_low_pnl_pct,
        "bounce_pct_from_local_low": bounce_pct_from_local_low,
        "atr_pct":           round(atr / snap.price * 100, 2) if (atr and snap.price) else None,
        "dex":               pos.get("dex", "unknown"),
        "narrative":         pos.get("narrative", "unknown"),
        "entry_verdict":     pos.get("ai_entry_verdict"),
        "catalyst":          pos.get("catalyst"),
        # Moonbag state — post-TP1 V-recovery tracking
        "moonbag_armed":     pos.get("moonbag_armed"),
        "post_tp1_low_pnl_pct": (
            round((float(pos["post_tp1_low"]) / entry - 1) * 100, 1)
            if pos.get("post_tp1_low") and entry > 0 else None
        ),
        "v_recovery":        (
            round(snap.price / float(pos["post_tp1_low"]), 2)
            if pos.get("post_tp1_low") and float(pos["post_tp1_low"]) > 0 else None
        ),
        # Strategy tag so the brain cascade can pull the right learned rules.
        # "meteora" lane is a sub-strategy of copy_trade but has its own
        # prompt template, so we keep it separate for hint purposes.
        "strategy":          (
            "meteora" if pos.get("dex") == "meteora"
            else (pos.get("strategy") or "copy_trade")
        ),
        # Signal source — drives which TP/SL/exit rails the brain should
        # reason about. "creator_alpha_*" sources use +100%/-75% rails
        # with brain-managed moonbag; others use standard +20%/-25%.
        "signal_source":     pos.get("signal_source") or "unknown",
        "tp1_target_pct":    (
            100.0 if str(pos.get("signal_source") or "").startswith("creator_alpha") else 20.0
        ),
        "floor_pct":         (
            -75.0 if str(pos.get("signal_source") or "").startswith("creator_alpha") else -25.0
        ),
        "moonbag_managed":   str(pos.get("signal_source") or "").startswith("creator_alpha"),
    }
    # Merge whale-concentration probe (G7 WHALE_PATTERN gate). Optional —
    # when None, the fields are absent and brains skip the gate.
    if whale:
        for k in ("largest_sell_sol", "sell_concentration",
                  "largest_buy_sol", "buy_concentration", "m30s_swap_count"):
            if k in whale:
                ctx[k] = whale[k]
    return json.dumps(ctx)


def _calc_drawdown(pos: dict) -> float | None:
    """Return drawdown from peak as a negative %. None if no peak data."""
    current = pos.get("current_price") or pos.get("entry_price")
    peak    = pos.get("peak_price") or pos.get("entry_price")
    if not current or not peak or peak <= 0:
        return None
    return (current - peak) / peak * 100.0


def _parse_ai_decision(raw: str, tier: str) -> AIDecision | None:
    """Parse AI JSON response into an AIDecision. Lenient parsing.

    Order of attempts:
      1. Strip markdown code fences and try json.loads on the whole thing.
      2. Regex-extract the first {...} block and try json.loads on that.
      3. Give up (return None — the cascade just retries on next tick).
    """
    text = (raw or "").strip()
    if not text:
        return None
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    d: dict | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            d = parsed
    except Exception:
        # Fallback: extract the first balanced {...} JSON object from the
        # text and try that. Handles prose-wrapped JSON outputs which Groq
        # sometimes produces when not in strict json_object mode.
        import re
        match = re.search(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    d = parsed
            except Exception:
                d = None

    if d is None:
        return None

    try:
        action     = str(d.get("action", "HOLD")).upper()
        confidence = float(d.get("confidence", 0.5))
        reason     = str(d.get("reason", ""))[:200]
        urgency    = str(d.get("urgency", "low")).lower()
        if action not in ("HOLD", "SELL", "REDUCE", "WATCH"):
            action = "HOLD"
        confidence = max(0.0, min(1.0, confidence))
        # Risk level — prefer explicit field, fall back to action mapping.
        # HIGH = code triggers exit. MEDIUM = monitor. LOW = hold confidently.
        raw_risk = str(d.get("risk", d.get("risk_level", ""))).upper()
        if raw_risk not in ("LOW", "MEDIUM", "HIGH"):
            raw_risk = {"SELL": "HIGH", "WATCH": "MEDIUM", "HOLD": "LOW",
                        "REDUCE": "HIGH"}.get(action, "MEDIUM")
        return AIDecision(action=action, confidence=confidence, reason=reason,
                          urgency=urgency, tier=tier, risk_level=raw_risk)
    except Exception:
        return None


# ── Prompts ───────────────────────────────────────────────────────────────────

_CLAUDE_SYSTEM = """You are the depth tier of a three-brain cascade (Groq 30s → Gemini 2m → YOU 5m) running live on Solana meme coins. Groq flags immediate rugs; Gemini confirms patterns; your job is the quant decision with full context.

MINDSET: You are a quantitative meme-coin trader. You feed off accumulated trade data (win/loss patterns, wallet-behaviour history, bad_token_dna, learned patterns injected into your prompt) and the live token makeup (holders, liquidity, BSR, narrative, DEX, pair age, entry verdict, prior brain decisions on this mint). Every decision is data-backed and falsifiable. You are not cautious by default — you are *logical*. A rising position with expanding holders and stable liq is a HOLD even if it's already up 80%; a stalled position with holders leaving is a SELL even at +5%.

MOMENTUM DIAGNOSTICS: The context includes `chg_h1_pct`, `chg_h6_pct`, `chg_h24_pct` and a derived `momentum_ratio` (= h6/h1). Use these to tell fresh continuation from dying rally:
 - momentum_ratio 1-2, h1 strongly positive → fresh breakout, continuation likely
 - momentum_ratio 3-5 → rally aging, cautious HOLD
 - momentum_ratio >5 or h6 positive while h1 near zero → peak was hours ago, rollback in progress, bias to SELL
 - `pct_off_peak` (negative number) is drawdown from our entry-relative peak: -20% off peak with stalling bsr is a SELL signal even above TP1.

CATALYST: `catalyst` (if populated) is a Grok/Gemini social-buzz snapshot at entry — fields `active` (bool), `confidence` (0-10), `reason`. Use it as a softener:
 - active=true + confidence≥6 → narrative is real, HOLD through minor noise
 - active=false OR confidence≤3 → no catalyst backing, weaker hold bias; any negative price signal should trigger SELL faster
 - Missing (null) → no social data yet; don't assume either way.

MOONBAG PHASE (post-TP1 only): After TP1 fires the 25% bag is in one of two states:
 - `moonbag_armed=false` (DORMANT): the bag is riding through a cooldown. The runtime will IGNORE your SELL calls — you are in observer mode only. Keep scoring so the dashboard shows your read, but your job here is informational. Don't "panic" the dormant bag.
 - `moonbag_armed=true` (ARMED): price has V-recovered ≥1.5× from the post-TP1 low. Leg 2 is in play. You are now the primary exit driver for the 25%. Watch for DISTRIBUTION: holder_delta_5min negative, liq_chg_5min_pct < -10%, bsr < 1.0, top-10 concentration rising. If 2+ of these align, SELL at high confidence — Leg 2 exits are profit protection, not fresh entry.
 - `v_recovery` shows current ÷ post-TP1 low; `post_tp1_low_pnl_pct` is how deep the cooldown went.

TOKEN MAKEUP REASONING: Before deciding, mentally assess:
 1. Is this token's profile (age, liq, holder count, narrative) consistent with winners in our history, or with losers?
 2. What's the active flow: accumulation (growing holders + rising BSR + stable liq) or distribution (holders falling + BSR < 0.45 + liq draining)?
 3. Is P&L at a level where giving back is acceptable to capture the runner, or are we in "protect gains" mode?
 4. Does the learned-patterns block contradict or confirm what I see?

OUTPUT: Respond with valid JSON only — no prose. SELL only when the data shows a clear danger signal; HOLD when momentum is intact; WATCH when signals are mixed and another tick will clarify.
"""

_HOLD_SELL_PROMPT = """Decide HOLD / SELL / WATCH on this open Solana meme-coin position by evaluating six gates, then synthesizing.

=== STRATEGY BRIEFING — READ THIS FIRST ===

We enter Solana meme tokens at the bonding-curve (BC) stage — seconds after creation —
from a curated network of 46 confirmed operators (MEGA OPERATOR, DWOGE, and 44 others)
whose child wallets consistently produce 1000%+ tokens. Examples: Milkers (+1380%),
Macy's Inc (HBBSGUB +160%), BOOBFACE (+110%), Trump Coin, CAMINO.

Entry filters already applied before you see a position:
  - MC gate: token MC < $20K at entry (not already pumped by bundlers)
  - Age gate: token < 30 min old (not a stale signal)
  - Bundle detection: clean or near-clean creation slot
  - Holder guard: no hard-block flags

RAILS:
  - Hard TP: +100% → auto full-exit. You do NOT manage this — it fires automatically.
  - Catastrophic floor: -25% → auto full-exit. You do NOT manage this either.
  - Position size: 0.2 SOL per trade × 2 concurrent max.
  - You are the ONLY brain. No Gemini, no Claude.

YOUR WINDOW: pnl = 15% to 80% only.
  - Below 15%: still developing, floor will handle a rug. Stay out of it.
  - Above 80%: 20% from the +100% TP. The token is running. DO NOT SELL. Let it hit the TP.
  - In 15-80%: this is your zone — watch for stagnation and distribution.

TWO SCENARIOS you will see in this range:

  SCENARIO A — RUNNER (HOLD):
    Token came off the BC and is climbing steadily. Holders growing, BSR > 0.55,
    liq stable or growing. This is a Milkers or BOOBFACE in motion. Your job:
    HOLD and let the +100% TP fire. Do NOT sell at 30% or 50% — we want the full double.
    These tokens move fast. If everything looks healthy, HOLD with high confidence.

  SCENARIO B — STALL/DUMP (SELL):
    Token hit 20-40%, then momentum died. Holders flat or falling, BSR dipping,
    price chopping sideways or slowly declining. The pump is over, insiders are
    distributing. Your job: SELL now and bank the 20-40% profit before it reverses
    back to breakeven or worse. Do not wait for a recovery that isn't coming.
    Be decisive — a 30% exit is far better than a -25% floor exit.

RISK LEVELS — your primary output. Code acts on risk, not on "should I sell?":
  HIGH   = observable danger: liq draining + holders fleeing + sell pressure. Code exits immediately.
  MEDIUM = mixed signals, one gate concerning but not confirmed. Code monitors.
  LOW    = all gates healthy, momentum intact. Code holds. Do NOT output HIGH on vague concern.

SELL threshold: risk=HIGH with confidence ≥ 0.75 → code exits immediately.
HOLD threshold: risk=LOW or MEDIUM → hold. When in doubt, MEDIUM not HIGH.

EVALUATION GATES (assess each independently before deciding):

  G1 HOLDER_FLOW    — holder_delta_5min: < -50 = exodus → SELL signal | -50..0 = mild distribution → caution | 0..50 = stable | > 50 = accumulation → HOLD signal
                      Cross-check holder_delta_30s — fast distribution shows here first.
  G2 LIQUIDITY      — liq_chg_5min_pct: < -30 = rug-in-progress → SELL urgency=high | -30..-10 = draining → caution | > 0 = healthy → HOLD signal
                      Cross-check liq_chg_30s_pct < -10 = LP pull happening NOW → SELL high.
  G3 BUY_PRESSURE   — bsr: < 0.35 = mass selling → SELL high | 0.35..0.50 = mixed → WATCH | > 0.55 with growing liq = strong HOLD
  G4 PRICE_ACTION   — chg_5min_pct + pct_off_peak: < -8% with no bounce = dying → SELL | -3..+3 = consolidation → WATCH | rising = HOLD
  G5 PHASE          — tp1_hit + moonbag_armed: pre-TP1 (default in hard-TP era) = primary exit decider | DORMANT moonbag (legacy) = observer only | ARMED moonbag (legacy) = primary exit again
  G6 PULLBACK_SHAPE — bounce_pct_from_local_low + local_low_pnl_pct + holder_delta_30s + liq_chg_30s_pct:
                      FALSE PULLBACK (HOLD bias): pnl currently negative BUT bounce_pct_from_local_low > +3% AND holder_delta_30s ≥ 0 AND liq_chg_30s_pct > -5% → dip absorbed, recovery in progress, do not panic-sell
                      REAL DUMP (SELL bias): pnl negative AND bounce_pct_from_local_low ≤ 0 AND holder_delta_30s < 0 AND liq_chg_30s_pct < -5% → still falling with distribution, exit before catastrophic floor
                      MIXED → WATCH another tick
  G7 WHALE_PATTERN  — sell_concentration + largest_sell_sol (last 30s on-chain). Disambiguates "1 whale exit" from "broad distribution":
                      SINGLE WHALE EXIT (HOLD bias): sell_concentration > 0.65 AND largest_sell_sol > 1.5 AND m30s_swap_count >= 5 → one wallet dumped, base holders unchanged. Don't panic — wait for absorption.
                      BROAD DISTRIBUTION (SELL bias): sell_concentration < 0.40 AND multiple sells > 1 SOL → many wallets exiting, real exodus
                      THIN BOOK (caution): m30s_swap_count < 5 → not enough data, lean on G1-G6
                      Field absent → fields not provided this tick, skip the gate

FEW-SHOT EXAMPLES:

Example A — clear HOLD:
  context: pnl=+18%, bsr=0.62, holder_delta_5min=+34, liq_chg_5min_pct=+6.1, chg_5min_pct=+2.4, tp1_hit=false, bounce_pct_from_local_low=+8, local_low_pnl_pct=+9
  output: {{"reasoning":{{"G1_HOLDER_FLOW":"delta=+34 = accumulation","G2_LIQUIDITY":"+6.1% = growing","G3_BUY_PRESSURE":"bsr 0.62 = buying dominant","G4_PRICE_ACTION":"+2.4% with growing liq = active","G5_PHASE":"pre-TP, primary decider","G6_PULLBACK_SHAPE":"already +8% above local low and accumulating = constructive"}},"risk":"LOW","action":"HOLD","confidence":0.80,"reason":"5 of 6 gates positive, no distribution signal","urgency":"low"}}

Example B — clear danger (real dump):
  context: pnl=+12%, bsr=0.31, holder_delta_5min=-87, holder_delta_30s=-22, liq_chg_5min_pct=-22.4, liq_chg_30s_pct=-8.2, chg_5min_pct=-9.1, bounce_pct_from_local_low=0, tp1_hit=false
  output: {{"reasoning":{{"G1_HOLDER_FLOW":"delta=-87 (5m) and -22 (30s) = exodus","G2_LIQUIDITY":"-22.4% (5m) and -8.2% (30s) = active drain","G3_BUY_PRESSURE":"bsr 0.31 = mass selling","G4_PRICE_ACTION":"-9.1% no bounce = dying","G5_PHASE":"pre-TP, exit decisively","G6_PULLBACK_SHAPE":"no bounce + holders fleeing + liq draining = real dump"}},"risk":"HIGH","action":"SELL","confidence":0.90,"reason":"holders fleeing + liq actively draining + sell pressure dominant + no bounce","urgency":"high"}}

Example C — false pullback (hold through dip):
  context: pnl=-8%, bsr=0.58, holder_delta_5min=-3, holder_delta_30s=+5, liq_chg_5min_pct=-2.1, liq_chg_30s_pct=+1.4, chg_5min_pct=-4.2, bounce_pct_from_local_low=+5.8, local_low_pnl_pct=-13, peak_pnl_pct=+11, tp1_hit=false
  output: {{"reasoning":{{"G1_HOLDER_FLOW":"5m -3 but 30s +5 = base holders absorbing dip","G2_LIQUIDITY":"5m -2.1% but 30s +1.4% = stabilising","G3_BUY_PRESSURE":"bsr 0.58 = buyers stepping in","G4_PRICE_ACTION":"-4.2% but rebounding","G5_PHASE":"pre-TP","G6_PULLBACK_SHAPE":"already +5.8% off local -13% low with positive 30s flow = false pullback, recovery in progress"}},"risk":"LOW","action":"HOLD","confidence":0.74,"reason":"dip absorbed, holders/liq stabilising, ride the recovery","urgency":"low"}}

POSITION DATA:
{context}

Respond with JSON only — same shape as examples above. The `risk` field is the primary signal the code acts on. Be precise: only HIGH when multiple gates confirm danger simultaneously.
{{"reasoning": {{"G1_HOLDER_FLOW":"...","G2_LIQUIDITY":"...","G3_BUY_PRESSURE":"...","G4_PRICE_ACTION":"...","G5_PHASE":"...","G6_PULLBACK_SHAPE":"...","G7_WHALE_PATTERN":"..."}}, "risk": "LOW"|"MEDIUM"|"HIGH", "action": "HOLD"|"SELL"|"WATCH", "confidence": 0.0-1.0, "reason": "one sentence", "urgency": "low"|"medium"|"high"}}"""


_METEORA_HOLD_SELL_PROMPT = """You are a quant trader managing a Meteora DLMM meme-coin position. Think precisely.
There is NO fixed take-profit — you decide when to exit based on real signals.

POSITION DATA:
{context}

YOUR JOB: Read the liquidity and holder signals like a quant. If momentum is building, let it run.
If momentum is dying, exit cleanly and preserve capital.

METEORA SIGNAL RULES (apply in order):

EXIT signals — act decisively:
1. liq_chg_5min_pct < -15% → liquidity draining, smart money exiting = SELL high confidence
2. holder_delta_5min < -30 → holders distributing = SELL urgency=high
3. liq_chg_5min_pct < -8% AND holder_delta_5min < 0 → both declining = SELL
4. chg_5min_pct < -6% with liq flat or declining = momentum dead = SELL
5. bsr < 0.40 → sell pressure overwhelming buys = SELL

HOLD / RUN signals — stay in and let it work:
6. liq_chg_5min_pct > +5% AND holder_delta_5min > 0 → liquidity AND holders growing = HOLD, this can run further
7. holder_delta_5min > +20 → new buyers piling in = strong HOLD
8. chg_5min_pct > +3% with growing liq = active momentum = HOLD
9. bsr > 0.60 with stable or growing liq = buying pressure dominant = HOLD
10. P&L > 20% with no exit signals = let winners run, HOLD

CONTEXT: Meteora has deeper liquidity than PumpSwap. Sustained moves of 50-200% are normal
when the dual-DEX structure holds. Do not exit at 15% if the signals say the move is continuing.

Respond with JSON only:
{{"action": "HOLD"|"SELL"|"WATCH", "confidence": 0.0-1.0, "reason": "one sentence stating key signal observed", "urgency": "low"|"medium"|"high", "liq_trend": "growing"|"stable"|"draining", "holder_trend": "growing"|"stable"|"declining"}}"""


_ENTRY_SCAN_PROMPT = """You are assessing a new Solana meme-coin position just opened by a copy-trade bot.
Rate whether this token is likely a RUNNER or a RUG_RISK based on entry conditions.

ENTRY DATA:
{context}

Rate based on:
- Liquidity: < $5k = rug risk, $5-20k = normal, > $20k = healthy
- Holder count: < 100 = risky, > 300 = healthy
- Narrative: animal/meme tokens rug more than utility/ai/defi
- Whale quality: known profitable whale = better signal
- Momentum: are conditions favorable for a run?

Respond with JSON only:
{{"verdict": "RUNNER"|"NORMAL"|"RUG_RISK", "confidence": 0.0-1.0, "reason": "one sentence", "key_risks": ["...", "..."]}}"""


# ── Learned patterns injection ────────────────────────────────────────────────

_PATTERNS_PATH = os.path.join(_DIR, "winning_patterns.json")
_LESSONS_PATH = os.path.join(_DIR, "eliza_lessons.txt")
_cached_patterns: str = ""
_patterns_loaded_ts: float = 0.0
_cached_lessons: str = ""
_lessons_loaded_ts: float = 0.0


def _get_eliza_lessons() -> str:
    """Load eliza_lessons.txt and return first 3000 chars. Cached 10min."""
    global _cached_lessons, _lessons_loaded_ts
    if time.time() - _lessons_loaded_ts < 600:
        return _cached_lessons
    try:
        if os.path.exists(_LESSONS_PATH):
            with open(_LESSONS_PATH) as f:
                raw = f.read(3000)
            _cached_lessons = "\n\nELIZA ON-CHAIN LESSONS:\n" + raw
        else:
            _cached_lessons = ""
        _lessons_loaded_ts = time.time()
    except Exception:
        _cached_lessons = ""
    return _cached_lessons


def _get_learned_patterns(strategy_hint: str = "") -> str:
    """Return learned patterns + eliza lessons as system prompt injection.

    If strategy_hint is set (e.g. "monster_lifecycle", "copy_trade", "meteora"),
    patterns tagged with that strategy are surfaced first, then GLOBAL rules,
    then everything else. Per-strategy verdict (if available) is injected as a
    headline. Cached 5min only for the no-hint baseline.
    """
    global _cached_patterns, _patterns_loaded_ts
    if time.time() - _patterns_loaded_ts < 300 and not strategy_hint:
        return _cached_patterns
    try:
        lines = []
        if os.path.exists(_PATTERNS_PATH):
            with open(_PATTERNS_PATH) as f:
                data = json.load(f)
            patterns = data.get("patterns", []) or []

            # Strategy-specific verdict injection
            verdicts = data.get("per_strategy_verdicts") or {}
            legacy_meteora = data.get("meteora_verdict", "")
            lines.append("\n\nLEARNED PATTERNS FROM PAST TRADES:")

            if strategy_hint:
                hint_upper = strategy_hint.upper()
                # Headline insight for this strategy
                v = verdicts.get(strategy_hint) or (legacy_meteora if strategy_hint == "meteora" else "")
                if v and v.strip() and v.strip().lower() != "insufficient_data":
                    lines.append(f"{hint_upper} INSIGHT: {v}")

                # Rank: strategy-specific → GLOBAL → other lanes
                own, global_, other = [], [], []
                for p in patterns:
                    ptag = (p.split(":", 1)[0] if ":" in p else "").strip().lower()
                    if ptag == strategy_hint.lower():
                        own.append(p)
                    elif ptag == "global":
                        global_.append(p)
                    else:
                        other.append(p)
                ranked = own + global_ + other
                # Favour own+global heavily; keep a couple of cross-lane hints for context
                for p in own[:6]:
                    lines.append(f"- {p}")
                for p in global_[:3]:
                    lines.append(f"- {p}")
                for p in other[:2]:
                    lines.append(f"- (cross-lane) {p}")
                if not own and not global_ and not other:
                    lines.append("- (no learned patterns yet — need more closed trades)")
            else:
                for p in patterns[:10]:
                    lines.append(f"- {p}")

        lessons = _get_eliza_lessons()
        result = "\n".join(lines) + lessons if lines else lessons
        if not strategy_hint:
            _cached_patterns = result
            _patterns_loaded_ts = time.time()
        return result
    except Exception:
        if not strategy_hint:
            _cached_patterns = ""
        return ""


# ── State persistence (lightweight — just monitor count for diagnostics) ──────

def save_monitor_state() -> None:
    try:
        state = {
            "ts": time.time(),
            "active_monitors": list(_active_monitors.keys()),
            "count": len(_active_monitors),
        }
        with open(_MONITOR_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
