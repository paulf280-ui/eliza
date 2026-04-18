"""Bonding-curve price resolver — fallback when DexScreener has not indexed a token.

Background
──────────
DexScreener typically lags 30-90 seconds behind pump.fun bonding-curve token launches.
During that window, our `_get_price_and_mc()` returns (None, None, None) and the
copy-trade signal handler logs `not_indexed` and skips the trade.

Analysis of `copy_trade_signal_log.json` (1315 events, 12-day window):
  157 of 562 skips (28%) were `not_indexed`.
  102 of those came from Whale_CyaE (75% historical WR).
   24 came from Wallet_PMJA.
   10 came from Wallet_3BLj (the wallet whose ONE filled signal was +174.6%).

These are the highest-alpha signals in the dataset, and we miss them entirely
because of an indexing race condition.

Fix
───
Read the bonding-curve account directly from Helius and compute the price from
the `virtual_sol_reserves / virtual_token_reserves` ratio. This is exactly what
pump.fun's frontend does and what the on-chain swap math uses, so the price is
identical to what a live trade would execute at.

Pump.fun bonding-curve account layout (after 8-byte Anchor discriminator):
    u64  virtual_token_reserves   (offset  8)
    u64  virtual_sol_reserves     (offset 16)
    u64  real_token_reserves      (offset 24)
    u64  real_sol_reserves        (offset 32)
    u64  token_total_supply       (offset 40)
    bool complete                 (offset 48)

The bonding-curve PDA for a mint is:
    seeds = [b"bonding-curve", mint_pubkey]
    program = 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
"""
from __future__ import annotations

import asyncio
import base64
import os
import struct
from typing import Optional

import aiohttp
from solders.pubkey import Pubkey  # type: ignore

# pump.fun bonding-curve program
_PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
_BC_SEED = b"bonding-curve"

# Lamports per SOL
_LAMPORTS = 1_000_000_000
# pump.fun BC tokens have 6 decimals
_TOKEN_DECIMALS = 1_000_000

# Cached SOL/USD price (refreshed every 5 min by external feed)
# Defaults to a conservative middle-of-range value if not available
_SOL_USD_CACHE: dict[str, float | int] = {"price": 150.0, "ts": 0}
_SOL_USD_TTL = 300  # 5 minutes


def _bc_pda_for_mint(mint: str) -> str:
    """Derive the bonding-curve PDA address for a given mint."""
    mint_pk = Pubkey.from_string(mint)
    pda, _ = Pubkey.find_program_address([_BC_SEED, bytes(mint_pk)], _PUMP_PROGRAM)
    return str(pda)


def _decode_bc_account(b64_data: str) -> Optional[tuple[int, int, int, int, int, bool]]:
    """Decode a bonding-curve account's data field into typed reserves.

    Returns (virtual_token_reserves, virtual_sol_reserves, real_token_reserves,
             real_sol_reserves, token_total_supply, complete) or None if the
    layout doesn't match expectations.
    """
    try:
        raw = base64.b64decode(b64_data)
        if len(raw) < 49:
            return None
        # Skip 8-byte Anchor discriminator
        v_tok = struct.unpack_from("<Q", raw, 8)[0]
        v_sol = struct.unpack_from("<Q", raw, 16)[0]
        r_tok = struct.unpack_from("<Q", raw, 24)[0]
        r_sol = struct.unpack_from("<Q", raw, 32)[0]
        supply = struct.unpack_from("<Q", raw, 40)[0]
        complete = raw[48] != 0
        return v_tok, v_sol, r_tok, r_sol, supply, complete
    except Exception:
        return None


