"""
smart_money_tracker.py — Auto-discover smart money wallets from winning trades.

After every winning trade, the top holder wallets at entry time are cross-referenced
across all past wins. Wallets appearing in 3+ winning trades are promoted to
"smart money" tier and added to the watchlist.

During A2 scanning: if a current token has smart money wallets as holders,
Jarvis is alerted immediately and the token gets an automatic score boost.

Store: smart_money_wallets.json
  {
    "wallets": {
      "wallet_addr": {
        "win_count": 3,
        "total_seen": 5,
        "wins": ["mint1", "mint2", "mint3"],
        "first_seen_ts": 1234567890,
        "last_seen_ts": 1234567892,
        "promoted_ts": 1234567892
      }
    }
  }
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

_STORE_PATH = os.path.join(os.path.dirname(__file__), "smart_money_wallets.json")
_MAX_WALLET_WINS = 100  # cap wins list per wallet

_store: dict | None = None

# ─── Threshold ────────────────────────────────────────────────────────────────
WIN_THRESHOLD = 3  # wins in which a wallet appears before it's "smart money"


# ─── Persistence ──────────────────────────────────────────────────────────────

def _load() -> dict:
    global _store
    if _store is not None:
        return _store
    try:
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH) as f:
                data = json.load(f)
            _store = data if isinstance(data, dict) else {"wallets": {}}
        else:
            _store = {"wallets": {}}
    except Exception:
        _store = {"wallets": {}}
    return _store


def _save() -> None:
    global _store
    if _store is None:
        return
    try:
        with open(_STORE_PATH, "w") as f:
            json.dump(_store, f, indent=2)
    except Exception:
        pass


# ─── Core API ─────────────────────────────────────────────────────────────────

def record_win_appearances(mint: str, wallets: list[str]) -> list[str]:
    """Record that these wallets were early holders of a winning token.

    Returns list of newly promoted smart money wallets (if any hit the threshold).
    """
    if not wallets:
        return []

    store = _load()
    ws: dict[str, Any] = store.setdefault("wallets", {})
    now = time.time()
    newly_promoted: list[str] = []

    for wallet in wallets:
        if not wallet or len(wallet) < 32:  # sanity check for valid Solana pubkey
            continue
        entry = ws.setdefault(wallet, {
            "win_count": 0,
            "total_seen": 0,
            "wins": [],
            "first_seen_ts": now,
            "last_seen_ts": now,
        })
        # Avoid double-counting same win
        if mint in entry.get("wins", []):
            entry["total_seen"] = entry.get("total_seen", 0) + 1
            entry["last_seen_ts"] = now
            continue

        entry["win_count"] = entry.get("win_count", 0) + 1
        entry["total_seen"] = entry.get("total_seen", 0) + 1
        entry["last_seen_ts"] = now
        wins_list = entry.setdefault("wins", [])
        wins_list.append(mint)
        if len(wins_list) > _MAX_WALLET_WINS:
            entry["wins"] = wins_list[-_MAX_WALLET_WINS:]

        # Promote to smart money if threshold reached
        if entry["win_count"] >= WIN_THRESHOLD and "promoted_ts" not in entry:
            entry["promoted_ts"] = now
            newly_promoted.append(wallet)

    _save()
    return newly_promoted


def record_all_appearances(mint: str, wallets: list[str]) -> None:
    """Record that these wallets were holders, regardless of win/loss outcome.
    Only increments total_seen (not win_count). Used for loss/rug tracking.
    """
    if not wallets:
        return
    store = _load()
    ws: dict[str, Any] = store.setdefault("wallets", {})
    now = time.time()
    for wallet in wallets:
        if not wallet or len(wallet) < 32:
            continue
        entry = ws.get(wallet)
        if entry is None:
            continue  # don't create entries for non-win appearances
        entry["total_seen"] = entry.get("total_seen", 0) + 1
        entry["last_seen_ts"] = now
    _save()


def get_smart_money_set() -> set[str]:
    """Return the set of promoted smart money wallet addresses."""
    store = _load()
    return {
        addr
        for addr, data in store.get("wallets", {}).items()
        if data.get("promoted_ts") is not None
    }


def is_smart_money(wallet: str) -> bool:
    store = _load()
    entry = store.get("wallets", {}).get(wallet)
    return bool(entry and entry.get("promoted_ts") is not None)


def get_all_tracked() -> dict[str, dict]:
    """Return all wallet entries (promoted + pending)."""
    return dict(_load().get("wallets", {}))


def get_stats() -> dict:
    """Summary stats for Jarvis reporting."""
    store = _load()
    ws = store.get("wallets", {})
    promoted = [v for v in ws.values() if v.get("promoted_ts")]
    pending = [v for v in ws.values() if not v.get("promoted_ts")]
    return {
        "total_tracked": len(ws),
        "smart_money_count": len(promoted),
        "pending_count": len(pending),
        "top_smart_money": sorted(
            [(addr, v["win_count"]) for addr, v in ws.items() if v.get("promoted_ts")],
            key=lambda x: -x[1],
        )[:10],
    }


def get_wallet_detail(wallet: str) -> dict | None:
    """Return details for a specific wallet."""
    store = _load()
    return store.get("wallets", {}).get(wallet)


async def resolve_token_account_owners(
    token_accounts: list[str],
    rpc: Any,
) -> dict[str, str]:
    """Resolve SPL token account addresses → owner wallet addresses.

    Uses getMultipleAccounts with jsonParsed encoding.
    Returns {token_account_addr: owner_wallet_addr}.
    """
    if not token_accounts:
        return {}

    result: dict[str, str] = {}
    BATCH = 100

    for i in range(0, len(token_accounts), BATCH):
        batch = token_accounts[i:i + BATCH]
        try:
            accounts = await rpc.get_multiple_accounts(batch, encoding="jsonParsed")
            for addr, acct in zip(batch, accounts or []):
                if not acct:
                    continue
                # jsonParsed: acct["data"]["parsed"]["info"]["owner"]
                parsed = (acct.get("data") or {})
                if isinstance(parsed, dict):
                    owner = parsed.get("parsed", {}).get("info", {}).get("owner", "")
                    if owner:
                        result[addr] = owner
        except Exception:
            pass

    return result
