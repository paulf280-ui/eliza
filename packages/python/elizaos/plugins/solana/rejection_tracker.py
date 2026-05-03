"""
rejection_tracker.py — Automatic rejection outcome tracking.

Every token the bot REJECTS is logged here with the reason and filter values.
2 hours later (or on demand) we fetch DexScreener to see what it actually did:
  - Pumped → our filter was too tight, evidence to loosen it
  - Rugged → our filter correctly saved us, evidence to keep/tighten it
  - Flat    → inconclusive

This builds a ground-truth evidence base so Jarvis can tune filters using
actual data instead of guesses.

Usage:
  from elizaos.plugins.solana.rejection_tracker import record, check_outcomes

  # At each rejection point in run_traderbot.py:
  record(mint, reason="fill_cap", filter_name="fill_cap_pct",
         filter_value=72.1, threshold=68.0, strategy="a2")

  # In jarvis_command_centre.py (on demand or on a schedule):
  results = await check_outcomes(min_age_hours=2.0)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_STORE_PATH = os.path.join(os.path.dirname(__file__), "rejected_tokens.json")
_MAX_RECORDS = 500     # keep last 500 rejections; older ones pruned on save


# ── In-memory store (loaded from disk on first access) ────────────────────────
_store: list[dict] | None = None


def _load() -> list[dict]:
    global _store
    if _store is not None:
        return _store
    try:
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH) as f:
                data = json.load(f)
            _store = data if isinstance(data, list) else []
        else:
            _store = []
    except Exception:
        _store = []
    return _store


def _save() -> None:
    global _store
    if _store is None:
        return
    try:
        # Prune oldest records to stay under cap
        if len(_store) > _MAX_RECORDS:
            _store = _store[-_MAX_RECORDS:]
        with open(_STORE_PATH, "w") as f:
            json.dump(_store, f, indent=2)
    except Exception:
        pass


def record(
    mint: str,
    reason: str,
    *,
    filter_name: str = "",
    filter_value: float | str | None = None,
    threshold: float | str | None = None,
    strategy: str = "",
    score: int | None = None,
    extra: dict | None = None,
) -> None:
    """Record a rejected token for later outcome tracking.

    Args:
        mint:         full mint address
        reason:       short string e.g. "fill_cap", "holders_low", "unsafe_rugcheck"
        filter_name:  the config key that triggered rejection e.g. "fill_cap_pct"
        filter_value: the actual value that caused rejection e.g. 72.1
        threshold:    the current threshold e.g. 68.0
        strategy:     "a2", "grad", "raydium" etc.
        score:        token quality score at time of rejection
        extra:        any other contextual data (MC, age, etc.)
    """
    store = _load()

    # Dedup: don't record the same mint+reason within 10 minutes
    cutoff = time.time() - 600
    for r in reversed(store):
        if r["mint"] == mint and r["reason"] == reason and r.get("ts", 0) > cutoff:
            return  # already recorded recently

    rec: dict[str, Any] = {
        "mint": mint,
        "reason": reason,
        "ts": time.time(),
        "strategy": strategy,
        "outcome_checked": False,
        "outcome_price_change_pct": None,
        "outcome_checked_at": None,
    }
    if filter_name:
        rec["filter_name"] = filter_name
    if filter_value is not None:
        rec["filter_value"] = filter_value
    if threshold is not None:
        rec["threshold"] = threshold
    if score is not None:
        rec["score"] = score
    if extra:
        rec["extra"] = extra

    store.append(rec)
    _save()


async def check_outcomes(min_age_hours: float = 2.0) -> dict:
    """Fetch DexScreener outcomes for all rejections older than min_age_hours.

    Returns a summary dict with:
        checked:     count of records updated this run
        pumped:      count that went up ≥ 50% after rejection
        rugged:      count that went down ≥ 50% after rejection
        flat:        count that changed < 50% either way
        filter_evidence: {filter_name: {saved_us: N, missed_pump: N}}
        records:     list of updated records (sorted by outcome)
    """
    import aiohttp

    store = _load()
    cutoff = time.time() - min_age_hours * 3600

    to_check = [r for r in store if not r["outcome_checked"] and r["ts"] < cutoff]
    if not to_check:
        return {"checked": 0, "pumped": 0, "rugged": 0, "flat": 0, "records": []}

    # Batch DexScreener lookups — max 30 per request (DexScreener allows comma-separated mints)
    BATCH = 30
    mint_to_idx: dict[str, int] = {}
    for i, r in enumerate(to_check):
        mint_to_idx[r["mint"]] = store.index(r)

    outcomes: dict[str, float | None] = {}  # mint → price_change_pct (or None)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
        mints = list(mint_to_idx.keys())
        for batch_start in range(0, len(mints), BATCH):
            batch = mints[batch_start: batch_start + BATCH]
            mint_csv = ",".join(batch)
            try:
                async with sess.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint_csv}"
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    pairs = data.get("pairs") or []
                    # Group pairs by mint, pick highest-liquidity pair for each
                    seen: dict[str, dict] = {}
                    for p in pairs:
                        base = (p.get("baseToken") or {}).get("address", "")
                        if not base:
                            continue
                        existing = seen.get(base)
                        cur_liq = float((p.get("liquidity") or {}).get("usd") or 0)
                        ex_liq = float((existing.get("liquidity") or {}).get("usd") or 0) if existing else 0
                        if not existing or cur_liq > ex_liq:
                            seen[base] = p
                    for base, pair in seen.items():
                        # Use the age of the pair vs time of our rejection
                        pc = (pair.get("priceChange") or {})
                        # Use h6 as best proxy for "what happened after ~2h"
                        chg = pc.get("h6") or pc.get("h1") or pc.get("h24")
                        if chg is not None:
                            outcomes[base] = float(chg)
                        else:
                            outcomes[base] = None
            except Exception:
                pass

    # Update records
    pumped = rugged = flat = unknown = 0
    filter_evidence: dict[str, dict] = {}
    updated_records: list[dict] = []

    for rec in to_check:
        mint = rec["mint"]
        chg = outcomes.get(mint)
        rec["outcome_checked"] = True
        rec["outcome_checked_at"] = time.time()
        rec["outcome_price_change_pct"] = chg

        if chg is None:
            rec["outcome_verdict"] = "delisted_or_unknown"
            unknown += 1
        elif chg >= 50:
            rec["outcome_verdict"] = "pumped"
            pumped += 1
        elif chg <= -50:
            rec["outcome_verdict"] = "rugged"
            rugged += 1
        else:
            rec["outcome_verdict"] = "flat"
            flat += 1

        fn = rec.get("filter_name", rec.get("reason", "unknown"))
        if fn not in filter_evidence:
            filter_evidence[fn] = {"saved_us": 0, "missed_pump": 0, "total": 0}
        filter_evidence[fn]["total"] += 1
        if chg is not None and chg >= 50:
            filter_evidence[fn]["missed_pump"] += 1
        elif chg is None or chg <= -50:
            filter_evidence[fn]["saved_us"] += 1

        updated_records.append(dict(rec))

    _save()

    # Sort: missed pumps first (most interesting), then rugs, then flat
    updated_records.sort(
        key=lambda r: (
            0 if r.get("outcome_verdict") == "pumped" else
            1 if r.get("outcome_verdict") == "rugged" else
            2 if r.get("outcome_verdict") == "flat" else 3
        )
    )

    return {
        "checked": len(to_check),
        "pumped": pumped,
        "rugged": rugged,
        "flat": flat,
        "unknown": unknown,
        "filter_evidence": filter_evidence,
        "records": updated_records,
    }


def get_recent(n: int = 50, with_outcomes_only: bool = False) -> list[dict]:
    """Return the last n rejection records (newest first)."""
    store = _load()
    recs = [r for r in store if not with_outcomes_only or r.get("outcome_checked")]
    return list(reversed(recs[-n:]))


def get_stats() -> dict:
    """Return summary stats about tracked rejections."""
    store = _load()
    total = len(store)
    checked = sum(1 for r in store if r.get("outcome_checked"))
    pumped = sum(1 for r in store if r.get("outcome_verdict") == "pumped")
    rugged = sum(1 for r in store if r.get("outcome_verdict") == "rugged")

    from collections import Counter
    reason_counts = Counter(r.get("reason", "?") for r in store)

    return {
        "total_tracked": total,
        "outcomes_checked": checked,
        "pumped_after_reject": pumped,
        "rugged_after_reject": rugged,
        "top_rejection_reasons": dict(reason_counts.most_common(8)),
    }


def get_brain_summary(lookback_hours: float = 48.0) -> str:
    """Compact summary for injection into Groq/Gemini/Claude prompts.

    Teaches the brains where each scout's filter is false-positive-heavy, so
    their SELL calls can be weighed against 'would the scout have missed a +50%
    move here?'. Brains don't override scout decisions but they can ease up
    on panic SELLs when rejection stats show the filter is balanced/tight.
    """
    store = _load()
    cutoff = time.time() - lookback_hours * 3600
    recent = [r for r in store if r.get("outcome_checked") and r.get("ts", 0) >= cutoff]
    if not recent:
        return ""

    from collections import defaultdict
    by_scout: dict[str, list[dict]] = defaultdict(list)
    for r in recent:
        by_scout[r.get("strategy") or "unknown"].append(r)

    lines = [f"SCOUT REJECTION STATS (last {len(recent)} checked, {int(lookback_hours)}h):"]
    for scout, items in sorted(by_scout.items()):
        n = len(items)
        pumped = sum(1 for i in items if i.get("outcome_verdict") == "pumped")
        rugged = sum(1 for i in items if i.get("outcome_verdict") == "rugged")
        pump_pct = pumped / n * 100 if n else 0
        verdict = (
            "TIGHT — saving us"       if pump_pct < 10 else
            "balanced"                 if pump_pct < 20 else
            "OVER-REJECTING — review"  if pump_pct < 35 else
            "STRONGLY over-rejecting"
        )
        lines.append(
            f"  - {scout}: {n} rejected, {pumped} ({pump_pct:.0f}%) pumped ≥+50%, "
            f"{rugged} rugged → {verdict}"
        )
    return "\n".join(lines)


def get_opus_summary(lookback_hours: float = 14 * 24) -> str:
    """Detailed per-(scout, reason) breakdown for the Opus pattern analyzer.

    Surfaces which specific filter reasons are missing the most pumps so
    Opus can recommend precise tuning (e.g. 'lifecycle: m5_hot reason rejects
    30 setups, 35% pumped ≥+50% — relax m5 cap from +5 to +15').
    """
    store = _load()
    cutoff = time.time() - lookback_hours * 3600
    recent = [r for r in store if r.get("outcome_checked") and r.get("ts", 0) >= cutoff]
    if not recent:
        return ""

    from collections import defaultdict
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in recent:
        buckets[(r.get("strategy") or "unknown", r.get("reason", "?"))].append(r)

    lines = [f"\n=== SCOUT REJECTION PERFORMANCE ({int(lookback_hours)}h) ===",
             "Per scout|reason — false-negative rate (% of rejects that pumped ≥+50%):"]
    for (scout, reason), items in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
        n = len(items)
        if n < 3:
            continue
        pumped = sum(1 for i in items if i.get("outcome_verdict") == "pumped")
        pump_pct = pumped / n * 100 if n else 0
        # Typical threshold sample
        thresholds = [i.get("threshold") for i in items if i.get("threshold") is not None]
        values = [i.get("filter_value") for i in items if i.get("filter_value") is not None]
        extra = ""
        if thresholds and values and all(isinstance(v, (int, float)) for v in values):
            avg_val = sum(values) / len(values)
            extra = f" avg_val={avg_val:.1f} thresh={thresholds[0]}"
        lines.append(f"  {scout} | {reason}: n={n} fn_rate={pump_pct:.0f}%{extra}")
    return "\n".join(lines)