async def _fetch_sol_usd(session: aiohttp.ClientSession) -> float:
    """Cached SOL→USD via CoinGecko. Falls back to last known on error."""
    import time as _t
    now = _t.time()
    cached_ts = _SOL_USD_CACHE.get("ts", 0)
    if isinstance(cached_ts, (int, float)) and now - cached_ts < _SOL_USD_TTL:
        return float(_SOL_USD_CACHE["price"])
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                d = await r.json()
                p = float(d.get("solana", {}).get("usd") or 0)
                if 50 < p < 1000:
                    _SOL_USD_CACHE["price"] = p
                    _SOL_USD_CACHE["ts"] = now
                    return p
    except Exception:
        pass
    return float(_SOL_USD_CACHE["price"])


async def get_bc_price_and_mc(
    mint: str,
    session: aiohttp.ClientSession,
    helius_api_key: Optional[str] = None,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Read pump.fun bonding-curve reserves from Helius and compute (price_sol, mc_usd, liq_usd).

    Returns (None, None, None) if the BC account doesn't exist (token isn't a
    pump.fun BC token, or has graduated to PumpSwap), or on RPC failure.

    This is a fallback for `_get_price_and_mc()` when DexScreener has not yet
    indexed a fresh launch. The price returned here is the *exact* price a swap
    would execute at, since it's computed from the same virtual reserves the
    bonding curve uses.
    """
    key = helius_api_key or os.environ.get("HELIUS_API_KEY")
    if not key:
        return None, None, None

    try:
        bc_pda = _bc_pda_for_mint(mint)
    except Exception:
        return None, None, None

    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [bc_pda, {"encoding": "base64", "commitment": "confirmed"}],
    }
    try:
        async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return None, None, None
            j = await r.json()
            value = (j.get("result") or {}).get("value")
            if not value:
                # No BC account → not a pump.fun BC token (already graduated, or different DEX)
                return None, None, None
            data = value.get("data")
            if not data or not isinstance(data, list) or not data[0]:
                return None, None, None
            decoded = _decode_bc_account(data[0])
            if decoded is None:
                return None, None, None
    except Exception:
        return None, None, None

    v_tok, v_sol, r_tok, r_sol, supply, complete = decoded

    if v_tok == 0 or v_sol == 0:
        return None, None, None

    # Price in SOL per token: virtual_sol_reserves / virtual_token_reserves,
    # adjusted for token decimals (6) and SOL decimals (9).
    # price_sol_per_token = (v_sol / 1e9) / (v_tok / 1e6) = v_sol * 1e-3 / v_tok
    price_sol = (v_sol / _LAMPORTS) / (v_tok / _TOKEN_DECIMALS)

    if price_sol <= 0 or price_sol > 0.01:
        # Same sanity ceiling as DexScreener path
        return None, None, None

    sol_usd = await _fetch_sol_usd(session)

    # Total supply is in raw units (6 decimals)
    supply_tokens = supply / _TOKEN_DECIMALS
    mc_usd = supply_tokens * price_sol * sol_usd

    # Real SOL reserves represent actual liquidity backing the curve.
    # Pool USD value = real_sol_reserves * 2 (curve has SOL on one side, tokens on the other,
    # AMM convention is that liquidity = both sides combined ≈ 2 * single side at the peg).
    liq_usd = (r_sol / _LAMPORTS) * sol_usd * 2

    return price_sol, (mc_usd if mc_usd > 0 else None), (liq_usd if liq_usd > 0 else None)


async def get_price_with_fallback(
    mint: str,
    session: aiohttp.ClientSession,
    dex_fn,
    helius_api_key: Optional[str] = None,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """DexScreener-first, BC-reserves-fallback price resolver.

    `dex_fn` is the existing `_get_price_and_mc` callable. If it returns
    (None, None, None) we try the bonding-curve resolver as a fallback.

    Returns (price_sol, mc_usd, liq_usd) or (None, None, None) if both fail.
    """
    price, mc, liq = await dex_fn(mint, session)
    if price is not None:
        return price, mc, liq
    # DexScreener missed it — try BC reserves
    return await get_bc_price_and_mc(mint, session, helius_api_key=helius_api_key)
