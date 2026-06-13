"""deployer_check.py — Deployer reputation ("historical fingerprinting").

Cabals rotate wallets but deployers leave a paper trail: the same creator
wallet launching token after token that goes to zero. Given a mint, this
module answers: WHO deployed it, HOW MANY tokens have they launched, and
HOW MANY of those are dead.

Creator resolution (all verified on-chain methods, 2026-06-12):
  1. DAS getAsset creators field (non-pump tokens with metadata creators)
  2. pump.fun bonding-curve PDA → creator @ byte 49 (pre-graduation tokens;
     the account is CLOSED at graduation so this only covers on-curve tokens)
  3. pump-amm pool (GPA by base_mint, indexed offsets 43/75) → coin_creator
     @ byte 211 (graduated tokens — pool persists forever)
  4. Fee payer of the mint's earliest tx (page-capped walk, last resort)

Launch history: Helius Enhanced Transactions API on the CREATOR wallet with
type=CREATE — returns parsed PUMP_FUN create events incl. the minted token.
Death check: DexScreener batch lookup — dead = no pair or liquidity < $1k.

Fails open: any error returns {"creator": None, ...} — never blocks a scan.
"""

from __future__ import annotations

import os
import time
from typing import Any

import aiohttp

_PUMP_PROGRAM     = "6EF8rrecthR3Dkzfv2qPbR3vMA3wRkNQNRkE6jBpwGdU"
_PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_WSOL             = "So11111111111111111111111111111111111111112"
_ZERO_PUBKEY      = "11111111111111111111111111111111"

_DEAD_LIQ_USD = 1_000   # liquidity below this = dead (LP drained)
_DEAD_MC_USD  = 15_000  # market cap below this = collapsed (graduation ≈ $69k)
_MAX_SAMPLE   = 25      # DexScreener batch limit is 30 mints per call
_SIG_PAGE_CAP = 6       # max pages when walking to the earliest tx
_CREATE_PAGES = 3       # max Enhanced-API pages for launch history
_DAS_TIMEOUT  = 15.0

# In-memory cache: creator reputations rarely change within a session
_rep_cache: dict[str, tuple[float, dict]] = {}   # creator → (ts, report)
_REP_TTL = 3600.0


def _helius_key() -> str:
    return os.getenv("HELIUS_API_KEY", "")


async def _rpc(session: aiohttp.ClientSession, method: str, params: Any,
               timeout: float = _DAS_TIMEOUT) -> Any:
    key = _helius_key()
    if not key:
        return None
    try:
        async with session.post(
            f"https://mainnet.helius-rpc.com/?api-key={key}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                return (await r.json()).get("result")
    except Exception:
        pass
    return None


async def _curve_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    """creator from the pump.fun bonding-curve PDA (pre-graduation only)."""
    try:
        import base64
        from solders.pubkey import Pubkey
        pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM),
        )
        info = await _rpc(session, "getAccountInfo",
                          [str(pda), {"encoding": "base64", "commitment": "confirmed"}])
        data = ((info or {}).get("value") or {}).get("data")
        if not data:
            return None
        raw = base64.b64decode(data[0] if isinstance(data, list) else data)
        if len(raw) < 81:
            return None
        creator = str(Pubkey(raw[49:81]))
        return None if creator == _ZERO_PUBKEY else creator
    except Exception:
        return None


async def _amm_coin_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    """coin_creator from the pump-amm pool (graduated tokens).

    GPA with memcmp on base_mint (offset 43) + WSOL quote (offset 75) —
    both offsets are index-backed on Helius so this is a single fast call.
    """
    try:
        import base64
        from solders.pubkey import Pubkey
        accounts = await _rpc(session, "getProgramAccounts", [
            _PUMP_AMM_PROGRAM,
            {"encoding": "base64", "filters": [
                {"memcmp": {"offset": 43, "bytes": mint}},
                {"memcmp": {"offset": 75, "bytes": _WSOL}},
            ]},
        ])
        for acc in accounts or []:
            raw = base64.b64decode(acc["account"]["data"][0])
            if len(raw) >= 243:
                creator = str(Pubkey(raw[211:243]))
                if creator != _ZERO_PUBKEY:
                    return creator
    except Exception:
        pass
    return None


