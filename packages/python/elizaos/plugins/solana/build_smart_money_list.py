"""build_smart_money_list.py — one-shot historical build of our smart-money wallet roster.

Walks `monster_addresses.json`, queries Helius for each mint's early swap
history, extracts first-N-buyer wallets, aggregates appearances across
monsters. Wallets appearing in ≥ THRESHOLD monsters are promoted into
`smart_money_list.json`.

Usage (on AWS):
    HELIUS_API_KEY=<key> python3 build_smart_money_list.py

Idempotent — overwrites the output file. Safe to re-run whenever
monster_addresses.json gains new entries. Rate-limited to respect Helius
Business plan (~50 rps ceiling), so expect ~15-30 min wall clock for
~40 monsters.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import aiohttp

BASE = Path(__file__).parent
MONSTER_FILE = BASE / "monster_addresses.json"
OUTPUT_FILE  = BASE / "smart_money_list.json"

# Wallets appearing in >= this many monsters get promoted to smart-money.
# PDF suggests "3+" as the watchlist threshold. Lowered to 2 for our smaller
# dataset (43 monsters) so we have a meaningful initial roster.
MIN_MONSTER_APPEARANCES = 2

# How many early buyers to consider per monster. PDF uses first 200 in the
# first 30 minutes post-migration.
FIRST_N_BUYERS   = 200
WINDOW_MINUTES   = 30
MAX_PAGES        = 6      # hard cap per monster to bound cost
PAGE_SIZE        = 100
REQ_DELAY_SECS   = 0.3    # ~3 rps — well under Business-plan limit

# Heuristic: exclude well-known system / program / aggregator addresses that
# show up on buyer side of SPL transfers but aren't human wallets.
_SYSTEM_ADDRS: set[str] = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun fee account
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # pump-amm program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter aggregator/router
}


def _helius_key() -> str:
    k = os.environ.get("HELIUS_API_KEY", "")
    if not k:
        sys.exit("HELIUS_API_KEY not set — abort")
    return k


async def _resolve_pool(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Pick the highest-liquidity pump-amm / pumpswap pair for a mint."""
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None
    best = None
    best_liq = 0.0
    for p in (data.get("pairs") or []):
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq > best_liq and p.get("dexId") in ("pumpswap", "pump-amm", "pumpfun"):
            best_liq = liq
            best = p.get("pairAddress")
    return best


async def _fetch_enhanced_txs(
    session: aiohttp.ClientSession,
    address: str,
    key: str,
    before: str | None = None,
) -> list[dict]:
    """One page of Helius enhanced transactions. Returns up to PAGE_SIZE."""
    params = f"?api-key={key}&limit={PAGE_SIZE}"
    if before:
        params += f"&before={before}"
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions{params}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return []
            return await r.json()
    except Exception:
        return []


def _extract_buyers(txs: list[dict], mint: str) -> list[tuple[int, str]]:
    """Return (block_time, buyer_wallet) pairs for txs where mint was bought.

    A "buy" = a token transfer where:
      - mint matches
      - toUserAccount is a non-system wallet
      - tokenAmount > 0
    """
    pairs: list[tuple[int, str]] = []
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        ts = int(tx.get("timestamp") or 0)
        for tt in tx.get("tokenTransfers") or []:
            if tt.get("mint") != mint:
                continue
            buyer = tt.get("toUserAccount")
            if not buyer or buyer in _SYSTEM_ADDRS:
                continue
            try:
                amt = float(tt.get("tokenAmount") or 0)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            pairs.append((ts, buyer))
            break   # one buyer per tx
    return pairs


async def _early_buyers(
    session: aiohttp.ClientSession,
    pool: str,
    mint: str,
    key: str,
) -> list[str]:
    """Walk enhanced-tx pages oldest-to-newest-via-pagination, collect first-N."""
    all_pairs: list[tuple[int, str]] = []
    before = None
    for _ in range(MAX_PAGES):
        await asyncio.sleep(REQ_DELAY_SECS)
        page = await _fetch_enhanced_txs(session, pool, key, before=before)
        if not page:
            break
        all_pairs.extend(_extract_buyers(page, mint))
        if len(page) < PAGE_SIZE:
            break
        before = page[-1].get("signature")
        if not before:
            break

    if not all_pairs:
        return []

    # Helius returns newest-first. Sort by timestamp ascending to get chronological.
    all_pairs.sort(key=lambda p: p[0])
    earliest_ts = all_pairs[0][0]
    cutoff = earliest_ts + WINDOW_MINUTES * 60
    window = [b for (ts, b) in all_pairs if ts <= cutoff]

    # Dedupe preserving first appearance
    seen: set[str] = set()
    ordered: list[str] = []
    for b in window:
        if b not in seen:
            seen.add(b)
            ordered.append(b)
        if len(ordered) >= FIRST_N_BUYERS:
            break
    return ordered


async def main() -> None:
    key = _helius_key()
    data = json.loads(MONSTER_FILE.read_text())
    monsters = data.get("monsters") or []
    valid = [m for m in monsters if m.get("mint")]
    print(f"[smart-money] processing {len(valid)} monster mints")

    wallet_appearances: dict[str, set[str]] = defaultdict(set)
    processed = 0
    failed = 0

    async with aiohttp.ClientSession() as session:
        for m in valid:
            mint = m["mint"]
            name = m.get("symbol") or m.get("name") or mint[:8]
            pool = await _resolve_pool(session, mint)
            if not pool:
                failed += 1
                print(f"[smart-money] {name} ({mint[:8]}) → no pool (skip)")
                continue
            buyers = await _early_buyers(session, pool, mint, key)
            processed += 1
            print(f"[smart-money] {name} ({mint[:8]}) → {len(buyers)} early buyers")
            for w in buyers:
                wallet_appearances[w].add(mint)

    promoted = {
        w: sorted(list(mints))
        for w, mints in wallet_appearances.items()
        if len(mints) >= MIN_MONSTER_APPEARANCES
    }
    ranked = sorted(promoted.items(), key=lambda kv: len(kv[1]), reverse=True)

    output = {
        "generated_utc":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "threshold_appearances":   MIN_MONSTER_APPEARANCES,
        "first_n_buyers_per_token": FIRST_N_BUYERS,
        "window_minutes":          WINDOW_MINUTES,
        "monsters_processed":      processed,
        "monsters_failed":         failed,
        "total_unique_wallets":    len(wallet_appearances),
        "promoted_count":          len(promoted),
        "wallets":                 {w: ms for w, ms in ranked},
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"[smart-money] ✅ wrote {OUTPUT_FILE.name} — "
          f"{len(promoted)} promoted / {len(wallet_appearances)} scanned "
          f"({processed} processed / {failed} failed)")
    if ranked:
        print(f"[smart-money] top 5:")
        for w, mints in ranked[:5]:
            print(f"  {w[:10]}... → {len(mints)} monsters")


if __name__ == "__main__":
    asyncio.run(main())
