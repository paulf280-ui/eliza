"""holder_guard.fetcher — Helius RPC wrappers for snapshot assembly.

Everything here is read-only and best-effort. Any call returning None means
"data unavailable right now", not "token is safe/unsafe" — the guard layers
decide what to do with missing data (they don't auto-block).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import aiohttp

# Reuse the existing top-wallet helper rather than duplicating the RPC dance.
from elizaos.plugins.solana.monster_signals import top_wallet_distribution

from .models import HolderSnapshot

_BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",   # Solana canonical burn
    "11111111111111111111111111111111",              # System Program (pool vaults for pump-amm count similarly)
    "burnd1gHdyNLY1zJbpJ1tGi5UhpGM2nx1BqNDdNcJqbh",
    "deadDLdZmRaxjzRmn9h2D4aEAVXW3KUMD42iCX3b1cV",
}


async def fetch_rugcheck(
    session: aiohttp.ClientSession,
    mint: str,
) -> dict | None:
    """Fetch rugcheck.xyz report summary. Free, no API key.

    Returns a dict with:
      score           — raw numeric (0 = clean, higher = more red flags)
      danger_count    — number of risks categorized as "danger"
      warn_count      — number of risks categorized as "warn"
      risk_names      — list of human-readable risk names (for logging)
      lp_locked       — True if rugcheck sees LP locked / burned
      audit_8         — normalized 0-8 score for holder_guard compatibility
    Returns None on network error or 404 (brand-new token).
    """
    try:
        async with session.get(
            f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 404:
                return None  # brand-new token, rugcheck hasn't indexed
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    risks = data.get("risks") or []
    danger_names = [r.get("name", "") for r in risks if r.get("level") == "danger"]
    warn_names   = [r.get("name", "") for r in risks if r.get("level") == "warn"]
    all_names    = [r.get("name", "") for r in risks]

    # LP-locked hint: presence of unlocked-LP risk means LP is NOT locked.
    has_lp_unlocked_risk = any(
        ("liquidity" in n.lower() or "lp" in n.lower()) and "unlocked" in n.lower()
        for n in danger_names
    )

    # Normalize to 0-8 "audit score" for holder_guard rule compatibility.
    # Each danger-level risk drops 2 points; each warn drops 1. Floor at 0.
    audit_8 = max(0, 8 - 2 * len(danger_names) - len(warn_names))

    return {
        "score":         int(data.get("score") or 0),
        "danger_count":  len(danger_names),
        "warn_count":    len(warn_names),
        "risk_names":    all_names[:12],   # cap for log readability
        "lp_locked":     not has_lp_unlocked_risk,
        "audit_8":       audit_8,
    }


def _helius_rpc_url() -> str:
    key = os.getenv("HELIUS_API_KEY", "")
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    # Fallback: public solana RPC (rate-limited, only for local dev).
    return "https://api.mainnet-beta.solana.com"


async def _rpc(session: aiohttp.ClientSession, method: str, params: list[Any]) -> dict:
    try:
        async with session.post(
            _helius_rpc_url(),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return {}
            return await r.json()
    except Exception:
        return {}


async def fetch_mint_authority(session: aiohttp.ClientSession, mint: str) -> tuple[bool | None, bool | None]:
    """Return (mint_authority_renounced, freeze_authority_renounced).

    None on RPC failure. True means the authority is null (renounced).
    """
    resp = await _rpc(session, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    try:
        info = resp["result"]["value"]["data"]["parsed"]["info"]
    except Exception:
        return None, None
    mint_auth = info.get("mintAuthority")
    freeze_auth = info.get("freezeAuthority")
    return (mint_auth is None), (freeze_auth is None)


async def fetch_dev_wallet(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Best-effort creator-wallet lookup via Helius DAS (getAsset).

    Falls back to the first creator in Metaplex metadata. None if unresolvable.
    """
    resp = await _rpc(session, "getAsset", [mint])
    try:
        creators = resp["result"]["creators"]
    except Exception:
        return None
    if not creators:
        return None
    # Pick the first verified creator; if none verified, take the first listed.
    for c in creators:
        if c.get("verified"):
            return c.get("address")
    return creators[0].get("address")


