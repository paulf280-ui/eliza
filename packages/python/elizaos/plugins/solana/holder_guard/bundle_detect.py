"""holder_guard.bundle_detect — same-block bundle-bot detection.

Implementation of Algorithm 1 from Luo et al. 2026 ("Resisting Manipulative
Bots in Meme Coin Copy Trading"). A bundle bot is a wallet controlled by the
creator that buys the mint within the same Solana slot as the mint's
creation. Solana has no public mempool and ~400ms blocks, so same-slot
non-creator buys cannot be naive front-running — they imply pre-coordinated
control.

We use Solana JSON-RPC `getSignaturesForAddress` (1000 sigs per page) to
walk back to the mint's earliest signatures cheaply, then fetch only the
genesis-slot transactions in full. This reaches further back than Helius's
enhanced /v0 API would in the same number of calls.

Returns:
  True  — bundle pattern detected (≥1 non-creator buy at creation slot)
  False — no bundle pattern at the creation slot
  None  — couldn't determine (RPC failure, history exceeds MAX_SIG_PAGES,
          or genesis tx couldn't be parsed)

Catches the dumb 80% of bundle attacks (same-slot coordination). Does NOT
catch "gradual bundles" where the creator funds wallets that buy in
subsequent blocks — the paper notes those need candlestick-pattern analysis.
"""
from __future__ import annotations

import os
import time

import aiohttp

SIG_PAGE_SIZE = 1000          # Solana RPC max
MAX_SIG_PAGES = 25            # 25,000 sigs walked back. Hot tokens (>200 sigs/min sustained
                              # for 90+ min) won't reach genesis here and return None.
                              # Lifecycle scout targets 30-90min old tokens, where this is
                              # almost always sufficient.
MAX_GENESIS_TX_PROBES = 20    # cap full-tx fetches at the genesis slot
RPC_TIMEOUT_SECS = 10
TX_TIMEOUT_SECS = 6

# Per-mint result cache so repeated scout signals on the same token don't
# re-walk thousands of sigs. Includes failures (None verdict) so we don't
# retry hopelessly on hot tokens within the same window.
_CACHE_TTL_SECS = 300
_cache: dict[str, tuple[float, bool | None]] = {}


def _rpc_url() -> str:
    key = os.getenv("HELIUS_API_KEY", "")
    return (
        f"https://mainnet.helius-rpc.com/?api-key={key}"
        if key else "https://api.mainnet-beta.solana.com"
    )


async def _rpc(session: aiohttp.ClientSession, method: str, params: list, timeout: float) -> dict | None:
    """Returns the parsed JSON response, or None on RPC failure (timeout / non-200 / exception).

    Returning None lets callers distinguish between a successful empty
    response (e.g. no more signatures) and a failure (e.g. timeout). Empty
    on success means reached oldest; None means we don't know.
    """
    try:
        async with session.post(
            _rpc_url(),
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


async def detect_bundle_bot(
    session: aiohttp.ClientSession,
    mint: str,
    creator: str | None = None,
) -> bool | None:
    """Algorithm 1: non-creator buy at the mint's creation slot ⇒ bundle bot."""
    # ── Cache check ─────────────────────────────────────────────────────────
    cached = _cache.get(mint)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECS:
        return cached[1]

    verdict = await _detect_uncached(session, mint, creator)
    _cache[mint] = (time.time(), verdict)
    return verdict


async def _detect_uncached(
    session: aiohttp.ClientSession,
    mint: str,
    creator: str | None,
) -> bool | None:
    # ── Phase 1: walk getSignaturesForAddress back to the mint's first sig ──
    all_sigs: list[dict] = []
    before: str | None = None
    reached_oldest = False

    for _ in range(MAX_SIG_PAGES):
        params: list = [mint, {"limit": SIG_PAGE_SIZE}]
        if before:
            params[1]["before"] = before
        resp = await _rpc(session, "getSignaturesForAddress", params, RPC_TIMEOUT_SECS)
        if resp is None:
            return None  # RPC failure — abort, don't pretend we reached genesis
        results = resp.get("result") or []
        if not results:
            reached_oldest = True
            break
        all_sigs.extend(results)
        if len(results) < SIG_PAGE_SIZE:
            reached_oldest = True
            break
        before = results[-1].get("signature")
        if not before:
            reached_oldest = True
            break

    if not all_sigs or not reached_oldest:
        return None

    # ── Phase 2: identify genesis slot (oldest signature's slot) ────────────
    all_sigs.sort(key=lambda s: int(s.get("slot") or 0))
    genesis_slot = all_sigs[0].get("slot")
    if genesis_slot is None:
        return None
    genesis_sigs = [s for s in all_sigs if s.get("slot") == genesis_slot]

    # ── Phase 3: fetch full tx details for genesis-slot signatures ─────────
    txs: list[dict] = []
    for s in genesis_sigs[:MAX_GENESIS_TX_PROBES]:
        sig = s.get("signature")
        if not sig:
            continue
        tx_resp = await _rpc(
            session,
            "getTransaction",
            [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
            TX_TIMEOUT_SECS,
        )
        if tx_resp is None:
            continue  # individual tx failure is OK, keep going
        result = tx_resp.get("result")
        if result:
            txs.append(result)

    if not txs:
        return None

    # ── Phase 4: derive creator from earliest tx's feePayer if not supplied ─
    derived_creator = creator
    if not derived_creator:
        # Find the txs message and pull feePayer (account index 0)
        first_tx = txs[0]
        try:
            account_keys = first_tx["transaction"]["message"]["accountKeys"]
            # account_keys can be list of strings or list of {"pubkey": ..., "signer": True} dicts
            first = account_keys[0]
            derived_creator = first if isinstance(first, str) else first.get("pubkey")
        except Exception:
            derived_creator = None
    if not derived_creator:
        return None

    # ── Phase 5: scan all genesis-slot txs for non-creator token receipts ──
    non_creator_buyers: set[str] = set()
    for tx in txs:
        meta = tx.get("meta") or {}
        # Use post/preTokenBalances for receipts of THIS mint
        post = meta.get("postTokenBalances") or []
        pre  = meta.get("preTokenBalances") or []
        # Map account_index → pre amount for this mint
        pre_map: dict[int, float] = {}
        for b in pre:
            if b.get("mint") != mint:
                continue
            try:
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            except (ValueError, TypeError):
                amt = 0.0
            pre_map[int(b.get("accountIndex", -1))] = amt
        # Resolve account_keys for owner mapping
        try:
            account_keys = tx["transaction"]["message"]["accountKeys"]
        except Exception:
            account_keys = []
        for b in post:
            if b.get("mint") != mint:
                continue
            owner = b.get("owner")
            if not owner or owner == derived_creator:
                continue
            try:
                post_amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            except (ValueError, TypeError):
                post_amt = 0.0
            pre_amt = pre_map.get(int(b.get("accountIndex", -1)), 0.0)
            if post_amt > pre_amt:
                non_creator_buyers.add(owner)

    return len(non_creator_buyers) > 0
