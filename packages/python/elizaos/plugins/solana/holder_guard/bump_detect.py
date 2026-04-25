"""holder_guard.bump_detect — bump-bot wallet count for a mint.

Algorithm 3 from Luo et al. 2026. A bump bot is a wallet that repeatedly
buys and sells identical quantities of a meme coin (a "flip"), inflating
visibility without taking a real net position. The paper's metric is

    α = F / (ΔP + ε)

where F = number of consecutive opposite-sign equal-magnitude trades and
ΔP = absolute net position change. Wallets with α ≥ ξ (default 50) are
classified as bump bots.

Counter-intuitive finding: per Fig 4c of the paper, the *presence* of bump
bots correlates with HIGHER token returns. Bump bots are attention
manipulation, and attention attracts real buyers. We therefore surface
this as a metadata signal (positive confirmation) — never a hard-block
rule.

Returns:
  int  — number of bump-bot wallets detected
  None — couldn't determine (RPC failure or insufficient data)
"""
from __future__ import annotations

import os
import time

import aiohttp

PAGE_SIZE = 100               # Helius enhanced /v0 max
MAX_PAGES = 10                # 1000 most recent txs is enough to characterise a wallet's flip pattern
XI = 50.0                     # paper default threshold for the bump-bot ratio
EPS = 1.0                     # paper default to avoid div-by-zero
MIN_FLIPS_PER_WALLET = 4      # below this, the ratio is unstable — skip
TIMEOUT_SECS = 8

# Known program/system addresses to exclude from "wallet" candidates.
_SYSTEM_ADDRS: set[str] = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump.fun fee account
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # pump-amm program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter aggregator/router
}

# 5-min per-mint cache.
_CACHE_TTL_SECS = 300
_cache: dict[str, tuple[float, int | None]] = {}


async def _fetch_page(session: aiohttp.ClientSession, mint: str, before: str | None) -> list[dict] | None:
    key = os.getenv("HELIUS_API_KEY", "")
    if not key:
        return None
    url = f"https://api.helius.xyz/v0/addresses/{mint}/transactions?api-key={key}&limit={PAGE_SIZE}"
    if before:
        url += f"&before={before}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECS)) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data if isinstance(data, list) else []
    except Exception:
        return None


def _is_flip(a: float, b: float) -> bool:
    """Consecutive trades a, b form a flip iff opposite signs and equal magnitude.

    Uses relative tolerance for magnitude comparison since token amounts can
    be very large (millions or billions of units).
    """
    if a == 0 or b == 0:
        return False
    if (a > 0) == (b > 0):
        return False  # same sign — not a flip
    mag_a, mag_b = abs(a), abs(b)
    return abs(mag_a - mag_b) / max(mag_a, mag_b) < 0.001


async def detect_bump_bots(session: aiohttp.ClientSession, mint: str) -> int | None:
    """Algorithm 3: count wallets with bump-bot flip pattern on this mint."""
    cached = _cache.get(mint)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECS:
        return cached[1]

    # ── Pull recent transactions ─────────────────────────────────────────
    txs: list[dict] = []
    before: str | None = None
    for _ in range(MAX_PAGES):
        page = await _fetch_page(session, mint, before)
        if page is None:
            _cache[mint] = (time.time(), None)
            return None
        if not page:
            break
        txs.extend(page)
        if len(page) < PAGE_SIZE:
            break
        before = page[-1].get("signature")
        if not before:
            break

    if not txs:
        _cache[mint] = (time.time(), None)
        return None

    # ── Build per-wallet signed-quantity trade sequences ─────────────────
    # wallet → list[(timestamp, signed_quantity_in_tokens)]
    trades: dict[str, list[tuple[int, float]]] = {}
    for tx in txs:
        ts = int(tx.get("timestamp") or 0)
        for tt in tx.get("tokenTransfers") or []:
            if tt.get("mint") != mint:
                continue
            try:
                amt = float(tt.get("tokenAmount") or 0)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            buyer  = tt.get("toUserAccount")
            seller = tt.get("fromUserAccount")
            if buyer and buyer not in _SYSTEM_ADDRS:
                trades.setdefault(buyer, []).append((ts, +amt))
            if seller and seller not in _SYSTEM_ADDRS:
                trades.setdefault(seller, []).append((ts, -amt))

    # ── Apply Algorithm 3 per wallet ─────────────────────────────────────
    bump_count = 0
    for wallet, seq in trades.items():
        if len(seq) < MIN_FLIPS_PER_WALLET * 2:
            continue
        seq.sort(key=lambda t: t[0])
        flips = 0
        net = 0.0
        for i in range(len(seq) - 1):
            net += seq[i][1]
            if _is_flip(seq[i][1], seq[i + 1][1]):
                flips += 1
        net = abs(net)
        if flips < MIN_FLIPS_PER_WALLET:
            continue
        ratio = flips / (net + EPS)
        if ratio >= XI:
            bump_count += 1

    _cache[mint] = (time.time(), bump_count)
    return bump_count
