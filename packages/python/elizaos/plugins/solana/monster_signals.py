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
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import aiohttp

from . import strategy_e_monster as monster
from . import rejection_tracker as rt

BASE = Path(__file__).parent


def _log_reject(scout: str, mint: str, reason: str, snapshot: dict,
                filter_name: str = "", filter_value: Any = None,
                threshold: Any = None) -> None:
    """Thin wrapper so a tracker failure can't kill a scout loop."""
    try:
        rt.record(
            mint=mint,
            reason=reason,
            filter_name=filter_name or reason,
            filter_value=filter_value,
            threshold=threshold,
            strategy=scout,
            extra=snapshot,
        )
    except Exception:
        pass
SIGNAL_LOG_FILE = BASE / "monster_signal_log.json"
WHITELIST_FILE  = BASE / "monster_creator_whitelist.json"

HELIUS_KEY = os.environ.get("HELIUS_API_KEY") or "7c90bfcc-bf96-413c-bab2-d3977546cf88"
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SYSTEM_PROGRAM   = "11111111111111111111111111111111"

# Mints we must never attempt to buy as a monster position — stablecoins / WSOL / wrapped,
# PLUS tokens that were hard-skipped due to max_h1_seen > 150% (these persist across restarts).
# Critical: previously this was in-memory only — a restart cleared it, letting the bot enter
# tokens that had already peaked massively (Discomorphism: peaked $130K, entered at $63K after
# restart wiped the skip list). Now persisted to disk.
_SKIP_MINTS_FILE = Path(__file__).parent / "monster_skip_mints_persistent.json"

def _load_persistent_skip_mints() -> set:
    try:
        import json as _j
        if _SKIP_MINTS_FILE.exists():
            return set(_j.loads(_SKIP_MINTS_FILE.read_text()))
    except Exception:
        pass
    return set()

def _save_persistent_skip_mints(mints: set) -> None:
    try:
        import json as _j
        _SKIP_MINTS_FILE.write_text(_j.dumps(list(mints)))
    except Exception:
        pass

_MONSTER_SKIP_MINTS: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # WSOL
    "USD1ttGY1N17NEEH4tLSfBJBkmBQZxSBRAZrMPAbDLDp",  # USD1
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",  # stSOL
}
# Load persisted skip mints from previous sessions
_MONSTER_SKIP_MINTS.update(_load_persistent_skip_mints())

# ─── Feature flags ──────────────────────────────────────────────────────
def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")

# 2026-05-02 — CREATOR-ALPHA-ONLY mode. All other scouts disabled by default
# while we validate the operator-tracking strategy in isolation. To re-enable
# any of these, set the env var to "true" or flip the default below.
CLUSTER_CONFIRM_ENABLED  = _env_on("MONSTER_CLUSTER_CONFIRM_ENABLED", "false")
SERIAL_DEPLOYER_ENABLED  = _env_on("MONSTER_SERIAL_DEPLOYER_ENABLED", "false")
LIFECYCLE_SCOUT_ENABLED  = _env_on("MONSTER_LIFECYCLE_ENABLED", "false")
BREAKOUT_SCOUT_ENABLED   = _env_on("MONSTER_BREAKOUT_ENABLED", "false")

# ─── Cadences ───────────────────────────────────────────────────────────
CLUSTER_POLL_SECS         = 45     # poll cluster wallets every 45s
CLUSTER_WINDOW_SECS       = 10 * 60  # 10min confirmation window
CLUSTER_MIN_DISTINCT      = 3      # ≥3 cluster wallets on same mint

SERIAL_POLL_SECS          = 20     # poll deployer wallets every 20s — fast lane
SERIAL_GRAD_WAIT_SECS     = 60 * 60  # wait up to 1h for graduation
SERIAL_GRAD_POLL_SECS     = 30     # re-check graduation every 30s

LIFECYCLE_POLL_SECS       = 60     # DexScreener lifecycle scan every 60s (halved from 120s 2026-04-30 to widen entry-margin past TP)

SIGNAL_DEDUP_WINDOW_SECS  = 60 * 60 * 12  # don't re-signal the same mint within 12h

# ─── Shared concentration cap (all scouts) ──────────────────────────────
# Top-10 aggregate pct-of-supply at entry. Per holder_guard PDF spec, >35%
# means insiders hold enough exit liquidity that retail is the dump-counterparty.
# TRADE 2026-04-24 died -17% with top-10 at 81.3% — would have been blocked.
# Each scout also keeps its own top-1 cap (lifecycle=10, breakout=12, serial=15).
MONSTER_TOP10_MAX_PCT     = 35.0

# ─── Holder organic-buyer-base floor ────────────────────────────────────
# Tokens with too few unique holders look healthy by price/volume but have
# no real buyer base — they're being moved by 5-10 wallets pretending to be
# a market. PDF sweet-spot says 500; dialled to 200 for first run since
# we have no data validation yet. Tighten if we see late-entry losses on
# tokens with ~200-400 holders.
MONSTER_MIN_UNIQUE_HOLDERS = 200

# ─── Buy velocity ratio ─────────────────────────────────────────────────
# Compares recent 1h buy rate to the prior-5h average. <1.0 means buy
# pressure is decelerating — we'd be entering as momentum dies.
# Computed from DexScreener h1 + h6 fields; no extra RPC.
MONSTER_BUY_VELOCITY_MIN_RATIO = 0.3

# ─── Creator-burn cooldown (skip new tokens from creators that just lost us) ──
# Reads monster_closed_trades.json on demand. If the token's on-chain creator
# has any prior monster trade closing ≤ -30% within the last 48h, skip.
# Counters the "rug-then-redeploy" cycle some creators run.
CREATOR_BURN_LOSS_THRESHOLD_PCT = -30.0
CREATOR_BURN_WINDOW_SECS         = 48 * 3600

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


# ─── Helius webhook fast-path queue ──────────────────────────────────────
# Populated by /api/webhooks/helius/pumpswap-grad in dashboard_api when
# Helius pushes a CREATE_POOL event on the PumpSwap program. The lifecycle
# scout drains this queue at the start of every cycle and fetches each
# mint's data immediately rather than waiting for DexScreener's
# search?q=pumpswap feed to surface it (which lags 30-90s).
#
# Each entry: {"mint": str, "pool": str, "ts": float, "received_at": float}
# Capacity bounded so a misconfigured webhook can't OOM the bot.
_helius_fresh_grad_queue: list[dict] = []
_HELIUS_QUEUE_MAX_LEN = 500


# Mints received via Helius webhook — known to be genuinely fresh.
# These get the reduced age floor (5 min vs 20 min default).
_webhook_sourced_mints: set[str] = set()

def push_fresh_grad(mint: str, pool: str, on_chain_ts: float | None = None) -> None:
    """Called by the dashboard webhook receiver. Bounded list semantics —
    drop oldest if we hit the cap (means lifecycle scout is starving)."""
    import time as _time
    if not mint:
        return
    _webhook_sourced_mints.add(mint)  # track for reduced age floor
    rec = {
        "mint": mint,
        "pool": pool or "",
        "ts": float(on_chain_ts) if on_chain_ts else _time.time(),
        "received_at": _time.time(),
    }
    _helius_fresh_grad_queue.append(rec)
    if len(_helius_fresh_grad_queue) > _HELIUS_QUEUE_MAX_LEN:
        # Drop oldest. If we're hitting this, the scout is stuck or the
        # webhook firehose is too aggressive — log it.
        dropped = _helius_fresh_grad_queue.pop(0)
        print(f"[helius-fast-path] ⚠️  queue full ({_HELIUS_QUEUE_MAX_LEN}), "
              f"dropped oldest mint={dropped.get('mint','?')[:8]}")


def drain_fresh_grad_queue() -> list[dict]:
    """Called by lifecycle scout at the start of every cycle. Returns and
    clears the queue atomically (single-threaded asyncio context)."""
    drained = list(_helius_fresh_grad_queue)
    _helius_fresh_grad_queue.clear()
    return drained


# ─── Creator-alpha scout — operator + direct-creator wallet monitoring ──
# Polls a curated list of "alpha" wallets every 60s for new pump.fun token
# creations. Two paths:
#   1. DIRECT-CREATE: wallet emits a pump.fun Create instruction → we have the mint
#   2. OPERATOR-FUND: parent operator sends 0.5-2.0 SOL to a fresh wallet →
#      we watch that recipient for up to 90 min; if they create a token, fire
# Detected mints are held in a pending-list until they graduate to pump-amm,
# then pushed into _helius_fresh_grad_queue so the lifecycle scout sees them
# on its very next cycle (within 30s of graduation).
_CREATOR_ALPHA_PLAYBOOK_PATH = BASE / "creator_alpha_playbook.json"
_CREATOR_ALPHA_CHILD_TTL_SECS = 90 * 60       # drop unwatched children
_CREATOR_ALPHA_MINT_TTL_SECS  = 60 * 60       # drop pending mints if no graduation
_CREATOR_ALPHA_POLL_SECS      = 1             # cycle cadence — 1s ultra-fast polling to catch token creation within seconds
_CREATOR_ALPHA_MIN_FUND_SOL   = 0.5           # fresh-wallet fund pattern lower bound
_CREATOR_ALPHA_MAX_FUND_SOL   = 2.0           # upper bound

_creator_alpha_playbook: dict | None = None
_creator_alpha_last_sigs: dict[str, str] = {}        # wallet → newest processed sig
_creator_alpha_watched_children: dict[str, dict] = {}  # child → {parent, funded_ts, amount_sol}
_creator_alpha_pending_mints: dict[str, dict] = {}   # mint → {detected_ts, source, creator, parent_op}
_creator_alpha_recent_signals: deque = deque(maxlen=50)  # rolling activity feed for /api endpoint

# Priority mints — when creator_alpha scout sees a graduation, the mint is added
# here in addition to the fresh_grad_queue. The lifecycle scout consults this
# set and applies RELAXED gates: liq floor $5k (vs $30k), age min 0 (vs 20min),
# h1 max 600% (vs 100%). Hard safety gates (rugcheck, top10, top1, creator_burn)
# still apply. Entries tagged with signal_source = "creator_alpha_*" so they
# show up distinctly in the dashboard's per-source performance breakdown.
_creator_alpha_priority_mints: dict[str, dict] = {}  # mint → {marked_ts, source, creator, parent_op}
_CREATOR_ALPHA_PRIORITY_TTL_SECS = 15 * 60   # 15 min — token must enter via priority path within this window
_CREATOR_ALPHA_PRIORITY_LIQ_MIN  = 5_000     # vs LIFECYCLE_MIN_LIQ_USD=30k
_CREATOR_ALPHA_PRIORITY_AGE_MIN  = 0         # vs LIFECYCLE_MIN_AGE_SECS=1200
_CREATOR_ALPHA_PRIORITY_H1_MAX   = 600       # vs LIFECYCLE_H1_CHANGE_MAX_PCT=100

# Direct-entry mode — fire entry on token creation rather than waiting for
# graduation. With this enabled the bot enters at $5-30k MC instead of
# $50-100k+ MC. Uses pump.fun bonding-curve buy via PumpPortal pool="pump".
CREATOR_ALPHA_DIRECT_ENTRY = _env_on("CREATOR_ALPHA_DIRECT_ENTRY_ENABLED", "true")


def mark_creator_alpha_priority(mint: str, source: str, creator: str | None,
                                 parent_op: str | None) -> None:
    """Called by creator_alpha_scout when pushing a graduated mint to the
    fast-path queue. Adds the mint to the priority set so the lifecycle
    scout applies relaxed gates."""
    _creator_alpha_priority_mints[mint] = {
        "marked_ts": time.time(),
        "source": source,
        "creator": creator,
        "parent_op": parent_op,
    }


