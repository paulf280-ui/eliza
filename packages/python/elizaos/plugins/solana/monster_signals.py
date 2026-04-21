"""Monster strategy entry-signal scouts.

Three independent loops that feed `strategy_e_monster.open_monster_position`:

  1. cluster_confirm_scout_loop   — when ≥3 cluster wallets buy same fresh mint ≤10min
  2. serial_deployer_sniper_loop  — when any of 4 whitelisted creators mints a new token
  3. lifecycle_scout_loop         — fresh PumpSwap+Meteora monster lifecycle shape

All three share a single slot pool on the monster side (`strategy_e_monster.
can_open_new_position`) so first signal to fire wins when capital is constrained.

Safety:
- Each loop respects its own feature flag (env var) and only runs when
  `MONSTER_STRATEGY_ENABLED=true` at the top level.
- Each scout logs a "detected" record to `monster_signal_log.json` even if
  the actual entry is skipped (e.g. slot pool full, safety check failed) so
  we can audit miss rate in paper trading.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp

from . import strategy_e_monster as monster

BASE = Path(__file__).parent
SIGNAL_LOG_FILE = BASE / "monster_signal_log.json"
WHITELIST_FILE  = BASE / "monster_creator_whitelist.json"

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM   = "11111111111111111111111111111111"

# Mints we must never attempt to buy as a monster position — stablecoins / WSOL / wrapped.
# DexScreener and on-chain scans will occasionally return pairs where the stable is
# tagged as "baseToken"; we filter here before any open_monster_position call.
_MONSTER_SKIP_MINTS: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # WSOL
    "USD1ttGY1N17NEEH4tLSfBJBkmBQZxSBRAZrMPAbDLDp",  # USD1
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",  # stSOL
}

# ─── Feature flags ──────────────────────────────────────────────────────
def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")

CLUSTER_CONFIRM_ENABLED  = _env_on("MONSTER_CLUSTER_CONFIRM_ENABLED", "true")
SERIAL_DEPLOYER_ENABLED  = _env_on("MONSTER_SERIAL_DEPLOYER_ENABLED", "true")
LIFECYCLE_SCOUT_ENABLED  = _env_on("MONSTER_LIFECYCLE_ENABLED", "true")
BREAKOUT_SCOUT_ENABLED   = _env_on("MONSTER_BREAKOUT_ENABLED", "true")

# ─── Cadences ───────────────────────────────────────────────────────────
CLUSTER_POLL_SECS         = 45     # poll cluster wallets every 45s
CLUSTER_WINDOW_SECS       = 10 * 60  # 10min confirmation window
CLUSTER_MIN_DISTINCT      = 3      # ≥3 cluster wallets on same mint

SERIAL_POLL_SECS          = 20     # poll deployer wallets every 20s — fast lane
SERIAL_GRAD_WAIT_SECS     = 60 * 60  # wait up to 1h for graduation
SERIAL_GRAD_POLL_SECS     = 30     # re-check graduation every 30s

LIFECYCLE_POLL_SECS       = 120    # DexScreener lifecycle scan every 2min

SIGNAL_DEDUP_WINDOW_SECS  = 60 * 60 * 12  # don't re-signal the same mint within 12h

# ─── Whitelist loader ───────────────────────────────────────────────────
def load_whitelist() -> dict:
    if not WHITELIST_FILE.exists():
        return {"serial_monster_deployers": [], "coordination_cluster_buyers": []}
    try:
        return json.loads(WHITELIST_FILE.read_text())
    except Exception:
        return {"serial_monster_deployers": [], "coordination_cluster_buyers": []}


def cluster_wallets() -> set[str]:
    wl = load_whitelist()
    return {e["wallet"] for e in wl.get("coordination_cluster_buyers", []) if e.get("wallet")}


def serial_deployer_wallets() -> set[str]:
    wl = load_whitelist()
    return {e["wallet"] for e in wl.get("serial_monster_deployers", []) if e.get("wallet")}


# ─── Signal-log persistence ─────────────────────────────────────────────
def _log_signal(entry: dict) -> None:
    log: list = []
    if SIGNAL_LOG_FILE.exists():
        try:
            log = json.loads(SIGNAL_LOG_FILE.read_text())
        except Exception:
            log = []
    log.append({"ts": time.time(), **entry})
    # Keep the last 500 events to bound file size.
    SIGNAL_LOG_FILE.write_text(json.dumps(log[-500:], indent=2))


_recent_signalled: dict[str, float] = {}
def _recently_signalled(mint: str) -> bool:
    ts = _recent_signalled.get(mint)
    return bool(ts and (time.time() - ts) < SIGNAL_DEDUP_WINDOW_SECS)


def _mark_signalled(mint: str) -> None:
    _recent_signalled[mint] = time.time()


# ─── Helius helpers ─────────────────────────────────────────────────────
async def _rpc(session: aiohttp.ClientSession, method: str, params: Any, timeout: int = 20) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(HELIUS_RPC, json=payload,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json()


async def top1_wallet_pct(session: aiohttp.ClientSession, mint: str) -> float | None:
    """Return top-1 NON-POOL holder pct-of-supply.

    Walks the top-20 token-account holders and skips any whose owner is a PDA
    (i.e. not owned by the System Program) — these are pool vaults / programs.
    The first real wallet is reported.
    """
    sup = await _rpc(session, "getTokenSupply", [mint])
    total = float(((sup.get("result") or {}).get("value") or {}).get("uiAmount") or 0)
    if total <= 0:
        return None
    large = await _rpc(session, "getTokenLargestAccounts", [mint])
    vals = ((large.get("result") or {}).get("value") or [])
    if not vals:
        return None
    addrs = [v["address"] for v in vals[:20]]
    parsed = await _rpc(session, "getMultipleAccounts",
                        [addrs, {"encoding": "jsonParsed"}])
    accounts = ((parsed.get("result") or {}).get("value") or [])

    tas: list[tuple[str, float]] = []   # (wallet_owner, uiAmount)
    for v, acc in zip(vals[:20], accounts, strict=False):
        if not acc:
            continue
        try:
            wallet = acc["data"]["parsed"]["info"]["owner"]
        except Exception:
            continue
        try:
            bal = float(v.get("uiAmount") or 0)
        except Exception:
            continue
        tas.append((wallet, bal))
    if not tas:
        return None

    # Check which owners are actual wallets (owned by System Program) vs PDAs.
    owners = [w for w, _ in tas]
    info = await _rpc(session, "getMultipleAccounts",
                      [owners, {"encoding": "base64"}])
    infos = ((info.get("result") or {}).get("value") or [])
    for (_wallet, bal), w_info in zip(tas, infos, strict=False):
        # If the owner account doesn't exist yet, treat as a wallet.
        owner_prog = (w_info or {}).get("owner") if w_info else SYSTEM_PROGRAM
        if owner_prog == SYSTEM_PROGRAM:
            return round(bal / total * 100, 3) if total else None
    return None


async def _dex_pairs(session: aiohttp.ClientSession, mint: str) -> list[dict]:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=6),
        ) as r:
            if r.status != 200:
                return []
            d = await r.json()
            return (d.get("pairs") or [])
    except Exception:
        return []


def _best_pair(pairs: list[dict]) -> dict | None:
    if not pairs:
        return None
    return sorted(
        pairs,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
        reverse=True,
    )[0]


# ─── 1) Cluster-confirm scout ───────────────────────────────────────────
# Polls each cluster wallet's latest signatures; parses SPL token-balance
# deltas to detect buys; when ≥3 distinct cluster wallets have bought the
# same mint within a 10-min window we fire open_monster_position.

async def _wallet_recent_buys(session: aiohttp.ClientSession, wallet: str,
                              seen_sig_set: set[str]) -> list[tuple[str, float]]:
    """Return [(mint, block_time), ...] for token buys by `wallet` since last poll.

    We identify a buy as: a signature where the wallet's post-token-balance for
    an SPL mint > pre-token-balance (or pre was absent).  Ignores WSOL.
    """
    WSOL = "So11111111111111111111111111111111111111112"
    buys: list[tuple[str, float]] = []
    try:
        sigs_res = await _rpc(session, "getSignaturesForAddress",
                              [wallet, {"limit": 10}])
        sigs = (sigs_res.get("result") or [])
    except Exception:
        return buys

    for s in sigs:
        sig = s.get("signature")
        if not sig or sig in seen_sig_set:
            continue
        seen_sig_set.add(sig)
        try:
            tx_res = await _rpc(session, "getTransaction",
                                [sig, {"encoding": "jsonParsed",
                                       "maxSupportedTransactionVersion": 0}])
            tx = tx_res.get("result") or {}
        except Exception:
            continue
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        pre_map: dict[tuple[int, str], float] = {}
        for b in pre:
            if b.get("owner") != wallet:
                continue
            try:
                pre_map[(b.get("accountIndex"), b.get("mint"))] = float(
                    (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
                )
            except Exception:
                pass
        for b in post:
            if b.get("owner") != wallet:
                continue
            mint = b.get("mint")
            if not mint or mint == WSOL:
                continue
            try:
                new_amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            except Exception:
                continue
            old_amt = pre_map.get((b.get("accountIndex"), mint), 0.0)
            if new_amt > old_amt:
                bt = s.get("blockTime") or time.time()
                buys.append((mint, float(bt)))
    return buys


async def cluster_confirm_scout_loop(runtime: Any,
                                      session: aiohttp.ClientSession) -> None:
    if not CLUSTER_CONFIRM_ENABLED:
        print("[monster-cluster] disabled (MONSTER_CLUSTER_CONFIRM_ENABLED=false)")
        return
    wallets = cluster_wallets()
    if not wallets:
        print("[monster-cluster] whitelist empty — loop exiting")
        return
    print(f"[monster-cluster] loop started — watching {len(wallets)} cluster wallets")

    # mint → {wallet: first_buy_ts}
    buys_by_mint: dict[str, dict[str, float]] = defaultdict(dict)
    seen_sigs: set[str] = set()

    while True:
        try:
            now = time.time()
            for w in wallets:
                buys = await _wallet_recent_buys(session, w, seen_sigs)
                for mint, bt in buys:
                    buys_by_mint[mint][w] = bt
                # Tiny inter-wallet sleep to avoid RPC burst
                await asyncio.sleep(0.2)

            # Expire old entries
            for mint in list(buys_by_mint.keys()):
                fresh = {w: t for w, t in buys_by_mint[mint].items()
                         if now - t <= CLUSTER_WINDOW_SECS}
                if fresh:
                    buys_by_mint[mint] = fresh
                else:
                    del buys_by_mint[mint]

            # Check trigger
            for mint, ws in list(buys_by_mint.items()):
                if len(ws) < CLUSTER_MIN_DISTINCT:
                    continue
                if mint in _MONSTER_SKIP_MINTS:
                    # Stablecoin / wrapped — whales funding or swapping, not a meme buy
                    del buys_by_mint[mint]
                    continue
                if _recently_signalled(mint):
                    continue
                _mark_signalled(mint)
                print(f"[monster-cluster] 🎯 {len(ws)} cluster wallets on {mint[:8]} — firing")
                _log_signal({
                    "source": "cluster_confirm",
                    "mint": mint,
                    "cluster_wallets": list(ws.keys()),
                    "window_first_buy_ts": min(ws.values()),
                })
                if not monster.can_open_new_position():
                    print("[monster-cluster] slot pool full — logged only")
                    continue
                # Token name: use symbol from DexScreener if available
                pairs = await _dex_pairs(session, mint)
                sym = (pairs[0].get("baseToken") or {}).get("symbol") if pairs else None
                await monster.open_monster_position(
                    mint=mint,
                    token_name=sym or mint[:8],
                    signal_source="cluster_confirm",
                    sol_size=monster.MONSTER_DEFAULT_SIZE_SOL,
                    session=session,
                    runtime=runtime,
                    metadata={"cluster_wallets": list(ws.keys())},
                )
        except Exception as e:
            print(f"[monster-cluster] loop error: {e}")
        await asyncio.sleep(CLUSTER_POLL_SECS)


# ─── 2) Serial-deployer sniper ──────────────────────────────────────────
# Polls the 4 whitelisted creator wallets for NEW token mints. When a mint
# is detected, waits up to SERIAL_GRAD_WAIT_SECS for it to graduate to
# PumpSwap/Raydium, then fires open_monster_position.

async def _creator_new_mints(session: aiohttp.ClientSession, creator: str,
                              seen_mints: set[str]) -> list[str]:
    """Recent pump.fun token creations by `creator`. Returns new mints only."""
    try:
        sigs_res = await _rpc(session, "getSignaturesForAddress",
                              [creator, {"limit": 5}])
        sigs = (sigs_res.get("result") or [])
    except Exception:
        return []
    found: list[str] = []
    for s in sigs:
        sig = s.get("signature")
        if not sig:
            continue
        try:
            tx_res = await _rpc(session, "getTransaction",
                                [sig, {"encoding": "jsonParsed",
                                       "maxSupportedTransactionVersion": 0}])
            tx = tx_res.get("result") or {}
        except Exception:
            continue
        if not tx:
            continue
        # Detect pump.fun token creation: tx involves PUMP_FUN_PROGRAM and
        # postTokenBalances has a new mint previously absent.
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            involves_pump = any(
                (k.get("pubkey") if isinstance(k, dict) else k) == PUMP_FUN_PROGRAM
                for k in keys
            )
        except Exception:
            involves_pump = False
        if not involves_pump:
            continue
        meta = tx.get("meta") or {}
        for b in (meta.get("postTokenBalances") or []):
            mint = b.get("mint")
            if not mint or mint in seen_mints:
                continue
            if mint.endswith("pump") or len(mint) >= 40:  # plausible pf mint
                seen_mints.add(mint)
                found.append(mint)
    return found


async def _wait_for_graduation(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Wait up to SERIAL_GRAD_WAIT_SECS for a PumpSwap/Raydium pool to appear.

    Returns the pool venue ("pump-amm" / "raydium") when detected, or None on timeout.
    """
    waited = 0.0
    while waited < SERIAL_GRAD_WAIT_SECS:
        pairs = await _dex_pairs(session, mint)
        for p in pairs:
            dx = (p.get("dexId") or "").lower()
            if dx in ("pumpswap", "pump-amm"):
                return "pump-amm"
            if dx == "raydium":
                return "raydium"
        await asyncio.sleep(SERIAL_GRAD_POLL_SECS)
        waited += SERIAL_GRAD_POLL_SECS
    return None


