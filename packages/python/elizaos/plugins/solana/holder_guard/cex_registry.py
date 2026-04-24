"""holder_guard.cex_registry — known Solana CEX deposit/hot addresses.

Used to detect dev wallet / top-holder movements to exchanges — a strong
leading indicator of a dump. Public info from Solscan / Arkham labels.

Curated manually; refresh the JSON file weekly.
"""
from __future__ import annotations

import json
import os

_REGISTRY_FILE = os.path.join(os.path.dirname(__file__), "cex_addresses.json")

# Seed set — well-known hot / deposit wallets for major CEXes on Solana.
# Sources: Solscan labels, Arkham public annotations (as of 2026-04).
_SEED_REGISTRY: dict[str, str] = {
    # Binance
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "binance_hot_1",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "binance_hot_2",
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "binance_hot_3",
    # Coinbase
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase_hot_1",
    "GgYBNE2rovgiYtQA17bXbbpG2MZTVa9aYajk6TAx3Dmq": "coinbase_hot_2",
    # Kraken
    "FaM6cKC7VxVKwADiHvzWBXLzfTgvgFg4cg6Kch9xXJYf": "kraken_hot_1",
    # OKX
    "B8vV6AN8bknMZHHg9rWxmSP5V5QnV5JT6jK4SWmtoGxY": "okx_hot_1",
    # Bybit
    "BS3UHH8n4H2Kkb4GXfH8dSZ8Qp8m4WBzMRYc5dDRxhzR": "bybit_hot_1",
    # Gate.io
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w":   "gate_hot_1",
    # KuCoin
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "kucoin_hot_1",
}


def _load() -> dict[str, str]:
    if not os.path.exists(_REGISTRY_FILE):
        try:
            with open(_REGISTRY_FILE, "w") as f:
                json.dump(_SEED_REGISTRY, f, indent=2)
        except Exception:
            pass
        return dict(_SEED_REGISTRY)
    try:
        with open(_REGISTRY_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(_SEED_REGISTRY)


_cached: dict[str, str] | None = None


def is_cex_address(address: str) -> bool:
    global _cached
    if _cached is None:
        _cached = _load()
    return address in _cached


def label(address: str) -> str | None:
    global _cached
    if _cached is None:
        _cached = _load()
    return _cached.get(address)


def all_addresses() -> set[str]:
    global _cached
    if _cached is None:
        _cached = _load()
    return set(_cached.keys())


def refresh() -> None:
    """Force reload from disk — call after manual edits to cex_addresses.json."""
    global _cached
    _cached = _load()