async def _earliest_payer(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Fee payer of the mint's earliest tx — last resort, page-capped."""
    before: str | None = None
    oldest_sig: str | None = None
    for _ in range(_SIG_PAGE_CAP):
        params: dict = {"limit": 1000, "commitment": "confirmed"}
        if before:
            params["before"] = before
        sigs = await _rpc(session, "getSignaturesForAddress", [mint, params])
        if not sigs:
            break
        oldest_sig = sigs[-1]["signature"]
        if len(sigs) < 1000:
            break
        before = oldest_sig
    else:
        return None  # history too deep — give up rather than guess wrong

    if not oldest_sig:
        return None
    tx = await _rpc(session, "getTransaction", [
        oldest_sig,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        return keys[0]["pubkey"] if keys else None
    except Exception:
        return None


async def _resolve_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Find the wallet that deployed this token."""
    # 1. DAS metadata creators field (non-pump tokens)
    asset = await _rpc(session, "getAsset", {"id": mint})
    for c in (asset or {}).get("creators") or []:
        if c.get("address"):
            return c["address"]

    if mint.endswith("pump"):
        # 2. Bonding curve (pre-graduation) — account closed after migration
        creator = await _curve_creator(session, mint)
        if creator:
            return creator
        # 3. pump-amm pool coin_creator (graduated)
        creator = await _amm_coin_creator(session, mint)
        if creator:
            return creator

    # 4. Earliest-tx fee payer (capped walk)
    return await _earliest_payer(session, mint)


async def _launch_history(session: aiohttp.ClientSession, creator: str) -> list[str]:
    """All tokens this wallet has created, via Enhanced API CREATE events."""
    key = _helius_key()
    if not key:
        return []
    mints: list[str] = []
    before = ""
    for _ in range(_CREATE_PAGES):
        url = (f"https://api.helius.xyz/v0/addresses/{creator}/transactions"
               f"?api-key={key}&type=CREATE&limit=100{before}")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_DAS_TIMEOUT)) as r:
                if r.status != 200:
                    break
                txs = await r.json()
        except Exception:
            break
        if not isinstance(txs, list) or not txs:
            break
        for t in txs:
            for tt in t.get("tokenTransfers") or []:
                m = tt.get("mint")
                if m and m not in mints:
                    mints.append(m)
        if len(txs) < 100:
            break
        before = f"&before={txs[-1].get('signature', '')}"
    return mints


async def _count_dead(session: aiohttp.ClientSession, mints: list[str]) -> tuple[int, int]:
    """Return (dead, sampled) over up to _MAX_SAMPLE mints via DexScreener batch."""
    sample = mints[:_MAX_SAMPLE]
    if not sample:
        return 0, 0
    try:
        async with session.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{','.join(sample)}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            pairs = await r.json() if r.status == 200 else []
    except Exception:
        return 0, 0

    # Best (highest-liquidity) pair per token, tracking liq + market cap.
    # Pump-amm LP is burned at graduation so liquidity never fully drains —
    # a rugged token keeps a ~$2-5k liquidity floor. Market cap is the honest
    # death signal: graduation is ~$69k MC, so < _DEAD_MC_USD = collapsed.
    best: dict[str, tuple[float, float]] = {}   # mint → (liq, mcap)
    for p in pairs or []:
        m = (p.get("baseToken") or {}).get("address", "")
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        mc  = float(p.get("marketCap") or p.get("fdv") or 0)
        if m and liq > best.get(m, (-1, 0))[0]:
            best[m] = (liq, mc)

    dead = 0
    for m in sample:
        liq, mc = best.get(m, (0.0, 0.0))
        if liq < _DEAD_LIQ_USD or (0 < mc < _DEAD_MC_USD):
            dead += 1
    return dead, len(sample)


def blend_deployer_into_score(result: dict, deployer: dict | None) -> dict:
    """Compute the final cabal score from the token's OWN numbers — no fixed
    floors (a flat 55 made every serial-rugger token score identically).

    score = cluster component + deployer component, capped at 100
      cluster component  = % of traced supply held by coordinated wallets
      deployer component = excess death rate × sample confidence × 75
        - excess death rate: (dead_pct − 40) / 60 — most meme tokens die
          naturally, so only a dead-rate ABOVE the ~40% baseline adds risk
        - sample confidence: min(1, sampled/10) — 5 launches is weaker
          evidence than 25 launches

    Idempotent: recomputes the cluster component from clusters/holders every
    time, so re-blending a cached result never double-counts.
    Risk and is_controlled re-derived at the map's thresholds (35 / 65).
    """
    result["deployer"] = deployer

    clusters = result.get("clusters") or result.get("coordinated_clusters") or []
    holders  = result.get("holders") or []
    total_pct = sum(float(h.get("pct", 0)) for h in holders) if holders else 100.0
    # For a coordinated_exit the dump magnitude (sold_pct) is the real signal —
    # current holdings may be near-zero after they sold. Count whichever is larger.
    def _cluster_weight(c: dict) -> float:
        w = float(c.get("combined_pct", 0))
        if c.get("type") == "coordinated_exit":
            w = max(w, float(c.get("sold_pct", 0)))
        return w
    coord_pct = sum(_cluster_weight(c) for c in clusters)
    base = min(coord_pct / total_pct * 100.0, 100.0) if total_pct > 0 else 0.0

    dep      = deployer or {}
    dead_pct = float(dep.get("dead_pct") or 0)
    sampled  = int(dep.get("sampled") or 0)
    excess     = max(0.0, dead_pct - 40.0) / 60.0
    confidence = min(1.0, sampled / 10.0)
    dep_component = excess * confidence * 75.0

    score = round(min(100.0, base + dep_component), 1)
    result["cabal_score"] = score
    result["risk"] = "HIGH" if score >= 65 else "MEDIUM" if score >= 35 else "CLEAN"
    result["is_controlled"] = score >= 35
    return result


def _verdict(launched: int, dead: int, sampled: int) -> str:
    if launched <= 1:
        return "FIRST_LAUNCH"
    if sampled == 0:
        return "UNKNOWN"
    dead_pct = dead / sampled * 100
    if dead_pct >= 70 and launched >= 3:
        return "SERIAL_RUGGER"
    if dead_pct >= 40:
        return "POOR_TRACK_RECORD"
    return "NORMAL"


async def get_deployer_report(session: aiohttp.ClientSession, mint: str) -> dict:
    """Full deployer reputation for a mint. Fails open to creator=None."""
    empty = {"creator": None, "creator_short": None, "tokens_launched": 0,
             "dead": 0, "sampled": 0, "dead_pct": 0.0, "verdict": "UNKNOWN"}
    try:
        creator = await _resolve_creator(session, mint)
        if not creator:
            return empty

        cached = _rep_cache.get(creator)
        if cached and time.time() - cached[0] < _REP_TTL:
            return cached[1]

        launches = await _launch_history(session, creator)
        if launches:
            launched = len(launches)
            others = [m for m in launches if m != mint]
            dead, sampled = await _count_dead(session, others)
            verdict = _verdict(launched, dead, sampled)
        else:
            # History lookup failed or returned nothing — don't claim
            # "first launch" on evidence we don't have.
            launched, dead, sampled, verdict = 1, 0, 0, "UNKNOWN"

        report = {
            "creator":         creator,
            "creator_short":   creator[:6] + "…" + creator[-4:],
            "tokens_launched": launched,
            "dead":            dead,
            "sampled":         sampled,
            "dead_pct":        round(dead / sampled * 100, 1) if sampled else 0.0,
            "verdict":         verdict,
        }
        _rep_cache[creator] = (time.time(), report)
        return report
    except Exception:
        return empty