async def _creator_alpha_direct_entry(runtime: Any, session: aiohttp.ClientSession,
                                       mint: str, source: str, creator: str | None,
                                       parent_op: str | None) -> None:
    """Direct-entry path for creator_alpha — buy on the bonding curve immediately
    after detecting a token creation from a tracked operator/creator. Bypasses
    the wait-for-graduation flow. Uses PumpPortal pool='pump' for the buy."""
    if not CREATOR_ALPHA_DIRECT_ENTRY:
        return
    # Age gate + MC gate — single DexScreener call, two filters:
    #
    # AGE GATE: skip if token is >30 min old (stale signal after pause/restart).
    #
    # MC GATE: skip if current MC is already above threshold (token has pumped
    # before we arrived). MARATHON entered at $47K MC after bundlers pumped it
    # from $2K in 88s — bot bought the peak. BOOBFACE entered at $6.6K (fresh).
    # Default threshold $20K separates these cleanly. Configurable via dashboard.
    try:
        from elizaos.plugins.solana import live_config as _lc_mc
        _mc_threshold = float(_lc_mc.get("creator_alpha_max_entry_mc_usd", 20_000))
    except Exception:
        _mc_threshold = 20_000

    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as _r:
            if _r.status == 200:
                _pairs = (await _r.json()).get("pairs") or []

                # DEAD TOKEN GATE: fresh BC tokens are always indexed by DexScreener
                # within seconds of creation. Zero pairs = old/dead/unindexed token
                # that has no business being in our strategy.
                # KOSPI6900 was 574 days old with 0 DexScreener pairs — this gate
                # would have caught it. Never enter tokens DexScreener doesn't know about.
                if not _pairs:
                    print(
                        f"[creator-alpha] ⏭ DEAD-TOKEN-GATE: {mint[:14]} has 0 DexScreener "
                        f"pairs — old/dead/unindexed token, not a fresh BC launch, skipping"
                    )
                    return

                # Age gate
                _oldest_pair_ts = min(
                    p.get("pairCreatedAt", 0) or 0 for p in _pairs
                ) / 1000
                _age_min = (time.time() - _oldest_pair_ts) / 60
                if _oldest_pair_ts > 0 and _age_min > 30:
                    print(
                        f"[creator-alpha] ⏭ AGE-GATE: {mint[:14]} is {_age_min:.0f}min old — "
                        f"BC pump already happened, skipping late entry"
                    )
                    return

                # MC gate — two checks, ceiling AND floor:
                #
                # CEILING ($20K): token already pumped before we arrived
                # (MARATHON entered at $47K after bundlers pumped it 88s)
                #
                # FLOOR ($6K): token has had NO organic buying beyond bundlers.
                # All recent losses entered at $2,400-3,000 MC = 1-8% BC progress,
                # meaning only the bundler bought. Genuine tokens reach $6K+ quickly
                # because organic buyers push the price up.
                # BOOBFACE $6.6K → ALLOWED (+110%) | UNCTON $2.4K → BLOCKED ✅
                _best_pair = max(_pairs, key=lambda p: float(
                    (p.get("liquidity") or {}).get("usd") or 0
                ))
                _current_mc = float(_best_pair.get("marketCap") or 0)

                try:
                    from elizaos.plugins.solana import live_config as _lc_mc2
                    _mc_floor = float(_lc_mc2.get("creator_alpha_min_entry_mc_usd", 6_000))
                except Exception:
                    _mc_floor = 6_000

                if _current_mc > _mc_threshold:
                    print(
                        f"[creator-alpha] ⏭ MC-CEILING: {mint[:14]} MC=${_current_mc:,.0f} "
                        f"already above ${_mc_threshold:,.0f} — "
                        f"pump happened before we arrived, skipping"
                    )
                    return

                if _current_mc > 0 and _current_mc < _mc_floor:
                    print(
                        f"[creator-alpha] ⏭ MC-FLOOR: {mint[:14]} MC=${_current_mc:,.0f} "
                        f"below ${_mc_floor:,.0f} minimum — "
                        f"only bundlers bought, no organic interest yet, skipping"
                    )
                    return

                # ── SNAPSHOT 1: capture price + buyer activity ─────────────────
                _price_snap1 = float(_best_pair.get("priceNative") or 0)
                _m5_snap1_buys = int((_best_pair.get("txns") or {}).get("m5", {}).get("buys") or 0)
                _m5_snap1_sells = int((_best_pair.get("txns") or {}).get("m5", {}).get("sells") or 0)

                if _price_snap1 > 0:
                    await asyncio.sleep(6)
                    try:
                        async with session.get(
                            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as _r2:
                            if _r2.status == 200:
                                _pairs2 = (await _r2.json()).get("pairs") or []
                                if _pairs2:
                                    _best2 = max(_pairs2, key=lambda p: float(
                                        (p.get("liquidity") or {}).get("usd") or 0
                                    ))
                                    _price_snap2 = float(_best2.get("priceNative") or 0)
                                    _m5_snap2_buys = int((_best2.get("txns") or {}).get("m5", {}).get("buys") or 0)
                                    _m5_snap2_sells = int((_best2.get("txns") or {}).get("m5", {}).get("sells") or 0)

                                    if _price_snap2 > 0 and _price_snap1 > 0:
                                        _chg_pct = (_price_snap2 - _price_snap1) / _price_snap1 * 100

                                        # DIRECTION GATE: price actively falling → dump in progress
                                        if _chg_pct < -2.0:
                                            print(
                                                f"[creator-alpha] ⏭ DIRECTION-GATE: {mint[:14]} "
                                                f"price fell {_chg_pct:.1f}% in 6s — dumping, skip"
                                            )
                                            return

                                        # TRACTION GATE: price flat AND zero buy activity in m5.
                                        # A genuine runner has buyers flowing in — price moves up or
                                        # m5 buys increment between snapshots. Tokens with ZERO buys
                                        # in both snapshots and flat price have no organic demand —
                                        # they peaked at creation (bundler-only) and no one else cares.
                                        # Only apply when DexScreener HAS had time to index (m5 > 0
                                        # in either snapshot means it's indexed and active).
                                        _m5_buys_total = _m5_snap1_buys + _m5_snap2_buys
                                        _m5_sells_total = _m5_snap1_sells + _m5_snap2_sells
                                        _price_flat = abs(_chg_pct) < 1.5
                                        if (
                                            _price_flat
                                            and _m5_buys_total == 0
                                            and _m5_sells_total > 0
                                        ):
                                            print(
                                                f"[creator-alpha] ⏭ TRACTION-GATE: {mint[:14]} "
                                                f"flat price ({_chg_pct:+.1f}%), 0 buys, {_m5_sells_total} sells — "
                                                f"bundlers exiting with no buyers, skip"
                                            )
                                            return

                                        # DEAD-FLOOR GATE: price completely flat AND MC is at the
                                        # absolute floor of a pump.fun token ($2-4K = nobody buying).
                                        # The MC gate blocks tokens that already PUMPED above $20K.
                                        # This gate blocks tokens that NEVER pumped — dead on arrival.
                                        #
                                        # UNCTON: $2.4K MC, price flat, 1 holder — flatline chart.
                                        # BOOBFACE: $6.6K MC, price rising → gate does NOT fire.
                                        #
                                        # Logic: if price is essentially flat (< +0.5% gain) AND MC
                                        # is below $5K, zero buying pressure has entered since launch.
                                        # A token about to run will be moving UP during this window.
                                        _mc2 = float(_best2.get("marketCap") or 0)
                                        if _price_flat and _mc2 > 0 and _mc2 < 5_000 and _chg_pct < 0.5:
                                            print(
                                                f"[creator-alpha] ⏭ DEAD-FLOOR-GATE: {mint[:14]} "
                                                f"MC=${_mc2:,.0f} flat ({_chg_pct:+.1f}%) — "
                                                f"no buyers at floor price, skip"
                                            )
                                            return
                    except Exception:
                        pass  # direction check failed — proceed with entry
            else:
                # DexScreener non-200 — treat as unindexed, skip to be safe
                print(f"[creator-alpha] ⏭ DEXSCREENER-GATE: {mint[:14]} returned HTTP {_r.status}, skipping")
                return
    except Exception:
        # DexScreener completely unreachable — skip rather than risk a bad entry.
        # When the API is down we have no way to verify the token is fresh.
        print(f"[creator-alpha] ⏭ DEXSCREENER-GATE: {mint[:14]} unreachable, skipping to be safe")
        return
    # Slot check via source-aware capacity
    if not monster.can_open_new_position(source):
        print(f"[creator-alpha] ⏭ slot pool full — {mint[:14]} skipped (src={source})")
        return
    sol_size = monster.get_size_for_source(source)
    token_name = mint[:8]  # we don't have a name yet — token's brand-new
    metadata = {
        "creator": creator,
        "parent_op": parent_op,
        "entry_path": "bonding_curve",
        "detected_via": "creator_alpha_scout",
    }
    print(f"[creator-alpha] ⚡ DIRECT ENTRY {mint[:14]} src={source} size={sol_size} SOL pool=pump")
    try:
        ok = await monster.open_monster_position(
            mint=mint,
            token_name=token_name,
            signal_source=source,
            sol_size=sol_size,
            session=session,
            runtime=runtime,
            metadata=metadata,
            pool="pump",  # bonding curve — token is brand-new, not graduated
        )
        _creator_alpha_recent_signals.append({
            "kind": "direct_entry", "mint": mint, "source": source,
            "creator": creator, "parent_op": parent_op,
            "sol_size": sol_size, "success": bool(ok), "ts": time.time(),
        })
    except Exception as exc:
        print(f"[creator-alpha] direct-entry error on {mint[:14]}: {exc}")
        _creator_alpha_recent_signals.append({
            "kind": "direct_entry_error", "mint": mint, "source": source,
            "error": str(exc)[:80], "ts": time.time(),
        })


def _is_creator_alpha_priority(mint: str) -> dict | None:
    """Returns the priority record for the mint if it's in the set AND fresh,
    else None. Expired entries are cleaned up lazily."""
    rec = _creator_alpha_priority_mints.get(mint)
    if not rec:
        return None
    if time.time() - rec["marked_ts"] > _CREATOR_ALPHA_PRIORITY_TTL_SECS:
        _creator_alpha_priority_mints.pop(mint, None)
        return None
    return rec


def _load_creator_alpha_playbook() -> dict:
    global _creator_alpha_playbook
    if _creator_alpha_playbook is not None:
        return _creator_alpha_playbook
    try:
        with open(_CREATOR_ALPHA_PLAYBOOK_PATH) as f:
            _creator_alpha_playbook = json.load(f)
    except Exception:
        _creator_alpha_playbook = {"direct_creators": {}, "operators": []}
    return _creator_alpha_playbook


def creator_alpha_recent_signals() -> list[dict]:
    """Public accessor for the dashboard /api endpoint."""
    return list(_creator_alpha_recent_signals)


def _creator_alpha_tracked_wallets() -> tuple[list[str], list[str]]:
    pb = _load_creator_alpha_playbook()
    direct: list[str] = []
    for tier_key in ("tier_a", "tier_b", "tier_c", "high_hit"):
        for entry in (pb.get("direct_creators") or {}).get(tier_key, []):
            w = entry.get("wallet")
            if w and w not in direct:
                direct.append(w)
    operators = [op["wallet"] for op in (pb.get("operators") or []) if op.get("wallet")]
    return direct, operators


async def _creator_alpha_poll_recent(session: aiohttp.ClientSession, wallet: str, limit: int = 5) -> list[dict]:
    """Fetch most-recent N sigs for wallet, return only NEW ones since last poll."""
    last = _creator_alpha_last_sigs.get(wallet)
    new_sigs: list[dict] = []
    try:
        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
            "params": [wallet, {"limit": limit}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return new_sigs
            res = await r.json()
        sigs = res.get("result") or []
        if not sigs:
            return new_sigs
        for s in sigs:
            if s.get("err"):
                continue
            if s["signature"] == last:
                break
            new_sigs.append(s)
        _creator_alpha_last_sigs[wallet] = sigs[0]["signature"]
    except Exception:
        pass
    return new_sigs


async def _creator_alpha_detect_create(session: aiohttp.ClientSession, sig: str, signer: str) -> str | None:
    """Parse a tx; return new pump.fun mint if signer created one, else None."""
    try:
        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            tx_res = await r.json()
        tx = tx_res.get("result")
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            return None
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        if not keys:
            return None
        actual_signer = keys[0].get("pubkey") if isinstance(keys[0], dict) else keys[0]
        if actual_signer != signer:
            return None
        acct_strs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        la = (tx["meta"].get("loadedAddresses") or {})
        acct_strs.extend(la.get("writable") or [])
        acct_strs.extend(la.get("readonly") or [])
        if PUMP_FUN_PROGRAM not in acct_strs:
            return None
        logs = tx["meta"].get("logMessages") or []
        if not any("Instruction: Create" in l for l in logs):
            return None
        for a in acct_strs:
            if isinstance(a, str) and a.endswith("pump"):
                return a
    except Exception:
        pass
    return None


async def _creator_alpha_detect_funding(session: aiohttp.ClientSession, sig: str, operator: str) -> list[tuple[str, float]]:
    """Parse a tx; return list of (recipient, amount_sol) for fresh-wallet
    fundings in the configured SOL band where operator is signer."""
    transfers: list[tuple[str, float]] = []
    try:
        async with session.post(HELIUS_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return transfers
            tx_res = await r.json()
        tx = tx_res.get("result")
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            return transfers
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        if not keys:
            return transfers
        signer = keys[0].get("pubkey") if isinstance(keys[0], dict) else keys[0]
        if signer != operator:
            return transfers
        meta = tx["meta"]
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        for i, k in enumerate(keys[1:], start=1):
            addr = k.get("pubkey") if isinstance(k, dict) else k
            if not isinstance(addr, str) or i >= len(pre):
                continue
            delta = (post[i] - pre[i]) / 1e9
            if _CREATOR_ALPHA_MIN_FUND_SOL <= delta <= _CREATOR_ALPHA_MAX_FUND_SOL:
                transfers.append((addr, delta))
    except Exception:
        pass
    return transfers


async def creator_alpha_scout_loop(runtime: Any, session: aiohttp.ClientSession) -> None:
    """Poll direct-creator and operator wallets for early launch signals.

    Each cycle (60s):
      1. For each direct creator: fetch new sigs, detect pump.fun creates →
         add mint to pending list
      2. For each operator: fetch new sigs, detect 0.5-2.0 SOL outgoing
         transfers → add recipient to watched_children (90min TTL)
      3. For each watched child: poll for pump.fun creates → add mint to
         pending list, stop watching
      4. For each pending mint: query DexScreener; if pump-amm pair exists,
         push to fresh_grad_queue (lifecycle scout enters on next cycle)

    RPC budget: ~25k calls/day across all watched wallets at 60s cadence.
    """
    direct, operators = _creator_alpha_tracked_wallets()
    if not direct and not operators:
        print("[creator-alpha] playbook empty — scout idle")
        return

    print(f"[creator-alpha] starting · direct_creators={len(direct)} · operators={len(operators)} · "
          f"poll={_CREATOR_ALPHA_POLL_SECS}s · child_ttl={_CREATOR_ALPHA_CHILD_TTL_SECS//60}min")

    # Warm cache — set last_sig for each wallet so first cycle doesn't fire
    # historical signals
    for w in direct + operators:
        await _creator_alpha_poll_recent(session, w, limit=1)
        await asyncio.sleep(0.05)

    cycle = 0
    while True:
        try:
            cycle += 1
            now = time.time()

            # 1. Direct creators
            for w in direct:
                new = await _creator_alpha_poll_recent(session, w)
                for s in new:
                    mint = await _creator_alpha_detect_create(session, s["signature"], w)
                    if mint and mint not in _creator_alpha_pending_mints:
                        rec = {"detected_ts": now, "source": "creator_alpha_direct",
                               "creator": w, "parent_op": None, "mint": mint}
                        _creator_alpha_pending_mints[mint] = rec
                        _creator_alpha_recent_signals.append({**rec, "kind": "direct_create"})
                        print(f"[creator-alpha] 🎯 DIRECT-CREATE creator={w[:10]} mint={mint[:14]}")
                        # DIRECT-ENTRY path: fire bonding-curve buy NOW
                        if CREATOR_ALPHA_DIRECT_ENTRY:
                            asyncio.create_task(_creator_alpha_direct_entry(
                                runtime, session, mint,
                                "creator_alpha_direct", w, None,
                            ))
                await asyncio.sleep(0.04)

            # 2. Operators → spawn child watchers
            for op in operators:
                new = await _creator_alpha_poll_recent(session, op)
                for s in new:
                    transfers = await _creator_alpha_detect_funding(session, s["signature"], op)
                    for child, amt in transfers:
                        if child in _creator_alpha_watched_children:
                            continue
                        if child in direct or child in operators:
                            continue
                        _creator_alpha_watched_children[child] = {
                            "parent": op, "funded_ts": now, "amount_sol": amt,
                        }
                        # NOTE: do NOT warm-cache the sig cursor here. If we did,
                        # we'd suppress legitimate creates that happened within
                        # seconds of (or even just before) the funding tx — the
                        # exact race condition we WANT to catch. Stale-create
                        # protection lives in the blockTime check below instead.
                        _creator_alpha_recent_signals.append({
                            "kind": "operator_fund", "parent": op, "child": child,
                            "amount_sol": amt, "ts": now,
                        })
                        print(f"[creator-alpha] 👁 OPERATOR-FUND parent={op[:10]} → child={child[:10]} "
                              f"({amt:.2f} SOL) — watching {_CREATOR_ALPHA_CHILD_TTL_SECS//60}min")
                await asyncio.sleep(0.04)

            # 3. Watched children — check for creates
            stale_children: list[str] = []
            for child, info in list(_creator_alpha_watched_children.items()):
                if now - info["funded_ts"] > _CREATOR_ALPHA_CHILD_TTL_SECS:
                    stale_children.append(child)
                    continue
                new = await _creator_alpha_poll_recent(session, child)
                for s in new:
                    # Defense-in-depth: if this sig's blockTime predates our
                    # funding event by > 60s, it's a stale historical sig that
                    # slipped through — ignore (we don't want to enter on a
                    # token created before we started watching).
                    sig_ts = s.get("blockTime") or 0
                    if sig_ts and sig_ts < (info["funded_ts"] - 60):
                        print(f"[creator-alpha] ⏭ stale-create skip: sig from "
                              f"{int(info['funded_ts'] - sig_ts)}s before funding")
                        continue
                    mint = await _creator_alpha_detect_create(session, s["signature"], child)
                    if mint and mint not in _creator_alpha_pending_mints:
                        rec = {"detected_ts": now, "source": "creator_alpha_operator",
                               "creator": child, "parent_op": info["parent"], "mint": mint}
                        _creator_alpha_pending_mints[mint] = rec
                        _creator_alpha_recent_signals.append({**rec, "kind": "operator_create"})
                        print(f"[creator-alpha] 🎯 OPERATOR-CREATE parent={info['parent'][:10]} "
                              f"creator={child[:10]} mint={mint[:14]}")
                        stale_children.append(child)
                        # DIRECT-ENTRY path: fire bonding-curve buy NOW
                        if CREATOR_ALPHA_DIRECT_ENTRY:
                            asyncio.create_task(_creator_alpha_direct_entry(
                                runtime, session, mint,
                                "creator_alpha_operator", child, info["parent"],
                            ))
                await asyncio.sleep(0.04)
            for c in stale_children:
                _creator_alpha_watched_children.pop(c, None)

            # 4. Pending mints → resolve graduation status
            stale_mints: list[str] = []
            for mint, pinfo in list(_creator_alpha_pending_mints.items()):
                if now - pinfo["detected_ts"] > _CREATOR_ALPHA_MINT_TTL_SECS:
                    print(f"[creator-alpha] ⏱ aged-out mint={mint[:14]} (no graduation in 60min)")
                    stale_mints.append(mint)
                    continue
                try:
                    async with session.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        if r.status != 200:
                            continue
                        d = await r.json()
                    pairs = d.get("pairs") or []
                    ps = next((p for p in pairs
                               if (p.get("dexId") or "").lower() in ("pumpswap", "pump-amm")), None)
                    if ps:
                        pool = ps.get("pairAddress") or ""
                        on_chain_ts = (ps.get("pairCreatedAt") or 0) / 1000 if ps.get("pairCreatedAt") else None
                        push_fresh_grad(mint, pool, on_chain_ts=on_chain_ts)
                        # ALSO mark as priority — lifecycle scout will use
                        # relaxed gates for this mint on its next cycle.
                        mark_creator_alpha_priority(
                            mint=mint,
                            source=pinfo["source"],
                            creator=pinfo.get("creator"),
                            parent_op=pinfo.get("parent_op"),
                        )
                        _creator_alpha_recent_signals.append({
                            "kind": "graduated", "mint": mint, "source": pinfo["source"],
                            "creator": pinfo.get("creator"), "parent_op": pinfo.get("parent_op"),
                            "ts": now, "lag_secs": int(now - pinfo["detected_ts"]),
                        })
                        print(f"[creator-alpha] 🚀 GRADUATED mint={mint[:14]} pool={pool[:14]} "
                              f"src={pinfo['source']} lag={int(now - pinfo['detected_ts'])}s "
                              f"— pushed to fast-path with PRIORITY (relaxed gates)")
                        stale_mints.append(mint)
                except Exception:
                    pass
            for m in stale_mints:
                _creator_alpha_pending_mints.pop(m, None)

            # Summary every 5 cycles
            if cycle % 5 == 0:
                print(f"[creator-alpha] cycle {cycle}: tracked={len(direct)+len(operators)} "
                      f"watched_children={len(_creator_alpha_watched_children)} "
                      f"pending_mints={len(_creator_alpha_pending_mints)}")

        except Exception as e:
            print(f"[creator-alpha] loop error: {e}")
        await asyncio.sleep(_CREATOR_ALPHA_POLL_SECS)


# ─── Lifecycle bounce-watchlist ─────────────────────────────────────────
# Mints that pass all baseline filters but have a bad entry shape (m5 deeply
# negative, h1 high + rolling over, m5 overheated) are deferred here instead
# of skipped. Each cycle we append a snapshot and re-evaluate; once the shape
# turns "good" AND we see a confirmed bounce off the local low, we enter.
# 2026-04-28 user-requested: SCAMDEX entered at m5=-7.5%/h1=+70% (post-peak
# rollover) and rugged in 27s. The chart later formed a higher low and ran
# back up — that's the entry we should have caught.
_lifecycle_deferred: dict[str, dict] = {}  # mint → {"first_seen": ts, "snapshots": [...]}

# 24h rolling window of cycle-level reject counters — surfaced in the dashboard
# as a histogram so we can see WHY the scout is rejecting candidates without
# tailing logs. Each entry is {ts, candidates, in_age, baseline, watchlisted,
# entered, rejects: {age:N, liq:N, ...}}. At 30s cadence, 24h = 2880 entries
# ≈ 600KB on disk; deque trims oldest automatically.
_REJECT_HISTORY_PATH = Path(__file__).parent / "lifecycle_rejects_24h.json"
_DAILY_SUMMARY_PATH  = Path(__file__).parent / "lifecycle_daily_summaries.json"
_reject_history: deque = deque(maxlen=2880)
_last_daily_snapshot_date: str = ""  # "YYYY-MM-DD" of last written summary
try:
    if _REJECT_HISTORY_PATH.exists():
        with open(_REJECT_HISTORY_PATH) as _f:
            _loaded = json.load(_f)
            if isinstance(_loaded, list):
                _reject_history.extend(_loaded[-2880:])
except Exception:
    pass


def _maybe_write_daily_summary() -> None:
    """At day rollover, append yesterday's aggregate stats to the permanent
    lifecycle_daily_summaries.json. Called from _persist_reject_snapshot so it
    fires automatically without needing a cron job."""
    global _last_daily_snapshot_date
    import datetime as _dt
    today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    if _last_daily_snapshot_date == today or not _reject_history:
        return
    # Only write if we have a previous date to summarise (not first run of the day)
    if _last_daily_snapshot_date and _last_daily_snapshot_date != today:
        try:
            snaps = list(_reject_history)
            totals: dict[str, int] = {}
            for s in snaps:
                for k, v in (s.get("rejects") or {}).items():
                    totals[k] = totals.get(k, 0) + int(v)
            total_candidates = sum(s.get("candidates", 0) for s in snaps)
            total_entered    = sum(s.get("entered", 0)    for s in snaps)
            summary = {
                "date":              _last_daily_snapshot_date,
                "cycles":            len(snaps),
                "candidates_total":  total_candidates,
                "entered_total":     total_entered,
                "pass_rate_pct":     round(total_entered / max(total_candidates, 1) * 100, 3),
                "reject_totals":     totals,
                "avg_rej_per_cycle": round(sum(totals.values()) / max(len(snaps), 1), 1),
            }
            existing: list = []
            if _DAILY_SUMMARY_PATH.exists():
                with open(_DAILY_SUMMARY_PATH) as _df:
                    existing = json.load(_df)
            # Avoid duplicate dates
            existing = [e for e in existing if e.get("date") != _last_daily_snapshot_date]
            existing.append(summary)
            with open(_DAILY_SUMMARY_PATH, "w") as _df:
                json.dump(existing, _df, indent=2)
            print(f"[lifecycle] 📊 daily summary saved: {_last_daily_snapshot_date} "
                  f"— {total_entered} entries / {total_candidates} candidates "
                  f"({summary['pass_rate_pct']:.2f}% pass rate)")
        except Exception as _dse:
            print(f"[lifecycle] daily summary error: {_dse}")
    _last_daily_snapshot_date = today


def _persist_reject_snapshot(snapshot: dict) -> None:
    """Append a cycle snapshot to the rolling 24h window, flush to disk,
    and trigger a daily summary at day rollover."""
    _maybe_write_daily_summary()
    _reject_history.append(snapshot)
    try:
        with open(_REJECT_HISTORY_PATH, "w") as f:
            json.dump(list(_reject_history), f)
    except Exception:
        pass

LIFECYCLE_WATCHLIST_TTL_SECS    = 10 * 60 * 60   # drop deferred mints after 10h — monsters take time
LIFECYCLE_WATCHLIST_MAX_AGE_SECS = 10 * 60 * 60  # watchlisted tokens get 10h age cap (vs 12h for direct)
LIFECYCLE_SNAPSHOT_WINDOW_SECS   = 30 * 60   # keep last 30min of price snapshots per mint
# Tiered bounce minimums — higher liquidity = more committed capital = less bounce proof needed.
# Validated against trade history:
#   Diamond $38K liq → tier 3 (4%) → +4.3% bounce PASSES  (ran to +344%)
#   MAGA    $19.7K liq → tier 1 (8%) → +3.6% bounce BLOCKED (was noise)
#   LADA    $21K liq → tier 1 (8%) → +21% bounce PASSES    (ran +11,446%)
#   USP     $27K liq → tier 2 (6%) → +69% bounce PASSES    (ran +68%)
LIFECYCLE_BOUNCE_MIN_PCT_LOW     = 8.0       # liq < $25K  — thin pool, need strong bounce signal
LIFECYCLE_BOUNCE_MIN_PCT_MID     = 6.0       # liq $25K-35K — moderate commitment
LIFECYCLE_BOUNCE_MIN_PCT_HIGH    = 4.0       # liq > $35K  — high commitment, less proof needed
LIFECYCLE_BOUNCE_MIN_PCT         = 8.0       # default (used as fallback in old code paths)
LIFECYCLE_M5_GOOD_LOW            = -5.0      # m5 in [-5%, +12%] = clean entry shape
LIFECYCLE_M5_GOOD_HIGH           = 10.0      # monster DNA: quiet entry ≤ +10%, NOT already pumping when we buy
LIFECYCLE_POSTPEAK_H1_THRESHOLD  = 55.0      # raised from 30 — h1=30-55% with flat m5 is consolidation after moderate move,
                                              # not necessarily rolling over. MEMEART h1=43% was still heading higher.
LIFECYCLE_MAX_M5_VOL_LIQ         = 3.0       # vol_m5/liq cap — same gate breakout scout uses (CCP 2026-04-30: vol/liq=4x flagged WASH on raydium-scout but bypassed on lifecycle_bounce → bought a wash-driven dead-cat bounce, -10%)

# ── Bounce-watchlist entry gates (calibrated 2026-05-01 from on-chain swap data
# of CCP/TRUTH/FOFAR + 23-trade monster outcome history). All apply only when
# the bounce is firing from the watchlist (on_watch=True). CCP failed on FOUR
# of these gates simultaneously; both winners (TRUTH +21%, FOFAR +21%) pass all.
LIFECYCLE_BOUNCE_H1_MIN_PCT          = 0.0   # h1 must be net positive at bounce-confirm (winners +5 to +80%; CCP -2.6%)
LIFECYCLE_BOUNCE_TOP10_MAX_PCT       = 13.5  # winner ceiling 12.53%; CCP 15.95%
LIFECYCLE_BOUNCE_TOP1_MAX_PCT        = 5.0   # raised from 2.0 — 2.0 was from 2 trades only; 3.3-3.4% blocked real bounces (26vtHb7b, FGijtUZ5 2026-05-07)
LIFECYCLE_BOUNCE_M5_BUYS_MIN         = 20    # winners had 28-66 m5 buyers; CCP had 11
LIFECYCLE_BOUNCE_M5_BUY_SELL_RATIO   = 1.5   # winners 1.66x to 9.3x; CCP 0.85x (more sells than buys = distribution pretending to be a bounce)

# ── Viral-momentum override (catches early-pump fresh-grads) ───────────────
# 5-token on-chain backtest (CCP, FOFAR, EVA had viral signatures; TRUTH, RC
# did not). For all 3 viral tokens TP+20% would have hit in the SAME minute
# the override fires. CCP would convert from -10% loss → +20% win. Slow
# tokens (TRUTH, RC) never reach 50 unique buyers in 30min, so override
# stays off and bounce path catches them later. Survivorship bias: we have
# zero data on viral-fire-then-rug — the -25% SL is the backstop.
LIFECYCLE_VIRAL_ENABLED = False           # 2026-05-02 — DISABLED.
                                          # 7-day data: 0 wins / 5 trades / -0.250 SOL.
                                          # The override fires when m5_buys spikes — but
                                          # that spike happens AT the FOMO top of these
                                          # tokens (DWOGE/PETS/NKT/ROME post-mortems), not
                                          # at the breakout. To re-enable, flip to True;
                                          # consider raising threshold to 100+ first.
LIFECYCLE_VIRAL_OVERRIDE_BUYERS = 50      # DexScreener m5.buys txn count threshold (when ENABLED)
LIFECYCLE_VIRAL_BS_MIN          = 1.5     # buy-dominant pressure (m5_buys / m5_sells)
LIFECYCLE_VIRAL_H1_MAX_PCT      = 600.0   # cap relaxation when override fires (vs normal 100)
LIFECYCLE_VIRAL_AGE_MIN_SECS    = 10 * 60 # drop the 20-min age floor to 10 when viral


def _lifecycle_record_snapshot(mint: str, price: float, m5: float, h1: float, liq: float) -> None:
    now = time.time()
    entry = _lifecycle_deferred.setdefault(mint, {"first_seen": now, "snapshots": []})
    entry["snapshots"].append({"ts": now, "price": price, "m5": m5, "h1": h1, "liq": liq})
    cutoff = now - LIFECYCLE_SNAPSHOT_WINDOW_SECS
    entry["snapshots"] = [s for s in entry["snapshots"] if s["ts"] >= cutoff]
    # Track highest h1 ever seen for this mint while in watchlist.
    # Used to block bounce entries on tokens that already ran hard — if it
    # ever hit h1 ≥ 50%, the big move already happened. We're late.
    if h1 > entry.get("max_h1_seen", 0.0):
        entry["max_h1_seen"] = h1


def _lifecycle_local_low_price(mint: str) -> float | None:
    snaps = (_lifecycle_deferred.get(mint) or {}).get("snapshots") or []
    prices = [s["price"] for s in snaps if s.get("price", 0) > 0]
    return min(prices) if prices else None


def _lifecycle_bounce_confirmed(mint: str, current_price: float, current_m5: float, current_liq: float) -> tuple[bool, float]:
    """Returns (confirmed, bounce_pct). A real bounce needs:
       - price ≥+LIFECYCLE_BOUNCE_MIN_PCT% above the lowest price we've seen
       - m5 currently positive (momentum has flipped, not just an oscillation)
       - liquidity stable or growing through the bounce (real buyers, not thin-book ramp)
    """
    low = _lifecycle_local_low_price(mint)
    if not low or low <= 0:
        return False, 0.0
    bounce_pct = (current_price - low) / low * 100.0
    # Tiered bounce minimum based on liquidity depth
    if current_liq >= 35_000:
        min_bounce = LIFECYCLE_BOUNCE_MIN_PCT_HIGH   # 4% — high commitment pool
    elif current_liq >= 25_000:
        min_bounce = LIFECYCLE_BOUNCE_MIN_PCT_MID    # 6% — moderate pool
    else:
        min_bounce = LIFECYCLE_BOUNCE_MIN_PCT_LOW    # 8% — thin pool, needs strong signal
    if bounce_pct < min_bounce:
        return False, bounce_pct
    if current_m5 < 0:
        return False, bounce_pct
    snaps = (_lifecycle_deferred.get(mint) or {}).get("snapshots") or []
    if snaps:
        first_liq = snaps[0].get("liq") or 0
        if first_liq > 0 and current_liq < first_liq * 0.85:
            # liquidity dropped >15% since we started watching = LP draining, not a healthy bounce
            return False, bounce_pct
    return True, bounce_pct


def _lifecycle_cleanup_deferred() -> None:
    now = time.time()
    expired = [m for m, e in _lifecycle_deferred.items()
               if (now - e.get("first_seen", now)) > LIFECYCLE_WATCHLIST_TTL_SECS]
    for m in expired:
        _lifecycle_deferred.pop(m, None)


def _lifecycle_classify_entry_shape(m5: float, h1: float,
                                     first_seen: float = 0.0) -> tuple[str, str]:
    """Returns (shape, reason). shape ∈ {good, pullback, postpeak, overheated}.

    Staircase detection: tokens that have been in the watchlist for 60+ minutes
    without dumping are confirmed grinders. Allow m5 up to +20% (vs +12% for
    standard entries) — they are always mid-step on the staircase, never truly
    quiet. DUST is the reference case.
    """
    import time as _t
    _minutes_watched = (_t.time() - first_seen) / 60 if first_seen > 0 else 0
    _is_staircase = _minutes_watched >= 60
    _m5_ceiling = 20.0 if _is_staircase else LIFECYCLE_M5_GOOD_HIGH

    if m5 > _m5_ceiling:
        tag = "staircase-overheated" if _is_staircase else "overheated"
        return tag, (f"m5=+{m5:.1f}% > +{_m5_ceiling:.0f}% — "
                     f"{'staircase grinder still hot' if _is_staircase else 'mid-spike chase'}")
    if m5 < LIFECYCLE_M5_GOOD_LOW:
        return "pullback", f"m5={m5:.1f}% < {LIFECYCLE_M5_GOOD_LOW:.0f}% — actively falling"
    if h1 >= LIFECYCLE_POSTPEAK_H1_THRESHOLD and m5 <= 0:
        return "postpeak", f"h1=+{h1:.0f}% with m5={m5:.1f}% — rollover from peak"
    # Range position: h1 is elevated (token already ran hard in the last hour)
    # AND m5 still positive = we're entering near the TOP of the h1 move.
    # Defer to watchlist — wait for the pullback before entering.
    # ALIEN/alein post-mortem 2026-05-08: entered at h1=+70-400% while m5>0.
    if h1 >= 50.0 and m5 > 2.0:
        return "overheated", (f"h1=+{h1:.0f}% elevated, m5=+{m5:.1f}% still rising "
                              f"— near top of h1 move, wait for pullback")
    return "good", f"m5={m5:.1f}%, h1={h1:.0f}%"


# ─── Zombie token detection ──────────────────────────────────────────────
# A "zombie" token is an old mint being re-pumped. The pair on pump-amm looks
# fresh (passes age 20-240min gate) but the underlying mint existed on other
# DEXes months/years earlier. Classic pump-and-dump setup.
# Detection: fetch ALL DexScreener pairs for the mint. If the OLDEST pair is
# more than LIFECYCLE_ZOMBIE_MAX_PAIR_AGE_SECS old, reject.
# ALIEN (2024 vintage re-pumped 2026-05-08) would have failed this check.
LIFECYCLE_ZOMBIE_MAX_PAIR_AGE_SECS = 3 * 24 * 3600  # 3 days
_zombie_cache: dict[str, bool] = {}  # mint → is_zombie (TTL via bot restart)


async def _is_zombie_token(session: aiohttp.ClientSession, mint: str) -> bool:
    """Returns True if any DEX pair for this mint is older than 3 days."""
    if mint in _zombie_cache:
        return _zombie_cache[mint]
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return False  # can't check → fail open
            d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return False
            oldest_ms = min(
                (p.get("pairCreatedAt") or (time.time() * 1000))
                for p in pairs
            )
            age_secs = time.time() - oldest_ms / 1000
            result = age_secs > LIFECYCLE_ZOMBIE_MAX_PAIR_AGE_SECS
            _zombie_cache[mint] = result
            if result:
                age_days = age_secs / 86400
                oldest_dex = next(
                    (p.get("dexId", "?") for p in pairs
                     if (p.get("pairCreatedAt") or 0) == oldest_ms), "?"
                )
                print(f"[monster-lifecycle] 🧟 {mint[:8]} zombie — oldest pair "
                      f"{age_days:.0f}d old on {oldest_dex} — classic re-pump, skip")
            return result
    except Exception:
        return False  # error → fail open


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


def _buy_velocity_ratio(p: dict) -> float | None:
    """Ratio of last-1h buy count to prior 5h average buy rate.

    >=1.0 means recent buy pressure is keeping pace with or accelerating
    past the established trend; <1.0 means it's decelerating (entering
    into fading momentum). None when h6 data is missing or h1 == h6.
    """
    txns = p.get("txns") or {}
    h1 = txns.get("h1") or {}
    h6 = txns.get("h6") or {}
    try:
        b1 = float(h1.get("buys") or 0)
        b6 = float(h6.get("buys") or 0)
    except (ValueError, TypeError):
        return None
    if b6 <= b1 or b1 < 1:
        return None
    prior_per_hour = (b6 - b1) / 5.0
    if prior_per_hour <= 0:
        return None
    return round(b1 / prior_per_hour, 3)


_burned_creators_cache: dict[str, tuple[float, set[str]]] = {}
_BURNED_CACHE_TTL_SECS = 300


def _load_burned_creators() -> set[str]:
    """Set of creator wallets with any monster close ≤ threshold inside window.

    Cached for 5 min so we don't reread monster_closed_trades.json on every
    scout tick. Scout signals fire on the order of seconds-to-minutes — 5min
    cache is plenty fresh. Returns empty set if the file or 'creator' field
    is missing on records (common when older trades pre-date this feature).
    """
    now = time.time()
    cached = _burned_creators_cache.get("k")
    if cached and (now - cached[0]) < _BURNED_CACHE_TTL_SECS:
        return cached[1]

    burned: set[str] = set()
    cutoff = now - CREATOR_BURN_WINDOW_SECS
    closed_path = BASE / "monster_closed_trades.json"
    try:
        if closed_path.exists():
            for t in json.loads(closed_path.read_text()) or []:
                if not isinstance(t, dict):
                    continue
                if float(t.get("close_ts") or 0) < cutoff:
                    continue
                if float(t.get("final_pnl_pct") or 0) > CREATOR_BURN_LOSS_THRESHOLD_PCT:
                    continue
                creator = (t.get("metadata") or {}).get("creator") or t.get("creator")
                if creator:
                    burned.add(creator)
    except Exception:
        pass

    _burned_creators_cache["k"] = (now, burned)
    return burned


async def _fetch_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    """Best-effort creator-wallet lookup via Helius DAS getAsset.

    Returns None on RPC error or when the asset has no creator metadata.
    Cheap (~one RPC call) but adds ~100-300ms per scout signal.
    """
    helius_key = os.getenv("HELIUS_API_KEY", "")
    url = (
        f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        if helius_key else "https://api.mainnet-beta.solana.com"
    )
    try:
        async with session.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": [mint]},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            creators = ((data.get("result") or {}).get("creators")) or []
            if not creators:
                return None
            for c in creators:
                if c.get("verified"):
                    return c.get("address")
            return creators[0].get("address")
    except Exception:
        return None


async def is_creator_burned(session: aiohttp.ClientSession, mint: str) -> tuple[bool, str | None]:
    """Return (True, creator) if the mint's creator recently lost us money."""
    creator = await _fetch_creator(session, mint)
    if not creator:
        return False, None
    return (creator in _load_burned_creators()), creator


async def _try_smart_money_overlap(session: aiohttp.ClientSession, mint: str) -> int | None:
    """Best-effort smart-money overlap count for scout metadata.

    Returns None on any failure (roster empty, pool missing, Helius down) —
    callers treat None as missing data, not "bad signal". Kept defensive so
    a smart-money lookup failure can never kill a scout signal.
    """
    try:
        from elizaos.plugins.solana.holder_guard.smart_money import count_for_mint
        return await count_for_mint(session, mint)
    except Exception:
        return None


async def top_wallet_distribution(session: aiohttp.ClientSession, mint: str) -> dict | None:
    """Return distribution stats for the top non-pool holders.

    Returns {"top1_pct": X, "top10_pct": Y, "wallets_scanned": N} or None.
    Uses the same RPC data as top1_wallet_pct but keeps all qualifying wallets
    so we can compute the top-10 aggregate — a much richer signal than top-1
    alone. Low top-10 (<25%) means holders are broadly distributed; high top-10
    (>60%) means concentrated.
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

    tas: list[tuple[str, float]] = []
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

    owners = [w for w, _ in tas]
    info = await _rpc(session, "getMultipleAccounts",
                      [owners, {"encoding": "base64"}])
    infos = ((info.get("result") or {}).get("value") or [])

    real_wallets: list[tuple[str, float]] = []
    for (wallet, bal), w_info in zip(tas, infos, strict=False):
        owner_prog = (w_info or {}).get("owner") if w_info else SYSTEM_PROGRAM
        if owner_prog == SYSTEM_PROGRAM:
            real_wallets.append((wallet, bal))

    if not real_wallets or total <= 0:
        return None

    real_wallets.sort(key=lambda x: x[1], reverse=True)
    top1_bal = real_wallets[0][1]
    top10_bal = sum(b for _, b in real_wallets[:10])
    return {
        "top1_pct": round(top1_bal / total * 100, 3),
        "top10_pct": round(top10_bal / total * 100, 2),
        "wallets_scanned": len(real_wallets),
    }


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


async def pct_off_recent_peak(
    session: aiohttp.ClientSession,
    pair_address: str,
    lookback_minutes: int = 60,
) -> float | None:
    """Return (current / max(last_N_min)) as a 0..1 ratio. None on fetch failure.

    Uses GeckoTerminal's free OHLCV endpoint (no API key). Aggregates 1-minute
    candles across the last `lookback_minutes` and returns the ratio of the
    latest close to the window peak. Scouts use this to reject entries
    currently far off the recent high (post-peak rollback).
    """
    try:
        limit = max(lookback_minutes, 10)
        url = (
            f"https://api.geckoterminal.com/api/v2/networks/solana/pools/"
            f"{pair_address}/ohlcv/minute?aggregate=1&limit={limit}"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return None
            d = await r.json()
        # OHLCV rows: [ts, open, high, low, close, volume]
        rows = (d.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not rows:
            return None
        highs = [float(row[2]) for row in rows if len(row) >= 5]
        closes = [float(row[4]) for row in rows if len(row) >= 5]
        if not highs or not closes:
            return None
        peak = max(highs)
        current = closes[0]  # GeckoTerminal returns newest-first
        if peak <= 0:
            return None
        return current / peak
    except Exception:
        return None


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
                # Market-state guard — cluster wallets can buy absurd tops.
                # Without these checks we entered CATEROID-class tokens after
                # 10,000%+ pumps (2026-04-22 root-cause investigation).
                pairs = await _dex_pairs(session, mint)
                best = _best_pair(pairs)
                if not best:
                    print(f"[monster-cluster] 🚫 {mint[:8]} — no DexScreener pair yet, defer")
                    continue
                _liq_usd = float((best.get("liquidity") or {}).get("usd") or 0)
                _mc_usd  = float(best.get("marketCap") or best.get("fdv") or 0)
                _pc      = best.get("priceChange") or {}
                _h1      = float(_pc.get("h1")  or 0)
                _h6      = float(_pc.get("h6")  or 0)
                _h24     = float(_pc.get("h24") or 0)
                _info    = best.get("info") or {}
                try:
                    _holders_at_entry = int(_info.get("holders")) if _info.get("holders") is not None else None
                except (ValueError, TypeError):
                    _holders_at_entry = None
                _pca     = best.get("pairCreatedAt")
                try:
                    _age_secs = (time.time() - float(_pca) / 1000.0) if _pca else None
                except (ValueError, TypeError):
                    _age_secs = None
                _snap = {
                    "liq_usd": _liq_usd, "mc_usd": _mc_usd,
                    "h1": _h1, "h6": _h6, "h24": _h24,
                    "age_min": round(_age_secs / 60, 1) if _age_secs else None,
                }
                # Hard liquidity floor — a $5k pool rugs on our exit alone
                if _liq_usd < 20_000:
                    print(f"[monster-cluster] 🚫 {mint[:8]} liq=${_liq_usd:.0f} <$20k — skip")
                    _log_reject("cluster_confirm", mint, "liq_floor", _snap,
                                filter_name="liq_usd", filter_value=_liq_usd, threshold=20_000)
                    continue
                # Hard age cap — monster is the FRESH-pump lane. unc 2026-04-22
                # fired at age 5.8 DAYS with h1=-4.8% h6=-8.8% (cluster wallets
                # accumulating a mature $8M mcap token that pumped a week ago).
                # Cap at 24h; other scouts cover the <12h sub-ranges.
                if _age_secs and _age_secs > 24 * 3600:
                    print(f"[monster-cluster] 🚫 {mint[:8]} age={_age_secs/3600:.0f}h >24h — mature token, skip")
                    _log_reject("cluster_confirm", mint, "age_cap", _snap,
                                filter_name="age_hours", filter_value=round(_age_secs/3600, 1), threshold=24)
                    continue
                # Don't buy into a falling candle. Cluster wallets accumulating
                # weakness rarely mean-revert fast enough for our 30% TP1.
                # Allow tiny dips (-2%) as entry-noise tolerance.
                if _h1 < -2.0:
                    print(f"[monster-cluster] 🚫 {mint[:8]} h1={_h1:+.1f}% — falling into entry, skip")
                    _log_reject("cluster_confirm", mint, "falling_candle", _snap,
                                filter_name="h1_change", filter_value=_h1, threshold=-2.0)
                    continue
                # Mcap velocity filter (same rationale as breakout_candle)
                if _age_secs and _age_secs > 0 and _age_secs < 3 * 3600 and _mc_usd > 0:
                    _velocity = _mc_usd / (_age_secs / 60)
                    if _velocity > 5_000:
                        print(f"[monster-cluster] 🚫 {mint[:8]} mc-velocity ${_velocity:.0f}/min >$5k — second-wave")
                        _log_reject("cluster_confirm", mint, "mcap_velocity", _snap,
                                    filter_name="mcap_velocity", filter_value=round(_velocity), threshold=5000)
                        continue
                # Already-pumped cap: skip if h1 > 200% (we'd be chasing the top)
                if _h1 > 200.0:
                    print(f"[monster-cluster] 🚫 {mint[:8]} h1=+{_h1:.0f}% — already blown past")
                    _log_reject("cluster_confirm", mint, "h1_blown", _snap,
                                filter_name="h1_change", filter_value=_h1, threshold=200)
                    continue
                # Dying-momentum: h6 >> h1 means peak was hours ago
                if _h1 > 0 and _h6 > _h1 * 5:
                    print(f"[monster-cluster] 🚫 {mint[:8]} h6/h1={_h6 / _h1:.1f} — post-peak rollback")
                    _log_reject("cluster_confirm", mint, "h6_h1_dying", _snap,
                                filter_name="h6_h1_ratio", filter_value=round(_h6/_h1, 1), threshold=5)
                    continue
                # Real peak-distance check via GeckoTerminal candles
                _pair_addr = best.get("pairAddress")
                _peak_ratio_cluster: float | None = None
                if _pair_addr:
                    _peak_ratio = await pct_off_recent_peak(session, _pair_addr, 60)
                    if _peak_ratio is not None and _peak_ratio < 0.85:
                        print(f"[monster-cluster] 🚫 {mint[:8]} at {_peak_ratio*100:.0f}% of 60m peak — rollback")
                        _log_reject("cluster_confirm", mint, "peak_distance", _snap,
                                    filter_name="peak_ratio_60m", filter_value=round(_peak_ratio, 2), threshold=0.85)
                        continue
                    _peak_ratio_cluster = _peak_ratio
                # Buy velocity check — recent 1h vs prior 5h average.
                _cl_velocity = _buy_velocity_ratio(best)
                if _cl_velocity is not None and _cl_velocity < MONSTER_BUY_VELOCITY_MIN_RATIO:
                    print(f"[monster-cluster] 🚫 {mint[:8]} buy_velocity={_cl_velocity:.2f} < {MONSTER_BUY_VELOCITY_MIN_RATIO} — momentum decelerating")
                    _log_reject("cluster_confirm", mint, "buy_velocity", _snap,
                                filter_name="buy_velocity_ratio", filter_value=_cl_velocity, threshold=MONSTER_BUY_VELOCITY_MIN_RATIO)
                    continue
                # Compute top-1 + top-10 wallet distribution (free via Helius).
                # Low top10% (<25%) = broadly distributed = runner shape;
                # high top10% (>60%) = concentrated = rug risk.
                _cluster_dist = await top_wallet_distribution(session, mint)
                _top1 = (_cluster_dist or {}).get("top1_pct")
                _top10 = (_cluster_dist or {}).get("top10_pct")
                if _top10 is not None and _top10 >= MONSTER_TOP10_MAX_PCT:
                    print(f"[monster-cluster] 🚫 {mint[:8]} top10={_top10}% ≥ {MONSTER_TOP10_MAX_PCT}% — insiders hold exit liquidity, skip")
                    _log_reject("cluster_confirm", mint, "top10_concentration", _snap,
                                filter_name="top10_pct", filter_value=_top10, threshold=MONSTER_TOP10_MAX_PCT)
                    continue
                # Creator burn check — skip if a recent monster trade by this
                # creator lost us money.
                _cl_burned, _cl_creator = await is_creator_burned(session, mint)
                if _cl_burned:
                    print(f"[monster-cluster] 🚫 {mint[:8]} creator={_cl_creator[:8]} recently burned us — skip")
                    _log_reject("cluster_confirm", mint, "creator_burned", _snap,
                                filter_name="creator", filter_value=_cl_creator)
                    continue

                # Rugcheck safety gate — unlocked LP, bundled ownership, etc.
                try:
                    from elizaos.plugins.solana.axiom_copy_trader import _quick_safety_check
                    _cl_rc_safe, _cl_rc_reason = await _quick_safety_check(mint, session)
                    if not _cl_rc_safe:
                        print(f"[monster-cluster] 🛡 rugcheck block {mint[:8]}: {_cl_rc_reason}")
                        _log_reject("cluster_confirm", mint, "rugcheck", _snap,
                                    filter_name="rugcheck", filter_value=_cl_rc_reason)
                        continue
                except Exception as _cl_rc_err:
                    print(f"[monster-cluster] rugcheck import error: {_cl_rc_err} — allowing")
                print(f"[monster-cluster] 🎯 {len(ws)} cluster wallets on {mint[:8]} — firing (liq=${_liq_usd:.0f} mc=${_mc_usd:.0f} h1=+{_h1:.0f}% top1={_top1}% top10={_top10}%)")
                _log_signal({
                    "source": "cluster_confirm",
                    "mint": mint,
                    "cluster_wallets": list(ws.keys()),
                    "window_first_buy_ts": min(ws.values()),
                    "liq_usd": _liq_usd,
                    "mc_usd":  _mc_usd,
                    "h1":      _h1,
                    "h6":      _h6,
                    "h24":     _h24,
                    "age_min": round(_age_secs / 60, 1) if _age_secs else None,
                })
                if not monster.can_open_new_position():
                    print("[monster-cluster] slot pool full — logged only")
                    continue
                sym = (best.get("baseToken") or {}).get("symbol")
                await monster.open_monster_position(
                    mint=mint,
                    token_name=sym or mint[:8],
                    signal_source="cluster_confirm",
                    sol_size=monster.get_default_size_sol(),
                    session=session,
                    runtime=runtime,
                    metadata={
                        "cluster_wallets": list(ws.keys()),
                        "liq_usd": _liq_usd,
                        "mc_usd":  _mc_usd,
                        "h1": _h1, "h6": _h6, "h24": _h24,
                        "age_min": round(_age_secs / 60, 1) if _age_secs else None,
                        "pct_off_peak_at_entry": _peak_ratio_cluster,
                        "holders_at_entry": _holders_at_entry,
                        "top1_pct": _top1,
                        "top10_pct": _top10,
                        "creator": _cl_creator,
                        "buy_velocity_ratio": _cl_velocity,
                        "smart_money_overlap": await _try_smart_money_overlap(session, mint),
                    },
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
    # plus top10 < MONSTER_TOP10_MAX_PCT — one RPC call gets both.
    try:
        _dist = await top_wallet_distribution(session, mint)
    except Exception:
        _dist = None
    t1 = (_dist or {}).get("top1_pct")
    t10 = (_dist or {}).get("top10_pct")
    if t1 is not None and t1 >= 15.0:
        print(f"[monster-serial] {mint[:8]} top1={t1}% ≥15 — skipping")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "blocked_top1", "top1": t1})
        return
    if t10 is not None and t10 >= MONSTER_TOP10_MAX_PCT:
        print(f"[monster-serial] {mint[:8]} top10={t10}% ≥ {MONSTER_TOP10_MAX_PCT}% — skipping")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "blocked_top10", "top10": t10})
        return

    # Creator burn check — even whitelisted serial deployers can have a bad
    # week. If their last 48h includes a -30% close on us, skip new mints
    # until the cooldown passes.
    if creator in _load_burned_creators():
        print(f"[monster-serial] {mint[:8]} creator={creator[:8]} recently burned us — skip")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "blocked_creator_burned"})
        return

    pairs = await _dex_pairs(session, mint)
    pair0 = pairs[0] if pairs else {}
    sym = (pair0.get("baseToken") or {}).get("symbol") if pair0 else None

    # Buy velocity check (when DexScreener has h1/h6 data populated yet)
    _se_velocity = _buy_velocity_ratio(pair0)
    if _se_velocity is not None and _se_velocity < MONSTER_BUY_VELOCITY_MIN_RATIO:
        print(f"[monster-serial] {mint[:8]} buy_velocity={_se_velocity:.2f} — momentum decelerating, skip")
        _log_signal({"source": "serial_deployer", "mint": mint,
                     "creator": creator, "phase": "blocked_velocity", "velocity": _se_velocity})
        return

    await monster.open_monster_position(
        mint=mint,
        token_name=sym or mint[:8],
        signal_source="serial_deployer",
        sol_size=monster.get_default_size_sol(),
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

# ── GRADUATION SNIPE thresholds — derived from 21 winning PumpSwap trades ──
# Data: winning entry MC avg=$149K median=$115K, liq ≥$14K, age 20-240min.
# Previous MIN_MC_USD=250K was 5-10x past the optimal entry — that's why the
# lifecycle scout kept missing opportunities. Correct entry is POST-graduation
# cool-off: $25K-$300K MC, real liquidity, buyers still present.
LIFECYCLE_MIN_LIQ_USD       = 20_000   # paper test: $20K floor (lower for paper; confirmed monsters had $29K+ but $20K keeps more candidates)
                                        # <$30K = micro-cap rug territory. Real committed capital required.
LIFECYCLE_MAX_LIQ_USD       = 300_000
LIFECYCLE_MIN_MC_USD        = 55_000   # raised from 25K — 10-winner/10-loser cross-reference:
                                        # 7/10 losers entered below $55K MC. $45K change was made
                                        # while key was corrupted (no trades) — restored to $55K.
LIFECYCLE_MAX_MC_USD        = 1_000_000 # extended to $1M — PAIN ($632K), PAC ($850K) still running at entry
LIFECYCLE_MIN_LIQ_MC_RATIO  = 0.04
LIFECYCLE_MAX_LIQ_MC_RATIO  = 0.60    # was 0.30 — 0.30 contradicted min_liq for MC < $50K (impossible zone)
LIFECYCLE_MIN_AGE_SECS         = 60 * 60  # 1h floor — monsters are 1-4h old at entry (BURNIE 14h, milkers 20h)
LIFECYCLE_WEBHOOK_MIN_AGE_SECS = 30 * 60  # 30min for Helius webhook grads — still need time to settle
LIFECYCLE_MAX_AGE_SECS         = 12 * 60 * 60  # 12h ceiling — BURNIE 14h missed; captures most monster window
LIFECYCLE_TOP1_MAX_PCT      = 10.0
LIFECYCLE_TOP10_MAX_PCT     = 21.0    # 10-winner/10-loser analysis: 7/10 losers had top10>20%;
                                        # winners averaged 17.6%. Uses lifecycle-specific cap separate
                                        # from MONSTER_TOP10_MAX_PCT (35%) used by other strategies.
LIFECYCLE_BUY_RATIO_MIN     = 48.0
LIFECYCLE_BUY_RATIO_MAX     = 72.0    # paper test: raised to 72 to allow more candidates through
                                        # entry; mama (79%) and UNFAZED (75%) were losers. BR>72% = buying
                                        # exhausted, pump peak. Winners averaged 57.9%.
LIFECYCLE_H1_CHANGE_MAX_PCT = 120.0   # lowered from 200 — Bee had h1=200%, bounced, re-entered, hit -23% SL.
                                        # Winners (RICH, Aura) had h1 ~30-80% at entry. 120% still allows
                                        # genuine momentum entries without chasing exhausted movers.
LIFECYCLE_H1_CHANGE_MIN_PCT = 0.0     # raised from -30 — all winners had positive h1 at entry; negative h1 = dying token
LIFECYCLE_M5_CHANGE_MAX_PCT = 20.0    # was 15 — slightly looser


async def _fetch_via_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/search?q={query}",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return []
            d = await r.json()
            return d.get("pairs") or []
    except Exception:
        return []


async def _fetch_via_profile_feed(session: aiohttp.ClientSession) -> list[dict]:
    """Pull recently-profiled Solana tokens, then resolve their pumpswap/pump-amm
    pair data via the bulk tokens endpoint. The token-profiles feed surfaces
    *just-launched* tokens that the search endpoint often misses (search ranks
    by social/volume which lags fresh launches by hours).
    """
    try:
        async with session.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return []
            profiles = await r.json()
    except Exception:
        return []
    if not isinstance(profiles, list):
        return []
    sol_addrs = [
        str(p.get("tokenAddress") or "")
        for p in profiles
        if p.get("chainId") == "solana" and p.get("tokenAddress")
    ]
    if not sol_addrs:
        return []
    # DexScreener accepts up to 30 comma-separated mints in one call.
    addrs_chunk = ",".join(sol_addrs[:30])
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{addrs_chunk}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r2:
            if r2.status != 200:
                return []
            d2 = await r2.json()
    except Exception:
        return []
    pairs = d2.get("pairs") or []
    # For each token, keep only the highest-liquidity pumpswap/pump-amm pair.
    by_mint: dict[str, dict] = {}
    for p in pairs:
        if (p.get("dexId") or "").lower() not in ("pumpswap", "pump-amm"):
            continue
        mint = (p.get("baseToken") or {}).get("address")
        if not mint:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        existing = by_mint.get(mint)
        if not existing or liq > float((existing.get("liquidity") or {}).get("usd") or 0):
            by_mint[mint] = p
    return list(by_mint.values())


async def _recent_pumpswap_profiles(session: aiohttp.ClientSession) -> list[dict]:
    """Fresh PumpSwap pair feed for the lifecycle scout.

    Merges two DexScreener sources:
      1. search?q=pumpswap — broader feed, ranks by social/volume
      2. token-profiles/latest/v1 — fresh-launch feed, resolves to pair data
    The search feed alone goes stale during quiet markets (oldest token in the
    feed can be 3-6h old, well past our 90min age window). The profile feed
    surfaces recently-launched tokens reliably. 2026-04-29 fix.
    """
    primary = await _fetch_via_search(session, "pumpswap")
    fresh = await _fetch_via_profile_feed(session)
    # Dedupe by mint, preferring the entry with higher liquidity (more current data).
    by_mint: dict[str, dict] = {}
    for p in primary + fresh:
        mint = (p.get("baseToken") or {}).get("address")
        if not mint:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        existing = by_mint.get(mint)
        if not existing or liq > float((existing.get("liquidity") or {}).get("usd") or 0):
            by_mint[mint] = p
    return list(by_mint.values())


async def dexscreener_backup_poll_loop(session: aiohttp.ClientSession) -> None:
    """Secondary scanner: polls DexScreener every 5 minutes for new PumpSwap pairs.

    Helius webhooks miss some quiet graduations (RoyalPop, CROWDCAM never appeared
    in the webhook feed). This fills that gap by querying DexScreener directly and
    injecting any new mints into the fresh_grad_queue so the lifecycle loop sees them.
    Does NOT change any filters — just improves coverage.
    """
    import time as _t
    _seen_mints: set = set()
    print("[ds-backup] DexScreener backup poll started — every 5 min for missed PumpSwap grads")
    await asyncio.sleep(60)  # let main loop warm up first

    while True:
        try:
            async with session.get(
                "https://api.dexscreener.com/token-profiles/latest/v1",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    profiles = await r.json()
                    new_count = 0
                    for tok in (profiles if isinstance(profiles, list) else []):
                        if tok.get("chainId") != "solana":
                            continue
                        mint = tok.get("tokenAddress", "")
                        if not mint or mint in _seen_mints:
                            continue
                        _seen_mints.add(mint)
                        # Check if it's a PumpSwap pair not already in our queue
                        try:
                            async with session.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                                timeout=aiohttp.ClientTimeout(total=6),
                            ) as r2:
                                if r2.status == 200:
                                    ds = await r2.json()
                                    for p in (ds.get("pairs") or []):
                                        if (p.get("chainId") == "solana" and
                                                (p.get("dexId") or "").lower() in ("pumpswap", "pump-amm") and
                                                mint not in _MONSTER_SKIP_MINTS):
                                            pca = p.get("pairCreatedAt", 0)
                                            age_min = (_t.time() * 1000 - pca) / 60000 if pca else 999
                                            if 5 <= age_min <= 90:
                                                pool = p.get("pairAddress", "")
                                                _fresh_grad_queue.append({"mint": mint, "pool": pool,
                                                                           "ts": int(_t.time()), "source": "ds_backup"})
                                                new_count += 1
                                                break
                        except Exception:
                            pass
                        await asyncio.sleep(0.1)
                    if new_count:
                        print(f"[ds-backup] queued {new_count} new PumpSwap mints from DexScreener profiles")
        except Exception as e:
            print(f"[ds-backup] poll error: {e}")
        await asyncio.sleep(5 * 60)  # every 5 minutes


async def lifecycle_scout_loop(runtime: Any,
                                session: aiohttp.ClientSession) -> None:
    if not LIFECYCLE_SCOUT_ENABLED:
        print("[monster-lifecycle] disabled (MONSTER_LIFECYCLE_ENABLED=false)")
        return
    print("[monster-lifecycle] loop started")

    while True:
        try:
            _lifecycle_cleanup_deferred()
            # ── Drain Helius webhook fast-path queue first ─────────────────
            # Fresh graduations pushed via /api/webhooks/helius/pumpswap-grad
            # are merged with the regular DexScreener feed below. We resolve
            # each fresh-grad mint via the bulk tokens endpoint so the rest
            # of the pipeline sees them as standard DexScreener pair entries.
            fresh_grads = drain_fresh_grad_queue()
            fresh_grad_pairs: list[dict] = []
            if fresh_grads:
                fresh_mints = [g.get("mint") for g in fresh_grads if g.get("mint")]
                if fresh_mints:
                    chunk = ",".join(fresh_mints[:30])
                    try:
                        async with session.get(
                            f"https://api.dexscreener.com/latest/dex/tokens/{chunk}",
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as r_fg:
                            if r_fg.status == 200:
                                d_fg = await r_fg.json()
                                # Keep only the highest-liq pump-amm pair per mint
                                by_mint: dict[str, dict] = {}
                                for p in (d_fg.get("pairs") or []):
                                    if (p.get("dexId") or "").lower() not in ("pumpswap", "pump-amm"):
                                        continue
                                    m = (p.get("baseToken") or {}).get("address")
                                    if not m:
                                        continue
                                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                                    if m not in by_mint or liq > float((by_mint[m].get("liquidity") or {}).get("usd") or 0):
                                        by_mint[m] = p
                                fresh_grad_pairs = list(by_mint.values())
                                print(f"[monster-lifecycle] 📬 webhook fast-path: "
                                      f"{len(fresh_grads)} mints received, "
                                      f"{len(fresh_grad_pairs)} resolved to pump-amm pairs")
                    except Exception as _fg_err:
                        print(f"[monster-lifecycle] webhook resolve error: {_fg_err}")

            pairs = await _recent_pumpswap_profiles(session)
            # Webhook-sourced pairs go FIRST so any matching mint is evaluated
            # before the DexScreener-sourced ones; same-mint dedup happens via
            # _recently_signalled inside the loop.
            pairs = fresh_grad_pairs + pairs
            now_ms = time.time() * 1000
            cycle_seen = 0
            cycle_in_age_window = 0
            cycle_passed_baseline = 0
            cycle_added_to_watchlist = 0
            cycle_entered = 0
            cycle_viral_fires = 0   # how many tokens triggered the viral-momentum override this cycle
            cycle_fresh_grads = len(fresh_grad_pairs)
            # Per-reason reject counters — emitted in cycle summary so
            # we can see WHY passed_baseline is low without per-token logs.
            cycle_rejects: dict[str, int] = defaultdict(int)
            for p in pairs:
                try:
                    dex_id = (p.get("dexId") or "").lower()
                    if dex_id not in ("pumpswap", "pump-amm"):
                        continue
                    mint = (p.get("baseToken") or {}).get("address")
                    if not mint or mint in _MONSTER_SKIP_MINTS:
                        continue
                    cycle_seen += 1
                    if _recently_signalled(mint):
                        continue
                    pca = p.get("pairCreatedAt")
                    if not pca:
                        continue
                    on_watch = mint in _lifecycle_deferred
                    age_secs = (now_ms - float(pca)) / 1000
                    age_max = LIFECYCLE_WATCHLIST_MAX_AGE_SECS if on_watch else LIFECYCLE_MAX_AGE_SECS

                    # Creator-alpha priority — if this mint was just graduated
                    # by a tracked operator/creator, we trust the wallet signal
                    # more than the standard "is this token mature" filters.
                    # Use relaxed gates: liq $5k floor (vs $30k), age=0 (vs 20min),
                    # h1 max 600% (vs 100%), wash cap effectively disabled.
                    # Hard safety gates (top1, top10, rugcheck, creator_burn) STAY.
                    priority_rec = _is_creator_alpha_priority(mint)

                    # Viral-momentum eligibility — checked once and reused at every
                    # soft-gate site below. Token must pass all of:
                    #   age >= 10min (vs normal 20min)
                    #   m5_buys >= 80 (DexScreener txn count)
                    #   m5 buys/sells >= 1.5 (buy-dominant pressure)
                    # When viral_eligible=True the bot can bypass the age floor,
                    # the h1>100% cap, and the shape gate. Hard safety gates
                    # (top1, top10, rugcheck, creator_burn) still apply.
                    _txns_m5 = (p.get("txns") or {}).get("m5") or {}
                    _vir_buys = int(_txns_m5.get("buys") or 0)
                    _vir_sells = int(_txns_m5.get("sells") or 0)
                    _vir_bs = (_vir_buys / _vir_sells) if _vir_sells > 0 else float("inf")
                    _vir_h1 = float((p.get("priceChange") or {}).get("h1") or 0)
                    viral_eligible = (
                        LIFECYCLE_VIRAL_ENABLED
                        and age_secs >= LIFECYCLE_VIRAL_AGE_MIN_SECS
                        and _vir_buys >= LIFECYCLE_VIRAL_OVERRIDE_BUYERS
                        and _vir_bs >= LIFECYCLE_VIRAL_BS_MIN
                        and _vir_h1 <= LIFECYCLE_VIRAL_H1_MAX_PCT
                    )

                    if priority_rec:
                        age_floor = _CREATOR_ALPHA_PRIORITY_AGE_MIN
                    elif viral_eligible:
                        age_floor = LIFECYCLE_VIRAL_AGE_MIN_SECS
                    else:
                        # Webhook-sourced grads: reduce floor to 5min.
                        # Helius tells us the EXACT graduation moment — we
                        # don't need to wait 20min for a token we already
                        # know is fresh. AREA51/alein post-mortem: 20min
                        # delay means we arrive after the first leg every time.
                        if mint in _webhook_sourced_mints:
                            age_floor = LIFECYCLE_WEBHOOK_MIN_AGE_SECS
                        else:
                            age_floor = LIFECYCLE_MIN_AGE_SECS
                    if age_secs < age_floor or age_secs > age_max:
                        if on_watch and age_secs > age_max:
                            _lifecycle_deferred.pop(mint, None)  # aged out of watchlist
                        cycle_rejects["age"] += 1
                        continue
                    cycle_in_age_window += 1
                    liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
                    liq_min = _CREATOR_ALPHA_PRIORITY_LIQ_MIN if priority_rec else LIFECYCLE_MIN_LIQ_USD
                    if not (liq_min <= liq_usd <= LIFECYCLE_MAX_LIQ_USD):
                        cycle_rejects["liq"] += 1
                        continue
                    mc_usd = float(p.get("marketCap") or p.get("fdv") or 0)
                    if not (LIFECYCLE_MIN_MC_USD <= mc_usd <= LIFECYCLE_MAX_MC_USD):  # MC-GATE
                        cycle_rejects["mc"] += 1
                        continue
                    liq_mc = (liq_usd / mc_usd) if mc_usd > 0 else 0
                    if not (LIFECYCLE_MIN_LIQ_MC_RATIO <= liq_mc <= LIFECYCLE_MAX_LIQ_MC_RATIO):
                        cycle_rejects["lmratio"] += 1
                        continue
                    pc = p.get("priceChange") or {}
                    h1_change = float(pc.get("h1") or 0)
                    m5_change = float(pc.get("m5") or 0)
                    price_native = float(p.get("priceNative") or p.get("priceUsd") or 0)
                    h1_max = _CREATOR_ALPHA_PRIORITY_H1_MAX if priority_rec else LIFECYCLE_H1_CHANGE_MAX_PCT
                    # Deep retest path removed — all deep_retest trades were losses:
                    # 404kitty -97.7%, WOMBLE -47.3%. The path selected for
                    # tokens that already spiked-and-dumped, not quiet grinders.
                    _deep_retest = False
                    if h1_change > h1_max:
                        if not viral_eligible:
                            # Track fresh tokens with elevated h1 so we catch the
                            # normalization/pullback. MIRA pumped to h1=350% at 12:00,
                            # was never watchlisted → missed the 12:30 entry at $86K MC.
                            if not on_watch and not priority_rec and h1_change <= 300.0:
                                _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                                print(f"[monster-lifecycle] 📋 {mint[:8]} h1={h1_change:.0f}% "
                                      f"— watching for pullback/normalization")
                            elif on_watch and h1_change <= 300.0:
                                # Already watching — keep snapshots fresh so pullback is visible
                                _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                            cycle_rejects["h1_high"] += 1
                            continue
                    if h1_change < LIFECYCLE_H1_CHANGE_MIN_PCT:
                        cycle_rejects["h1_low"] += 1
                        continue

                    # ── COOL-OFF POSITION CHECK ──────────────────────────────
                    # Florentina lesson: the bot entered at $163K when the h6
                    # showed a big 6h run. The token had peaked at ~$250K and
                    # was only 35% below peak — recovery bounce was already
                    # well underway. The IDEAL entry (shown on chart) was at
                    # $80-100K = 60-70% below peak.
                    #
                    # Rule: if the token had a large 6h run (h6 > 150%) but
                    # h1 is POSITIVE (>+15%), the price has already recovered
                    # significantly from its cool-off low. We're entering into
                    # a bounce, not into a cool-off. Skip.
                    #
                    # If h6 big AND h1 is flat-to-negative → we ARE in the
                    # cool-off window → allow entry.
                    #
                    # IMPORTANT: only applies to tokens ≥ 90min old. For younger
                    # tokens h6 == h1 by definition (they haven't been trading for
                    # 6 hours), so the check fires falsely on every strong young
                    # token (ETZKRf2V/6qkLuxj5 post-mortem 2026-05-07).
                    pc_h6 = float((p.get("priceChange") or {}).get("h6") or 0)
                    if pc_h6 > 150 and h1_change > 15 and age_secs > 90 * 60:
                        cycle_rejects["cooloff_missed"] = cycle_rejects.get("cooloff_missed", 0) + 1
                        if on_watch:
                            print(f"[monster-lifecycle] ⏱ {mint[:8]} cool-off missed — "
                                  f"h6={pc_h6:+.0f}% but h1={h1_change:+.1f}% (recovery bounce running, "
                                  f"ideal entry was earlier in the dip)")
                            _lifecycle_deferred.pop(mint, None)
                        continue

                    # Wash-trading cap — vol_m5/liq > 3x is the same gate the
                    # breakout scout uses; lifecycle_bounce was bypassing it.
                    # WITH override: m5_buys ≥ 20 indicates viral momentum, not wash
                    # (GOBLIN 2026-04-25 ran vol/liq=12x with real buyers and printed
                    # 2 real spikes; we missed it). Same threshold as the bounce gate.
                    vol_m5_lc = float((p.get("volume") or {}).get("m5") or 0)
                    if not priority_rec and liq_usd > 0 and (vol_m5_lc / liq_usd) > LIFECYCLE_MAX_M5_VOL_LIQ:
                        # Priority mints skip the wash-cap entirely — wash-during-launch
                        # is normal for fresh graduations. Standard mints still get
                        # the m5_buys override when wash is flagged.
                        lc_txns_m5 = (p.get("txns") or {}).get("m5") or {}
                        lc_m5_buys = int(lc_txns_m5.get("buys") or 0)
                        if lc_m5_buys < BREAKOUT_WASH_OVERRIDE_BUYERS:
                            if on_watch:
                                print(f"[monster-lifecycle] 🧼 {mint[:8]} wash-flagged "
                                      f"vol_m5/liq={vol_m5_lc / liq_usd:.1f}x > {LIFECYCLE_MAX_M5_VOL_LIQ:.0f}x "
                                      f"buys={lc_m5_buys} — drop watch")
                                _lifecycle_deferred.pop(mint, None)
                            cycle_rejects["wash"] += 1
                            continue
                        print(f"[monster-lifecycle] 🌊 {mint[:8]} wash-override: "
                              f"vol/liq={vol_m5_lc/liq_usd:.1f}x but m5_buys={lc_m5_buys} "
                              f"≥ {BREAKOUT_WASH_OVERRIDE_BUYERS} — viral momentum, allow")

                    # ── Bounce-first gate (universal) ────────────────────
                    # ALL lifecycle entries must prove a pullback + bounce
                    # from the watchlist. NO direct entries allowed.
                    #
                    # Bullseye post-mortem 2026-05-19:
                    #   - 35 min old (just past old 30-min guard)
                    #   - h1=0% (DexScreener returns null h1 for <60min tokens)
                    #   - Null h1 bypassed both young-token guard AND watchlist
                    #     gate (which only fired on h1 > 30%)
                    #   - Entered at $67K MC, token had already peaked $170K
                    #   - No bounce evidence, no pullback confirmation
                    #   - Rugged: -97% loss
                    #
                    # A token that looks clean on metrics alone (age, liq, MC,
                    # buy-ratio) is NOT confirmed safe. The bounce path is the
                    # only reliable entry signal — it proves support exists.
                    # Exceptions: creator_alpha (wallet signal), viral override
                    # (buyer flow signal), deep_retest (price pullback verified).
                    if not on_watch and not priority_rec and not viral_eligible:
                        _lifecycle_record_snapshot(mint, float(p.get("priceNative") or 0), m5_change, h1_change, liq_usd)
                        cycle_added_to_watchlist += 1
                        print(f"[monster-lifecycle] 📋 {mint[:8]} age={age_secs/60:.0f}min h1={h1_change:.0f}% "
                              f"— watchlist (quiet accumulation confirmation required)")
                        continue

                    # ── Entry-shape gate (with bounce-watchlist) ─────────
                    cycle_passed_baseline += 1
                    price_native = float(p.get("priceNative") or p.get("priceUsd") or 0)
                    _first_seen = (_lifecycle_deferred.get(mint) or {}).get("first_seen", 0.0)
                    shape, shape_reason = _lifecycle_classify_entry_shape(
                        m5_change, h1_change, first_seen=_first_seen)

                    if shape != "good" and not viral_eligible and not priority_rec:
                        # Viral override fast-paths through the shape gate — at the
                        # peak of a viral pump, m5 will read "overheated" (>+8%) but
                        # the buyer-flow signal tells us this IS the entry, not a
                        # reason to defer. Same for creator_alpha priority mints —
                        # we trust the wallet signal more than the shape heuristic.
                        # Still falls through to safety gates below.
                        # Deep retest bypass: price pullback is verified — an
                        # "overheated" or "postpeak" shape here reflects the bounce
                        # starting, not the original spike. Only block "pullback"
                        # (m5 < -5% = actively dropping; bounce gate catches it too).
                        if _deep_retest and shape != "pullback":
                            pass  # bounce gate enforces m5 >= 0 + buyer count
                        else:
                            _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                            cycle_added_to_watchlist += 1
                            print(f"[monster-lifecycle] 👁 {mint[:8]} {shape} — {shape_reason} (watchlist)")
                            continue
                    if priority_rec and shape != "good":
                        # Never bypass OVERHEATED shape for creator_alpha — if the token
                        # is mid-spike we're entering at the peak regardless of wallet signal.
                        # OrbCoin: lag=2741s (46min), h1=+781%, entered at the top of the
                        # spike then hit -30% floor. Pullback/postpeak are safe to bypass
                        # (token has already moved, wallet signal is the edge). Overheated
                        # means it's still pumping — that edge is gone.
                        if shape == "overheated":
                            cycle_rejects["ca_overheated"] = cycle_rejects.get("ca_overheated", 0) + 1
                            _creator_alpha_priority_mints.pop(mint, None)
                            print(f"[monster-lifecycle] 🔥 {mint[:8]} creator-alpha BLOCKED "
                                  f"shape=overheated h1={h1_change:.0f}% — spike in progress, not bypassing")
                            continue
                        print(f"[monster-lifecycle] ⭐ {mint[:8]} CREATOR-ALPHA-PRIORITY: "
                              f"src={priority_rec['source']} bypassing shape={shape}, age={age_secs/60:.1f}min, "
                              f"liq=${liq_usd:,.0f}, h1={h1_change:.0f}%")
                    if viral_eligible and shape != "good":
                        # Log the override fire so we can audit outcomes in real time.
                        print(f"[monster-lifecycle] 🚀 {mint[:8]} VIRAL-OVERRIDE: "
                              f"m5_buys={_vir_buys}/{_vir_sells} ({_vir_bs:.1f}x) "
                              f"h1={h1_change:.0f}% age={age_secs/60:.0f}min "
                              f"shape={shape} — bypassing soft gates")
                        cycle_viral_fires += 1
                    if on_watch:
                        # ── Monster DNA: quiet accumulation gates ─────────────────────
                        # Confirmed from 17 monster tokens: buy_ratio 45-57%, quiet m5
                        # (±10%), balanced buying over multiple hours. These tokens do
                        # NOT spike at entry — they silently grind up over 6-20 hours.

                        # Gate 1 — h1 must be net positive (trending up, not rolling over)
                        if h1_change < LIFECYCLE_BOUNCE_H1_MIN_PCT:
                            print(f"[monster-lifecycle] 🪦 {mint[:8]} h1={h1_change:.1f}% negative "
                                  f"— rolling over, drop from watchlist")
                            _lifecycle_deferred.pop(mint, None)
                            continue

                        # Gate 2 — m5 must be quiet: not spiking (already pumping) or
                        # dumping (actively falling). Monster DNA: ±10% at entry.
                        if m5_change > LIFECYCLE_M5_GOOD_HIGH:
                            _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                            cycle_added_to_watchlist += 1
                            print(f"[monster-lifecycle] ⏳ {mint[:8]} m5=+{m5_change:.1f}% > +{LIFECYCLE_M5_GOOD_HIGH:.0f}% "
                                  f"— mid-spike, wait for quiet")
                            cycle_rejects["m5_hot"] = cycle_rejects.get("m5_hot", 0) + 1
                            continue
                        if m5_change < LIFECYCLE_M5_GOOD_LOW:
                            _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                            cycle_added_to_watchlist += 1
                            print(f"[monster-lifecycle] ⏳ {mint[:8]} m5={m5_change:.1f}% < {LIFECYCLE_M5_GOOD_LOW:.0f}% "
                                  f"— actively falling, wait for stability")
                            cycle_rejects["m5_falling"] = cycle_rejects.get("m5_falling", 0) + 1
                            continue

                        # Gate 3 — minimum 3 min on watchlist for accumulation confirmation.
                        # (3 min for paper testing — increase to 10 min for live trading to
                        # require sustained balanced buying across multiple data points.)
                        _first_seen_ts = (_lifecycle_deferred.get(mint) or {}).get("first_seen", 0.0)
                        _watched_mins = (time.time() - _first_seen_ts) / 60.0 if _first_seen_ts > 0 else 0.0
                        if _watched_mins < 3.0:
                            _lifecycle_record_snapshot(mint, price_native, m5_change, h1_change, liq_usd)
                            cycle_added_to_watchlist += 1
                            print(f"[monster-lifecycle] ⏳ {mint[:8]} only {_watched_mins:.0f}min watched "
                                  f"— need 3min of consistent quiet data")
                            cycle_rejects["watch_time"] = cycle_rejects.get("watch_time", 0) + 1
                            continue

                        print(f"[monster-lifecycle] 🔄 {mint[:8]} quiet accumulation confirmed — "
                              f"m5={m5_change:+.1f}% h1={h1_change:+.1f}% watched={_watched_mins:.0f}min liq=${liq_usd:,.0f}")
                    txns_h1 = (p.get("txns") or {}).get("h1") or {}
                    buys = txns_h1.get("buys") or 0
                    sells = txns_h1.get("sells") or 0
                    total = buys + sells
                    if total < 30:  # too thin — skip
                        cycle_rejects["txn_thin"] += 1
                        continue
                    br = (buys / total) * 100
                    if not (LIFECYCLE_BUY_RATIO_MIN <= br <= LIFECYCLE_BUY_RATIO_MAX):
                        cycle_rejects["buy_ratio"] += 1
                        continue
                    base_info = (p.get("info") or {})
                    socials = base_info.get("socials") or []
                    websites = base_info.get("websites") or []
                    if not socials and not websites:
                        cycle_rejects["no_socials"] += 1
                        continue

                    # Holder count floor — tokens with no organic buyer base look
                    # healthy on price/volume but are 5-10 wallets pretending to
                    # be a market.
                    try:
                        _lc_holders = int(base_info.get("holders")) if base_info.get("holders") is not None else None
                    except (ValueError, TypeError):
                        _lc_holders = None
                    if _lc_holders is not None and _lc_holders < MONSTER_MIN_UNIQUE_HOLDERS:
                        cycle_rejects["holders"] += 1
                        continue

                    # Buy velocity — recent 1h vs prior 5h average. Skip
                    # decelerating tokens (entering as momentum dies).
                    _lc_velocity = _buy_velocity_ratio(p)
                    if _lc_velocity is not None and _lc_velocity < MONSTER_BUY_VELOCITY_MIN_RATIO:
                        cycle_rejects["velocity"] += 1
                        continue

                    # Expensive last: top-1 + top-10 non-pool holders (one RPC call)
                    _lc_dist = await top_wallet_distribution(session, mint)
                    t1 = (_lc_dist or {}).get("top1_pct")
                    t10 = (_lc_dist or {}).get("top10_pct")
                    if t1 is None or t1 >= LIFECYCLE_TOP1_MAX_PCT:
                        cycle_rejects["top1"] += 1
                        continue
                    if t10 is not None and t10 >= LIFECYCLE_TOP10_MAX_PCT:
                        cycle_rejects["top10"] += 1
                        continue  # top10 > 22% — coordinated dump risk (10-trade analysis 2026-05-09)
                    # Bounce-path holder cap — top1 only. The top10 aggregate
                    # (13.5% ceiling from 2 trades) was blocking ideal entries:
                    # alein had top10=21% across 10 wallets (avg 2.1% each, no
                    # single dump risk) and went +496%. Top1 catches the real
                    # threat — one wallet that can move the market alone.
                    # alein post-mortem 2026-05-08.
                    if on_watch:
                        # Exhausted mover: if h1 ever exceeded 150% while in our watchlist,
                        # the token already had its big spike. A quiet period now is a
                        # dead-cat consolidation after a blown-out move — not an accumulation.
                        _max_h1 = (_lifecycle_deferred.get(mint) or {}).get("max_h1_seen", 0.0)
                        if _max_h1 >= 150.0:
                            print(f"[monster-lifecycle] 🪦 {mint[:8]} max_h1={_max_h1:.0f}% — "
                                  f"exhausted mover, hard skip")
                            _lifecycle_deferred.pop(mint, None)
                            _MONSTER_SKIP_MINTS.add(mint)
                            _save_persistent_skip_mints(_MONSTER_SKIP_MINTS)
                            cycle_rejects["late_to_party"] = cycle_rejects.get("late_to_party", 0) + 1
                            continue
                        # Legacy 5% bounce top1 check removed — the main LIFECYCLE_TOP1_MAX_PCT=10%
                        # check above already covers single-wallet dump risk. The 5% threshold
                        # was blocking 8P2fEWMK-class quiet accumulators in an infinite cycle.

                    # Zombie token check — reject old mints being re-pumped.
                    # Compares pump-amm pair age (recent) vs oldest pair on ANY
                    # DEX. Gap reveals classic P&D on dormant/dead tokens.
                    # Only runs for tokens that cleared all other gates to
                    # minimise extra DexScreener calls. Fails open on API error.
                    if not priority_rec:  # creator-alpha fast-path skips this
                        if await _is_zombie_token(session, mint):
                            cycle_rejects["zombie"] = cycle_rejects.get("zombie", 0) + 1
                            _lifecycle_deferred.pop(mint, None)
                            continue

                    # Creator burn check — skip if this mint's creator lost us
                    # money on a prior trade in the last 48h.
                    _burned, _creator = await is_creator_burned(session, mint)
                    if _burned:
                        print(f"[monster-lifecycle] 🚫 {mint[:8]} creator={_creator[:8]} recently burned us — skip")
                        continue

                    # Dev reputation tier (per pump.fun quant guide: creator history)
                    # Hard-blocks blacklisted creators (2+ confirmed rugs); logs a
                    # positive flag for proven/whitelisted creators so the trade
                    # journal captures the signal. We don't loosen filters for
                    # whitelisted creators — entry quality stays constant; this
                    # is for retrospective analysis and future tier-aware sizing.
                    _creator_tier = "unknown"
                    if _creator:
                        try:
                            from elizaos.plugins.solana.dev_reputation import get_reputation
                            _rep = get_reputation()
                            if _rep.is_blacklisted(_creator):
                                print(f"[monster-lifecycle] 🚫 {mint[:8]} creator={_creator[:8]} BLACKLISTED — skip")
                                continue
                            if _rep.is_proven(_creator):
                                _creator_tier = "proven"
                                print(f"[monster-lifecycle] ⭐ {mint[:8]} creator={_creator[:8]} PROVEN tier")
                            elif _rep.is_whitelisted(_creator):
                                _creator_tier = "whitelisted"
                                print(f"[monster-lifecycle] ✓ {mint[:8]} creator={_creator[:8]} whitelisted")
                        except Exception as _rep_err:
                            print(f"[monster-lifecycle] dev_reputation lookup error: {_rep_err} — allowing")

                    # Rugcheck safety gate — catches unlocked LP, bundled/concentrated
                    # ownership that made it past our top-10 check, and other
                    # structural red flags. Same gate breakout scout uses.
                    try:
                        from elizaos.plugins.solana.axiom_copy_trader import _quick_safety_check
                        _rc_safe, _rc_reason = await _quick_safety_check(mint, session)
                        if not _rc_safe:
                            print(f"[monster-lifecycle] 🛡 rugcheck block {mint[:8]}: {_rc_reason}")
                            continue
                    except Exception as _rc_err:
                        print(f"[monster-lifecycle] rugcheck import error: {_rc_err} — allowing")

                    _mark_signalled(mint)
                    if priority_rec:
                        # Creator-alpha priority entries get distinct source so
                        # the dashboard's per-source breakdown shows their P&L
                        # separately from generic lifecycle entries.
                        sig_src = priority_rec.get("source") or "creator_alpha"
                        # One-shot consumption — drop from priority set so we
                        # don't double-tag a re-evaluation
                        _creator_alpha_priority_mints.pop(mint, None)
                    elif viral_eligible and shape != "good":
                        sig_src = "lifecycle_viral"
                    else:
                        sig_src = "lifecycle_quiet" if on_watch else "lifecycle"
                    print(f"[monster-lifecycle] 🎯 {mint[:8]} {sig_src} match "
                          f"age={age_secs/60:.0f}min liq=${liq_usd:,.0f} br={br:.0f}% top1={t1}%")
                    _log_signal({
                        "source": sig_src,
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
                    # ── GROQ ENTRY CONFIRMATION ───────────────────────────
                    # Fast Groq check (200-400ms) before committing SOL.
                    # Groq looks at the full price context and flags if we're
                    # chasing a recovery bounce rather than entering a cool-off.
                    # Florentina: h6=+400%, h1=+4%, MC=$163K — Groq would flag
                    # "h6 shows big 6h run, current MC near recovery high, wait
                    # for deeper cool-off or next confirmed dip."
                    _groq_entry_ok = True
                    _groq_skip_reason = ""
                    try:
                        _groq_key_lc = os.getenv("GROQ_API_KEY", "")
                        if _groq_key_lc:
                            pc_h6_lc = float((p.get("priceChange") or {}).get("h6") or 0)
                            pc_h24_lc = float((p.get("priceChange") or {}).get("h24") or 0)
                            _entry_prompt = (
                                f"Post-graduation cool-off entry check. Token facts:\n"
                                f"Age: {age_secs/60:.0f} min | MC: ${mc_usd:,.0f} | Liq: ${liq_usd:,.0f}\n"
                                f"Price changes: m5={m5_change:+.1f}% h1={h1_change:+.1f}% h6={pc_h6_lc:+.0f}% h24={pc_h24_lc:+.0f}%\n"
                                f"Buy ratio (h1): {br:.0f}% | Top10 holders: {t10:.1f}%\n\n"
                                f"Strategy: enter during the COOL-OFF or early consolidation phase after graduation, "
                                f"before the next leg up. For tokens under 60min old, h1 flat-to-slightly-positive "
                                f"(0-20%) with buyers returning IS a valid entry — do NOT require a 40-70% retreat "
                                f"that can't happen on a token this young. For older tokens (60min+) with a big h6 run, "
                                f"require more meaningful cool-off (h1 flat or negative vs h6). "
                                f"Key signals: buy ratio healthy, m5 stabilising, liq not draining.\n\n"
                                f"Is this a valid entry RIGHT NOW? Reply JSON only: "
                                f'{{\"enter\": true/false, \"reason\": \"one sentence\"}}'
                            )
                            async with session.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                headers={"Authorization": f"Bearer {_groq_key_lc}", "Content-Type": "application/json"},
                                json={"model": "llama-3.3-70b-versatile",
                                      "messages": [{"role": "user", "content": _entry_prompt}],
                                      "temperature": 0.1, "max_tokens": 80},
                                timeout=aiohttp.ClientTimeout(total=4),
                            ) as _gr:
                                if _gr.status == 200:
                                    _grd = await _gr.json()
                                    _raw = (_grd.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                                    import json as _json_gr
                                    try:
                                        _clean = _raw.strip().strip("```json").strip("```").strip()
                                        _parsed = _json_gr.loads(_clean)
                                        _groq_entry_ok = bool(_parsed.get("enter", True))
                                        _groq_skip_reason = _parsed.get("reason", "")
                                        if not _groq_entry_ok:
                                            print(f"[monster-lifecycle] 🤖 GROQ SKIP {mint[:8]}: {_groq_skip_reason}")
                                    except Exception:
                                        pass  # parse fail → allow entry
                    except Exception:
                        pass  # Groq unreachable → allow entry

                    if not _groq_entry_ok:
                        cycle_rejects["groq_skip"] = cycle_rejects.get("groq_skip", 0) + 1
                        continue

                    # Drop from watchlist on entry; the position now lives in
                    # _monster_positions and the deferred snapshot history is no
                    # longer needed.
                    _lifecycle_deferred.pop(mint, None)
                    if not monster.can_open_new_position():
                        continue
                    sym = (p.get("baseToken") or {}).get("symbol") or mint[:8]
                    await monster.open_monster_position(
                        mint=mint,
                        token_name=sym,
                        signal_source=sig_src,
                        sol_size=monster.get_default_size_sol(),
                        session=session,
                        runtime=runtime,
                        metadata={"age_min": round(age_secs / 60, 1),
                                  "liq_usd": liq_usd, "mc_usd": mc_usd,
                                  "liq_mc_ratio": round(liq_mc, 4),
                                  "buy_ratio": br, "top1_pct": t1, "top10_pct": t10,
                                  "h1_change": h1_change, "m5_change": m5_change,
                                  "holders_at_entry": _lc_holders,
                                  "buy_velocity_ratio": _lc_velocity,
                                  "creator": _creator,
                                  "creator_tier": _creator_tier,
                                  "smart_money_overlap": await _try_smart_money_overlap(session, mint)},
                    )
                    cycle_entered += 1
                except Exception as _pair_exc:
                    print(f"[monster-lifecycle] ⚠️  per-pair exception {mint[:8]}: {_pair_exc}")
                    continue
            # Per-cycle summary so silence is diagnosable. If candidates=0 the
            # data source is starving; if in_age=0 the feed is stale; if
            # baseline=0 filters are too tight; if entered=0+watched>0 we're
            # patiently waiting for bounce confirmation.
            rej_str = ",".join(f"{k}={v}" for k, v in sorted(cycle_rejects.items()) if v > 0)
            print(
                f"[monster-lifecycle] cycle: candidates={cycle_seen} "
                f"fresh_grads={cycle_fresh_grads} "
                f"in_age_window={cycle_in_age_window} "
                f"passed_baseline={cycle_passed_baseline} "
                f"watchlisted={cycle_added_to_watchlist} "
                f"deferred_total={len(_lifecycle_deferred)} "
                f"entered={cycle_entered}"
                + (f" viral_fires={cycle_viral_fires}" if cycle_viral_fires else "")
                + (f" rej[{rej_str}]" if rej_str else "")
            )
            _persist_reject_snapshot({
                "ts": time.time(),
                "candidates": cycle_seen,
                "fresh_grads": cycle_fresh_grads,
                "in_age": cycle_in_age_window,
                "baseline": cycle_passed_baseline,
                "watchlisted": cycle_added_to_watchlist,
                "deferred_total": len(_lifecycle_deferred),
                "entered": cycle_entered,
                "viral_fires": cycle_viral_fires,
                "rejects": dict(cycle_rejects),
            })
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
# MOONDOGE 2026-04-21: liq=$34k, m5=+89%, buys-only wash → LP pulled 88% in 5min,
# we lost 98.7%. Raise min liq, cap m5, require buyer dominance, cap vol/liq.
BREAKOUT_MIN_LIQ_USD         = 60_000   # was 30k — MOONDOGE passed at $34k, LP pulled
BREAKOUT_MAX_LIQ_USD         = 500_000
BREAKOUT_MIN_MC_USD          = 100_000
BREAKOUT_MAX_MC_USD          = 5_000_000
BREAKOUT_M5_MIN_PCT          = 20.0
BREAKOUT_M5_MAX_PCT          = 60.0   # NEW: reject pump traps (MOONDOGE m5=+89% = manipulation)
BREAKOUT_H1_MIN_PCT          = 0.0    # reject dead-cat bounces (SOLMONEY 2026-04-22: h1=-15% → dumped 28pp in 30s)
BREAKOUT_H1_MAX_PCT          = 150.0
BREAKOUT_MIN_M5_VOL_USD      = 3_000
BREAKOUT_MIN_H1_TXNS         = 30
BREAKOUT_MIN_BUY_RATIO_PCT   = 65.0   # raised from 55 — ETF 2026-04-22 passed at 58.1% and exhausted
BREAKOUT_MAX_M5_VOL_LIQ      = 3.0    # NEW: wash-trading cap (vol_m5/liq). MOONDOGE was 1.07x + +89% m5
BREAKOUT_WASH_OVERRIDE_BUYERS = 20    # GOBLIN 2026-05-01: vol/liq=12x for hours but token did 2 real spikes (5d chart)
                                      # — if m5_buys >= 20 unique, the high vol/liq is viral momentum not wash.
                                      # Same threshold as the bounce-watchlist m5_buys gate; calibrated against
                                      # CCP (m5_buys=11, lost) vs TRUTH (28, won) vs FOFAR (58, won) vs EVA (66, won).
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
                    if m5 > BREAKOUT_M5_MAX_PCT:
                        continue  # pump trap — MOONDOGE m5=+89% rugged us -98.7%
                    if h1 > BREAKOUT_H1_MAX_PCT:
                        continue
                    if h1 < BREAKOUT_H1_MIN_PCT:
                        continue
                    _bc_snap = {
                        "age_min": round(age_secs / 60, 1),
                        "liq_usd": liq_usd, "mc_usd": mc_usd,
                        "m5": m5, "h1": h1,
                    }
                    # Second-wave rejection via mcap velocity. ETF 2026-04-22
                    # passed every filter at age=101min mc=$750k and we bought
                    # 7 min after its +20,805% ATH. Its lifetime mcap velocity
                    # was $7,366/min — far above any healthy fresh breakout.
                    # A normal 2h-old breakout sits around $1-3k/min. Above 5k/min
                    # on a <3h-old token means the pump already happened.
                    if age_secs > 0:
                        mcap_velocity = mc_usd / (age_secs / 60)
                        if age_secs < 3 * 3600 and mcap_velocity > 5_000:
                            _log_reject("breakout_candle", mint, "mcap_velocity", _bc_snap,
                                        filter_name="mcap_velocity", filter_value=round(mcap_velocity), threshold=5000)
                            continue  # second-wave bounce, not fresh breakout
                    # Dying-momentum filter: if h6 dwarfs h1, the peak was in the
                    # older part of the 6h window and we're buying a rollback.
                    # Fresh breakouts have h6/h1 ≈ 1-2; ETF-shaped exhaustion has
                    # h6/h1 > 5 (most gain happened hours ago, tiny h1 bounce now).
                    h6 = float(pc.get("h6") or 0)
                    h24 = float(pc.get("h24") or 0)
                    _bc_snap["h6"] = h6
                    _bc_snap["h24"] = h24
                    if h1 > 0 and h6 > h1 * 5:
                        _log_reject("breakout_candle", mint, "h6_h1_dying", _bc_snap,
                                    filter_name="h6_h1_ratio", filter_value=round(h6/h1, 1), threshold=5)
                        continue  # momentum dying — post-peak rollback
                    # Real peak-distance check via GeckoTerminal 1-min candles.
                    # Reject if currently <85% of the 60-min peak (post-peak rollback).
                    pair_addr = p.get("pairAddress")
                    breakout_peak_ratio: float | None = None
                    if pair_addr:
                        peak_ratio = await pct_off_recent_peak(session, pair_addr, 60)
                        if peak_ratio is not None and peak_ratio < 0.85:
                            _log_reject("breakout_candle", mint, "peak_distance", _bc_snap,
                                        filter_name="peak_ratio_60m", filter_value=round(peak_ratio, 2), threshold=0.85)
                            continue  # >15% off recent peak → rollback, not breakout
                        breakout_peak_ratio = peak_ratio
                    vol_m5 = float((p.get("volume") or {}).get("m5") or 0)
                    if vol_m5 < BREAKOUT_MIN_M5_VOL_USD:
                        continue
                    # Wash-trading cap: reject if 5-min volume churns more than Nx liquidity
                    # WITH override: high vol/liq with high unique-buyer count is viral
                    # momentum, not wash. GOBLIN 2026-04-25 ran vol/liq=12x for hours and
                    # printed two real spikes — bot blocked every scan. The bounce-gate
                    # m5_buys ≥ 20 threshold (validated against CCP/TRUTH/FOFAR/EVA)
                    # discriminates real momentum from wash.
                    if liq_usd > 0 and (vol_m5 / liq_usd) > BREAKOUT_MAX_M5_VOL_LIQ:
                        bc_txns_m5 = (p.get("txns") or {}).get("m5") or {}
                        bc_m5_buys = int(bc_txns_m5.get("buys") or 0)
                        if bc_m5_buys < BREAKOUT_WASH_OVERRIDE_BUYERS:
                            continue
                        # Override fires — log so we can audit how often the relaxed
                        # path actually triggers and what its outcomes look like.
                        print(f"[breakout-scout] 🌊 {mint[:8]} wash-override: "
                              f"vol/liq={vol_m5/liq_usd:.1f}x but m5_buys={bc_m5_buys} "
                              f"≥ {BREAKOUT_WASH_OVERRIDE_BUYERS} — viral momentum, allow")
                    txns_h1 = (p.get("txns") or {}).get("h1") or {}
                    buys = txns_h1.get("buys") or 0
                    sells = txns_h1.get("sells") or 0
                    total = buys + sells
                    if total < BREAKOUT_MIN_H1_TXNS:
                        continue
                    br = (buys / total) * 100 if total > 0 else 0
                    if br < BREAKOUT_MIN_BUY_RATIO_PCT:
                        continue  # wash/distribution — need buyer dominance
                    base_info = p.get("info") or {}
                    if not (base_info.get("socials") or base_info.get("websites")):
                        continue

                    # Buy velocity — skip decelerating breakouts (the candle
                    # already happened; if buys aren't accelerating we're late)
                    _br_velocity = _buy_velocity_ratio(p)
                    if _br_velocity is not None and _br_velocity < MONSTER_BUY_VELOCITY_MIN_RATIO:
                        continue

                    # Holder count floor — same rationale as lifecycle scout
                    try:
                        _br_holders = int(base_info.get("holders")) if base_info.get("holders") is not None else None
                    except (ValueError, TypeError):
                        _br_holders = None
                    if _br_holders is not None and _br_holders < MONSTER_MIN_UNIQUE_HOLDERS:
                        continue

                    _dist = await top_wallet_distribution(session, mint)
                    t1 = (_dist or {}).get("top1_pct")
                    t10 = (_dist or {}).get("top10_pct")
                    if t1 is None or t1 >= BREAKOUT_TOP1_MAX_PCT:
                        continue
                    if t10 is not None and t10 >= MONSTER_TOP10_MAX_PCT:
                        continue  # top-10 concentration = rug setup, skip breakout

                    # Creator burn check
                    _br_burned, _br_creator = await is_creator_burned(session, mint)
                    if _br_burned:
                        print(f"[monster-breakout] 🚫 {mint[:8]} creator={_br_creator[:8]} recently burned us — skip")
                        continue

                    # Rugcheck: block unlocked-LP danger tokens (LP pull = what killed MOONDOGE)
                    try:
                        from elizaos.plugins.solana.axiom_copy_trader import _quick_safety_check
                        safe, rc_reason = await _quick_safety_check(mint, session)
                        if not safe:
                            print(f"[monster-breakout] 🛡 rugcheck block {mint[:8]}: {rc_reason}")
                            continue
                    except Exception as _rc_err:
                        print(f"[monster-breakout] rugcheck import error: {_rc_err} — allowing")

                    _mark_signalled(mint)
                    print(f"[monster-breakout] 🎯 {mint[:8]} BREAKOUT age={age_secs/60:.0f}min "
                          f"m5={m5:+.0f}% h1={h1:+.0f}% liq=${liq_usd:,.0f} vol_m5=${vol_m5:,.0f} top1={t1}% top10={t10}%")
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
                        sol_size=monster.get_default_size_sol(),
                        session=session,
                        runtime=runtime,
                        metadata={
                            "age_min": round(age_secs / 60, 1),
                            "liq_usd": liq_usd, "mc_usd": mc_usd,
                            "m5": m5, "h1": h1, "h6": h6, "h24": h24,
                            "m5_change": m5, "h1_change": h1,
                            "vol_m5_usd": vol_m5, "buy_ratio": br,
                            "top1_pct": t1,
                            "top10_pct": t10,
                            "pct_off_peak_at_entry": breakout_peak_ratio,
                            "holders_at_entry": _br_holders,
                            "buy_velocity_ratio": _br_velocity,
                            "creator": _br_creator,
                            "smart_money_overlap": await _try_smart_money_overlap(session, mint),
                        },
                    )
                except Exception:
                    continue
        except Exception as e:
            print(f"[monster-breakout] loop error: {e}")
        await asyncio.sleep(BREAKOUT_POLL_SECS)