async def serial_deployer_sniper_loop(runtime: Any,
                                       session: aiohttp.ClientSession) -> None:
    if not SERIAL_DEPLOYER_ENABLED:
        print("[monster-serial] disabled (MONSTER_SERIAL_DEPLOYER_ENABLED=false)")
        return
    creators = serial_deployer_wallets()
    if not creators:
        print("[monster-serial] whitelist empty — loop exiting")
        return
    print(f"[monster-serial] loop started — watching {len(creators)} deployer wallets")

    seen_mints: set[str] = set()

    # Warm the cache with what's already current so we don't re-fire on startup
    for c in creators:
        try:
            await _creator_new_mints(session, c, seen_mints)
        except Exception:
            pass

    while True:
        try:
            for c in creators:
                new_mints = await _creator_new_mints(session, c, seen_mints)
                for mint in new_mints:
                    if mint in _MONSTER_SKIP_MINTS:
                        continue
                    if _recently_signalled(mint):
                        continue
                    _mark_signalled(mint)
                    print(f"[monster-serial] 🚀 {c[:8]} deployed {mint[:8]} — awaiting graduation")
                    _log_signal({
                        "source": "serial_deployer",
                        "mint": mint,
                        "creator": c,
                        "phase": "detected",
                    })
                    # Spawn a waiter so we don't block the poll loop
                    asyncio.create_task(
                        _serial_after_graduation(runtime, session, mint, c)
                    )
                await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[monster-serial] loop error: {e}")
        await asyncio.sleep(SERIAL_POLL_SECS)


