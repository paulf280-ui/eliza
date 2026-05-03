"""Post-graduation pattern analysis.

Filters out bonding-curve chatter and old-token revivals. Focuses on the
window the user actually trades: PumpSwap/Raydium/Meteora graduates within
their first 48h, captured while the monster was still forming.

Outputs a tight signature summary + a ranked list by peak gain.
"""

from __future__ import annotations
import json
from pathlib import Path
from statistics import median

BASE = Path(__file__).parent
DATA = json.loads((BASE / "monster_addresses.json").read_text())
ALL = [m for m in DATA["monsters"] if m.get("mint")]


def peak_gain(m: dict) -> float:
    mult = m.get("approx_multiplier")
    if isinstance(mult, (int, float)) and mult > 0:
        return float(mult) * 100.0
    return float(m.get("h24_gain_pct") or m.get("h24_gain_pct_apr4") or 0)


def age_at_check_h(m: dict) -> float | None:
    a = m.get("age_hours_at_check")
    return float(a) if a is not None else None


def buy_ratio(m: dict) -> float | None:
    for k in ("buy_ratio_h1_pct", "buy_ratio_pct", "buy_ratio_h24_pct"):
        if m.get(k) is not None:
            return float(m[k])
    return None


def has_social(m: dict) -> bool:
    return bool(
        m.get("has_socials") or m.get("twitter_url") or m.get("x_community_url")
        or m.get("telegram_url") or m.get("website_url") or m.get("kol_creator_tweet")
    )


# ── Tag each token ────────────────────────────────────────────────────
FRESH = []       # <= 72h at check — the window we actually trade
REVIVAL = []     # > 7 days old — out of scope
MIDAGE = []      # 3-7 days — fringe
UNKNOWN = []

for m in ALL:
    a = age_at_check_h(m)
    if a is None:
        UNKNOWN.append(m)
    elif a <= 72:
        FRESH.append(m)
    elif a <= 168:
        MIDAGE.append(m)
    else:
        REVIVAL.append(m)

print(f"Total confirmed: {len(ALL)}")
print(f"  FRESH (<72h post-graduation): {len(FRESH)}")
print(f"  MIDAGE (3-7d):                {len(MIDAGE)}")
print(f"  REVIVAL (>7d — out of scope): {len(REVIVAL)}")
print(f"  UNKNOWN age:                   {len(UNKNOWN)}")

# ── Rank FRESH graduates by peak gain ────────────────────────────────
FRESH.sort(key=lambda m: -peak_gain(m))

print(f"\n{'='*92}")
print("FRESH GRADUATES (<72h old at check) — ranked by peak multiplier")
print(f"{'='*92}")
print(f"{'Symbol':<12} {'DEX':<10} {'Mult':>6} {'Age(h)':>6} "
      f"{'Liq$':>9} {'MC$':>10} {'Liq/MC':>6} {'Top1%':>6} "
      f"{'BuyR%':>6} {'VolLiq':>7} {'Social':>6} {'Serial':>6}")
print("-" * 92)

# Serial-creator set
from collections import defaultdict
creators = defaultdict(list)
for m in ALL:
    if m.get("creator_wallet"):
        creators[m["creator_wallet"]].append(m["symbol"])
serial = {c for c, ss in creators.items() if len(ss) >= 2}

def fmt(x, w=6):
    if x is None: return " ?".rjust(w)
    if isinstance(x, float):
        if abs(x) >= 1000: return f"{x/1000:.1f}k".rjust(w)
        return f"{x:.1f}".rjust(w)
    return str(x).rjust(w)

for m in FRESH:
    sym = m["symbol"][:11]
    dex = m.get("dex", "?")[:9]
    mult = peak_gain(m) / 100
    age = age_at_check_h(m) or 0
    liq = m.get("liq_usd_at_check") or 0
    mc = m.get("mc_usd_at_check") or m.get("fdv_at_check") or 0
    liq_mc = (liq / mc * 100) if mc > 0 else None
    t1 = m.get("top1_holder_pct")
    br = buy_ratio(m)
    vl = m.get("vol_liq_ratio") or m.get("vol_liq_ratio_at_check") or m.get("vol_liq_ratio_at_entry_est")
    soc = "Y" if has_social(m) else "-"
    ser = "Y" if m.get("creator_wallet") in serial else "-"

    print(f"{sym:<12} {dex:<10} {mult:>5.0f}x {age:>5.1f}h "
          f"{fmt(liq,9):>9} {fmt(mc,10):>10} "
          f"{fmt(liq_mc,6):>6} {fmt(t1,6):>6} "
          f"{fmt(br,6):>6} {fmt(vl,7):>7} {soc:>6} {ser:>6}")

