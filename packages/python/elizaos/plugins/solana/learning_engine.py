"""
learning_engine.py — Adaptive learning system for the copy-trade bot.

Captures full trade context at entry and exit, schedules post-exit price checks
to evaluate timing quality, and runs Claude Opus pattern analysis every 50 trades.
Learned patterns are injected into the TradeMonitor AI prompts.

Key components:
  record_trade_entry(mint, pos)          — call when position opens
  record_trade_exit(mint, close_record)  — call when position closes
  check_post_exit_due(session)           — call periodically (e.g. every 5min)
  run_learning_scheduler(session)        — long-running background task
  get_dynamic_prompt()                   — returns learned rules for AI injection

Storage:
  learning_outcomes.json  — all TradeOutcome records (capped 1000)
  winning_patterns.json   — Opus-extracted patterns (read by trade_monitor.py)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp

_DIR = os.path.dirname(__file__)
_OUTCOMES_PATH = os.path.join(_DIR, "learning_outcomes.json")
_PATTERNS_PATH = os.path.join(_DIR, "winning_patterns.json")
_PENDING_CHECKS_PATH = os.path.join(_DIR, "learning_pending_checks.json")

_MAX_OUTCOMES = 1000

# ── In-memory state ───────────────────────────────────────────────────────────
_outcomes: list[dict] = []
_outcomes_loaded = False

# Tracks how many trades since last Opus analysis
_trades_since_analysis: int = 0
_ANALYSIS_EVERY_N = 50      # run Opus every 50 closed trades
_last_analysis_ts: float = 0.0

# Pending post-exit checks: mint → {exit_ts, exit_price, checks_done}
# Persisted to disk so that pending windows survive bot restarts
# (otherwise every restart wipes the 5/30/60-min lookups mid-flight).
_pending_checks: dict[str, dict] = {}
_pending_checks_loaded = False


def _load_pending_checks() -> None:
    global _pending_checks, _pending_checks_loaded
    if _pending_checks_loaded:
        return
    try:
        if os.path.exists(_PENDING_CHECKS_PATH):
            with open(_PENDING_CHECKS_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                _pending_checks = data
    except Exception as _e:
        print(f"[learning] pending_checks load error: {_e}")
    _pending_checks_loaded = True


def _save_pending_checks() -> None:
    try:
        tmp = _PENDING_CHECKS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_pending_checks, f, indent=2)
        os.replace(tmp, _PENDING_CHECKS_PATH)
    except Exception as _e:
        print(f"[learning] pending_checks save error: {_e}")
_POST_EXIT_WINDOWS = [
    ("5m",   5  * 60),
    ("30m",  30 * 60),
    ("60m",  60 * 60),
]


# ── TradeOutcome dataclass ────────────────────────────────────────────────────

@dataclass
class TradeOutcome:
    """Full signal fingerprint for a closed trade — used for pattern analysis."""
    # Identity
    mint:            str
    token_name:      str
    wallet:          str           # whale wallet alias (or "monster" for strategy E)
    ts_open:         float         # epoch of position open
    ts_close:        float         # epoch of position close
    hold_mins:       float

    # Entry context
    entry_price:     float
    entry_mc_usd:    float | None
    entry_liq_usd:   float | None
    entry_holder_cnt: int | None
    narrative:       str

    # Exit context
    exit_reason:     str
    pnl_pct:         float
    pnl_sol:         float
    peak_pnl_pct:    float
    tp1_hit:         bool
    tp2_hit:         bool
    locked_sol:      float

    # Exit timing quality (filled in by post-exit checks)
    price_5m_pct:    float | None = None   # % change 5min after our exit
    price_30m_pct:   float | None = None   # % change 30min after our exit
    price_60m_pct:   float | None = None   # % change 60min after our exit
    exit_was_early:  bool | None  = None   # True if price kept rising after exit

    # Monitor AI context (if TradeMonitor was active)
    monitor_last_action:     str | None = None
    monitor_last_confidence: float | None = None
    monitor_exit_reason:     str | None = None

    # Tags for analysis
    was_winner:      bool = False
    was_rug:         bool = False     # True if hold < 5min and pnl < -30%

    # Computed tags (filled at creation)
    exit_category:   str = ""         # "tp_win" | "sl_loss" | "trail_win" | "rug" | "other"

    # ── Strategy segregation (added 2026-04-20) ───────────────────────────
    # So the pattern analyzer can produce PER-STRATEGY rules instead of
    # a single blended ruleset. "copy_trade" is the historical default.
    strategy:        str = "copy_trade"     # "copy_trade" | "monster_lifecycle" | "monster_cluster" | "monster_serial" | "meteora" | "raydium" | "pumpswap"
    signal_source:   str | None = None      # monster sub-source if strategy==monster_*

    # ── Entry-timing fingerprint (added 2026-04-20) ───────────────────────
    # Filled when we know them at entry. Used to learn whether we're
    # chasing pumps or catching setups early.
    h1_change_at_entry:   float | None = None   # % price change in h1 at entry time
    m5_change_at_entry:   float | None = None   # % price change in m5 at entry time
    age_hours_at_entry:   float | None = None   # token age (hours) at entry
    buy_ratio_at_entry:   float | None = None   # h1 buy ratio % at entry
    top1_pct_at_entry:    float | None = None   # top-1 holder % at entry
    top10_pct_at_entry:   float | None = None   # top-10 aggregate % at entry (distribution proxy)

    # ── Expanded entry fingerprint (added 2026-04-22) ─────────────────────
    # So the pattern analyzer can correlate these with win/loss.
    h6_change_at_entry:      float | None = None   # h6 priceChange at entry
    h24_change_at_entry:     float | None = None   # h24 priceChange at entry
    momentum_ratio_at_entry: float | None = None   # h6/h1 ratio at entry (>5 = dying)
    mcap_velocity_at_entry:  float | None = None   # mcap USD / age minutes at entry
    pct_off_peak_at_entry:   float | None = None   # current/60m_peak at entry (0-1)

    # ── Catalyst snapshot at entry (social_monitor result) ────────────────
    catalyst_active:     bool | None  = None        # Grok/Gemini buzz hit
    catalyst_confidence: int  | None  = None        # 0-10 buzz score

    # ── Moonbag lifecycle (post-TP1 25% bag) ──────────────────────────────
    # Did the V-recovery arm the brains? How deep did the cooldown go?
    # Lets us learn when ride-through paid vs. when we should have bailed earlier.
    moonbag_armed:             bool | None  = None  # reached v_recovery >= 1.5
    post_tp1_low_pnl_pct:      float | None = None  # deepest drawdown from entry post-TP1
    v_recovery_max:            float | None = None  # max current/post_tp1_low ratio reached


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_outcomes() -> list[dict]:
    global _outcomes, _outcomes_loaded
    if _outcomes_loaded:
        return _outcomes
    try:
        if os.path.exists(_OUTCOMES_PATH):
            with open(_OUTCOMES_PATH) as f:
                data = json.load(f)
            _outcomes = data if isinstance(data, list) else []
        else:
            _outcomes = []
    except Exception:
        _outcomes = []
    _outcomes_loaded = True
    return _outcomes


def _save_outcomes() -> None:
    global _outcomes
    try:
        if len(_outcomes) > _MAX_OUTCOMES:
            _outcomes = _outcomes[-_MAX_OUTCOMES:]
        with open(_OUTCOMES_PATH, "w") as f:
            json.dump(_outcomes, f, indent=2)
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def record_trade_entry(mint: str, pos: dict) -> None:
    """Store entry context. Called when a position opens in _open_position."""
    # We store entry metadata on the pos dict itself so it survives to close.
    # No file I/O needed at entry — the close record will carry this forward.
    pos.setdefault("_learning_entry_ts", time.time())
    pos.setdefault("_learning_entry_mc",  pos.get("mc_usd"))
    pos.setdefault("_learning_entry_liq", pos.get("liq_usd"))
    pos.setdefault("_learning_entry_holders", pos.get("entry_holder_count"))


def record_trade_exit(mint: str, close_record: dict) -> None:
    """Build a TradeOutcome and save it. Called after _close_paper_position."""
    global _trades_since_analysis
    _load_outcomes()

    exit_reason = close_record.get("reason", "")
    pnl_pct     = float(close_record.get("pnl_pct", 0.0))
    pnl_sol     = float(close_record.get("pnl_sol", 0.0))
    peak_pnl    = float(close_record.get("peak_pnl_pct", 0.0))
    hold_mins   = float(close_record.get("hold_mins", 0.0))
    tp1         = bool(close_record.get("tp1_hit", False))
    tp2         = bool(close_record.get("tp2_hit", False))
    locked      = float(close_record.get("locked_sol", 0.0))

    was_winner  = pnl_sol > 0
    was_rug     = hold_mins < 5 and pnl_pct < -30

    # Exit category
    if "take_profit" in exit_reason or tp1:
        exit_cat = "tp_win"
    elif "stop_loss" in exit_reason:
        exit_cat = "sl_loss"
    elif "trail" in exit_reason:
        exit_cat = "trail_win" if pnl_sol > 0 else "trail_loss"
    elif was_rug:
        exit_cat = "rug"
    elif "ai_" in exit_reason:
        exit_cat = "ai_exit"
    else:
        exit_cat = "other"

    outcome = TradeOutcome(
        mint=mint,
        token_name=close_record.get("token_name", mint[:8]),
        wallet=close_record.get("wallet", "?"),
        ts_open=float(close_record.get("ts", time.time())) - hold_mins * 60,
        ts_close=float(close_record.get("ts", time.time())),
        hold_mins=hold_mins,
        entry_price=float(close_record.get("entry_price") or 0.0),
        entry_mc_usd=close_record.get("_entry_mc"),
        entry_liq_usd=close_record.get("_entry_liq"),
        entry_holder_cnt=close_record.get("_entry_holders"),
        narrative=close_record.get("narrative", "unknown"),
        exit_reason=exit_reason,
        pnl_pct=round(pnl_pct, 2),
        pnl_sol=round(pnl_sol, 4),
        peak_pnl_pct=round(peak_pnl, 2),
        tp1_hit=tp1,
        tp2_hit=tp2,
        locked_sol=round(locked, 4),
        was_winner=was_winner,
        was_rug=was_rug,
        exit_category=exit_cat,
        # ── Strategy segregation — so patterns are learned per-strategy ──
        strategy=close_record.get("strategy", "copy_trade"),
        signal_source=close_record.get("signal_source"),
        # ── Entry-timing fingerprint (filled when caller has it) ──
        h1_change_at_entry=close_record.get("h1_change_at_entry"),
        m5_change_at_entry=close_record.get("m5_change_at_entry"),
        age_hours_at_entry=close_record.get("age_hours_at_entry"),
        buy_ratio_at_entry=close_record.get("buy_ratio_at_entry"),
        top1_pct_at_entry=close_record.get("top1_pct_at_entry"),
        top10_pct_at_entry=close_record.get("top10_pct_at_entry"),
        # ── Expanded entry fingerprint (2026-04-22) ──
        h6_change_at_entry=close_record.get("h6_change_at_entry"),
        h24_change_at_entry=close_record.get("h24_change_at_entry"),
        momentum_ratio_at_entry=close_record.get("momentum_ratio_at_entry"),
        mcap_velocity_at_entry=close_record.get("mcap_velocity_at_entry"),
        pct_off_peak_at_entry=close_record.get("pct_off_peak_at_entry"),
        catalyst_active=close_record.get("catalyst_active"),
        catalyst_confidence=close_record.get("catalyst_confidence"),
        moonbag_armed=close_record.get("moonbag_armed"),
        post_tp1_low_pnl_pct=close_record.get("post_tp1_low_pnl_pct"),
        v_recovery_max=close_record.get("v_recovery_max"),
    )

    _outcomes.append(asdict(outcome))
    _save_outcomes()
    _trades_since_analysis += 1

    # Schedule post-exit price checks — persist so 5/30/60-min lookups survive restart
    _load_pending_checks()
    exit_price = close_record.get("exit_price") or close_record.get("entry_price")
    _pending_checks[mint] = {
        "exit_ts":    time.time(),
        "exit_price": exit_price,
        "checks_done": [],
    }
    _save_pending_checks()

    print(
        f"[learning] 📝 Outcome recorded: {outcome.token_name} "
        f"{pnl_pct:+.1f}% ({exit_cat}) | {len(_outcomes)} total outcomes"
    )


def on_monitor_exit_signal(
    mint: str,
    reason: str,
    feed: Any,  # DataFeed — avoid circular import type hint
) -> None:
    """Called by TradeMonitor before it closes a position — annotate pending outcome."""
    # Record the AI exit context so it appears in the learning record
    _pending_checks.setdefault(mint, {})["monitor_exit_reason"] = reason


async def check_post_exit_due(session: aiohttp.ClientSession) -> None:
    """Check DexScreener prices for all pending post-exit checkpoints.

    Call this periodically (e.g. every 5 minutes from a background task).
    """
    _load_pending_checks()
    if not _pending_checks:
        return

    now = time.time()
    completed = []

    for mint, chk in list(_pending_checks.items()):
        exit_ts    = chk.get("exit_ts", 0)
        exit_price = chk.get("exit_price")
        done       = chk.get("checks_done", [])

        if not exit_price or exit_price <= 0:
            completed.append(mint)
            continue

        all_windows_done = True
        for label, secs in _POST_EXIT_WINDOWS:
            if label in done:
                continue
            if now - exit_ts < secs:
                all_windows_done = False
                continue  # not due yet

            # Fetch current price
            current_price = await _fetch_dex_price(mint, session)
            if current_price and current_price > 0:
                chg_pct = (current_price - exit_price) / exit_price * 100.0
                print(
                    f"[learning] 📊 Post-exit {label}: {mint[:8]} "
                    f"{chg_pct:+.1f}% vs our exit ({exit_price:.3e}→{current_price:.3e})"
                )
                # Update the latest outcome record for this mint
                _update_outcome_post_exit(mint, label, chg_pct)
                done.append(label)
            else:
                all_windows_done = False  # price fetch failed, retry next round

        _pending_checks[mint]["checks_done"] = done

        # Only mark complete after 60m window is done
        if all_windows_done and "60m" in done:
            completed.append(mint)

    for mint in completed:
        _pending_checks.pop(mint, None)

    # Persist any mutations (checks_done additions, completed mint removals)
    _save_pending_checks()


def _update_outcome_post_exit(mint: str, label: str, chg_pct: float) -> None:
    """Update the matching outcome record with post-exit price data."""
    _load_outcomes()
    # Find the most recent outcome for this mint
    for record in reversed(_outcomes):
        if record.get("mint") == mint:
            if label == "5m":
                record["price_5m_pct"]  = round(chg_pct, 1)
            elif label == "30m":
                record["price_30m_pct"] = round(chg_pct, 1)
            elif label == "60m":
                record["price_60m_pct"] = round(chg_pct, 1)
                # Mark exit as early if price rose > 20% in 60min after we left
                record["exit_was_early"] = chg_pct > 20.0
            break
    _save_outcomes()


async def _fetch_dex_price(mint: str, session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                return None
            d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None
            pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
            price_str = pairs[0].get("priceNative", "")
            return float(price_str) if price_str else None
    except Exception:
        return None


# ── Pattern analysis via Claude Opus ─────────────────────────────────────────

async def run_pattern_analysis(session: aiohttp.ClientSession) -> str | None:
    """Run AI analysis on accumulated trade outcomes using best available model.

    Tries Claude Opus first, falls back to Groq llama-3.3-70b if Claude is unavailable.
    Extracts actionable patterns (entry filters, exit timing, whale quality).
    Saves to winning_patterns.json for injection into TradeMonitor prompts.
    Returns the analysis text, or None on failure.
    """
    global _last_analysis_ts
    _load_outcomes()

    if len(_outcomes) < 10:
        print("[learning] ℹ️  Not enough outcomes for analysis yet (need 10+)")
        return None

    summary = _build_outcome_summary()
    prompt = f"""You are analysing Solana meme-coin trading results to extract actionable rules