async def _serial_after_graduation(runtime: Any, session: aiohttp.ClientSession,
                                    mint: str, creator: str) -> None:
    venue = await _wait_for_graduation(session, mint)
    if not venue:
        print(f"[monster-serial] {mint[:8]} did not graduate in {SERIAL_GRAD_WAIT_SECS}s — dropped")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "no_graduation"})
        return
    if not monster.can_open_new_position():
        print(f"[monster-serial] slot pool full at graduation — {mint[:8]} logged only")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "graduated_no_slot",
                     "venue": venue})
        return

    # Cheap safety pass: top1 < 15% (serial deployers get more slack than lifecycle)
    try:
        t1 = await top1_wallet_pct(session, mint)
    except Exception:
        t1 = None
    if t1 is not None and t1 >= 15.0:
        print(f"[monster-serial] {mint[:8]} top1={t1}% ≥15 — skipping")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "blocked_top1", "top1": t1})
        return

    pairs = await _dex_pairs(session, mint)
    sym = (pairs[0].get("baseToken") or {}).get("symbol") if pairs else None
    await monster.open_monster_position(
        mint=mint,
        token_name=sym or mint[:8],
        signal_source="serial_deployer",
        sol_size=monster.MONSTER_DEFAULT_SIZE_SOL,
        session=session,
        runtime=runtime,
        metadata={"creator": creator, "venue": venue, "top1_pct": t1},
    )