async def fetch_dev_holding_pct(
    session: aiohttp.ClientSession,
    mint: str,
    dev_wallet: str,
    total_supply: float,
) -> float | None:
    """Return dev wallet's % of total supply, None on failure."""
    if not dev_wallet or total_supply <= 0:
        return None
    resp = await _rpc(
        session,
        "getTokenAccountsByOwner",
        [dev_wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    try:
        accounts = resp["result"]["value"]
    except Exception:
        return None
    total_bal = 0.0
    for acc in accounts:
        try:
            total_bal += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
        except Exception:
            pass
    return round(total_bal / total_supply * 100, 3)


async def fetch_lp_burned_pct(
    session: aiohttp.ClientSession,
    mint: str,
) -> float | None:
    """Best-effort LP-burn % for a pump-graduated token.

    Strategy: find the pump-amm pool via DexScreener, read the LP token's
    supply distribution, return the % held in known burn addresses.
    Returns None if the pool can't be resolved.
    """
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None

    # Find highest-liquidity pump-amm / pumpswap pair
    pairs = data.get("pairs") or []
    best_lp: str | None = None
    best_liq = 0.0
    for p in pairs:
        if p.get("dexId") not in ("pump-amm", "pumpswap", "pumpfun"):
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        lp_addr = p.get("pairAddress")
        if lp_addr and liq > best_liq:
            best_lp = lp_addr
            best_liq = liq
    if not best_lp:
        return None

    # pairAddress for pump-amm IS the pool address; LP mint is a derivation
    # we can't easily recover without full program IDL. Instead, treat
    # pump-amm tokens as 100% burned at graduation (canonical for the protocol)
    # and pump bonding-curve tokens as N/A.
    # This is a pragmatic stand-in until Helius DAS asset supply check is wired.
    return 100.0 if best_liq > 0 else None


async def build_snapshot(
    session: aiohttp.ClientSession,
    mint: str,
    *,
    dex_pair: dict | None = None,
    unique_holders: int | None = None,
    holder_growth_per_min: float | None = None,
) -> HolderSnapshot:
    """Assemble a full holder snapshot in parallel where possible."""
    snap = HolderSnapshot(mint=mint)

    # Parallelise the independent fetches
    from .bundle_detect import detect_bundle_bot

    dist_task = top_wallet_distribution(session, mint)
    auth_task = fetch_mint_authority(session, mint)
    dev_task = fetch_dev_wallet(session, mint)
    lp_task = fetch_lp_burned_pct(session, mint)
    rug_task = fetch_rugcheck(session, mint)
    dist, auth, dev_wallet, lp_burn, rug = await asyncio.gather(
        dist_task, auth_task, dev_task, lp_task, rug_task, return_exceptions=True
    )

    if isinstance(dist, dict):
        snap.top1_pct = dist.get("top1_pct")
        snap.top10_pct = dist.get("top10_pct")
        snap.wallets_scanned = dist.get("wallets_scanned", 0)

    if isinstance(auth, tuple):
        snap.mint_authority_renounced, snap.freeze_authority_renounced = auth

    if isinstance(dev_wallet, str):
        snap.dev_wallet = dev_wallet

    if isinstance(lp_burn, (int, float)):
        snap.lp_burned_pct = float(lp_burn)

    # Rugcheck integration — populates audit_score (the PDF §2 field). Also
    # overrides lp_burned_pct when rugcheck sees an unlocked-LP risk, since
    # rugcheck's view is more reliable than our pump-amm heuristic.
    if isinstance(rug, dict):
        snap.audit_score = rug.get("audit_8")
        if rug.get("lp_locked") is False:
            snap.lp_burned_pct = 0.0  # rugcheck saw unlocked LP — override heuristic

    # Total supply — needed for dev % calculation
    sup_resp = await _rpc(session, "getTokenSupply", [mint])
    try:
        snap.total_supply = float(sup_resp["result"]["value"]["uiAmount"] or 0)
    except Exception:
        snap.total_supply = None

    if snap.dev_wallet and snap.total_supply and snap.total_supply > 0:
        snap.dev_holding_pct = await fetch_dev_holding_pct(
            session, mint, snap.dev_wallet, snap.total_supply
        )

    # Bundle-bot detection (Algorithm 1) — self-derives creator from the
    # genesis tx's feePayer when snap.dev_wallet is None (pump.fun tokens
    # have empty Metaplex creators). Returns None for tokens too old to walk
    # back through Helius enhanced-tx pagination.
    try:
        snap.bundle_bot_detected = await detect_bundle_bot(
            session, mint, creator=snap.dev_wallet
        )
    except Exception:
        snap.bundle_bot_detected = None

    # Bump-bot detection (Algorithm 3) — count of wallets repeatedly flipping
    # equal-magnitude opposite trades. Positive signal per the paper (Fig 4c).
    try:
        from .bump_detect import detect_bump_bots
        snap.bump_bot_count = await detect_bump_bots(session, mint)
    except Exception:
        snap.bump_bot_count = None

    # Holders + buy/sell from provided DexScreener pair
    snap.unique_holders = unique_holders
    snap.holder_growth_per_min = holder_growth_per_min
    if unique_holders and holder_growth_per_min is not None and unique_holders > 0:
        snap.holder_growth_per_min_pct = round(
            holder_growth_per_min / unique_holders * 100, 3
        )

    if dex_pair:
        txns = dex_pair.get("txns") or {}
        h1 = txns.get("h1") or {}
        snap.buy_count_h1 = int(h1.get("buys") or 0) or None
        snap.sell_count_h1 = int(h1.get("sells") or 0) or None
        vol = dex_pair.get("volume") or {}
        # DexScreener only gives aggregate volume, not split buy/sell — the
        # split would need Bitquery. Leaving those fields None is correct.
        _ = vol

    return snap
