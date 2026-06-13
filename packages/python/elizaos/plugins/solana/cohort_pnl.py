"""cohort_pnl.py — Team / Insiders / Snipers cohort analysis with realized PnL.

For a token, reconstructs every trade from the AMM pool's history (Helius
Enhanced API — one address, every swap, including wallets that already fully
exited), then buckets traders into cohorts by how early they bought and
computes each cohort's initial buy, current holdings, and realized SOL profit.

  • Snipers  — bought in the first few seconds of the launch (launch bots)
  • Insiders — bought early (first minutes) but not in the snipe window
  • Team     — the deployer wallet and wallets it funded
  • Public   — everyone else

Realized PnL = net SOL flow per wallet (SOL received from sells − SOL spent on
buys). Positive = they've already extracted profit. It's an honest net-flow
figure, not a claim of precise tax-basis accounting.

Lazy / on-demand: heavier than the cabal scan, so the map loads it separately.
"""

from __future__ import annotations

import os
import time
from typing import Any

import aiohttp

_WSOL = "So11111111111111111111111111111111111111112"
_MAX_PAGES   = 10    # up to 1000 pool swaps — full history for most fresh tokens
_SNIPE_SECS  = 20    # first buy within this of launch = sniper
_INSIDER_SECS = 300  # first buy within 5 min (after snipe window) = insider
_MIN_SOL     = 0.002  # ignore dust SOL legs


def _lamports_to_sol(x: Any) -> float:
    try:
        return float(x) / 1e9
    except Exception:
        return 0.0


async def _get_pool_address(session: aiohttp.ClientSession, mint: str) -> str | None:
    try:
        async with session.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            pairs = await r.json() if r.status == 200 else []
    except Exception:
        return None
    best, best_liq = None, -1.0
    for p in pairs if isinstance(pairs, list) else []:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq > best_liq:
            best_liq, best = liq, p.get("pairAddress")
    return best


async def _fetch_pool_swaps(session: aiohttp.ClientSession, pool: str) -> list[dict]:
    """All swaps on the pool, oldest-inclusive (paginate to the end, capped)."""
    key = os.getenv("HELIUS_API_KEY", "")
    if not key:
        return []
    out: list[dict] = []
    before = ""
    for _ in range(_MAX_PAGES):
        url = (f"https://api.helius.xyz/v0/addresses/{pool}/transactions"
               f"?api-key={key}&type=SWAP&limit=100{before}")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                txs = await r.json() if r.status == 200 else []
        except Exception:
            break
        if not isinstance(txs, list) or not txs:
            break
        out.extend(txs)
        if len(txs) < 100:
            break
        before = f"&before={txs[-1].get('signature', '')}"
    return out


def _wallet_legs(tx: dict, mint: str) -> tuple[str | None, float, float]:
    """Return (trader, token_delta, sol_delta) for `mint` in this swap.

    token_delta > 0 = trader received the token (a BUY); < 0 = sold.
    sol_delta is the trader's net native SOL movement (received − sent).
    """
    trader = tx.get("feePayer")
    if not trader:
        return None, 0.0, 0.0
    tok = 0.0
    for t in tx.get("tokenTransfers") or []:
        if t.get("mint") != mint:
            continue
        amt = float(t.get("tokenAmount") or 0)
        if t.get("toUserAccount") == trader:
            tok += amt
        elif t.get("fromUserAccount") == trader:
            tok -= amt
    sol = 0.0
    for n in tx.get("nativeTransfers") or []:
        amt = _lamports_to_sol(n.get("amount"))
        if n.get("toUserAccount") == trader:
            sol += amt
        elif n.get("fromUserAccount") == trader:
            sol -= amt
    # WSOL legs (wrapped SOL) count as SOL too
    for t in tx.get("tokenTransfers") or []:
        if t.get("mint") != _WSOL:
            continue
        amt = float(t.get("tokenAmount") or 0)
        if t.get("toUserAccount") == trader:
            sol += amt
        elif t.get("fromUserAccount") == trader:
            sol -= amt
    return trader, tok, sol


async def get_cohort_pnl(
    session: aiohttp.ClientSession,
    mint: str,
    created_ts: float = 0.0,
    deployer_addr: str | None = None,
    swaps: list[dict] | None = None,
) -> dict:
    empty = {"cohorts": [], "swaps_analyzed": 0, "partial": False,
             "computed_at": time.time()}
    if swaps is None:
        pool = await _get_pool_address(session, mint)
        if not pool:
            return empty
        swaps = await _fetch_pool_swaps(session, pool)
    if not swaps:
        return empty
    partial = len(swaps) >= _MAX_PAGES * 100

    # Launch reference = the real pool-creation time when known (so sniper/
    # insider windows are anchored correctly even if we only fetched recent
    # swaps), else the earliest swap we saw.
    times = [t.get("timestamp") for t in swaps if t.get("timestamp")]
    launch = created_ts if created_ts and created_ts > 0 else (min(times) if times else 0)

    # Per-wallet aggregation
    w: dict[str, dict] = {}
    for tx in swaps:
        trader, tok, sol = _wallet_legs(tx, mint)
        if not trader or tok == 0:
            continue
        ts = tx.get("timestamp") or 0
        a = w.setdefault(trader, {"bought": 0.0, "sold": 0.0, "sol_spent": 0.0,
                                  "sol_recv": 0.0, "first_buy": None})
        if tok > 0:
            a["bought"]    += tok
            a["sol_spent"] += max(0.0, -sol)
            if a["first_buy"] is None or ts < a["first_buy"]:
                a["first_buy"] = ts
        else:
            a["sold"]     += -tok
            a["sol_recv"] += max(0.0, sol)

    total_bought = sum(a["bought"] for a in w.values()) or 1.0

    def cohort_of(addr: str, a: dict) -> str:
        if deployer_addr and addr == deployer_addr:
            return "Team"
        fb = a["first_buy"]
        if fb is None:
            return "Public"
        dt = fb - launch
        if dt <= _SNIPE_SECS:
            return "Snipers"
        if dt <= _INSIDER_SECS:
            return "Insiders"
        return "Public"

    agg: dict[str, dict] = {}
    for addr, a in w.items():
        c = cohort_of(addr, a)
        held = max(0.0, a["bought"] - a["sold"])
        realized = a["sol_recv"] - a["sol_spent"]
        slot = agg.setdefault(c, {"cohort": c, "wallets": 0, "initial_buy": 0.0,
                                  "current_holdings": 0.0, "realized_sol": 0.0})
        slot["wallets"]          += 1
        slot["initial_buy"]      += a["bought"]
        slot["current_holdings"] += held
        slot["realized_sol"]     += realized

    order = {"Team": 0, "Snipers": 1, "Insiders": 2, "Public": 3}
    cohorts = []
    for c in sorted(agg.values(), key=lambda x: order.get(x["cohort"], 9)):
        if c["cohort"] == "Public":
            continue  # public isn't a risk cohort — omit from the panel
        cohorts.append({
            "cohort":              c["cohort"],
            "wallets":             c["wallets"],
            "initial_buy_pct":     round(c["initial_buy"] / total_bought * 100, 1),
            "current_pct_of_buy":  round(c["current_holdings"] / c["initial_buy"] * 100, 1) if c["initial_buy"] else 0.0,
            "realized_sol":        round(c["realized_sol"], 1),
        })
    return {
        "cohorts":         cohorts,
        "swaps_analyzed":  len(swaps),
        "partial":         partial,
        "computed_at":     time.time(),
    }