# ─── 3) Lifecycle scout ─────────────────────────────────────────────────
# Scans recently-graduated PumpSwap tokens for the "clean lifecycle" shape.
# Tightened 2026-04-20 against 8-MEGA post-graduation corpus (all 100x+ within 72h),
# and again 2026-04-20 evening after SCHIZO/MLG both entered post-spike:
#   * PumpSwap pool exists
#   * age 1h–6h       (monster ramp phase; past 6h we tend to buy distribution)
#   * pair liq $50k–$300k
#   * mcap $300k–$3M
#   * liq/mcap 4–20%
#   * top1 non-pool holder < 10%
#   * h1 buy ratio 48–65%       (accumulation, not parabolic)
#   * h1 price change ≤ +40%    (don't chase — missed the ramp if > 40%)
#   * m5 price change ≤ +5%     (don't buy the micro-spike)
#   * socials present on base token

LIFECYCLE_MIN_LIQ_USD       = 50_000
LIFECYCLE_MAX_LIQ_USD       = 300_000
LIFECYCLE_MIN_MC_USD        = 300_000
LIFECYCLE_MAX_MC_USD        = 3_000_000
LIFECYCLE_MIN_LIQ_MC_RATIO  = 0.04
LIFECYCLE_MAX_LIQ_MC_RATIO  = 0.20
LIFECYCLE_MIN_AGE_SECS      = 20 * 60         # 20min (dropped 60→20 on 2026-04-21; MIM/hijabunc/TERMINAL/ALTSZN all spiked at 30-45min)
LIFECYCLE_MAX_AGE_SECS      = 90 * 60         # 90min (tightened 6h → 90min 2026-04-20; MIM/hijabunc bled at 126/167min)
LIFECYCLE_TOP1_MAX_PCT      = 10.0
LIFECYCLE_BUY_RATIO_MIN     = 48.0
LIFECYCLE_BUY_RATIO_MAX     = 65.0
LIFECYCLE_H1_CHANGE_MAX_PCT = 40.0            # skip if h1 > +40% — run already happened
LIFECYCLE_M5_CHANGE_MAX_PCT = 5.0             # skip if m5 > +5%  — buying the micro-spike


