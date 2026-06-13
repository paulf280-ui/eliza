"""cex_labels.py — resolve wallet addresses to exchange / entity names.

Uses the Helius Wallet Identity API (12,500+ maintained labels) — the same
provider we already pay for. We NEVER hard-code or guess exchange addresses:
a name only appears if Helius returns one, so the CEX-funding breakdown can't
mislabel a wallet. Unknown addresses are grouped honestly, never named.

Identity labels are stable, so results are cached in-process for the session.
"""

from __future__ import annotations

import os
import re
from typing import Any

import aiohttp

_BATCH_URL = "https://api.helius.xyz/v1/wallet/batch-identity"
_TIMEOUT   = 8.0

# address → {"name": str|None, "type": str, "category": str, "icon": str}
_cache: dict[str, dict] = {}

# "Binance 1" / "Coinbase 2" / "OKX Hot Wallet" → "Binance" / "Coinbase" / "OKX"
_SUFFIX = re.compile(r"\s+\d+$")


def _base_name(name: str) -> str:
    """Collapse 'Binance 1', 'Binance 2' → 'Binance' so per-exchange totals add up."""
    n = _SUFFIX.sub("", (name or "").strip())
    # Trim common hot-wallet descriptors
    for tail in (" Hot Wallet", " Deposit", " Cold Wallet", " Exchange"):
        if n.endswith(tail):
            n = n[: -len(tail)]
    return n.strip()


async def resolve_identities(
    session: aiohttp.ClientSession, addresses: list[str]
) -> dict[str, dict]:
    """Return {address: identity} for the given addresses via Helius.

    identity = {"name": str|None, "type": str, "category": str, "icon": str,
                "base_name": str|None}.  Fails open: unknown/errored addresses
    get {"type": "unknown", "name": None}.
    """
    key = os.getenv("HELIUS_API_KEY", "")
    uniq = [a for a in dict.fromkeys(addresses) if a]
    out: dict[str, dict] = {}
    to_fetch: list[str] = []
    for a in uniq:
        if a in _cache:
            out[a] = _cache[a]
        else:
            to_fetch.append(a)

    if to_fetch and key:
        # Helius batch endpoint — chunk to stay well within limits
        for i in range(0, len(to_fetch), 90):
            chunk = to_fetch[i : i + 90]
            try:
                async with session.post(
                    f"{_BATCH_URL}?api-key={key}",
                    json={"addresses": chunk},
                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
                ) as r:
                    rows = await r.json() if r.status == 200 else []
            except Exception:
                rows = []
            seen = set()
            for row in rows if isinstance(rows, list) else []:
                addr = row.get("address")
                if not addr:
                    continue
                name = row.get("name")
                ident = {
                    "name":      name,
                    "type":      row.get("type", "unknown"),
                    "category":  row.get("category", ""),
                    "icon":      row.get("icon", ""),
                    "base_name": _base_name(name) if name else None,
                }
                _cache[addr] = ident
                out[addr] = ident
                seen.add(addr)
            # Addresses Helius omitted (404/unknown) — cache the negative
            for a in chunk:
                if a not in seen:
                    ident = {"name": None, "type": "unknown", "category": "",
                             "icon": "", "base_name": None}
                    _cache[a] = ident
                    out[a] = ident

    # Anything still missing (no key, etc.) → unknown
    for a in uniq:
        out.setdefault(a, {"name": None, "type": "unknown", "category": "",
                           "icon": "", "base_name": None})
    return out


def is_exchange(identity: dict) -> bool:
    return (identity or {}).get("type") == "exchange" and bool((identity or {}).get("base_name"))
