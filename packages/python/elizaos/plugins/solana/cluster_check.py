"""cluster_check.py — Bundler / coordinated-wallet detection via native Solana RPC.

Detects token launches where multiple top holders were funded by the same
master wallet around the token's creation time — the classic bundler/insider
fingerprint visible on Bubblemaps. No third-party API required; all data
comes from standard Helius JSON-RPC calls included in the existing subscription.

How it works:
  1. getTokenLargestAccounts  → top 15 holder token accounts
  2. getAccountInfo (batched) → resolve token account → owner wallet
  3. getSignaturesForAddress  → walk backwards through each wallet's history
  4. getTransaction           → parse SOL balance deltas for inflow sender
  5. Group by shared funder   → clusters ≥ 2 wallets from same source = risk

Design principles:
  - Fails OPEN: any RPC error returns CLEAN so no entry is ever blocked
    by connectivity issues.
  - 25-second hard timeout on the entire check.
  - Tokens older than 8 hours are skipped (funding trace impractical).
  - High-volume funders (>30 wallets funded = exchange/infra) are ignored.
  - Called once per token when it first enters the deferred pool, result
    cached in _lc_entry["cluster"] for subsequent cycle checks.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import aiohttp

# ── Programs / pool vaults to ignore as token-account owners ────────────────
_SKIP_OWNERS: frozenset[str] = frozenset({
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM vault
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun bonding curve
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token program
    "TokenzQdBNbequAOoiqE29oRarev2WMxsoMwPth4Kzu",  # SPL Token-2022
    "11111111111111111111111111111111",               # System program
    "So11111111111111111111111111111111111111112",    # Wrapped SOL
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bv",  # Associated Token program
})

# ── Tuning constants ─────────────────────────────────────────────────────────
# Funding window around PAIR creation. pair_created_ts is the graduation
# moment, but cabal wallets are funded during the BONDING-CURVE phase which
# can run hours earlier — a 10min pre-window missed nearly all real funding
# (every token scored 0). 6h covers the curve phase of almost all launches.
_WINDOW_BEFORE_SECS   = 6 * 3600  # look 6h before pair creation
_WINDOW_AFTER_SECS    = 1800      # and 30 min after (snipers buy fast)
_MAX_TOKEN_AGE_HOURS  = 8.0    # skip check for tokens older than this
_MAX_SIGS_PER_WALLET  = 150    # cap pagination to control RPC cost
_MIN_SOL_INFLOW       = 5_000_000  # 0.005 SOL minimum — ignore dust/rent
_EXCHANGE_THRESHOLD   = 30     # funders with >30 cluster members = exchange
_HIGH_RISK_WALLETS    = 3      # ≥3 wallets from same funder = HIGH
_MEDIUM_RISK_WALLETS  = 2      # 2 wallets = MEDIUM
_CHECK_TIMEOUT_SECS   = 25.0   # hard timeout on entire check


# ── RPC helper ───────────────────────────────────────────────────────────────

async def _rpc(
    session: aiohttp.ClientSession,
    method: str,
    params: list,
    timeout: float = 5.0,
) -> Any:
    url = os.getenv("SOLANA_RPC_URL", "")
    if not url:
        return None
    for attempt in range(3):  # 1 try + 2 retries — survives 429 bursts
        try:
            async with _rpc_sem():
                async with session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    if r.status == 200:
                        return (await r.json()).get("result")
                    if r.status not in (429, 500, 502, 503):
                        return None  # 4xx errors won't improve on retry
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(0.4 * (attempt + 1))
    return None


# Throttle concurrent RPC calls — pre-indexing + funder walks + slot checks
# can burst 40+ parallel requests per token and trip Helius rate limits.
_rpc_semaphore: asyncio.Semaphore | None = None

def _rpc_sem() -> asyncio.Semaphore:
    global _rpc_semaphore
    if _rpc_semaphore is None:
        _rpc_semaphore = asyncio.Semaphore(10)
    return _rpc_semaphore


# ── Step 1 helpers ───────────────────────────────────────────────────────────

async def _resolve_owner_checked(
    session: aiohttp.ClientSession, token_acct: str
) -> tuple[str | None, bool]:
    """Return (owner_wallet, lookup_ok) for a token account.

    lookup_ok=False means the RPC call itself failed — the account is NOT
    necessarily a pool. Callers must not label failures as "LP Pool";
    that bug once cached 15×"LP Pool" garbage for 8 hours.
    """
    info = await _rpc(session, "getAccountInfo",
                      [token_acct, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    if not info:
        return None, False   # RPC failure — unknown, not a verified pool
    val  = info.get("value") or {}
    data = val.get("data") or {}
    if not isinstance(data, dict):
        return None, True    # raw (non-parsed) data = genuine program/pool account
    owner = (data.get("parsed") or {}).get("info", {}).get("owner", "")
    return (owner if owner and owner not in _SKIP_OWNERS else None), True


async def _resolve_owner(
    session: aiohttp.ClientSession, token_acct: str
) -> str | None:
    """Return owner wallet of a token account, or None if it is a program/pool."""
    owner, _ok = await _resolve_owner_checked(session, token_acct)
    return owner


# ── Step 2 helpers ───────────────────────────────────────────────────────────

def _parse_sol_sender(tx: dict, target_wallet: str) -> str | None:
    """
    Given a parsed transaction, return the account that sent SOL to target_wallet.
    Returns None if no qualifying inflow found.
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None

    pre_bals  = meta.get("preBalances") or []
    post_bals = meta.get("postBalances") or []
    msg       = (tx.get("transaction") or {}).get("message") or {}

    # Build account key list (handles both legacy and versioned tx formats)
    acct_keys: list[str] = []
    for ak in msg.get("accountKeys") or []:
        acct_keys.append(ak["pubkey"] if isinstance(ak, dict) else str(ak))

    if target_wallet not in acct_keys:
        return None

    w_idx = acct_keys.index(target_wallet)
    if w_idx >= len(pre_bals) or w_idx >= len(post_bals):
        return None

    received = post_bals[w_idx] - pre_bals[w_idx]
    if received < _MIN_SOL_INFLOW:
        return None  # no meaningful inflow to this wallet

    # Find the account whose balance decreased (the sender)
    for i, (pre, post) in enumerate(zip(pre_bals, post_bals)):
        if i == w_idx:
            continue
        if pre - post >= _MIN_SOL_INFLOW:
            sender = acct_keys[i] if i < len(acct_keys) else ""
            if sender and sender not in _SKIP_OWNERS:
                return sender
    return None