async def _recent_pumpswap_profiles(session: aiohttp.ClientSession) -> list[dict]:
    """DexScreener's trending/search feed for PumpSwap base tokens.

    DexScreener doesn't expose a public 'list new pumpswap pairs' endpoint, so
    we use the search endpoint filtered by pumpswap. This is best-effort;
    operator can seed via MONSTER_LIFECYCLE_WATCHLIST env var too.
    """
    try:
        async with session.get(
            "https://api.dexscreener.com/latest/dex/search?q=pumpswap",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return []
            d = await r.json()
            return d.get("pairs") or []
    except Exception:
        return []


async def lifecycle_scout_loop(runtime: Any,
                                session: aiohttp.ClientSession) -> None:
    if not LIFECYCLE_SCOUT_ENABLED:
        print("[monster-lifecycle] disabled (MONSTER_LIFECYCLE_ENABLED=false)")
        return
    print("[monster-lifecycle] loop started")

    while True:
        try:
            pairs = await _recent_pumpswap_profiles(session)
            now_ms = time.time() * 1000
            for p in pairs:
                try:
                    dex_id = (p.get("dexId") or "").lower()
                    if dex_id not in ("pumpswap", "pump-amm"):
                        continue
                    mint = (p.get("baseToken") or {}).get("address")
                    if not mint or mint in _MONSTER_SKIP_MINTS:
                        continue
                    if _recently_signalled(mint):
                        continue
                    pca = p.get("pairCreatedAt")
                    if not pca:
                        continue
                    age_secs = (now_ms - float(pca)) / 1000
                    if not (LIFECYCLE_MIN_AGE_SECS <= age_secs <= LIFECYCLE_MAX_AGE_SECS):
                        continue
                    liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
                    if not (LIFECYCLE_MIN_LIQ_USD <= liq_usd <= LIFECYCLE_MAX_LIQ_USD):
                        continue
                    mc_usd = float(p.get("marketCap") or p.get("fdv") or 0)
                    if not (LIFECYCLE_MIN_MC_USD <= mc_usd <= LIFECYCLE_MAX_MC_USD):
                        continue
                    liq_mc = (liq_usd / mc_usd) if mc_usd > 0 else 0
                    if not (LIFECYCLE_MIN_LIQ_MC_RATIO <= liq_mc <= LIFECYCLE_MAX_LIQ_MC_RATIO):
                        continue
                    pc = p.get("priceChange") or {}
                    h1_change = float(pc.get("h1") or 0)
                    m5_change = float(pc.get("m5") or 0)
                    if h1_change > LIFECYCLE_H1_CHANGE_MAX_PCT:
                        continue  # ran too hard in the last hour — chase risk
                    if m5_change > LIFECYCLE_M5_CHANGE_MAX_PCT:
                        continue  # micro-spike — wait for consolidation
                    txns_h1 = (p.get("txns") or {}).get("h1") or {}
                    buys = txns_h1.get("buys") or 0
                    sells = txns_h1.get("sells") or 0
                    total = buys + sells
                    if total < 30:  # too thin — skip
                        continue
                    br = (buys / total) * 100
                    if not (LIFECYCLE_BUY_RATIO_MIN <= br <= LIFECYCLE_BUY_RATIO_MAX):
                        continue
                    base_info = (p.get("info") or {})
                    socials = base_info.get("socials") or []
                    websites = base_info.get("websites") or []
                    if not socials and not websites:
                        continue

                    # Expensive last: top-1 non-pool holder
                    t1 = await top1_wallet_pct(session, mint)
                    if t1 is None or t1 >= LIFECYCLE_TOP1_MAX_PCT:
                        continue

                    _mark_signalled(mint)
                    print(f"[monster-lifecycle] 🎯 {mint[:8]} lifecycle match "
                          f"age={age_secs/60:.0f}min liq=${liq_usd:,.0f} br={br:.0f}% top1={t1}%")
                    _log_signal({
                        "source": "lifecycle",
                        "mint": mint,
                        "age_min": round(age_secs / 60, 1),
                        "liq_usd": liq_usd,
                        "mc_usd": mc_usd,
                        "liq_mc_ratio": round(liq_mc, 4),
                        "buy_ratio_pct": br,
                        "top1_pct": t1,
                        "h1_change_pct": h1_change,
                        "m5_change_pct": m5_change,
                    })
                    if not monster.can_open_new_position():
                        continue
                    sym = (p.get("baseToken") or {}).get("symbol") or mint[:8]
                    await monster.open_monster_position(
                        mint=mint,
                        token_name=sym,
                        signal_source="lifecycle",
                        sol_size=monster.MONSTER_DEFAULT_SIZE_SOL,
                        session=session,
                        runtime=runtime,
                        metadata={"age_min": round(age_secs / 60, 1),
                                  "liq_usd": liq_usd, "mc_usd": mc_usd,
                                  "liq_mc_ratio": round(liq_mc, 4),
                                  "buy_ratio": br, "top1_pct": t1,
                                  "h1_change": h1_change, "m5_change": m5_change},
                    )
                except Exception:
                    # Isolate per-pair errors
                    continue
        except Exception as e:
            print(f"[monster-lifecycle] loop error: {e}")
        await asyncio.sleep(LIFECYCLE_POLL_SECS)


# ─── 4) Breakout-candle scout ────────────────────────────────────────────
# Covers the gap the lifecycle scout leaves at age 90min-12h. Fires when a
# PumpSwap token shows a +20% m5 breakout on real volume — the same pattern
# we see in MIM/hijabunc/TERMINAL/ALTSZN at t+30-45min, and the only way to
# catch slow-cookers like Nintondo that spike 12h post-graduation.
#
# Gates (loose on age, strict on breakout quality):
#   * age 30min-12h                      (lifecycle covers 20-90min; overlap at 30-90min is fine)
#   * m5 >= +20%                         (breakout candle)
#   * h1 <= +150%                        (not already blown past — chase risk)
#   * liq $30k-$500k, mcap $100k-$5M
#   * vol_m5 >= $3k                      (real volume, not thin-book push)
#   * h1 txns >= 30
#   * socials present, top1 < 12%

BREAKOUT_POLL_SECS           = 90
BREAKOUT_MIN_AGE_SECS        = 30 * 60
BREAKOUT_MAX_AGE_SECS        = 12 * 3600
BREAKOUT_MIN_LIQ_USD         = 30_000
BREAKOUT_MAX_LIQ_USD         = 500_000
BREAKOUT_MIN_MC_USD          = 100_000
BREAKOUT_MAX_MC_USD          = 5_000_000
BREAKOUT_M5_MIN_PCT          = 20.0
BREAKOUT_H1_MIN_PCT          = 0.0    # reject dead-cat bounces (SOLMONEY 2026-04-22: h1=-15% → dumped 28pp in 30s)
BREAKOUT_H1_MAX_PCT          = 150.0
BREAKOUT_MIN_M5_VOL_USD      = 3_000
BREAKOUT_MIN_H1_TXNS         = 30
BREAKOUT_TOP1_MAX_PCT        = 12.0


async def breakout_candle_scout_loop(runtime: Any,
                                      session: aiohttp.ClientSession) -> None:
    if not BREAKOUT_SCOUT_ENABLED:
        print("[monster-breakout] disabled (MONSTER_BREAKOUT_ENABLED=false)")
        return
    print("[monster-breakout] loop started — watching for +20% m5 breakouts on 30min-12h pumpswap pairs")

    while True:
        try:
            pairs = await _recent_pumpswap_profiles(session)
            now_ms = time.time() * 1000
            for p in pairs:
                try:
                    if (p.get("dexId") or "").lower() not in ("pumpswap", "pump-amm"):
                        continue
                    mint = (p.get("baseToken") or {}).get("address")
                    if not mint or mint in _MONSTER_SKIP_MINTS:
                        continue
                    if _recently_signalled(mint):
                        continue
                    pca = p.get("pairCreatedAt")
                    if not pca:
                        continue
                    age_secs = (now_ms - float(pca)) / 1000
                    if not (BREAKOUT_MIN_AGE_SECS <= age_secs <= BREAKOUT_MAX_AGE_SECS):
                        continue
                    liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
                    if not (BREAKOUT_MIN_LIQ_USD <= liq_usd <= BREAKOUT_MAX_LIQ_USD):
                        continue
                    mc_usd = float(p.get("marketCap") or p.get("fdv") or 0)
                    if not (BREAKOUT_MIN_MC_USD <= mc_usd <= BREAKOUT_MAX_MC_USD):
                        continue
                    pc = p.get("priceChange") or {}
                    m5 = float(pc.get("m5") or 0)
                    h1 = float(pc.get("h1") or 0)
                    if m5 < BREAKOUT_M5_MIN_PCT:
                        continue
                    if h1 > BREAKOUT_H1_MAX_PCT:
                        continue
                    if h1 < BREAKOUT_H1_MIN_PCT:
                        continue
                    vol_m5 = float((p.get("volume") or {}).get("m5") or 0)
                    if vol_m5 < BREAKOUT_MIN_M5_VOL_USD:
                        continue
                    txns_h1 = (p.get("txns") or {}).get("h1") or {}
                    buys = txns_h1.get("buys") or 0
                    sells = txns_h1.get("sells") or 0
                    total = buys + sells
                    if total < BREAKOUT_MIN_H1_TXNS:
                        continue
                    br = (buys / total) * 100 if total > 0 else 0
                    base_info = p.get("info") or {}
                    if not (base_info.get("socials") or base_info.get("websites")):
                        continue

                    t1 = await top1_wallet_pct(session, mint)
                    if t1 is None or t1 >= BREAKOUT_TOP1_MAX_PCT:
                        continue

                    _mark_signalled(mint)
                    print(f"[monster-breakout] 🎯 {mint[:8]} BREAKOUT age={age_secs/60:.0f}min "
                          f"m5={m5:+.0f}% h1={h1:+.0f}% liq=${liq_usd:,.0f} vol_m5=${vol_m5:,.0f} top1={t1}%")
                    _log_signal({
                        "source": "breakout_candle",
                        "mint": mint,
                        "age_min": round(age_secs / 60, 1),
                        "liq_usd": liq_usd,
                        "mc_usd": mc_usd,
                        "m5_change_pct": m5,
                        "h1_change_pct": h1,
                        "vol_m5_usd": vol_m5,
                        "buy_ratio_pct": br,
                        "top1_pct": t1,
                    })
                    if not monster.can_open_new_position():
                        continue
                    sym = (p.get("baseToken") or {}).get("symbol") or mint[:8]
                    await monster.open_monster_position(
                        mint=mint,
                        token_name=sym,
                        signal_source="breakout_candle",
                        sol_size=monster.MONSTER_DEFAULT_SIZE_SOL,
                        session=session,
                        runtime=runtime,
                        metadata={
                            "age_min": round(age_secs / 60, 1),
                            "liq_usd": liq_usd, "mc_usd": mc_usd,
                            "m5_change": m5, "h1_change": h1,
                            "vol_m5_usd": vol_m5, "buy_ratio": br,
                            "top1_pct": t1,
                        },
                    )
                except Exception:
                    continue
        except Exception as e:
            print(f"[monster-breakout] loop error: {e}")
        await asyncio.sleep(BREAKOUT_POLL_SECS)
