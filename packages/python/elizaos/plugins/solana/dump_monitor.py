"""dump_monitor.py — live token monitoring + emergency dump webhooks.

Bots register a webhook for a mint they hold. A background loop polls the
watched tokens (batched, cheap) and the moment it sees a dump starting — a
sharp price drop, liquidity draining, or a confirmed coordinated same-block
sell — it POSTs the webhook so the bot can market-sell before the pool is
gone. This is the push model that fixes the "14-minute-old trace" problem.

v1 polls every ~15s (batched DexScreener, one call per 30 mints). On a trigger
it optionally confirms with the on-chain coordinated-exit check and fires a
structured payload. Watches persist to disk so a restart doesn't drop them.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp

_BASE = Path(__file__).parent
_WATCH_FILE = _BASE / "dump_watches.json"

# mint → {"webhooks": [url...], "last_price": float, "last_liq": float,
#         "added": ts, "last_fired": ts}
_watches: dict[str, dict] = {}

_POLL_SECS       = 15
_PRICE_DROP_PCT  = 12.0    # price down ≥ this since last poll → trigger
_LIQ_DROP_PCT    = 18.0    # liquidity down ≥ this → trigger (rug in progress)
_REFIRE_COOLDOWN = 120     # don't spam the same webhook within this window
_MAX_WATCHES     = 500


def _load() -> None:
    global _watches
    if _WATCH_FILE.exists():
        try:
            _watches = json.loads(_WATCH_FILE.read_text())
        except Exception:
            _watches = {}


def _save() -> None:
    try:
        _WATCH_FILE.write_text(json.dumps(_watches))
    except Exception:
        pass


def add_watch(mint: str, webhook: str) -> dict:
    if len(_watches) >= _MAX_WATCHES and mint not in _watches:
        return {"error": "watch capacity reached"}
    w = _watches.setdefault(mint, {"webhooks": [], "last_price": 0.0,
                                   "last_liq": 0.0, "added": time.time(),
                                   "last_fired": 0.0})
    if webhook not in w["webhooks"]:
        w["webhooks"].append(webhook)
    _save()
    return {"watching": mint, "webhooks": len(w["webhooks"]), "poll_secs": _POLL_SECS}


def remove_watch(mint: str, webhook: str | None = None) -> dict:
    w = _watches.get(mint)
    if not w:
        return {"removed": False}
    if webhook:
        w["webhooks"] = [u for u in w["webhooks"] if u != webhook]
        if not w["webhooks"]:
            _watches.pop(mint, None)
    else:
        _watches.pop(mint, None)
    _save()
    return {"removed": True}


def list_watches() -> dict:
    return {"watches": [{"mint": m, "webhooks": len(w["webhooks"]),
                         "added": w["added"]} for m, w in _watches.items()]}


async def _fire(session: aiohttp.ClientSession, mint: str, w: dict, payload: dict) -> None:
    w["last_fired"] = time.time()
    _save()
    for url in list(w["webhooks"]):
        try:
            await session.post(url, json=payload,
                               timeout=aiohttp.ClientTimeout(total=6))
            print(f"[dump-monitor] 🚨 fired {payload['event']} for {mint[:8]} → {url[:40]}")
        except Exception as e:
            print(f"[dump-monitor] webhook POST failed {url[:40]}: {e}")


async def _check_coordinated(session: aiohttp.ClientSession, mint: str) -> bool:
    """Confirm whether a coordinated same-block dump is on-chain right now."""
    try:
        from elizaos.plugins.solana.cluster_check import get_cluster_map
        import time as _t
        res = await get_cluster_map(session, mint, _t.time() - 3600)
        return bool(res.get("coordinated_exit"))
    except Exception:
        return False


async def monitor_loop(session: aiohttp.ClientSession | None = None) -> None:
    """Poll watched mints and fire webhooks on dump signals. Runs forever."""
    _load()
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    print(f"[dump-monitor] loop started — polling every {_POLL_SECS}s")
    try:
        while True:
            mints = list(_watches.keys())
            for i in range(0, len(mints), 30):
                chunk = mints[i:i + 30]
                try:
                    async with session.get(
                        f"https://api.dexscreener.com/tokens/v1/solana/{','.join(chunk)}",
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        pairs = await r.json() if r.status == 200 else []
                except Exception:
                    continue
                # best pair per mint
                best: dict[str, dict] = {}
                for p in pairs if isinstance(pairs, list) else []:
                    m = (p.get("baseToken") or {}).get("address", "")
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                    if m and liq > float((best.get(m) or {}).get("liquidity", {}).get("usd") or -1):
                        best[m] = p
                for m in chunk:
                    w = _watches.get(m)
                    p = best.get(m)
                    if not w or not p:
                        continue
                    price = float(p.get("priceUsd") or 0)
                    liq   = float((p.get("liquidity") or {}).get("usd") or 0)
                    lp, ll = w["last_price"], w["last_liq"]
                    trigger, reason = None, ""
                    if lp > 0 and price > 0 and (lp - price) / lp * 100 >= _PRICE_DROP_PCT:
                        trigger, reason = "dump_detected", f"price −{(lp-price)/lp*100:.0f}% since last check"
                    elif ll > 0 and liq > 0 and (ll - liq) / ll * 100 >= _LIQ_DROP_PCT:
                        trigger, reason = "liquidity_draining", f"liquidity −{(ll-liq)/ll*100:.0f}% since last check"
                    w["last_price"], w["last_liq"] = price, liq
                    if trigger and time.time() - w.get("last_fired", 0) > _REFIRE_COOLDOWN:
                        coordinated = await _check_coordinated(session, m)
                        await _fire(session, m, w, {
                            "event":        trigger,
                            "mint":         m,
                            "reason":       reason,
                            "coordinated":  coordinated,
                            "price_usd":    price,
                            "liquidity_usd": round(liq),
                            "action":       "consider_immediate_exit",
                            "ts":           int(time.time()),
                        })
            _save()
            await asyncio.sleep(_POLL_SECS)
    finally:
        if own:
            await session.close()