async def _first_slot_of_token_account(
    session: aiohttp.ClientSession,
    token_acct: str,
) -> int | None:
    """Return the slot of the OLDEST transaction on a token account.

    Token accounts are per-token so histories are short; we page max 3×100.
    The oldest tx is the account creation / first buy — if several top holders
    share the same slot, they bought in the same block (Jito bundle signature).
    """
    before: str | None = None
    oldest: dict | None = None
    for _ in range(3):
        params: dict = {"limit": 100, "commitment": "confirmed"}
        if before:
            params["before"] = before
        sigs = await _rpc(session, "getSignaturesForAddress", [token_acct, params])
        if not sigs:
            break
        oldest = sigs[-1]
        if len(sigs) < 100:
            break
        before = oldest["signature"]
    return (oldest or {}).get("slot")


async def _find_funder_in_window(
    session: aiohttp.ClientSession,
    wallet: str,
    window_start: float,
    window_end: float,
) -> str | None:
    """
    Walk backwards through a wallet's transaction history and return the first
    SOL sender found within [window_start, window_end], or None.

    Iterates in pages (newest → oldest) and stops as soon as a matching
    transaction is found or the window is passed.
    """
    before_sig: str | None = None
    total_checked = 0

    while total_checked < _MAX_SIGS_PER_WALLET:
        fetch = min(50, _MAX_SIGS_PER_WALLET - total_checked)
        params: dict = {"limit": fetch, "commitment": "confirmed"}
        if before_sig:
            params["before"] = before_sig

        sigs = await _rpc(session, "getSignaturesForAddress", [wallet, params])
        if not sigs:
            break

        total_checked += len(sigs)

        for sig_info in sigs:
            bt = sig_info.get("blockTime") or 0
            if bt > window_end:
                continue   # too recent — keep scanning backwards
            if bt < window_start:
                return None  # past the window entirely — stop

            if sig_info.get("err"):
                continue

            tx = await _rpc(session, "getTransaction", [
                sig_info["signature"],
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ])
            if not tx:
                continue

            sender = _parse_sol_sender(tx, wallet)
            if sender:
                return sender

        if len(sigs) < fetch:
            break  # no more history

        oldest_bt = sigs[-1].get("blockTime") or 0
        if oldest_bt < window_start:
            break  # gone past our window in this page

        before_sig = sigs[-1]["signature"]

    return None