PER STRATEGY. Each strategy (copy_trade, monster_lifecycle, monster_cluster_confirm,
monster_serial_deployer, meteora, pumpswap, raydium) has its OWN entry model and exit
profile — the brains need a distinct lesson for each lane, not a blended ruleset.

TRADE OUTCOME DATA (already bucketed by strategy):
{summary}

For EVERY strategy that has ≥3 trades in the data above, extract AT LEAST ONE rule.
Focus on what the ENTRY FINGERPRINT (h1%/m5%/age_hours at entry) tells you about
winners vs losers inside that specific lane — we are trying to correct entry timing
and hold duration per strategy.

For each rule, prefix it with the strategy tag in ALL CAPS: e.g.
"MONSTER_LIFECYCLE: skip when m5%@entry > +5 — all 3 losses had m5>10"
"COPY_TRADE: Frost setups with h1<+20% won 3/4, above that 1/5"

Also extract:
- cross-strategy rules (apply to every lane) under prefix "GLOBAL:"
- one exit-timing verdict per strategy (were we early / late / right)

Respond with JSON only:
{{
  "patterns": [
    "MONSTER_LIFECYCLE: Rule ...",
    "MONSTER_CLUSTER_CONFIRM: Rule ...",
    "MONSTER_SERIAL_DEPLOYER: Rule ...",
    "COPY_TRADE: Rule ...",
    "GLOBAL: Rule ..."
  ],
  "per_strategy_verdicts": {{
    "copy_trade": "one sentence on what makes copy_trade win/lose",
    "monster_lifecycle": "one sentence — or 'insufficient_data' if <3 trades",
    "monster_cluster_confirm": "...",
    "monster_serial_deployer": "..."
  }},
  "win_drivers": ["...", "..."],
  "loss_drivers": ["...", "..."],
  "exit_timing_verdict": "one sentence — are we exiting early or late overall?",
  "analysis_ts": {time.time()}
}}"""

    # ── Try Claude Opus first ───────────────────────────────────────────────
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    raw = data["content"][0]["text"]
                    return _save_pattern_analysis(raw, source="opus")
                else:
                    text = await r.text()
                    print(f"[learning] Opus unavailable ({r.status}) — falling back to Groq: {text[:100]}")
        except Exception as exc:
            print(f"[learning] Opus error: {exc} — falling back to Groq")

    # ── Fall back to Groq ───────────────────────────────────────────────────
    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        print("[learning] ⚠️  No GROQ_API_KEY either — pattern analysis skipped")
        return None

    try:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                text = await r.text()
                print(f"[learning] Groq API error {r.status}: {text[:200]}")
                return None
            data = await r.json()
            raw = data["choices"][0]["message"]["content"]
            return _save_pattern_analysis(raw, source="groq")
    except Exception as exc:
        print(f"[learning] Groq analysis error: {exc}")
        return None


def _save_pattern_analysis(raw: str, source: str) -> str | None:
    """Parse and save a pattern analysis response. Returns raw text on success.

    Salvage path: if JSON is truncated mid-string (common on token limit), try
    to extract the "patterns" array via regex and save what we can rather than
    losing the entire run.
    """
    global _last_analysis_ts
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        analysis = json.loads(text)
    except Exception as exc:
        print(f"[learning] Pattern parse error: {exc} (raw len={len(raw)}) — attempting salvage")
        import re
        # Extract complete pattern strings (rules end at closing quote + comma/bracket)
        pat_matches = re.findall(r'"([A-Z_]+:[^"\\]+?(?:\\.[^"\\]*)*)"', text)
        patterns = [p for p in pat_matches if len(p) > 30]  # filter tiny fragments
        if not patterns:
            print(f"[learning] salvage found 0 patterns — giving up")
            print(f"[learning] raw head: {raw[:300]}")
            return None
        analysis = {
            "patterns": patterns,
            "_salvaged": True,
            "_parse_error": str(exc),
            "analysis_ts": time.time(),
        }
        print(f"[learning] salvaged {len(patterns)} patterns from truncated response")

    analysis["_source"] = source
    with open(_PATTERNS_PATH, "w") as f:
        json.dump(analysis, f, indent=2)
    _last_analysis_ts = time.time()
    patterns = analysis.get("patterns", [])
    print(
        f"[learning] ✅ Pattern analysis saved ({source}) — "
        f"{len(patterns)} patterns from {len(_outcomes)} trades"
    )
    for p in patterns[:5]:
        print(f"[learning]   • {p[:200]}")
    return raw


def _strategy_bucket(r: dict) -> str:
    """Infer the strategy bucket for an outcome record.

    Newer records carry `strategy` directly; older ones need to be inferred
    from exit_reason / wallet / narrative so historic trades still bucket.
    """
    s = (r.get("strategy") or "").strip()
    if s:
        return s
    er = (r.get("exit_reason") or "").lower()
    wl = (r.get("wallet") or "").lower()
    if wl == "monster":
        src = (r.get("signal_source") or "").strip()
        return f"monster_{src}" if src else "monster"
    if "meteora" in er or wl in ("meteora", "strat_d"):
        return "meteora"
    if "pumpswap" in er or "pump-amm" in er:
        return "pumpswap"
    if "raydium" in er:
        return "raydium"
    return "copy_trade"


def _bucket_stats(trades: list[dict]) -> dict:
    """Compact win-rate / avg-pnl / hold / timing fingerprint for one bucket."""
    if not trades:
        return {}
    wins = [t for t in trades if t.get("was_winner")]
    rugs = [t for t in trades if t.get("was_rug")]
    pnls = [float(t.get("pnl_pct", 0.0)) for t in trades]
    holds = [float(t.get("hold_mins", 0.0)) for t in trades]
    peaks = [float(t.get("peak_pnl_pct", 0.0)) for t in trades]
    h1s   = [t.get("h1_change_at_entry") for t in trades if t.get("h1_change_at_entry") is not None]
    m5s   = [t.get("m5_change_at_entry") for t in trades if t.get("m5_change_at_entry") is not None]
    ages  = [t.get("age_hours_at_entry") for t in trades if t.get("age_hours_at_entry") is not None]
    mrs   = [t.get("momentum_ratio_at_entry") for t in trades if t.get("momentum_ratio_at_entry") is not None]
    vels  = [t.get("mcap_velocity_at_entry") for t in trades if t.get("mcap_velocity_at_entry") is not None]
    t10s  = [t.get("top10_pct_at_entry") for t in trades if t.get("top10_pct_at_entry") is not None]
    peaks_entry = [t.get("pct_off_peak_at_entry") for t in trades if t.get("pct_off_peak_at_entry") is not None]
    cat_hits = [t for t in trades if t.get("catalyst_active") is True]
    cat_conf = [t.get("catalyst_confidence") for t in trades if t.get("catalyst_confidence") is not None]
    moon_armed = [t for t in trades if t.get("moonbag_armed") is True]
    moon_rec = [t.get("v_recovery_max") for t in trades if t.get("v_recovery_max") is not None]
    return {
        "n":          len(trades),
        "wr":         round(len(wins) / len(trades) * 100, 1),
        "avg_pnl":    round(sum(pnls) / len(pnls), 1),
        "avg_hold":   round(sum(holds) / len(holds), 1),
        "avg_peak":   round(sum(peaks) / len(peaks), 1),
        "rugs":       len(rugs),
        "avg_h1_entry":       round(sum(h1s) / len(h1s), 1) if h1s else None,
        "avg_m5_entry":       round(sum(m5s) / len(m5s), 1) if m5s else None,
        "avg_age_hrs":        round(sum(ages) / len(ages), 1) if ages else None,
        "avg_momentum_ratio": round(sum(mrs) / len(mrs), 2) if mrs else None,
        "avg_mcap_velocity":  round(sum(vels) / len(vels), 0) if vels else None,
        "avg_top10_pct":      round(sum(t10s) / len(t10s), 1) if t10s else None,
        "avg_pct_off_peak":   round(sum(peaks_entry) / len(peaks_entry), 2) if peaks_entry else None,
        "catalyst_hit_rate":  round(len(cat_hits) / len(trades) * 100, 1) if trades else 0,
        "avg_catalyst_conf":  round(sum(cat_conf) / len(cat_conf), 1) if cat_conf else None,
        "moonbag_armed_rate": round(len(moon_armed) / len(trades) * 100, 1) if trades else 0,
        "avg_v_recovery_max": round(sum(moon_rec) / len(moon_rec), 2) if moon_rec else None,
    }


def _build_outcome_summary() -> str:
    """Build a compact summary of outcomes for pattern analysis.

    Buckets by STRATEGY so the analyzer can emit per-strategy rules
    (copy_trade vs monster_lifecycle vs monster_cluster_confirm vs monster_serial_deployer).
    Used by both Opus and Groq fallback.
    """
    _load_outcomes()
    recent = _outcomes[-200:]  # last 200 trades

    wins   = [r for r in recent if r.get("was_winner")]
    losses = [r for r in recent if not r.get("was_winner")]
    rugs   = [r for r in recent if r.get("was_rug")]

    win_rate = len(wins) / len(recent) * 100 if recent else 0

    # Exit reason breakdown
    from collections import Counter
    reasons = Counter(r.get("exit_category", "other") for r in recent)

    # Wallet performance
    wallet_perf: dict[str, list[float]] = {}
    for r in recent:
        w = r.get("wallet", "?")
        wallet_perf.setdefault(w, []).append(r.get("pnl_pct", 0.0))

    wallet_summary = {}
    for w, pnls in wallet_perf.items():
        win_pct = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        avg_pnl = sum(pnls) / len(pnls)
        wallet_summary[w] = {"n": len(pnls), "wr": round(win_pct, 1), "avg_pnl": round(avg_pnl, 1)}

    # Strategy bucket breakdown — the core of per-strategy learning
    strategy_buckets: dict[str, list[dict]] = {}
    for r in recent:
        strategy_buckets.setdefault(_strategy_bucket(r), []).append(r)
    strategy_summary = {s: _bucket_stats(ts) for s, ts in strategy_buckets.items()}

    # Post-exit timing quality
    early_exits = [r for r in recent if r.get("exit_was_early")]
    early_pct = len(early_exits) / len(recent) * 100 if recent else 0

    # Average peak vs actual exit
    w30_chg = [r["price_30m_pct"] for r in recent if r.get("price_30m_pct") is not None]
    avg_30m = sum(w30_chg) / len(w30_chg) if w30_chg else None

    lines = [
        f"Total trades: {len(recent)} | Win rate: {win_rate:.1f}%",
        f"Winners: {len(wins)} | Losses: {len(losses)} | Rugs: {len(rugs)}",
        f"Exit categories: {dict(reasons)}",
        f"Wallet performance: {json.dumps(wallet_summary)}",
        f"Exits too early (price up 20%+ after): {early_pct:.0f}% of trades",
    ]
    if avg_30m is not None:
        lines.append(f"Avg price change 30min after our exit: {avg_30m:+.1f}%")

    # Per-strategy headline stats (ranked by trade count)
    if strategy_summary:
        lines.append(f"\nSTRATEGY BUCKETS: {json.dumps(strategy_summary)}")

    # Per-strategy deep dives — the brains need their own lesson per lane
    lines.append("\n=== PER-STRATEGY DEEP DIVE (learn one rule per lane) ===")
    for strategy, trades in sorted(
        strategy_buckets.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if not trades:
            continue
        stats = _bucket_stats(trades)
        wins_b = [t for t in trades if t.get("was_winner")]
        lines.append(
            f"\n--- {strategy.upper()} ({stats['n']} trades, WR={stats['wr']:.0f}%, "
            f"avg_pnl={stats['avg_pnl']:+.1f}%, avg_hold={stats['avg_hold']:.0f}min, "
            f"avg_peak={stats['avg_peak']:+.1f}%, rugs={stats['rugs']}) ---"
        )
        fp_bits = []
        if stats.get("avg_h1_entry") is not None:
            fp_bits.append(f"avg h1%@entry={stats['avg_h1_entry']:+.1f}")
        if stats.get("avg_m5_entry") is not None:
            fp_bits.append(f"avg m5%@entry={stats['avg_m5_entry']:+.1f}")
        if stats.get("avg_age_hrs") is not None:
            fp_bits.append(f"avg age@entry={stats['avg_age_hrs']:.1f}h")
        if stats.get("avg_momentum_ratio") is not None:
            fp_bits.append(f"avg momentum_ratio={stats['avg_momentum_ratio']:.2f}")
        if stats.get("avg_mcap_velocity") is not None:
            fp_bits.append(f"avg mcap_velocity=${stats['avg_mcap_velocity']:,.0f}/min")
        if stats.get("avg_pct_off_peak") is not None:
            fp_bits.append(f"avg pct_off_peak@entry={stats['avg_pct_off_peak']:.2f}")
        if stats.get("avg_top10_pct") is not None:
            fp_bits.append(f"avg top10%@entry={stats['avg_top10_pct']:.1f}%")
        if fp_bits:
            lines.append("  ENTRY FINGERPRINT: " + " | ".join(fp_bits))
        mb_bits = []
        if stats.get("catalyst_hit_rate"):
            mb_bits.append(f"catalyst_hit={stats['catalyst_hit_rate']:.0f}%")
        if stats.get("moonbag_armed_rate"):
            mb_bits.append(f"moonbag_armed={stats['moonbag_armed_rate']:.0f}%")
        if stats.get("avg_v_recovery_max") is not None:
            mb_bits.append(f"avg v_recovery_max={stats['avg_v_recovery_max']:.2f}")
        if mb_bits:
            lines.append("  LIFECYCLE SIGNALS: " + " | ".join(mb_bits))
        if wins_b:
            lines.append(
                f"  Recent wins: {', '.join(t.get('token_name','?')[:10] for t in wins_b[-5:])}"
            )
        # Up to 8 recent samples for this bucket
        for t in trades[-8:]:
            h1 = t.get("h1_change_at_entry")
            m5 = t.get("m5_change_at_entry")
            age = t.get("age_hours_at_entry")
            entry_fp = []
            if h1 is not None: entry_fp.append(f"h1={h1:+.0f}%")
            if m5 is not None: entry_fp.append(f"m5={m5:+.0f}%")
            if age is not None: entry_fp.append(f"age={age:.1f}h")
            lines.append(
                f"    {t.get('token_name','?')[:12]} P&L={t.get('pnl_pct',0):+.0f}% "
                f"hold={t.get('hold_mins',0):.0f}min peak={t.get('peak_pnl_pct',0):+.0f}% "
                f"reason={t.get('exit_reason','?')[:22]}"
                + (f" | {' '.join(entry_fp)}" if entry_fp else "")
            )

    # Sample of recent trades (last 20)
    lines.append("\nRecent 20 trades (newest first):")
    for r in reversed(recent[-20:]):
        lines.append(
            f"  {r.get('token_name','?')[:12]} | {_strategy_bucket(r)} | "
            f"P&L={r.get('pnl_pct',0):+.0f}% | "
            f"hold={r.get('hold_mins',0):.0f}min | "
            f"reason={r.get('exit_reason','?')[:30]} | "
            f"cat={r.get('exit_category','?')}"
        )

    # Scout-rejection performance — lets Opus recommend filter tuning per reason
    try:
        from elizaos.plugins.solana import rejection_tracker as _rt
        rej_block = _rt.get_opus_summary(lookback_hours=14 * 24)
        if rej_block:
            lines.append(rej_block)
    except Exception:
        pass

    return "\n".join(lines)


# ── Bootstrap from copy_trade_paper_trades.json ───────────────────────────────

def bootstrap_from_copy_trades() -> int:
    """Seed learning_outcomes.json from copy_trade_paper_trades.json if sparse.

    Converts the historical paper trades into TradeOutcome records so the
    pattern analysis (run_pattern_analysis) has data to work from immediately.
    Returns the number of outcomes added.
    """
    global _trades_since_analysis
    _load_outcomes()

    if len(_outcomes) >= 20:
        return 0  # already have enough data

    history_path = os.path.join(_DIR, "copy_trade_paper_trades.json")
    if not os.path.exists(history_path):
        return 0

    try:
        with open(history_path) as f:
            trades = json.load(f)
    except Exception:
        return 0

    # Only use closed trades (have exit_price)
    closed = [t for t in trades if t.get("exit_price") and t.get("exit_price", 0) > 0]
    if not closed:
        return 0

    # Get mints already in outcomes to avoid duplicates
    existing_mints_ts = {(r.get("mint"), round(r.get("ts_close", 0))) for r in _outcomes}

    added = 0
    for t in closed:
        ts_close = float(t.get("ts", time.time()))
        mint = t.get("mint", "")
        if (mint, round(ts_close)) in existing_mints_ts:
            continue

        pnl_pct   = float(t.get("pnl_pct", 0.0))
        pnl_sol   = float(t.get("pnl_sol", 0.0))
        peak_pnl  = float(t.get("peak_pnl_pct") or 0.0)
        hold_mins = float(t.get("hold_mins", 0.0))
        exit_reason = t.get("reason", "")
        was_winner = pnl_sol > 0
        was_rug    = hold_mins < 5 and pnl_pct < -30

        if "take_profit" in exit_reason or "tp1" in exit_reason:
            exit_cat = "tp_win"
        elif "stop_loss" in exit_reason:
            exit_cat = "sl_loss"
        elif "trail" in exit_reason:
            exit_cat = "trail_win" if pnl_sol > 0 else "trail_loss"
        elif was_rug:
            exit_cat = "rug"
        else:
            exit_cat = "other"

        outcome = {
            "mint": mint,
            "token_name": t.get("token_name", mint[:8]),
            "wallet": t.get("wallet", "?"),
            "ts_open":  ts_close - hold_mins * 60,
            "ts_close": ts_close,
            "hold_mins": hold_mins,
            "entry_price": float(t.get("entry_price") or 0.0),
            "entry_mc_usd": None,
            "entry_liq_usd": None,
            "entry_holder_cnt": None,
            "narrative": t.get("narrative", "unknown"),
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_sol": round(pnl_sol, 4),
            "peak_pnl_pct": round(peak_pnl, 2),
            "tp1_hit": bool(t.get("tp1_hit", False)),
            "tp2_hit": bool(t.get("tp2_hit", False)),
            "locked_sol": round(float(t.get("locked_sol") or 0.0), 4),
            "price_5m_pct": None,
            "price_30m_pct": None,
            "price_60m_pct": None,
            "exit_was_early": None,
            "monitor_last_action": None,
            "monitor_last_confidence": None,
            "monitor_exit_reason": None,
            "was_winner": was_winner,
            "was_rug": was_rug,
            "exit_category": exit_cat,
        }
        _outcomes.append(outcome)
        existing_mints_ts.add((mint, round(ts_close)))
        added += 1

    if added > 0:
        _save_outcomes()
        _trades_since_analysis += added
        print(f"[learning] 📚 Bootstrapped {added} outcomes from copy_trade_paper_trades.json ({len(_outcomes)} total)")

    return added


# ── Learning Scheduler ────────────────────────────────────────────────────────

async def run_learning_scheduler(session: aiohttp.ClientSession) -> None:
    """Background task: post-exit checks every 5min, Opus analysis every 50 trades."""
    global _trades_since_analysis

    print("[learning] 🧠 Learning scheduler started")
    while True:
        try:
            await asyncio.sleep(300)   # 5-minute tick

            # Post-exit price checks (in-memory — for AI cascade outcomes)
            await check_post_exit_due(session)

            # Scout-rejection outcome checks — fills t+2h follow-up prices on
            # every rejected token so brains see false-positive rate per filter.
            try:
                from elizaos.plugins.solana import rejection_tracker as _rt
                _rt_res = await _rt.check_outcomes(min_age_hours=2.0)
                if _rt_res.get("checked", 0):
                    print(f"[learning] rejection_tracker: {_rt_res.get('checked')} checked, "
                          f"{_rt_res.get('pumped', 0)} pumped / {_rt_res.get('rugged', 0)} rugged")
            except Exception as _rt_err:
                print(f"[learning] rejection_tracker check error: {_rt_err}")

            # File-based post-exit tracker (survives restart) — drain pending checkpoints.
            # Without this call, post_exit_tracker.json accumulated 114 done:false records
            # and the `exit_was_early` signal never populated.
            try:
                from elizaos.plugins.solana.post_exit_tracker import check_due as _pet_check_due
                _pet_res = await _pet_check_due()
                if (_pet_res or {}).get("checkpoints_updated", 0):
                    print(f"[learning] post_exit_tracker: {_pet_res}")
            except Exception as _pet_err:
                print(f"[learning] post_exit_tracker drain error: {_pet_err}")

            # Pattern analysis every 50 trades or once a week (Sunday midnight)
            import datetime as _dt
            _now_dt = _dt.datetime.utcnow()
            _is_sunday_midnight = (
                _now_dt.weekday() == 6 and
                _now_dt.hour == 0 and
                _now_dt.minute < 5 and
                time.time() - _last_analysis_ts > 3600
            )

            if _trades_since_analysis >= _ANALYSIS_EVERY_N or _is_sunday_midnight:
                print(
                    f"[learning] 🔬 Triggering Opus analysis "
                    f"({_trades_since_analysis} trades since last run)"
                )
                await run_pattern_analysis(session)
                _trades_since_analysis = 0

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[learning] Scheduler error: {exc}")


# ── Dynamic prompt getter ─────────────────────────────────────────────────────

_cached_prompt_ts: float = 0.0
_cached_prompt_str: str = ""


def get_dynamic_prompt() -> str:
    """Return learned rules formatted as a prompt injection for Jarvis / AI cascade.

    Cached for 10 minutes to avoid repeated file reads.
    """
    global _cached_prompt_ts, _cached_prompt_str
    if time.time() - _cached_prompt_ts < 600:
        return _cached_prompt_str

    try:
        if os.path.exists(_PATTERNS_PATH):
            with open(_PATTERNS_PATH) as f:
                data = json.load(f)
            patterns = data.get("patterns", [])
            win_drivers  = data.get("win_drivers",  [])
            loss_drivers = data.get("loss_drivers", [])
            timing       = data.get("exit_timing_verdict", "")

            lines = ["\n=== LEARNED TRADING RULES (from Opus analysis) ==="]
            for p in patterns[:8]:
                lines.append(f"• {p}")
            if win_drivers:
                lines.append(f"WIN DRIVERS: {', '.join(win_drivers[:3])}")
            if loss_drivers:
                lines.append(f"LOSS DRIVERS: {', '.join(loss_drivers[:3])}")
            if timing:
                lines.append(f"EXIT TIMING: {timing}")
            _cached_prompt_str = "\n".join(lines)
        else:
            _cached_prompt_str = ""
    except Exception:
        _cached_prompt_str = ""

    _cached_prompt_ts = time.time()
    return _cached_prompt_str
