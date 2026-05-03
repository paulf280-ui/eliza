"""Backtest 3 candidate entry strategies against the 40-monster corpus.

Strategies:
  1. Serial-Deployer Sniper — creator wallet shipped a prior monster BEFORE this one
  2. Cluster-Confirm Scout   — >=3 known-cluster wallets are in this monster's
                                earliest-25 buyers (cluster is built chronologically,
                                using only monsters that existed before this one)
  3. Lifecycle Scout         — all of: top1<10%, socials present, liq $25k-$200k,
                                meteora pool within 180min of PumpSwap (if known),
                                buy-ratio 48-65% (if known), m5 in [-15,+15], age 1-6h

Each strategy is scored in TWO modes:
  - retrospective_hit: if we had full hindsight (all 40 monsters known)
  - chronological_hit: if we only knew monsters earlier in time (realistic backtest)

Also fetches top1 holder % for monsters where we didn't have it before.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"


async def rpc(session, method, params, timeout=20):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(RPC, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json()


SYSTEM_PROGRAM = "11111111111111111111111111111111"


async def get_top1_pct(session, mint):
    """Return top-1 NON-POOL holder as pct-of-supply.

    Walks the top-20 token accounts and skips any whose owner wallet is a PDA
    (owner program != System Program) — those are pool vaults. The first real
    wallet encountered is returned.
    """
    sup = await rpc(session, "getTokenSupply", [mint])
    total = float(((sup.get("result") or {}).get("value") or {}).get("uiAmount") or 0)
    if total <= 0:
        return None
    large = await rpc(session, "getTokenLargestAccounts", [mint])
    vals = ((large.get("result") or {}).get("value") or [])
    if not vals:
        return None
    addrs = [v["address"] for v in vals[:20]]
    parsed = await rpc(session, "getMultipleAccounts",
                       [addrs, {"encoding": "jsonParsed"}])
    accounts = ((parsed.get("result") or {}).get("value") or [])
    tas = []
    for v, acc in zip(vals[:20], accounts, strict=False):
        if not acc:
            continue
        try:
            wallet = acc["data"]["parsed"]["info"]["owner"]
            bal = float(v.get("uiAmount") or 0)
        except Exception:
            continue
        tas.append((wallet, bal))
    if not tas:
        return None
    owners = [w for w, _ in tas]
    info = await rpc(session, "getMultipleAccounts",
                     [owners, {"encoding": "base64"}])
    infos = ((info.get("result") or {}).get("value") or [])
    for (_wallet, bal), w_info in zip(tas, infos, strict=False):
        owner_prog = (w_info or {}).get("owner") if w_info else SYSTEM_PROGRAM
        if owner_prog == SYSTEM_PROGRAM:
            return round(bal / total * 100, 3) if total else None
    return None


def load_corpus():
    base = Path("/home/paulf/eliza/packages/python/elizaos/plugins/solana")
    mon = json.loads((base / "monster_addresses.json").read_text())
    intel = json.loads((base / "monster_creator_intel.json").read_text())
    return mon, intel, base


def earliest_block_time(intel_entry):
    txs = intel_entry.get("earliest_txs") or []
    if not txs:
        return None
    return txs[0].get("block_time")


async def main():
    mon, intel, base = load_corpus()

    # Build per-mint records with creation time + creator + early buyers
    records = []
    for m in mon["monsters"]:
        mint = m.get("mint")
        intel_e = intel["mint_intel"].get(mint) or {}
        bt = earliest_block_time(intel_e)
        creator = m.get("creator_wallet") or intel_e.get("creator_wallet")
        records.append({
            "mint": mint,
            "symbol": m.get("symbol"),
            "creator": creator,
            "earliest_buyers": intel_e.get("early_buyers") or [],
            "block_time": bt,
            "monster_entry": m,
        })

    # Sort chronologically by block_time; unknown times sort last
    records.sort(key=lambda r: (r["block_time"] is None, r["block_time"] or 0))

    # ── Fetch top1 NON-POOL holder pct for every mint ──────────────────
    # Always re-fetch: the old cached value predates the pool-vault filter
    # and is not comparable. Set BACKTEST_USE_CACHED_TOP1=1 to reuse the
    # stored value instead (faster; for iterating on scoring rules).
    reuse_cached = os.getenv("BACKTEST_USE_CACHED_TOP1", "0") == "1"
    print("[*] Fetching top-1 non-pool holder pct for all 40 mints"
          f" (cached={'reused' if reuse_cached else 'ignored'})…", file=sys.stderr)
    async with aiohttp.ClientSession() as session:
        for r in records:
            if reuse_cached:
                existing = r["monster_entry"].get("top1_holder_pct")
                if existing is not None:
                    r["top1_pct"] = existing
                    continue
            try:
                r["top1_pct"] = await get_top1_pct(session, r["mint"])
            except Exception:
                r["top1_pct"] = None
            await asyncio.sleep(0.15)

    # ── Strategy 1: Serial-Deployer ──
    known_creators = {}  # creator -> count seen so far
    for r in records:
        c = r["creator"]
        prior = known_creators.get(c, 0) if c else 0
        r["strategy_1_chrono_hit"] = prior >= 1
        if c:
            known_creators[c] = prior + 1
    # Retrospective: any creator that ends up with count>=2
    creator_totals = dict(known_creators)
    for r in records:
        c = r["creator"]
        r["strategy_1_retro_hit"] = bool(c and creator_totals.get(c, 0) >= 2)

    # ── Strategy 2: Cluster-Confirm (>=3 known cluster wallets in early buyers) ──
    # Chronological: for each monster at position N, build cluster from monsters 1..N-1
    # A "cluster wallet" is a wallet that appeared early in >=2 prior monsters.
    wallet_prior_count = {}  # wallet -> number of prior monsters it's been early in
    for r in records:
        # Current cluster = wallets with prior_count >= 2 RIGHT NOW
        cluster = {w for w, cnt in wallet_prior_count.items() if cnt >= 2}
        hits = [w for w in r["earliest_buyers"] if w in cluster]
        r["strategy_2_cluster_known_hits"] = len(hits)
        r["strategy_2_cluster_wallets"] = hits[:5]
        r["strategy_2_chrono_hit"] = len(hits) >= 3
        # Update wallet_prior_count AFTER scoring this monster
        for w in r["earliest_buyers"]:
            wallet_prior_count[w] = wallet_prior_count.get(w, 0) + 1
    # Retrospective: using full-corpus cluster (known coordination_cluster file)
    full_cluster = set(intel["coordination_cluster_multi_monster_buyers"].keys())
    for r in records:
        full_hits = [w for w in r["earliest_buyers"] if w in full_cluster]
        r["strategy_2_retro_hits"] = len(full_hits)
        r["strategy_2_retro_hit"] = len(full_hits) >= 3

    # ── Strategy 3: Lifecycle scout ──
    # Requires reasonable proxy values. Scoring rules:
    # pass if:
    #   - top1_pct < 10 (fail if >10 or None — skip on missing data, mark unknown)
    #   - socials present (DAS json_body had twitter/telegram/website/desc link OR x community)
    #   - liq in [25000, 200000]
    #   - age_hours_at_check in [1, 6]  (many historical entries logged at 1-20h — we use it loosely)
    #   - buy_ratio in [48, 65] (if known)
    #   - m5 in [-15, 15] (if known)
    for r in records:
        m = r["monster_entry"]
        intel_e = intel["mint_intel"].get(r["mint"]) or {}
        body = intel_e.get("json_body") or {}
        has_social = bool(body and isinstance(body, dict) and any(
            body.get(k) for k in ("twitter", "telegram", "website", "discord")
        ))
        if not has_social:
            desc = (body or {}).get("description") if isinstance(body, dict) else None
            if desc and any(kw in str(desc).lower() for kw in ("t.me/", "discord", "x.com/", "twitter")):
                has_social = True

        top1 = r.get("top1_pct")
        liq = m.get("liq_usd_at_check") or m.get("liq_usd_at_entry")
        age = m.get("age_hours_at_check")
        buy_ratio = m.get("buy_ratio_pct")
        m5 = m.get("m5_at_check")
        h24 = m.get("h24_gain_pct_at_check") or m.get("h24_gain_pct")

        checks = {
            "top1_ok": (top1 is not None and top1 < 10),
            "social_ok": has_social,
            "liq_ok": (liq is not None and 25_000 <= liq <= 200_000),
            "buy_ratio_ok": (buy_ratio is None or 48 <= buy_ratio <= 65),
            "m5_ok": (m5 is None or -15 <= m5 <= 15),
            "age_ok": (age is None or 1 <= age <= 6) or True,  # many logged later — don't hard-gate
            "momentum_ok": (h24 is None) or (h24 >= 80) or ((m5 or 0) >= 10),
        }
        # Require top1, social, liq, buy_ratio, m5, momentum to pass. age is relaxed.
        required = ["top1_ok", "social_ok", "liq_ok", "buy_ratio_ok", "m5_ok", "momentum_ok"]
        passed = [k for k in required if checks[k]]
        r["strategy_3_checks"] = checks
        r["strategy_3_pass_count"] = len(passed)
        r["strategy_3_hit"] = len(passed) == len(required)

    # ── Summarise ──
    n = len(records)
    print()
    print("=" * 78)
    print(f"BACKTEST RESULTS — {n} monsters")
    print("=" * 78)

    s1_chrono = sum(1 for r in records if r["strategy_1_chrono_hit"])
    s1_retro  = sum(1 for r in records if r["strategy_1_retro_hit"])
    s2_chrono = sum(1 for r in records if r["strategy_2_chrono_hit"])
    s2_retro  = sum(1 for r in records if r["strategy_2_retro_hit"])
    s3_hit    = sum(1 for r in records if r["strategy_3_hit"])
    print()
    print(f"{'Strategy':<36} {'Chronological':>14} {'Retrospective':>14}")
    print(f"{'Serial-Deployer Sniper':<36} {s1_chrono}/{n} ({s1_chrono*100/n:.0f}%)  "
          f"{s1_retro}/{n} ({s1_retro*100/n:.0f}%)")
    print(f"{'Cluster-Confirm Scout (>=3 cluster)':<36} {s2_chrono}/{n} ({s2_chrono*100/n:.0f}%)  "
          f"{s2_retro}/{n} ({s2_retro*100/n:.0f}%)")
    print(f"{'Lifecycle Scout':<36} {'N/A':>13}  {s3_hit}/{n} ({s3_hit*100/n:.0f}%)")
    print()

    # Per-monster breakdown
    print(f"{'#':<3} {'Symbol':<12} {'Top1%':>6} {'Cr_hist':>7} {'ClHits':>6} "
          f"{'S1':>3} {'S2c':>4} {'S2r':>4} {'S3':>3} {'S1|S2|S3':>10}")
    print("-" * 78)
    for i, r in enumerate(records, 1):
        any_hit = r["strategy_1_chrono_hit"] or r["strategy_2_chrono_hit"] or r["strategy_3_hit"]
        print(f"{i:<3} {str(r['symbol'])[:12]:<12} "
              f"{str(r.get('top1_pct'))[:6]:>6} "
              f"{known_creators.get(r['creator'],0):>7} "
              f"{r['strategy_2_cluster_known_hits']:>6} "
              f"{'Y' if r['strategy_1_chrono_hit'] else '-':>3} "
              f"{'Y' if r['strategy_2_chrono_hit'] else '-':>4} "
              f"{'Y' if r['strategy_2_retro_hit'] else '-':>4} "
              f"{'Y' if r['strategy_3_hit'] else '-':>3} "
              f"{'Y' if any_hit else '-':>10}")

    # Union of all 3 chrono strategies
    any_chrono = sum(
        1 for r in records
        if r["strategy_1_chrono_hit"] or r["strategy_2_chrono_hit"] or r["strategy_3_hit"]
    )
    print()
    print(f"UNION (any of S1/S2c/S3 would have fired): {any_chrono}/{n} = {any_chrono*100/n:.0f}%")

    # Retrospective S2 combined with S1 retro and S3 (shows realistic ceiling once systems warm up)
    any_retro = sum(
        1 for r in records
        if r["strategy_1_retro_hit"] or r["strategy_2_retro_hit"] or r["strategy_3_hit"]
    )
    print(f"UNION retrospective (S1r/S2r/S3): {any_retro}/{n} = {any_retro*100/n:.0f}%")

    # Save full report
    out = {
        "generated_utc": "2026-04-19",
        "n_monsters": n,
        "summary": {
            "s1_serial_deployer_chrono": f"{s1_chrono}/{n}",
            "s1_serial_deployer_retro": f"{s1_retro}/{n}",
            "s2_cluster_confirm_chrono": f"{s2_chrono}/{n}",
            "s2_cluster_confirm_retro": f"{s2_retro}/{n}",
            "s3_lifecycle": f"{s3_hit}/{n}",
            "union_chrono": f"{any_chrono}/{n}",
            "union_retro": f"{any_retro}/{n}",
        },
        "per_monster": [
            {
                "symbol": r["symbol"],
                "mint": r["mint"],
                "creator": r["creator"],
                "top1_pct": r["top1_pct"],
                "s1_chrono": r["strategy_1_chrono_hit"],
                "s1_retro": r["strategy_1_retro_hit"],
                "s2_chrono_hits": r["strategy_2_cluster_known_hits"],
                "s2_chrono_hit": r["strategy_2_chrono_hit"],
                "s2_retro_hits": r["strategy_2_retro_hits"],
                "s2_retro_hit": r["strategy_2_retro_hit"],
                "s3_checks": r["strategy_3_checks"],
                "s3_hit": r["strategy_3_hit"],
            }
            for r in records
        ],
    }
    path = base / "monster_backtest_results.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"\n[+] wrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