# ── Main entry point ─────────────────────────────────────────────────────────

async def check_holder_clusters(
    session: aiohttp.ClientSession,
    mint: str,
    token_created_ts: float,
    top_n: int = 15,
) -> dict:
    """
    Cluster-check the top N holders of a token.

    Returns a dict:
      risk:            "HIGH" | "MEDIUM" | "CLEAN"
      clusters:        list[{master, master_full, wallets, combined_pct, risk}]
      wallets_checked: int  (how many wallets had their history scanned)
      skip_reason:     str | None  (why we bailed out early, if we did)
    """
    def _clean(reason: str | None = None) -> dict:
        return {
            "risk": "CLEAN",
            "clusters": [],
            "wallets_checked": 0,
            "skip_reason": reason,
        }

    # Guard: token too old for reliable funding trace
    token_age_h = (time.time() - token_created_ts) / 3600
    if token_age_h > _MAX_TOKEN_AGE_HOURS:
        return _clean("token_too_old")

    window_start = token_created_ts - _WINDOW_BEFORE_SECS
    window_end   = token_created_ts + _WINDOW_AFTER_SECS

    # ── Step 1: Top holder token accounts ─────────────────────────────────
    h_result = await _rpc(session, "getTokenLargestAccounts",
                          [mint, {"commitment": "confirmed"}])
    if not h_result:
        return _clean("rpc_error_holders")

    accounts = (h_result.get("value") or [])[:top_n]
    if len(accounts) < 3:
        return _clean("too_few_holders")

    # Approximate total supply (top 15 typically covers ~80-90%)
    top_sum = sum(float(a.get("uiAmount") or 0) for a in accounts)
    total_supply_est = top_sum / 0.85 if top_sum > 0 else 1.0

    # ── Step 2: Resolve token account → owner wallet (batched) ────────────
    owners_raw: list[str | None] = await asyncio.gather(
        *[_resolve_owner(session, a["address"]) for a in accounts]
    )
    owner_wallets: list[tuple[str, float]] = [
        (owner, float(accounts[i].get("uiAmount") or 0))
        for i, owner in enumerate(owners_raw)
        if owner
    ]

    if len(owner_wallets) < 3:
        return _clean("too_few_resolved")

    # ── Step 3: Find SOL funder for each wallet in the creation window ─────
    # Run in batches of 5 to avoid hammering the RPC
    wallets_to_check = [w for w, _ in owner_wallets[:12]]
    funder_map: dict[str, str] = {}

    async def _check_one(wallet: str) -> tuple[str, str | None]:
        try:
            funder = await _find_funder_in_window(
                session, wallet, window_start, window_end
            )
            return wallet, funder
        except Exception:
            return wallet, None

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECS):
            results = await asyncio.gather(*[_check_one(w) for w in wallets_to_check])
    except (asyncio.TimeoutError, Exception):
        return _clean("timeout")

    for wallet, funder in results:
        if funder:
            funder_map[wallet] = funder

    if not funder_map:
        return _clean()  # no funding found in window → CLEAN

    # ── Step 4: Group wallets by shared funder → detect clusters ──────────
    funder_groups: dict[str, list[str]] = {}
    for wallet, funder in funder_map.items():
        funder_groups.setdefault(funder, []).append(wallet)

    clusters: list[dict] = []
    for funder, cluster_wallets in funder_groups.items():
        n = len(cluster_wallets)
        if n < _MEDIUM_RISK_WALLETS:
            continue
        if n > _EXCHANGE_THRESHOLD:
            continue  # exchange / CEX hot wallet — not a bundler

        combined_ui = sum(amt for w, amt in owner_wallets if w in cluster_wallets)
        combined_pct = round(combined_ui / total_supply_est * 100, 1)
        risk = "HIGH" if n >= _HIGH_RISK_WALLETS else "MEDIUM"

        clusters.append({
            "master":       funder[:20] + "...",
            "master_full":  funder,
            "wallets":      n,
            "combined_pct": combined_pct,
            "risk":         risk,
        })

    clusters.sort(key=lambda x: (-x["wallets"], -x["combined_pct"]))

    overall_risk = "CLEAN"
    if any(c["risk"] == "HIGH" for c in clusters):
        overall_risk = "HIGH"
    elif clusters:
        overall_risk = "MEDIUM"

    return {
        "risk":            overall_risk,
        "clusters":        clusters[:3],
        "wallets_checked": len(funder_map),
        "skip_reason":     None,
    }


