"""Score the 40-monster corpus against the PDF's quantitative framework.

Tests each PDF rule against existing data in monster_addresses.json.
Reports hit rate, outliers, and rules that contradict our prior analysis.
"""

from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
DATA = json.loads((BASE / "monster_addresses.json").read_text())
MONSTERS = [m for m in DATA["monsters"] if m.get("mint")]

# ── Helpers ───────────────────────────────────────────────────────────
def get_buy_ratio(m: dict) -> float | None:
    for k in ("buy_ratio_h1_pct", "buy_ratio_pct", "buy_ratio_h24_pct"):
        if k in m and m[k] is not None:
            return float(m[k])
    return None

def get_vol_mcap_ratio(m: dict) -> float | None:
    vl = m.get("vol_liq_ratio") or m.get("vol_liq_ratio_at_check")
    liq = m.get("liq_usd_at_check")
    mc = m.get("mc_usd_at_check") or m.get("fdv_at_check")
    if vl and liq and mc and mc > 0:
        return (vl * liq) / mc
    return None

def get_liq_mc_ratio(m: dict) -> float | None:
    if m.get("liq_mc_ratio"):
        return float(m["liq_mc_ratio"])
    liq = m.get("liq_usd_at_check")
    mc = m.get("mc_usd_at_check") or m.get("fdv_at_check")
    if liq and mc and mc > 0:
        return liq / mc
    return None

def get_multiplier(m: dict) -> float:
    x = m.get("approx_multiplier")
    if isinstance(x, (int, float)):
        return float(x)
    return 0.0

# ── Tier classification ───────────────────────────────────────────────
def tier(m: dict) -> str:
    """Classify by peak gain to compare rule-hit rate across tiers."""
    g = m.get("h24_gain_pct") or m.get("h24_gain_pct_apr4") or 0
    mult = get_multiplier(m)
    score = max(g, mult * 100)
    if score >= 1000: return "MEGA (>10x)"
    if score >= 200:  return "MONSTER (2-10x)"
    return "MODEST (<2x)"

buckets: dict[str, list[dict]] = defaultdict(list)
for m in MONSTERS:
    buckets[tier(m)].append(m)

# ── Rule 1: Buy ratio 48-65% zone (PDF: 1.1-1.3:1 = 52-56%) ──────────
def check_buy_zone(m):
    br = get_buy_ratio(m)
    return br is not None and 48 <= br <= 65

# ── Rule 2: Vol/mcap > 3x (PDF Phase 3 hold rule) ────────────────────
def check_vol_mcap(m):
    r = get_vol_mcap_ratio(m)
    return r is not None and r >= 3.0

# ── Rule 3: Liq/mcap 5-20% (PDF sweet spot) ─────────────────────────
def check_liq_mc(m):
    r = get_liq_mc_ratio(m)
    return r is not None and 0.05 <= r <= 0.20

# ── Rule 4: Top1 holder < 10% (our prior finding) ───────────────────
def check_top1(m):
    t1 = m.get("top1_holder_pct")
    if t1 is None: return None  # unknown
    return t1 < 10.0

# ── Rule 5: Has socials (PDF: narrative determines ceiling) ────────
def check_socials(m):
    return bool(
        m.get("has_socials")
        or m.get("twitter_url")
        or m.get("x_community_url")
        or m.get("kol_creator_tweet")
    )

# ── Rule 6: Age at check < 48h (PDF: first 30min decisive) ─────────
def check_age(m):
    a = m.get("age_hours_at_check")
    return a is not None and a < 48

# ── Rule 7: Rugcheck clean (< 500) ──────────────────────────────────
def check_rugcheck(m):
    r = m.get("rugcheck_score")
    return r is None or r < 500

# ── Serial-deployer detection ──────────────────────────────────────
creators = defaultdict(list)
for m in MONSTERS:
    if m.get("creator_wallet"):
        creators[m["creator_wallet"]].append(m["symbol"])
serial_creators = {c: syms for c, syms in creators.items() if len(syms) >= 2}

def check_serial_creator(m):
    return m.get("creator_wallet") in serial_creators

# ── Run ──────────────────────────────────────────────────────────────
RULES = [
    ("Buy ratio in 48-65% zone",      check_buy_zone),
    ("Vol/mcap >= 3x",                 check_vol_mcap),
    ("Liq/mcap in 5-20% zone",         check_liq_mc),
    ("Top1 holder < 10%",              check_top1),
    ("Has socials/narrative anchor",   check_socials),
    ("Age at check < 48h",             check_age),
    ("Rugcheck score < 500",           check_rugcheck),
    ("Serial creator (>=2 monsters)",  check_serial_creator),
]

print(f"\n{'='*70}")
print(f"PDF FRAMEWORK VALIDATION — {len(MONSTERS)} confirmed-mint monsters")
print(f"{'='*70}\n")

for bucket_name in ["MEGA (>10x)", "MONSTER (2-10x)", "MODEST (<2x)"]:
    tokens = buckets[bucket_name]
    if not tokens: continue
    print(f"\n── {bucket_name}  (n={len(tokens)}) ───────────────────")
    for rule_name, fn in RULES:
        results = [fn(m) for m in tokens]
        passed = sum(1 for r in results if r is True)
        unknown = sum(1 for r in results if r is None)
        testable = len(results) - unknown
        rate = (passed / testable * 100) if testable > 0 else 0
        print(f"  {rule_name:36s} {passed:2d}/{testable:2d}  ({rate:5.1f}%)  unknown={unknown}")

# ── Full score per token ────────────────────────────────────────────
print(f"\n{'='*70}")
print("PER-TOKEN SCORE (n rules passed out of 8)")
print(f"{'='*70}\n")

rows = []
for m in MONSTERS:
    scores = []
    for _, fn in RULES:
        r = fn(m)
        scores.append(1 if r is True else 0)
    total = sum(scores)
    rows.append((tier(m), m["symbol"], total, scores, m))

rows.sort(key=lambda r: (r[0], -r[2]))
for t, sym, total, scores, m in rows:
    mult = get_multiplier(m)
    gain = m.get("h24_gain_pct") or m.get("h24_gain_pct_apr4") or 0
    flags = "".join("✓" if s else "·" for s in scores)
    print(f"  {t:18s} {sym:12s} score={total}/8  [{flags}]  peak_gain={gain:>6.0f}%  mult={mult:.1f}x")

# ── Outliers: which rules do MEGA tokens MISS? ────────────────────
print(f"\n{'='*70}")
print("WHERE MEGA TOKENS VIOLATE A RULE (mismatches worth noting)")
print(f"{'='*70}\n")

for m in buckets["MEGA (>10x)"]:
    fails = []
    for rule_name, fn in RULES:
        r = fn(m)
        if r is False:
            fails.append(rule_name)
    if fails:
        print(f"  {m['symbol']:12s} fails: {', '.join(fails)}")

# ── Serial creators summary ────────────────────────────────────────
print(f"\n{'='*70}")
print("SERIAL-DEPLOYER CREATORS (>=2 monsters from same wallet)")
print(f"{'='*70}\n")
for c, syms in sorted(serial_creators.items(), key=lambda x: -len(x[1])):
    print(f"  {c}  → {', '.join(syms)}")

print()