# ── Split FRESH into mega / modest for pattern compare ────────────────
MEGA_FRESH = [m for m in FRESH if peak_gain(m) >= 1000]  # 10x+
MOD_FRESH = [m for m in FRESH if peak_gain(m) < 1000]

def summarize(label, bucket):
    if not bucket:
        print(f"\n  {label}: (empty)")
        return
    liqs = [m.get("liq_usd_at_check") or 0 for m in bucket if m.get("liq_usd_at_check")]
    mcs = [m.get("mc_usd_at_check") or m.get("fdv_at_check") or 0 for m in bucket
           if (m.get("mc_usd_at_check") or m.get("fdv_at_check"))]
    brs = [buy_ratio(m) for m in bucket if buy_ratio(m) is not None]
    t1s = [m["top1_holder_pct"] for m in bucket if m.get("top1_holder_pct") is not None]
    ages = [age_at_check_h(m) for m in bucket if age_at_check_h(m) is not None]
    liq_mcs = [(m.get("liq_usd_at_check") or 0) / (m.get("mc_usd_at_check") or m.get("fdv_at_check") or 1) * 100
               for m in bucket if (m.get("liq_usd_at_check") and (m.get("mc_usd_at_check") or m.get("fdv_at_check")))]
    n_social = sum(1 for m in bucket if has_social(m))
    n_serial = sum(1 for m in bucket if m.get("creator_wallet") in serial)

    print(f"\n  {label} (n={len(bucket)})")
    if liqs:    print(f"    liquidity USD     median ${median(liqs):>9,.0f}   range ${min(liqs):>6,.0f}–${max(liqs):>9,.0f}")
    if mcs:     print(f"    mcap USD          median ${median(mcs):>9,.0f}   range ${min(mcs):>6,.0f}–${max(mcs):>9,.0f}")
    if liq_mcs: print(f"    liq/mcap %        median  {median(liq_mcs):>9.1f}%   range  {min(liq_mcs):>5.1f}%–{max(liq_mcs):>5.1f}%")
    if brs:     print(f"    buy ratio %       median  {median(brs):>9.1f}%   range  {min(brs):>5.1f}%–{max(brs):>5.1f}%")
    if t1s:     print(f"    top1 holder %     median  {median(t1s):>9.2f}%   range  {min(t1s):>5.2f}%–{max(t1s):>5.2f}%   (n={len(t1s)})")
    if ages:    print(f"    age at check (h)  median  {median(ages):>9.1f}    range  {min(ages):>5.1f}–{max(ages):>5.1f}")
    print(f"    has socials       {n_social}/{len(bucket)}")
    print(f"    serial creator    {n_serial}/{len(bucket)}")

    # DEX split
    dex_count = defaultdict(int)
    for m in bucket:
        dex_count[m.get("dex", "?")] += 1
    dex_pretty = ", ".join(f"{d}:{n}" for d, n in sorted(dex_count.items(), key=lambda x: -x[1]))
    print(f"    DEX split         {dex_pretty}")


print(f"\n{'='*92}")
print("FRESH GRADUATE SIGNATURE — MEGA vs MODEST")
print(f"{'='*92}")
summarize("MEGA (>=10x peak)", MEGA_FRESH)
summarize("MODEST (<10x peak)", MOD_FRESH)

# ── Winning signature intersection ────────────────────────────────────
print(f"\n{'='*92}")
print("MEGA FRESH GRADUATES — which signals fire together?")
print(f"{'='*92}")
for m in MEGA_FRESH:
    liq = m.get("liq_usd_at_check") or 0
    mc = m.get("mc_usd_at_check") or m.get("fdv_at_check") or 0
    liq_mc = (liq/mc*100) if mc else 0
    t1 = m.get("top1_holder_pct")
    br = buy_ratio(m) or 0
    soc = has_social(m)
    ser = m.get("creator_wallet") in serial
    a = age_at_check_h(m) or 0

    flags = []
    if 30_000 <= liq <= 400_000: flags.append("LIQ✓")
    if 5 <= liq_mc <= 25: flags.append("L/MC✓")
    if t1 is not None and t1 < 10: flags.append("T1✓")
    if 48 <= br <= 65: flags.append("BUY✓")
    if soc: flags.append("SOC✓")
    if ser: flags.append("SER✓")
    if a < 24: flags.append("<24h")
    elif a < 72: flags.append("<72h")

    print(f"  {m['symbol']:<12} {m.get('dex','?'):<10} peak={peak_gain(m)/100:>5.0f}x   {' '.join(flags)}")