# ── Visual map entry point ────────────────────────────────────────────────────

# Known CEX / infrastructure addresses to label specially
_KNOWN_LABELS: dict[str, str] = {
    "5Q4aF1UefAcZMkKRnrYiLZhFrR7dZ3bsYvBKsUMGFVN": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Coinbase",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Kraken",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "OKX",
    "GugU1tP7doLeTw9hQP51xmJyg5uYTBign4K4BbsBug6K": "Bybit",
}


async def get_cluster_map(
    session: aiohttp.ClientSession,
    mint: str,
    token_created_ts: float,
    top_n: int = 15,
) -> dict:
    """
    Full visual data for the dashboard bubble-map panel.
    Returns per-holder data with cluster assignments, suitable for SVG rendering.

    Structure:
      risk:         "HIGH" | "MEDIUM" | "CLEAN"
      holders:      list of {rank, address, address_short, pct, cluster_id, is_lp, label}
      clusters:     list of {id, master_short, wallet_count, combined_pct, risk}
      total_supply_est: float
      wallets_checked:  int
      skip_reason:  str | None
      computed_at:  float (unix ts)
    """
    window_start = token_created_ts - _WINDOW_BEFORE_SECS
    window_end   = token_created_ts + _WINDOW_AFTER_SECS

    # ── Holders ──────────────────────────────────────────────────────────────
    h_result = await _rpc(session, "getTokenLargestAccounts",
                          [mint, {"commitment": "confirmed"}])
    if not h_result:
        return {"risk": "CLEAN", "cabal_score": 0.0, "is_controlled": False,
                "token_name": "", "holders": [], "clusters": [],
                "total_supply_est": 0, "wallets_checked": 0,
                "skip_reason": "rpc_error", "computed_at": time.time()}

    accounts = (h_result.get("value") or [])[:top_n]
    top_sum = sum(float(a.get("uiAmount") or 0) for a in accounts)
    total_supply_est = top_sum / 0.85 if top_sum > 0 else 1.0

    # Resolve owner wallets concurrently (lookup_ok distinguishes RPC failure
    # from a genuine pool account — failures must never be labelled "LP Pool")
    owners_checked = await asyncio.gather(
        *[_resolve_owner_checked(session, a["address"]) for a in accounts]
    )
    lookup_failures = sum(1 for _o, ok in owners_checked if not ok)

    # Build holder list with LP/pool detection
    raw_holders: list[dict] = []
    owner_wallets: list[tuple[str, float]] = []
    owner_token_accts: dict[str, str] = {}  # owner wallet → token account
    for i, (acc, (owner, lookup_ok)) in enumerate(zip(accounts, owners_checked)):
        ui   = float(acc.get("uiAmount") or 0)
        pct  = round(ui / total_supply_est * 100, 2)
        addr = owner or acc["address"]
        is_lp = lookup_ok and (owner is None)  # verified program/pool account
        label = _KNOWN_LABELS.get(addr) or ("LP Pool" if is_lp else None)
        raw_holders.append({
            "rank":          i + 1,
            "address":       addr,
            "address_short": addr[:6] + "…" + addr[-4:],
            "ui_amount":     ui,
            "pct":           pct,
            "cluster_id":    None,
            "is_lp":         is_lp,
            "label":         label,
        })
        if owner is not None:
            owner_wallets.append((addr, ui))
            owner_token_accts[addr] = acc["address"]

    # ── Cluster detection ────────────────────────────────────────────────────
    # No age cap here (unlike check_holder_clusters): the SaaS bubble map must
    # work for any token a user pastes in. Cabal wallets are typically fresh
    # one-shot wallets with short histories, so the bounded funder walk
    # (_MAX_SIGS_PER_WALLET) stays cheap even for tokens that are months old.
    funder_map: dict[str, str] = {}
    first_slot_map: dict[str, int] = {}  # owner wallet → slot of first buy
    wallets_to_check = [w for w, _ in owner_wallets[:12]]

    async def _check_one(wallet: str) -> tuple[str, str | None]:
        try:
            return wallet, await _find_funder_in_window(
                session, wallet, window_start, window_end
            )
        except Exception:
            return wallet, None

    async def _slot_one(wallet: str) -> tuple[str, int | None]:
        try:
            return wallet, await _first_slot_of_token_account(
                session, owner_token_accts[wallet]
            )
        except Exception:
            return wallet, None

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECS):
            funder_results, slot_results = await asyncio.gather(
                asyncio.gather(*[_check_one(w) for w in wallets_to_check]),
                asyncio.gather(*[_slot_one(w) for w in wallets_to_check]),
            )
        for wallet, funder in funder_results:
            if funder:
                funder_map[wallet] = funder
        for wallet, slot in slot_results:
            if slot:
                first_slot_map[wallet] = slot
    except (asyncio.TimeoutError, Exception):
        pass

    # Build cluster objects
    funder_groups: dict[str, list[str]] = {}
    for wallet, funder in funder_map.items():
        funder_groups.setdefault(funder, []).append(wallet)

    clusters: list[dict] = []
    cluster_id = 0
    wallet_to_cluster: dict[str, int] = {}

    for funder, cluster_wallets in sorted(
        funder_groups.items(), key=lambda kv: -len(kv[1])
    ):
        n = len(cluster_wallets)
        if n < _MEDIUM_RISK_WALLETS or n > _EXCHANGE_THRESHOLD:
            continue
        combined_ui  = sum(amt for w, amt in owner_wallets if w in cluster_wallets)
        combined_pct = round(combined_ui / total_supply_est * 100, 1)
        risk = "HIGH" if n >= _HIGH_RISK_WALLETS else "MEDIUM"
        clusters.append({
            "id":           cluster_id,
            "type":         "funding",
            "master_short": funder[:6] + "…" + funder[-4:],
            "master_full":  funder,
            "wallet_count": n,
            "combined_pct": combined_pct,
            "risk":         risk,
        })
        for w in cluster_wallets:
            wallet_to_cluster[w] = cluster_id
        cluster_id += 1

    # ── Time-sync detection: same-block (same-slot) first buys ──────────────
    # ≥3 top holders whose token accounts were created in the EXACT same slot
    # bought in the same block — the signature of a Jito-bundled multi-wallet
    # launch. Catches stealth bundles that route funding through intermediaries
    # (invisible to the funding trace above).
    slot_groups: dict[int, list[str]] = {}
    for wallet, slot in first_slot_map.items():
        if wallet in wallet_to_cluster:
            continue  # already in a funding cluster — don't double count
        slot_groups.setdefault(slot, []).append(wallet)

    time_sync = False
    for slot, sync_wallets in sorted(slot_groups.items(), key=lambda kv: -len(kv[1])):
        n = len(sync_wallets)
        if n < 3:
            continue
        time_sync = True
        combined_ui  = sum(amt for w, amt in owner_wallets if w in sync_wallets)
        combined_pct = round(combined_ui / total_supply_est * 100, 1)
        clusters.append({
            "id":           cluster_id,
            "type":         "time_sync",
            "master_short": "same-block bundle",
            "master_full":  f"slot {slot}",
            "wallet_count": n,
            "combined_pct": combined_pct,
            "risk":         "HIGH" if n >= 4 else "MEDIUM",
        })
        for w in sync_wallets:
            wallet_to_cluster[w] = cluster_id
        cluster_id += 1

    # Annotate holders with cluster IDs
    for h in raw_holders:
        cid = wallet_to_cluster.get(h["address"])
        if cid is not None:
            h["cluster_id"] = cid

    overall_risk = "CLEAN"
    if any(c["risk"] == "HIGH" for c in clusters):
        overall_risk = "HIGH"
    elif clusters:
        overall_risk = "MEDIUM"

    # Compute cabal_score: % of supply held by coordinated wallets
    coordinated_pct = sum(float(c.get("combined_pct", 0)) for c in clusters)
    holder_pct_sum = sum(float(h.get("pct", 0)) for h in raw_holders) if raw_holders else 100.0
    cabal_score = round(min(coordinated_pct / holder_pct_sum * 100, 100.0), 1) if holder_pct_sum > 0 else 0.0
    is_controlled = (overall_risk == "HIGH" or cabal_score >= 35.0)

    return {
        "risk":             overall_risk,
        "cabal_score":      cabal_score,
        "is_controlled":    is_controlled,
        "time_sync":        time_sync,
        "token_name":       "",
        "holders":          raw_holders,
        "clusters":         clusters,
        "total_supply_est": round(total_supply_est),
        "wallets_checked":  len(wallets_to_check),
        "funders_found":    len(funder_map),
        # degraded = too many owner lookups failed (RPC trouble) — callers
        # must NOT cache this result; serve it once and let the next query retry
        "degraded":         lookup_failures >= max(2, len(accounts) // 3),
        "skip_reason":      None,
        "computed_at":      time.time(),
    }
