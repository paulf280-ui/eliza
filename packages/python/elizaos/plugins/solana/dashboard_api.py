"""Dashboard API — REST endpoints and WebSocket handler for the live trading dashboard.

Registers routes on the existing aiohttp Application:
  GET  /api/status          Full snapshot (wallet + positions + risk + history + config)
  GET  /api/positions       Open positions with current prices
  GET  /api/history         Paginated trade history
  GET  /api/equity          Equity curve data
  GET  /api/launches        Recent pump.fun token launches
  GET  /api/report          Paper trading / session report (JSON)
  POST /api/control         Bot control: {"action": "pause"|"resume"|"reset_cb"}
  POST /api/positions/{mint}/close   Force-close an open position
  POST /api/chat            Jarvis Command Centre: {"message": "..."} → full AI routing
  GET  /ws                  WebSocket for real-time event streaming
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from elizaos.runtime import AgentRuntime

PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "false").lower() in ("true", "1", "yes")

# ── Module-level queue for startup/system alerts ──────────────────────────────
# Any code can call push_system_alert() and the message will appear in the
# Jarvis chat window on the next WebSocket broadcast cycle (~2s delay).
_system_alerts: list[dict] = []


def push_system_alert(message: str, level: str = "info") -> None:
    """Push a system alert to the Jarvis chat window.

    Args:
        message: The alert text to display.
        level: "info", "warning", or "error" — controls the emoji prefix.
    """
    prefix = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "ℹ️")
    _system_alerts.append({
        "type": "jarvis_chat",
        "message": "🤖 Startup Check",
        "reply": f"{prefix} {message}",
        "ts": time.time(),
    })


def push_config_update() -> None:
    """Push the full current config to all dashboard WebSocket clients.

    Call this after any Jarvis tool modifies live_config. The broadcast loop
    delivers the event within ~2s so the dashboard reflects the change
    immediately without requiring a page reload.
    """
    try:
        from elizaos.plugins.solana import live_config as _lc_push
        _system_alerts.append({
            "type": "config_update",
            "data": _lc_push.all_config(),
        })
    except Exception:
        pass


def _get_pos_mgr(runtime: AgentRuntime):
    try:
        return runtime.get_service("position_manager")
    except Exception:
        return None


def _get_wallet_svc(runtime: AgentRuntime):
    from elizaos.types import ServiceTypeRegistry
    try:
        return runtime.get_service(ServiceTypeRegistry.WALLET)
    except Exception:
        return None


def _get_pump_svc(runtime: AgentRuntime):
    from elizaos.types import ServiceTypeRegistry
    try:
        return runtime.get_service(ServiceTypeRegistry.TOKEN_DATA)
    except Exception:
        return None


def _get_token_monitor(runtime: AgentRuntime):
    try:
        return runtime.get_service("token_monitor")
    except Exception:
        return None


async def _get_wallet_data(runtime: AgentRuntime) -> dict[str, Any]:
    wallet_svc = _get_wallet_svc(runtime)
    if wallet_svc is None:
        return {"address": "", "sol_balance": 0, "token_balances": [], "read_only": True}
    try:
        sol_balance = await wallet_svc.get_sol_balance()
        token_balances = await wallet_svc.get_token_balances()
        return {
            "address": wallet_svc.get_public_key(),
            "sol_balance": round(sol_balance, 6),
            "token_balances": token_balances,
            "read_only": wallet_svc.is_read_only(),
        }
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("_get_wallet_data failed: %s", exc)
        sol_balance = 0.0
        try:
            sol_balance = await wallet_svc.get_sol_balance()
        except Exception:
            pass
        return {
            "address": getattr(wallet_svc, "get_public_key", lambda: "")(),
            "sol_balance": round(sol_balance, 6),
            "token_balances": [],
            "read_only": wallet_svc.is_read_only() if hasattr(wallet_svc, "is_read_only") else True,
        }


async def _get_current_prices(runtime: AgentRuntime, mints: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    pump_svc = _get_pump_svc(runtime)
    if pump_svc is None:
        return prices
    for mint in mints:
        try:
            bc = await pump_svc.get_bonding_curve(mint)
            if bc and "price_sol" in bc:
                prices[mint] = bc["price_sol"]
        except Exception:
            continue
    return prices


def _serialize_monster_position(mint: str, mp: dict[str, Any]) -> dict[str, Any]:
    """Serialize a strategy_e_monster position into the dashboard schema.

    The dashboard OPEN POSITIONS table consumes the same fields pos_mgr emits,
    so we project monster state onto that schema. Monster-only fields are added
    as extra keys (source, tp1_fired, locked_sol) — frontend ignores unknown keys.
    """
    entry = float(mp.get("entry_price") or 0.0)
    cur   = float(mp.get("current_price") or entry)
    sol_spent = float(mp.get("sol_spent") or 0.0)
    remaining = float(mp.get("remaining_fraction", 1.0))
    pnl_pct = ((cur / entry) - 1.0) * 100 if entry > 0 else 0.0
    age_secs = time.time() - float(mp.get("entry_ts") or time.time())
    return {
        "mint": mint,
        "dex": "pump-amm" if (mp.get("pool") or "").startswith("pump") else (mp.get("pool") or "pumpswap"),
        "entry_price_sol": entry,
        "entry_sol_spent": sol_spent,
        "token_amount": 0,
        "token_decimals": 6,
        "stop_loss_price": entry * 0.6 if entry > 0 else 0,  # -40% hard floor pre-TP1
        "tp1_price": entry * 2.0 if entry > 0 else 0,        # +100% = TP1
        "tp2_price": 0,
        "tp3_price": 0,
        "peak_price": float(mp.get("peak_price") or entry),
        "trailing_stop_price": 0,
        "tp1_hit": bool(mp.get("tp1_fired", False)),
        "tp2_hit": False,
        "tp3_hit": False,
        "entry_time": float(mp.get("entry_ts") or time.time()),
        "age_seconds": age_secs,
        "current_price_sol": cur,
        "pnl_pct": round(pnl_pct, 2),
        "unrealized_pnl_sol": round((pnl_pct / 100) * sol_spent * remaining, 6),
        "creator_wallet": (mp.get("metadata") or {}).get("creator_wallet"),
        "score": None,
        "fill_pct": 100.0,
        # Monster-specific extras
        "strategy": "monster",
        "signal_source": mp.get("signal_source"),
        "token_name": mp.get("token_name"),
        "locked_sol": float(mp.get("locked_sol") or 0.0),
        "peak_pnl_pct": float(mp.get("peak_pnl_pct") or 0.0),
        "remaining_fraction": remaining,
    }


def _generate_report(pos_mgr, wallet_data: dict, session_start: float) -> dict[str, Any]:
    """Generate a comprehensive session / paper-trading performance report."""
    if pos_mgr is None:
        return {"error": "Position manager not available"}

    trades = list(reversed(pos_mgr._trade_history))  # chronological order
    buys  = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell" and t.get("pnl_sol") is not None]

    total_invested = sum(t.get("sol_amount", 0) for t in buys)
    realized_pnl   = sum(t.get("pnl_sol", 0) or 0 for t in sells)

    winners = [t for t in sells if (t.get("pnl_sol") or 0) > 0]
    losers  = [t for t in sells if (t.get("pnl_sol") or 0) < 0]

    win_rate = len(winners) / len(sells) * 100 if sells else 0
    avg_win  = sum(t.get("pnl_pct", 0) or 0 for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t.get("pnl_pct", 0) or 0 for t in losers)  / len(losers)  if losers  else 0
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss)) if sells else 0

    best  = max(sells, key=lambda t: t.get("pnl_pct", 0) or 0, default=None)
    worst = min(sells, key=lambda t: t.get("pnl_pct", 0) or 0, default=None)

    # Max drawdown from equity snapshots
    snapshots = pos_mgr.get_equity_snapshots()
    max_drawdown_pct = 0.0
    if snapshots:
        peak = snapshots[0]["equity_sol"]
        for s in snapshots:
            if s["equity_sol"] > peak:
                peak = s["equity_sol"]
            elif peak > 0:
                dd = (peak - s["equity_sol"]) / peak * 100
                if dd > max_drawdown_pct:
                    max_drawdown_pct = dd

    session_hours = (time.time() - session_start) / 3600

    return {
        "generated_at": time.time(),
        "paper_trading": PAPER_TRADING,
        "session": {
            "start": session_start,
            "duration_hours": round(session_hours, 2),
        },
        "summary": {
            "total_trades": len(trades),
            "total_buys": len(buys),
            "total_sells": len(sells),
            "open_positions": pos_mgr.open_position_count if hasattr(pos_mgr, 'open_position_count') else len(pos_mgr.positions),
            "total_invested_sol": round(total_invested, 4),
            "realized_pnl_sol": round(realized_pnl, 4),
            "wallet_sol": wallet_data.get("sol_balance", 0),
        },
        "performance": {
            "win_rate_pct": round(win_rate, 1),
            "winners": len(winners),
            "losers": len(losers),
            "avg_win_pct": round(avg_win, 1),
            "avg_loss_pct": round(avg_loss, 1),
            "expectancy_pct": round(expectancy, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
        },
        "risk": pos_mgr.get_risk_summary(),
        "best_trade": best,
        "worst_trade": worst,
        "trade_history": trades,
        "equity_snapshots": snapshots,
    }


def register_dashboard_routes(app: web.Application, runtime: AgentRuntime) -> None:
    """Register all dashboard API routes on the aiohttp Application."""
    from elizaos.plugins.solana import live_config as _lc_global

    _session_start = time.time()

    # ── CORS middleware ──────────────────────────────────────────────────────

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    app.middlewares.append(cors_middleware)

    # ── GET /api/status ──────────────────────────────────────────────────────

    async def handle_status(request: web.Request) -> web.Response:
        pos_mgr = _get_pos_mgr(runtime)
        wallet_data = await _get_wallet_data(runtime)

        position_mints = list(pos_mgr.positions.keys()) if pos_mgr else []
        prices = await _get_current_prices(runtime, position_mints) if position_mints else {}

        monitor = _get_token_monitor(runtime)
        launches = []
        if monitor and hasattr(monitor, "get_recent_launches"):
            try:
                launches = monitor.get_recent_launches(20)
            except Exception:
                pass

        positions = pos_mgr.serialize_positions(prices) if pos_mgr else {}
        # Merge monster-strategy positions so the dashboard's initial snapshot
        # (and the 10s status poll) surfaces them — /api/positions already does
        # this, but the frontend reads /api/status for first paint.
        try:
            from elizaos.plugins.solana import strategy_e_monster as _mon
            for mint, mp in _mon.open_positions().items():
                positions[mint] = _serialize_monster_position(mint, mp)
        except Exception:
            pass

        copy_trade_enabled = os.getenv("COPY_TRADE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        data: dict[str, Any] = {
            "timestamp": time.time(),
            "paper_trading": PAPER_TRADING,
            "copy_trade_enabled": copy_trade_enabled,
            "wallet": wallet_data,
            "positions": positions,
            "risk": pos_mgr.get_risk_summary() if pos_mgr else {},
            "trade_history": pos_mgr.get_trade_history(50) if pos_mgr else [],
            "equity_history": pos_mgr.get_equity_snapshots() if pos_mgr else [],
            "activity_log": pos_mgr.get_activity_log(50) if pos_mgr else [],
            "recent_launches": launches,
            "config": {
                **(pos_mgr.get_config() if pos_mgr else {}),
                **_lc_global.all_config(),
            },
        }
        return web.json_response(data)

    # ── GET /api/positions ───────────────────────────────────────────────────

    async def handle_positions(request: web.Request) -> web.Response:
        pos_mgr = _get_pos_mgr(runtime)
        if not pos_mgr:
            positions = {}
            risk: dict[str, Any] = {}
        else:
            mints = list(pos_mgr.positions.keys())
            prices = await _get_current_prices(runtime, mints) if mints else {}
            positions = pos_mgr.serialize_positions(prices)
            risk = pos_mgr.get_risk_summary()

        # Merge monster-strategy positions so the dashboard OPEN POSITIONS
        # panel surfaces them alongside the legacy pos_mgr positions.
        try:
            from elizaos.plugins.solana import strategy_e_monster as _mon
            for mint, mp in _mon.open_positions().items():
                positions[mint] = _serialize_monster_position(mint, mp)
        except Exception:
            pass

        return web.json_response({"positions": positions, "risk": risk})

    # ── GET /api/history ─────────────────────────────────────────────────────

    async def handle_history(request: web.Request) -> web.Response:
        pos_mgr = _get_pos_mgr(runtime)
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        trades = pos_mgr.get_trade_history(limit, offset) if pos_mgr else []
        total = len(pos_mgr._trade_history) if pos_mgr else 0
        return web.json_response({"trades": trades, "total": total})

    # ── GET /api/equity ──────────────────────────────────────────────────────

    async def handle_equity(request: web.Request) -> web.Response:
        pos_mgr = _get_pos_mgr(runtime)
        since = float(request.query.get("since", "0"))
        points = pos_mgr.get_equity_snapshots(since) if pos_mgr else []
        wallet_data = await _get_wallet_data(runtime)
        return web.json_response({
            "points": points,
            "current_equity_sol": wallet_data["sol_balance"],
        })

    # ── GET /api/launches ────────────────────────────────────────────────────

    async def handle_launches(request: web.Request) -> web.Response:
        monitor = _get_token_monitor(runtime)
        launches = []
        if monitor and hasattr(monitor, "get_recent_launches"):
            try:
                limit = int(request.query.get("limit", "20"))
                launches = monitor.get_recent_launches(limit)
            except Exception:
                pass
        return web.json_response({"launches": launches})

    # ── GET /api/report ──────────────────────────────────────────────────────

    async def handle_report(request: web.Request) -> web.Response:
        pos_mgr = _get_pos_mgr(runtime)
        wallet_data = await _get_wallet_data(runtime)
        report = _generate_report(pos_mgr, wallet_data, _session_start)
        return web.json_response(report)

    # ── POST /api/control ────────────────────────────────────────────────────

    async def handle_control(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        action = body.get("action", "")
        pos_mgr = _get_pos_mgr(runtime)

        if not pos_mgr:
            return web.json_response({"error": "position manager not available"}, status=503)

        if action == "pause":
            pos_mgr.circuit_broken = True
            pos_mgr._log_activity("warning", "Trading PAUSED by dashboard user")
            pos_mgr._emit_event("risk_update", pos_mgr.get_risk_summary())
            return web.json_response({"ok": True, "circuit_broken": True})

        elif action == "resume":
            pos_mgr.circuit_broken = False
            pos_mgr.consecutive_losses = 0
            pos_mgr._log_activity("info", "Trading RESUMED by dashboard user")
            pos_mgr._emit_event("risk_update", pos_mgr.get_risk_summary())
            return web.json_response({"ok": True, "circuit_broken": False})

        elif action == "reset_cb":
            pos_mgr.circuit_broken = False
            pos_mgr.consecutive_losses = 0
            pos_mgr.daily_pnl_sol = 0.0
            pos_mgr.day_start = time.time()
            pos_mgr._log_activity("info", "Circuit breaker RESET by dashboard user")
            pos_mgr._emit_event("risk_update", pos_mgr.get_risk_summary())
            return web.json_response({"ok": True})

        return web.json_response({"error": f"unknown action: {action}"}, status=400)

    # ── POST /api/positions/{mint}/close ────────────────────────────────────

    async def handle_close_position(request: web.Request) -> web.Response:
        mint = request.match_info.get("mint", "")
        pos_mgr = _get_pos_mgr(runtime)

        # Copy-trade / strategy positions live in pos_mgr.positions
        if pos_mgr and mint in pos_mgr.positions:
            try:
                await pos_mgr._execute_auto_close(mint, "manual_close")
                return web.json_response({"ok": True, "mint": mint, "pool": "strategy"})
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=500)

        # Monster positions live in their own pool (strategy_e_monster)
        try:
            from elizaos.plugins.solana import strategy_e_monster as _mon
            mon_positions = _mon.open_positions()
            if mint in mon_positions:
                mp = mon_positions[mint]
                cur = float(mp.get("current_price") or mp.get("entry_price") or 0.0)
                await _mon._apply_exit(mint, "manual_close", 1.0, runtime, cur)
                return web.json_response({"ok": True, "mint": mint, "pool": "monster"})
        except Exception as exc:
            return web.json_response({"error": f"monster close failed: {exc}"}, status=500)

        return web.json_response({"error": f"no open position for {mint}"}, status=404)

    # ── Jarvis helper: analyze trade history ─────────────────────────────────

    def _analyze_trades(limit: int = 200) -> str:
        """Read trade_history.json and return a comprehensive performance breakdown."""
        import collections
        _th_path = os.path.join(os.path.dirname(__file__), "trade_history.json")
        try:
            with open(_th_path) as _f:
                all_trades = json.load(_f)
        except Exception as exc:
            return f"Could not read trade_history.json: {exc}"

        sells = [t for t in all_trades[-limit:] if t.get("side") == "sell" and t.get("pnl_pct") is not None]
        if not sells:
            return "No closed trades found in history."

        by_dex: dict = collections.defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "pct_sum": 0.0})
        by_hour: dict = collections.defaultdict(lambda: {"wins": 0, "losses": 0})
        best = max(sells, key=lambda t: t.get("pnl_pct", 0) or 0)
        worst = min(sells, key=lambda t: t.get("pnl_pct", 0) or 0)

        for t in sells:
            dex   = t.get("dex", "unknown")
            pnl   = t.get("pnl_pct", 0) or 0
            psol  = t.get("pnl_sol", 0) or 0
            hr    = int((t.get("timestamp", 0) % 86400) // 3600)
            by_dex[dex]["pnl"] += psol
            by_dex[dex]["pct_sum"] += pnl
            by_hour[hr]["wins" if pnl > 0 else "losses"] += 1
            if pnl > 0:
                by_dex[dex]["wins"] += 1
            else:
                by_dex[dex]["losses"] += 1

        winners = [t for t in sells if (t.get("pnl_pct") or 0) > 0]
        losers  = [t for t in sells if (t.get("pnl_pct") or 0) <= 0]
        wr = len(winners) / len(sells) * 100
        avg_w = sum(t.get("pnl_pct", 0) or 0 for t in winners) / len(winners) if winners else 0
        avg_l = sum(t.get("pnl_pct", 0) or 0 for t in losers)  / len(losers)  if losers  else 0
        net   = sum(t.get("pnl_sol", 0) or 0 for t in sells)

        lines = [
            f"=== TRADE ANALYSIS (last {len(sells)} closed trades) ===",
            f"Win rate: {wr:.1f}%  ({len(winners)}W / {len(losers)}L)",
            f"Avg win: {avg_w:+.1f}%  Avg loss: {avg_l:+.1f}%",
            f"Net P&L: {net:+.4f} SOL",
            f"Best trade: {best.get('mint','')[:8]}… {best.get('pnl_pct', 0):+.1f}% ({best.get('dex','?')})",
            f"Worst trade: {worst.get('mint','')[:8]}… {worst.get('pnl_pct', 0):+.1f}% ({worst.get('dex','?')})",
            "",
            "=== BY DEX ===",
        ]
        for dex, stats in sorted(by_dex.items()):
            total_dex = stats["wins"] + stats["losses"]
            dex_wr = stats["wins"] / total_dex * 100 if total_dex else 0
            avg_dex = stats["pct_sum"] / total_dex if total_dex else 0
            lines.append(f"  {dex}: {dex_wr:.0f}% WR ({stats['wins']}W/{stats['losses']}L) net={stats['pnl']:+.4f} SOL avg={avg_dex:+.1f}%")

        # Best 3 hours
        best_hours = sorted(by_hour.items(), key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]), reverse=True)[:3]
        lines.append("")
        hour_strs = [f"{h:02d}:00 ({s['wins']}W/{s['losses']}L)" for h, s in best_hours]
        lines.append("Best hours UTC: " + ", ".join(hour_strs))

        return "\n".join(lines)

    # ── Jarvis helper: read bot log ────────────────────────────────────────────

    def _read_log(lines: int = 40) -> str:
        """Return the last N lines of traderbot.out."""
        log_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "dashboard", "traderbot.out")
        log_path = os.path.normpath(log_path)
        try:
            with open(log_path) as _f:
                all_lines = _f.readlines()
            return "".join(all_lines[-lines:])
        except Exception as exc:
            return f"Could not read log: {exc}"

    # ── Jarvis helper: get detailed open positions ─────────────────────────────

    def _get_positions_detail(pos_mgr) -> str:
        """Return full detail on all open positions including SL/TP prices."""
        if pos_mgr is None or not pos_mgr.positions:
            return "No open positions."
        lines = [f"=== OPEN POSITIONS ({len(pos_mgr.positions)}) ==="]
        for mint, pos in pos_mgr.positions.items():
            age_min = pos.age_seconds() / 60
            p = pos_mgr._serialize_position(pos, pos.last_known_price)
            lines.append(
                f"{mint}\n"
                f"  DEX={pos.dex}  age={age_min:.1f}m  entry={pos.entry_price_sol:.8f} SOL\n"
                f"  Current={pos.last_known_price:.8f}  P&L={p.get('pnl_pct',0):+.1f}%  {p.get('pnl_sol',0):+.4f} SOL\n"
                f"  SL={pos.stop_loss_price:.8f}  TP1={pos.tp1_price:.8f}\n"
                f"  Tokens={pos.token_amount}  Spent={pos.entry_sol_spent:.4f} SOL"
            )
        return "\n".join(lines)

    # ── Jarvis helper: fetch token info from DexScreener ─────────────────────

    async def _fetch_token_info(mint: str) -> str:
        """Fetch price/liq/volume for a token mint via DexScreener."""
        import aiohttp as _aiohttp
        try:
            url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return f"DexScreener returned {resp.status}"
                    data = await resp.json()
            pairs = data if isinstance(data, list) else data.get("pairs", [])
            if not pairs:
                return f"No pairs found for {mint[:8]}…"
            # Pick highest liquidity pair
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            price  = best.get("priceNative", "?")
            liq    = (best.get("liquidity") or {}).get("usd", 0)
            vol24  = (best.get("volume") or {}).get("h24", 0)
            chg5m  = (best.get("priceChange") or {}).get("m5", 0)
            chg1h  = (best.get("priceChange") or {}).get("h1", 0)
            dex_id = best.get("dexId", "?")
            return (
                f"{mint[:8]}… on {dex_id}: "
                f"price={price} SOL, liq=${liq:,.0f}, vol24h=${vol24:,.0f}, "
                f"m5={chg5m:+.1f}%, h1={chg1h:+.1f}%"
            )
        except Exception as exc:
            return f"Token info fetch failed: {exc}"

    # ── Jarvis helper: execute a manual buy ───────────────────────────────────

    async def _eliza_execute_buy(rt: Any, mint: str, sol_amount: float, reason: str, pos_mgr: Any) -> str:
        """Execute a manual buy on behalf of Jarvis via PumpPortal/Raydium."""
        import aiohttp as _aiohttp
        import uuid
        from elizaos.types import ServiceTypeRegistry
        from elizaos.plugins.solana.constants import WSOL_MINT

        if pos_mgr and mint in pos_mgr.positions:
            return f"Already have open position for {mint[:8]}…"
        if pos_mgr and mint in pos_mgr._session_traded_mints:
            return f"{mint[:8]}… is blacklisted — aborting buy"
        if sol_amount < 0.001 or sol_amount > 1.0:
            return "sol_amount must be 0.001–1.0"

        paper = _lc_global.get("paper_trading", False)

        # Get current price from DexScreener
        try:
            _info = await _fetch_token_info(mint)
        except Exception:
            _info = ""
        # Parse price from info string (rough)
        price_sol = 0.0
        try:
            import re as _re
            _m = _re.search(r"price=([\d.e+-]+)", _info)
            if _m:
                price_sol = float(_m.group(1))
        except Exception:
            pass

        token_decimals = 6
        token_amount = int((sol_amount / price_sol) * (10 ** token_decimals)) if price_sol > 0 else 0

        if paper:
            sig = f"PAPER_{uuid.uuid4().hex[:12].upper()}"
        else:
            raydium_svc = rt.get_service(ServiceTypeRegistry.LP_POOL)
            if raydium_svc is None:
                return "RaydiumService not available"
            try:
                sig = await raydium_svc.swap(WSOL_MINT, mint, sol_amount, slippage=0.15, pool="pump-amm")
            except Exception as exc:
                try:
                    sig = await raydium_svc.swap(WSOL_MINT, mint, sol_amount, slippage=0.15, pool="raydium")
                except Exception as exc2:
                    return f"Swap failed: {exc} / {exc2}"

            # Read actual tokens received
            try:
                import asyncio as _asyncio
                wallet_svc = rt.get_service(ServiceTypeRegistry.WALLET)
                if wallet_svc:
                    await _asyncio.sleep(3)
                    bals = await wallet_svc.get_token_balances()
                    actual_raw = next((int(t["raw_amount"]) for t in bals if t["mint"] == mint), 0)
                    if actual_raw > 0:
                        actual_dec = next((t.get("decimals", token_decimals) for t in bals if t["mint"] == mint), token_decimals)
                        price_sol = sol_amount / (actual_raw / 10 ** actual_dec)
                        token_amount = actual_raw
                        token_decimals = actual_dec
            except Exception:
                pass

        if pos_mgr:
            pos_mgr.open_position(
                mint=mint,
                dex="pumpswap",
                entry_price_sol=price_sol,
                entry_sol_spent=sol_amount,
                token_amount=token_amount,
                token_decimals=token_decimals,
                signature=sig,
                score=0,
            )
            pos_mgr._log_activity("success", f"Jarvis manual buy: {mint[:8]}… {sol_amount:.4f} SOL — {reason}")

        return f"Bought {sol_amount:.4f} SOL of {mint[:8]}… sig={sig[:16]}"

    # ── Jarvis AI message handler with function calling ───────────────────────

    async def _eliza_handle_message(text: str, rt: Any) -> str:
        """
        Process a chat message through OpenAI with bot-control function calling.
        Jarvis can adjust filters, close positions, pause/resume trading, and more.
        """
        import openai as _openai
        from elizaos.plugins.solana import live_config as _lc

        pos_mgr    = _get_pos_mgr(rt)
        wallet_data = await _get_wallet_data(rt)

        # Build context for Jarvis
        positions_summary = []
        if pos_mgr:
            for mint, pos in pos_mgr.positions.items():
                positions_summary.append(
                    f"{mint[:8]}… dex={pos.dex} "
                    f"pnl={pos_mgr._serialize_position(pos).get('pnl_pct', 0):+.1f}% "
                    f"age={int(pos.age_seconds()//60)}m"
                )

        recent_trades = []
        if pos_mgr:
            for t in pos_mgr.get_trade_history(limit=8):
                recent_trades.append(
                    f"{t.get('mint','')[:8]}… {t.get('side','?')} "
                    f"pnl={t.get('pnl_pct') or 0:+.1f}% reason={t.get('reason','?')}"
                )

        risk = pos_mgr.get_risk_summary() if pos_mgr else {}
        cfg  = _lc.all_config()

        recent_cfg_changes = _lc.recent_changes(5)
        cfg_change_lines = "; ".join(
            f"{c['key']}→{c['new_value']} (by {c['changed_by']})" for c in recent_cfg_changes
        ) or "none"

        system_prompt = f"""You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — the autonomous AI brain of PF Capital TraderBot, a Solana memecoin trading system.
You speak with calm authority, precision, and the dry wit of the Iron Man Jarvis character. You address the user as "sir" occasionally.
You have FULL CONTROL over the bot. Use tools proactively — call analyze_performance before making filter decisions, read_log to diagnose problems, get_positions_detail to review open trades.

IMPORTANT PARAMETER FORMATS:
- stop_loss_pct / early_stop_loss_pct / max_daily_loss_pct: use DECIMALS (0.10 = 10%, 0.15 = 15%) — NOT percentages
- tp1_mult: multiplier above 1.0 (1.50 = +50%, 2.0 = +100%)
- strategy scores: integer 0–16
- strategy_X_buy_sol: SOL amount per trade, 0 = use global buy_sol

=== CURRENT STATE ===
Wallet: {wallet_data.get('sol_balance', 0):.4f} SOL  |  Paper mode: {cfg.get('paper_trading')}
Open positions ({len(positions_summary)}): {', '.join(positions_summary) or 'none'}
Recent trades: {'; '.join(recent_trades) or 'none'}
Circuit breaker: {'ACTIVE' if risk.get('circuit_broken') else 'OK'}
Daily P&L: {risk.get('daily_pnl_sol', 0):+.4f} SOL  |  Consecutive losses: {risk.get('consecutive_losses', 0)}

=== LIVE CONFIG ===
Paper trading: {cfg.get('paper_trading')}  |  Global position size: {cfg.get('buy_sol')} SOL
Strategy B (pumpswap grad-snipe): {'ON' if cfg.get('strategy_b_enabled') else 'OFF'}  size: {cfg.get('strategy_b_buy_sol') or cfg.get('buy_sol')} SOL  min liq: ${cfg.get('strategy_b_min_liq_usd'):,}  dip wait: {cfg.get('strategy_b_dip_wait_secs')}s
Strategy C (raydium scout):       {'ON' if cfg.get('strategy_c_enabled') else 'OFF'}  size: {cfg.get('strategy_c_buy_sol') or cfg.get('buy_sol')} SOL  min score: {cfg.get('strategy_c_min_score')}/16
Strategy D (meteora scout):       {'ON' if cfg.get('strategy_d_enabled') else 'OFF'}  size: {cfg.get('strategy_d_buy_sol') or cfg.get('buy_sol')} SOL  min score: {cfg.get('strategy_d_min_score')}/16  min liq: ${cfg.get('strategy_d_min_liq_usd'):,}
Trading window: {cfg.get('trading_window_start_utc'):02d}:00–{cfg.get('trading_window_end_utc'):02d}:00 UTC
Stop loss: {cfg.get('stop_loss_pct',0.12)*100:.0f}%  |  Early SL: {cfg.get('early_stop_loss_pct',0.07)*100:.0f}%  |  TP1 mult: {cfg.get('tp1_mult',1.5)}x (+{(cfg.get('tp1_mult',1.5)-1)*100:.0f}%)
Max concurrent positions: {cfg.get('max_concurrent_positions')}  |  Max daily loss: {cfg.get('max_daily_loss_pct',0.15)*100:.0f}%
Recent config changes: {cfg_change_lines}

=== GROK (X SOCIAL SCANNER) ===
Current focus: {(lambda _sm: _sm.get_focus() or 'default (all Solana meme tokens)')(rt.get_service('social_monitor')) if rt and rt.get_service('social_monitor') else 'unavailable'}
Use set_grok_focus to redirect Grok's X search to specific narratives, token types, or influencers.

When adjusting a filter, always explain your reasoning based on the data above."""

        # ── Function definitions ──────────────────────────────────────────────
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "adjust_filter",
                    "description": "Adjust a bot filter or risk parameter. Changes take effect on the next scan cycle.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "param": {
                                "type": "string",
                                "description": "Config key to change",
                                "enum": [
                                    "strategy_b_min_liq_usd", "strategy_b_dip_wait_secs",
                                    "strategy_c_min_score", "strategy_c_min_liq_usd",
                                    "strategy_d_min_score", "strategy_d_min_liq_usd",
                                    "stop_loss_pct", "early_stop_loss_pct",
                                    "tp1_mult", "buy_sol",
                                    "max_concurrent_positions", "max_daily_loss_pct",
                                    "trading_window_start_utc", "trading_window_end_utc",
                                ],
                            },
                            "value": {"description": "New value to set"},
                            "reason": {"type": "string", "description": "Why you're making this change"},
                        },
                        "required": ["param", "value", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "close_position",
                    "description": "Force-close an open position immediately.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mint": {"type": "string", "description": "Full mint address of position to close"},
                            "reason": {"type": "string", "description": "Reason for closing"},
                        },
                        "required": ["mint", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pause_trading",
                    "description": "Activate the circuit breaker to pause all new trades.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resume_trading",
                    "description": "Reset the circuit breaker and resume trading.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string"},
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_paper_trading",
                    "description": "Switch between paper trading (simulation) and live trading.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean", "description": "True = paper mode, False = live mode"},
                            "reason": {"type": "string"},
                        },
                        "required": ["enabled", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "enable_strategy",
                    "description": "Enable a trading strategy (B=grad-snipe, C=raydium, D=meteora).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "strategy": {"type": "string", "enum": ["B", "C", "D"], "description": "Which strategy to enable"},
                            "reason": {"type": "string"},
                        },
                        "required": ["strategy", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "disable_strategy",
                    "description": "Disable a trading strategy (B=grad-snipe, C=raydium, D=meteora).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "strategy": {"type": "string", "enum": ["B", "C", "D"], "description": "Which strategy to disable"},
                            "reason": {"type": "string"},
                        },
                        "required": ["strategy", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_trading_hours",
                    "description": "Change the active trading window (UTC hours). Strategy B only runs inside this window.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start_utc": {"type": "integer", "description": "Window open hour (0-23 UTC)"},
                            "end_utc":   {"type": "integer", "description": "Window close hour (0-23 UTC)"},
                            "reason": {"type": "string"},
                        },
                        "required": ["start_utc", "end_utc", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "buy_token",
                    "description": "Manually buy a token. Only use when the user explicitly asks to open a position.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mint": {"type": "string", "description": "Full token mint address"},
                            "sol_amount": {"type": "number", "description": "SOL to spend (0.01–1.0)"},
                            "reason": {"type": "string", "description": "Why you're buying this token"},
                        },
                        "required": ["mint", "sol_amount", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_position_exit",
                    "description": "Modify the stop-loss and/or take-profit levels for an open position.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mint":          {"type": "string", "description": "Full mint address of open position"},
                            "stop_loss_pct": {"type": "number", "description": "New stop-loss as decimal, e.g. 0.15 = 15% below entry"},
                            "tp1_pct":       {"type": "number", "description": "New TP1 target as decimal above entry, e.g. 0.50 = +50%"},
                            "reason":        {"type": "string"},
                        },
                        "required": ["mint", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "emergency_close_all",
                    "description": "Force-close ALL open positions immediately. Use only in critical situations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {"type": "string", "description": "Why emergency close is needed"},
                        },
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_token_info",
                    "description": "Look up current price, liquidity, and volume for any token mint from DexScreener.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mint": {"type": "string", "description": "Token mint address to look up"},
                        },
                        "required": ["mint"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "blacklist_token",
                    "description": "Permanently blacklist a mint address so the bot never buys it again.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mint":   {"type": "string", "description": "Token mint address to blacklist"},
                            "reason": {"type": "string"},
                        },
                        "required": ["mint", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_note",
                    "description": "Add a human-readable note to the activity log (visible on the dashboard).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "The note to log"},
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_performance",
                    "description": "Analyze the full trade history — win rates, P&L by DEX/strategy, best hours, best/worst trades. Call this whenever you need data to make decisions.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "description": "Number of most recent closed trades to analyze (default 200)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_log",
                    "description": "Read the last N lines of the bot's output log. Use this to diagnose errors, see what the bot is doing, or check for any issues.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "lines": {"type": "integer", "description": "Number of log lines to return (default 40, max 100)"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_positions_detail",
                    "description": "Get full detail on all open positions: entry price, current price, P&L, stop-loss price, TP price, age, token amount.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "reset_stats",
                    "description": "Reset risk counters. Options: 'circuit_breaker' (clear CB + consecutive losses), 'daily_pnl' (reset today's P&L counter), 'all' (both).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "what":   {"type": "string", "enum": ["circuit_breaker", "daily_pnl", "all"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["what", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_strategy_position_size",
                    "description": "Set a per-strategy position size override. Use 0 to revert to the global buy_sol setting.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "strategy":   {"type": "string", "enum": ["B", "C", "D"]},
                            "sol_amount": {"type": "number", "description": "SOL per trade for this strategy (0 = use global buy_sol)"},
                            "reason":     {"type": "string"},
                        },
                        "required": ["strategy", "sol_amount", "reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_wallet_tokens",
                    "description": "List all token balances in the wallet — useful to see what we hold and check for stuck/missed positions.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_grok_focus",
                    "description": (
                        "Redirect what Grok scans for on X. Takes effect on the next 90s scan cycle. "
                        "Use this to focus on specific narratives, token types, or influencers. "
                        "Examples: 'AI tokens', 'gaming', 'TRUMP related', 'Elon mentions', "
                        "'Meteora DLMM launches', 'tokens with 1000+ retweets'. "
                        "Pass empty string to reset to default (all Solana meme tokens)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "focus": {
                                "type": "string",
                                "description": "Focus directive for Grok X search. Empty string = default.",
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["focus", "reason"],
                    },
                },
            },
        ]

        # ── First API call ────────────────────────────────────────────────────
        client = _openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ]

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=600,
        )

        msg = response.choices[0].message
        actions_taken: list[str] = []

        # ── Execute any tool calls ────────────────────────────────────────────
        if msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]})

            for tc in msg.tool_calls:
                fn   = tc.function.name
                args = json.loads(tc.function.arguments)
                result_str = ""

                if fn == "adjust_filter":
                    _pct_keys = {"stop_loss_pct", "early_stop_loss_pct", "max_daily_loss_pct"}
                    _raw_val  = args["value"]
                    # Auto-convert: if Eliza passes 10 meaning "10%", convert to 0.10
                    if args["param"] in _pct_keys:
                        try:
                            _v = float(_raw_val)
                            if _v > 1.0:
                                _raw_val = _v / 100.0
                        except (TypeError, ValueError):
                            pass
                    ok, result_str = _lc.set_value(args["param"], _raw_val, changed_by="Jarvis", reason=args.get("reason",""))
                    if ok:
                        actions_taken.append(f"✓ {result_str}")
                        if pos_mgr:
                            pos_mgr._log_activity("info", f"Jarvis changed {args['param']} → {_raw_val} — {args.get('reason','')}")

                elif fn == "close_position":
                    mint = args["mint"]
                    if pos_mgr and mint in pos_mgr.positions:
                        try:
                            await pos_mgr._execute_auto_close(mint, "eliza_close")
                            result_str = f"Position {mint[:8]}… closed"
                            actions_taken.append(f"✓ Closed {mint[:8]}…")
                        except Exception as e:
                            result_str = f"Close failed: {e}"
                    else:
                        result_str = f"No open position for {mint[:8]}…"

                elif fn == "pause_trading":
                    if pos_mgr:
                        pos_mgr.circuit_broken   = True
                        pos_mgr.circuit_break_at = time.time()
                        result_str = "Circuit breaker activated — trading paused"
                        actions_taken.append("✓ Trading paused")

                elif fn == "resume_trading":
                    if pos_mgr:
                        pos_mgr.circuit_broken     = False
                        pos_mgr.consecutive_losses = 0
                        result_str = "Circuit breaker reset — trading resumed"
                        actions_taken.append("✓ Trading resumed")

                elif fn == "set_paper_trading":
                    enabled = args["enabled"]
                    _lc.set_value("paper_trading", enabled, changed_by="Jarvis", reason=args.get("reason",""))
                    mode = "paper" if enabled else "LIVE"
                    result_str = f"Switched to {mode} trading mode"
                    actions_taken.append(f"✓ Now in {mode} mode")

                elif fn == "enable_strategy":
                    s = args["strategy"].upper()
                    key = f"strategy_{s.lower()}_enabled"
                    _lc.set_value(key, True, changed_by="Jarvis", reason=args.get("reason",""))
                    result_str = f"Strategy {s} enabled"
                    actions_taken.append(f"✓ Strategy {s} ON")
                    if pos_mgr:
                        pos_mgr._log_activity("info", f"Jarvis: Strategy {s} enabled — {args.get('reason','')}")

                elif fn == "disable_strategy":
                    s = args["strategy"].upper()
                    key = f"strategy_{s.lower()}_enabled"
                    _lc.set_value(key, False, changed_by="Jarvis", reason=args.get("reason",""))
                    result_str = f"Strategy {s} disabled"
                    actions_taken.append(f"✓ Strategy {s} OFF")
                    if pos_mgr:
                        pos_mgr._log_activity("warning", f"Jarvis: Strategy {s} disabled — {args.get('reason','')}")

                elif fn == "set_trading_hours":
                    start = int(args["start_utc"])
                    end   = int(args["end_utc"])
                    _lc.set_value("trading_window_start_utc", start, changed_by="Jarvis", reason=args.get("reason",""))
                    _lc.set_value("trading_window_end_utc",   end,   changed_by="Jarvis", reason=args.get("reason",""))
                    result_str = f"Trading window set to {start:02d}:00–{end:02d}:00 UTC"
                    actions_taken.append(f"✓ Window: {start:02d}:00–{end:02d}:00 UTC")
                    if pos_mgr:
                        pos_mgr._log_activity("info", f"Jarvis: Trading window → {start:02d}:00–{end:02d}:00 UTC")

                elif fn == "buy_token":
                    mint_b     = args["mint"]
                    sol_amount = float(args["sol_amount"])
                    reason_b   = args.get("reason", "manual buy by Eliza")
                    result_str = await _eliza_execute_buy(rt, mint_b, sol_amount, reason_b, pos_mgr)
                    if result_str.startswith("Bought"):
                        actions_taken.append(f"✓ {result_str}")

                elif fn == "set_position_exit":
                    mint_e = args["mint"]
                    if pos_mgr and mint_e in pos_mgr.positions:
                        pos_e = pos_mgr.positions[mint_e]
                        changes = []
                        if "stop_loss_pct" in args:
                            new_sl = pos_e.entry_price_sol * (1 - float(args["stop_loss_pct"]))
                            pos_e.stop_loss_price = new_sl
                            changes.append(f"SL→{new_sl:.8f}")
                        if "tp1_pct" in args:
                            new_tp = pos_e.entry_price_sol * (1 + float(args["tp1_pct"]))
                            pos_e.tp1_price = new_tp
                            changes.append(f"TP1→{new_tp:.8f}")
                        result_str = f"{mint_e[:8]}… exits updated: {', '.join(changes)}"
                        actions_taken.append(f"✓ {result_str}")
                        if pos_mgr:
                            pos_mgr._log_activity("info", f"Jarvis adjusted exits on {mint_e[:8]}…: {', '.join(changes)}")
                    else:
                        result_str = f"No open position for {mint_e[:8]}…"

                elif fn == "emergency_close_all":
                    reason_ec = args.get("reason", "emergency close by Jarvis")
                    if pos_mgr:
                        mints_open = list(pos_mgr.positions.keys())
                        if not mints_open:
                            result_str = "No open positions to close"
                        else:
                            closed, failed = [], []
                            for m_ec in mints_open:
                                try:
                                    await pos_mgr._execute_auto_close(m_ec, "jarvis_emergency_close")
                                    closed.append(m_ec[:8])
                                except Exception as _ec_e:
                                    failed.append(f"{m_ec[:8]}({_ec_e})")
                            result_str = f"Closed {len(closed)} positions: {', '.join(closed)}"
                            if failed:
                                result_str += f". Failed: {', '.join(failed)}"
                            actions_taken.append(f"✓ Emergency closed {len(closed)} positions")
                            pos_mgr._log_activity("error", f"Jarvis EMERGENCY CLOSE ALL — {reason_ec}")
                    else:
                        result_str = "Position manager not available"

                elif fn == "get_token_info":
                    mint_gi = args["mint"]
                    result_str = await _fetch_token_info(mint_gi)

                elif fn == "blacklist_token":
                    mint_bl = args["mint"]
                    reason_bl = args.get("reason", "blacklisted by Jarvis")
                    if pos_mgr:
                        pos_mgr._session_traded_mints.add(mint_bl)
                        # Persist to traded_mints.json
                        _tm_path = os.path.join(os.path.dirname(__file__), "traded_mints.json")
                        try:
                            with open(_tm_path) as _f:
                                _tm = json.load(_f)
                        except Exception:
                            _tm = {}
                        _tm[mint_bl] = {"reason": reason_bl, "blacklisted_by": "Jarvis", "timestamp": time.time()}
                        with open(_tm_path, "w") as _f:
                            json.dump(_tm, _f, indent=2)
                        pos_mgr._log_activity("warning", f"Jarvis blacklisted {mint_bl[:8]}… — {reason_bl}")
                        result_str = f"Blacklisted {mint_bl[:8]}… permanently"
                        actions_taken.append(f"✓ Blacklisted {mint_bl[:8]}…")
                    else:
                        result_str = "Position manager not available"

                elif fn == "add_note":
                    note_text = args.get("text", "")
                    if pos_mgr and note_text:
                        pos_mgr._log_activity("info", f"📝 Jarvis: {note_text}")
                    result_str = f"Note added: {note_text}"
                    actions_taken.append("✓ Note logged")

                elif fn == "analyze_performance":
                    limit_pa = int(args.get("limit", 200))
                    result_str = _analyze_trades(limit_pa)

                elif fn == "read_log":
                    n_lines = min(int(args.get("lines", 40)), 100)
                    result_str = _read_log(n_lines)

                elif fn == "get_positions_detail":
                    result_str = _get_positions_detail(pos_mgr)

                elif fn == "reset_stats":
                    what_rs = args.get("what", "all")
                    reason_rs = args.get("reason", "reset by Jarvis")
                    if pos_mgr:
                        if what_rs in ("circuit_breaker", "all"):
                            pos_mgr.circuit_broken     = False
                            pos_mgr.consecutive_losses = 0
                        if what_rs in ("daily_pnl", "all"):
                            pos_mgr.daily_pnl_sol = 0.0
                            pos_mgr.day_start     = time.time()
                        pos_mgr._log_activity("info", f"Jarvis reset {what_rs} stats — {reason_rs}")
                        pos_mgr._emit_event("risk_update", pos_mgr.get_risk_summary())
                        result_str = f"Reset {what_rs}: CB={pos_mgr.circuit_broken} losses={pos_mgr.consecutive_losses} daily_pnl={pos_mgr.daily_pnl_sol:+.4f}"
                        actions_taken.append(f"✓ Reset {what_rs}")
                    else:
                        result_str = "Position manager not available"

                elif fn == "set_strategy_position_size":
                    s_sps = args["strategy"].upper()
                    sol_sps = float(args["sol_amount"])
                    key_sps = f"strategy_{s_sps.lower()}_buy_sol"
                    _lc.set_value(key_sps, sol_sps, changed_by="Jarvis", reason=args.get("reason",""))
                    if sol_sps == 0:
                        result_str = f"Strategy {s_sps} reverted to global position size ({_lc.get('buy_sol',0.05)} SOL)"
                    else:
                        result_str = f"Strategy {s_sps} position size set to {sol_sps} SOL per trade"
                    actions_taken.append(f"✓ {result_str}")
                    if pos_mgr:
                        pos_mgr._log_activity("info", f"Jarvis: {result_str}")

                elif fn == "get_wallet_tokens":
                    wallet_svc_t = _get_wallet_svc(rt)
                    if wallet_svc_t is None:
                        result_str = "Wallet service not available"
                    else:
                        try:
                            bals_t = await wallet_svc_t.get_token_balances()
                            sol_t  = await wallet_svc_t.get_sol_balance()
                            lines_t = [f"SOL balance: {sol_t:.6f}"]
                            for tok in bals_t:
                                lines_t.append(
                                    f"  {tok.get('mint','')[:8]}… "
                                    f"amount={tok.get('amount', tok.get('raw_amount',0))} "
                                    f"decimals={tok.get('decimals',6)}"
                                )
                            result_str = "\n".join(lines_t) if len(lines_t) > 1 else f"SOL: {sol_t:.6f} SOL, no tokens held"
                        except Exception as exc_t:
                            result_str = f"Wallet fetch failed: {exc_t}"

                elif fn == "set_grok_focus":
                    _focus_val  = args.get("focus", "")
                    _focus_why  = args.get("reason", "")
                    _sm = rt.get_service("social_monitor") if rt else None
                    if _sm is None:
                        result_str = "Social monitor service not running"
                    else:
                        _sm.set_focus(_focus_val)
                        if _focus_val:
                            result_str = (
                                f"Grok focus updated → '{_focus_val}'. "
                                f"Takes effect on the next scan cycle (~90s). Reason: {_focus_why}"
                            )
                        else:
                            result_str = "Grok focus reset to default (all Solana meme tokens)."

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

            # Second call — Jarvis summarises what she did
            followup = await client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                max_tokens=400,
            )
            reply_text = followup.choices[0].message.content or ""
        else:
            reply_text = msg.content or ""

        return reply_text

    # ── PATCH /api/config — REST endpoint for direct config updates ───────────

    async def handle_patch_config(request: web.Request) -> web.Response:
        from elizaos.plugins.solana import live_config as _lc
        try:
            body = await request.json()
            results = {}
            for key, value in body.items():
                ok, msg = _lc.set_value(key, value, changed_by="dashboard")
                results[key] = {"ok": ok, "message": msg}
            return web.json_response(results)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    # ── WebSocket /ws ────────────────────────────────────────────────────────

    _ws_clients: set[web.WebSocketResponse] = set()

    async def handle_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        _ws_clients.add(ws)

        # Send initial state snapshot on connection
        try:
            pos_mgr = _get_pos_mgr(runtime)
            wallet_data = await _get_wallet_data(runtime)
            await ws.send_json({
                "type": "init",
                "data": {
                    "paper_trading": PAPER_TRADING,
                    "wallet": wallet_data,
                    "positions": pos_mgr.serialize_positions() if pos_mgr else {},
                    "risk": pos_mgr.get_risk_summary() if pos_mgr else {},
                    "config": {
                        **(pos_mgr.get_config() if pos_mgr else {}),
                        **_lc_global.all_config(),
                    },
                },
            })
        except Exception:
            pass

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        action = data.get("action")
                        if action == "send_message":
                            text = data.get("text", "")
                            if text:
                                # Route through Jarvis Command Centre (Claude primary brain)
                                try:
                                    from elizaos.plugins.solana.jarvis_command_centre import route as _jcc_route
                                    reply = await asyncio.wait_for(
                                        _jcc_route(text, runtime),
                                        timeout=90.0,
                                    )
                                except asyncio.TimeoutError:
                                    reply = "Jarvis timed out (>90s) — likely building context or waiting on Claude. Try again."
                                except Exception as _jcc_err:
                                    reply = f"Jarvis error: {_jcc_err}"
                                # Guard: connection may have closed during the ~10s Claude API call
                                try:
                                    await ws.send_json({
                                        "type": "chat_response",
                                        "data": {"text": reply, "timestamp": time.time()},
                                    })
                                except Exception:
                                    break  # Client disconnected — exit cleanly
                        elif action == "ping":
                            try:
                                await ws.send_json({"type": "pong", "data": {"timestamp": time.time()}})
                            except Exception:
                                break
                    except Exception as _e:
                        # Sending the error response can also fail if connection is closing
                        try:
                            await ws.send_json({
                                "type": "chat_response",
                                "data": {"text": f"Error: {_e}", "timestamp": time.time()},
                            })
                        except Exception:
                            break
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            _ws_clients.discard(ws)

        return ws

    # ── Background broadcast loop ────────────────────────────────────────────

    async def _broadcast_loop() -> None:
        _hb_counter = 0
        while True:
            try:
                # Drain module-level system alerts (startup health checks etc.)
                while _system_alerts and _ws_clients:
                    alert = _system_alerts.pop(0)
                    dead: set[web.WebSocketResponse] = set()
                    for ws in list(_ws_clients):
                        try:
                            await ws.send_json(alert)
                        except Exception:
                            dead.add(ws)
                    _ws_clients -= dead

                pos_mgr = _get_pos_mgr(runtime)
                if pos_mgr and _ws_clients:
                    # Drain queued events (position_opened, position_closed, etc.)
                    while not pos_mgr._event_queue.empty():
                        try:
                            event = pos_mgr._event_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        dead: set[web.WebSocketResponse] = set()
                        for ws in list(_ws_clients):
                            try:
                                await ws.send_json(event)
                            except Exception:
                                dead.add(ws)
                        _ws_clients -= dead

                    # Push fresh position_update for every open position on every cycle
                    # Uses last_known_price so no extra RPC calls — just serialises current state
                    if pos_mgr.positions and _ws_clients:
                        for mint, pos in list(pos_mgr.positions.items()):
                            update_event = {
                                "type": "position_update",
                                "data": pos_mgr._serialize_position(pos, pos.last_known_price),
                            }
                            dead = set()
                            for ws in list(_ws_clients):
                                try:
                                    await ws.send_json(update_event)
                                except Exception:
                                    dead.add(ws)
                            _ws_clients -= dead

                # Heartbeat every 5 cycles (every ~10s)
                _hb_counter += 1
                if _hb_counter >= 5 and _ws_clients:
                    _hb_counter = 0
                    heartbeat = {"type": "heartbeat", "data": {"timestamp": time.time(), "paper_trading": PAPER_TRADING}}
                    dead = set()
                    for ws in list(_ws_clients):
                        try:
                            await ws.send_json(heartbeat)
                        except Exception:
                            dead.add(ws)
                    _ws_clients -= dead

            except Exception:
                pass

            await asyncio.sleep(2)

    async def start_broadcast(app: web.Application):
        app["_broadcast_task"] = asyncio.create_task(_broadcast_loop())

    async def stop_broadcast(app: web.Application):
        task = app.get("_broadcast_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(start_broadcast)
    app.on_cleanup.append(stop_broadcast)

    # ── Static file serving (production) ─────────────────────────────────────

    dashboard_dist = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "dashboard", "dist"
    )
    if os.path.isdir(dashboard_dist):
        async def handle_spa(request: web.Request) -> web.StreamResponse:
            path = request.match_info.get("path", "")
            file_path = os.path.join(dashboard_dist, path)
            if os.path.isfile(file_path):
                return web.FileResponse(file_path)
            return web.FileResponse(os.path.join(dashboard_dist, "index.html"))

        app.router.add_static("/assets", os.path.join(dashboard_dist, "assets"))

    # ── Register all routes ──────────────────────────────────────────────────

    # ── Creator whitelist management endpoints ───────────────────────────────

    async def handle_whitelist_get(request: web.Request) -> web.Response:
        """GET /api/whitelist — return all whitelisted and blacklisted creators."""
        from elizaos.plugins.solana.dev_reputation import get_reputation
        rep = get_reputation()
        all_records = rep.get_all_records()
        return web.json_response({
            "stats": rep.get_stats(),
            "records": all_records,
        })

    async def handle_whitelist_post(request: web.Request) -> web.Response:
        """POST /api/whitelist — add/remove/update a creator.
        Body: {"wallet": "...", "action": "whitelist"|"blacklist"|"remove", "reason": "..."}
        """
        from elizaos.plugins.solana.dev_reputation import get_reputation
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        wallet = body.get("wallet", "").strip()
        action = body.get("action", "").strip()
        reason = body.get("reason", "manual via dashboard").strip()
        if not wallet or action not in ("whitelist", "blacklist", "remove"):
            return web.json_response({"error": "wallet + action (whitelist|blacklist|remove) required"}, status=400)
        rep = get_reputation()
        if action == "whitelist":
            rep.whitelist(wallet, reason)
        elif action == "blacklist":
            rep.blacklist(wallet, reason)
        elif action == "remove":
            rep.remove(wallet)
        return web.json_response({"ok": True, "wallet": wallet, "action": action})

    # ── POST /api/speak — ElevenLabs Jarvis voice (falls back to OpenAI onyx) ──
    # ElevenLabs "George" voice: JBFqnCBsd6RMkjVDRZzb — deep British male, professional
    # Closest available voice to the Iron Man Jarvis character.
    # Sign up free at elevenlabs.io (10k chars/month), add ELEVENLABS_API_KEY to .env
    _EL_JARVIS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George — British, deep, authoritative
    _EL_API_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{_EL_JARVIS_VOICE_ID}"

    async def handle_speak(request: web.Request) -> web.Response:
        """POST /api/speak — Jarvis voice TTS.
        Prefers ElevenLabs (ELEVENLABS_API_KEY) for authentic deep British voice.
        Falls back to OpenAI onyx if ElevenLabs key not set.
        Body: {"text": "..."}
        Returns: audio/mpeg
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)

        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "").strip()

        # ── Path 1: ElevenLabs (preferred — authentic Jarvis voice) ──────────
        if elevenlabs_key:
            import aiohttp as _aiohttp_tts
            try:
                payload = {
                    "text": text[:4096],
                    "model_id": "eleven_turbo_v2_5",  # fast, low-latency
                    "voice_settings": {
                        "stability": 0.45,          # slight variation = natural speech
                        "similarity_boost": 0.85,   # stay close to original voice
                        "style": 0.20,              # subtle expressiveness
                        "use_speaker_boost": True,
                    },
                }
                headers = {
                    "xi-api-key": elevenlabs_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                }
                async with _aiohttp_tts.ClientSession() as _sess:
                    async with _sess.post(
                        _EL_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=_aiohttp_tts.ClientTimeout(total=15.0),
                    ) as _resp:
                        if _resp.status == 200:
                            audio_bytes = await _resp.read()
                            return web.Response(
                                body=audio_bytes,
                                content_type="audio/mpeg",
                                headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
                            )
                        # Non-200 from ElevenLabs → fall through to OpenAI
            except Exception:
                pass  # Fall through to OpenAI backup

        # ── Path 2: OpenAI onyx (fallback) ────────────────────────────────────
        import openai as _openai_tts
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            return web.json_response({"error": "No TTS provider configured (set ELEVENLABS_API_KEY or OPENAI_API_KEY)"}, status=503)
        try:
            client_tts = _openai_tts.AsyncOpenAI(api_key=api_key)
            response_tts = await asyncio.wait_for(
                client_tts.audio.speech.create(
                    model="tts-1",
                    voice="onyx",
                    input=text[:4096],
                    response_format="mp3",
                ),
                timeout=15.0,
            )
            audio_bytes = response_tts.content
            return web.Response(
                body=audio_bytes,
                content_type="audio/mpeg",
                headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
            )
        except asyncio.TimeoutError:
            return web.json_response({"error": "TTS timeout"}, status=504)
        except Exception as exc_tts:
            return web.json_response({"error": str(exc_tts)}, status=500)

    app.router.add_post("/api/speak", handle_speak)

    # ── Jarvis Command Centre chat endpoint ────────────────────────────────────
    async def handle_chat(request: web.Request) -> web.Response:
        """POST /api/chat  {"message": "..."} → {"reply": "...", "command_type": "..."}

        Routes through the full Jarvis Command Centre:
          - Direct commands (enable/disable/set) execute immediately
          - AI dispatch (ask grok, check token, run analysis) call the relevant AI
          - Free-form chat goes to Claude Sonnet with full trading context
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        message = (body.get("message") or body.get("text") or "").strip()
        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        try:
            from elizaos.plugins.solana.jarvis_command_centre import route as jarvis_route
            reply = await asyncio.wait_for(jarvis_route(message, runtime), timeout=60.0)

            # Detect command type for UI display
            lower = message.lower().strip()
            if any(lower.startswith(p) for p in ("enable", "disable", "pause", "resume", "start", "stop", "turn on", "turn off")):
                cmd_type = "control"
            elif lower.startswith("set ") or lower.startswith("adjust:"):
                cmd_type = "config"
            elif any(lower.startswith(p) for p in ("ask grok", "grok:", "check ", "research ")):
                cmd_type = "ai_dispatch"
            elif lower in ("status", "positions", "history", "config", "lessons", "help", "?"):
                cmd_type = "query"
            elif lower in ("run analysis", "analyse", "analyze", "deep dive"):
                cmd_type = "analysis"
            else:
                cmd_type = "chat"

            # Broadcast to WebSocket clients so the chat appears in the live feed
            _broadcast = getattr(request.app, "_jarvis_broadcast", None)
            if _broadcast:
                await _broadcast({
                    "type":    "jarvis_chat",
                    "message": message,
                    "reply":   reply,
                    "ts":      time.time(),
                })

            return web.json_response({
                "reply":        reply,
                "command_type": cmd_type,
                "ts":           time.time(),
            })

        except asyncio.TimeoutError:
            return web.json_response({"reply": "Request timed out — try again.", "command_type": "error"})
        except Exception as exc:
            return web.json_response({"reply": f"Error: {exc}", "command_type": "error"}, status=500)

    app.router.add_post("/api/chat", handle_chat)

    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/positions", handle_positions)
    app.router.add_get("/api/history", handle_history)
    app.router.add_get("/api/equity", handle_equity)
    app.router.add_get("/api/launches", handle_launches)
    app.router.add_get("/api/report", handle_report)
    app.router.add_post("/api/control", handle_control)
    app.router.add_patch("/api/config", handle_patch_config)
    app.router.add_post("/api/positions/{mint}/close", handle_close_position)
    app.router.add_get("/api/whitelist", handle_whitelist_get)
    app.router.add_post("/api/whitelist", handle_whitelist_post)

    async def handle_copy_trade_stats(_: web.Request) -> web.Response:
        try:
            from elizaos.plugins.solana.axiom_copy_trader import (
                get_paper_stats, WATCHED_WALLETS, _paper_trades, _signal_log
            )
            copy_trade_enabled = os.getenv("COPY_TRADE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
            stats = get_paper_stats()
            stats["copy_trade_enabled"] = copy_trade_enabled
            stats["watched_wallets"] = list(WATCHED_WALLETS.keys()) if copy_trade_enabled else []

            # Merge monster open positions so LIVE CHARTS + open-positions panel
            # surface them in the monster-only era (copy-trade off).
            try:
                from elizaos.plugins.solana import strategy_e_monster as _mon
                import time as _t
                for mint, mp in _mon.open_positions().items():
                    entry_p = float(mp.get("entry_price") or 0)
                    cur_p = float(mp.get("current_price") or 0) or entry_p
                    pnl_pct = ((cur_p / entry_p) - 1.0) * 100 if entry_p > 0 and cur_p > 0 else 0.0
                    stats.setdefault("open_positions", []).append({
                        "mint":               mint,
                        "token_name":         mp.get("token_name", mint[:8]),
                        "wallet":             f"monster/{mp.get('signal_source','?')}",
                        "entry_ts":           mp.get("entry_ts"),
                        "sol_spent":          mp.get("sol_spent"),
                        "entry_price":        entry_p,
                        "current_price":      cur_p,
                        "pnl_pct":            round(pnl_pct, 2),
                        "mc_usd":             (mp.get("metadata") or {}).get("mcap_usd")
                                              or (mp.get("metadata") or {}).get("mc_usd"),
                        "tp1_hit":            bool(mp.get("tp1_fired", False)),
                        "tp2_hit":            False,
                        "locked_sol":         round(mp.get("locked_sol") or 0.0, 4),
                        "remaining_fraction": round(mp.get("remaining_fraction") or 1.0, 3),
                        "narrative":          mp.get("signal_source", "monster"),
                        "peak_pnl_pct":       round(mp.get("peak_pnl_pct") or 0.0, 1),
                        "partial_exits":      (mp.get("partial_exits") or [])[-5:],
                        "strategy":           "monster",
                    })
                # Track slot count for the summary bar
                stats["open_count"] = int(stats.get("open_count") or 0) + len(_mon.open_positions())
            except Exception:
                pass
            # Build the recent trades feed: merge copy-trade paper trades
            # and monster-strategy closed trades into a single chronological
            # list so the dashboard trade cards surface BOTH strategies.
            merged: list[dict] = list(_paper_trades[-50:]) if copy_trade_enabled else []
            try:
                from elizaos.plugins.solana import strategy_e_monster as _mon
                for mc in _mon._monster_closed[-50:]:
                    entry_p = float(mc.get("entry_price") or 0)
                    close_p = float(mc.get("close_price") or entry_p)
                    entry_ts = float(mc.get("entry_ts") or 0)
                    close_ts = float(mc.get("close_ts") or 0)
                    hold_mins = ((close_ts - entry_ts) / 60.0) if (close_ts and entry_ts) else 0.0
                    merged.append({
                        "ts":            close_ts or mc.get("ts") or 0,
                        "dt":            None,
                        "mint":          mc.get("mint", ""),
                        "token_name":    mc.get("token_name", ""),
                        "wallet":        f"monster/{mc.get('signal_source','?')}",
                        "reason":        mc.get("close_reason", "monster_close"),
                        "sol_spent":     float(mc.get("sol_spent") or 0),
                        "entry_price":   entry_p,
                        "exit_price":    close_p,
                        "pnl_sol":       round(float(mc.get("final_pnl_sol") or 0), 4),
                        "pnl_pct":       round(float(mc.get("final_pnl_pct") or 0), 2),
                        "hold_mins":     round(hold_mins, 1),
                        "peak_pnl_pct":  round(float(mc.get("peak_pnl_pct") or 0), 2),
                        "peak_price":    mc.get("peak_price"),
                        "locked_sol":    round(float(mc.get("locked_sol") or 0), 4),
                        "partial_exits": mc.get("partial_exits") or [],
                        "narrative":     mc.get("signal_source", "monster"),
                        "tp1_hit":       bool(mc.get("tp1_fired", False)),
                        "tp2_hit":       False,
                        "strategy":      "monster",
                        "buy_sig":       mc.get("buy_sig"),
                    })
            except Exception:
                pass
            merged.sort(key=lambda r: float(r.get("ts") or 0))
            stats["recent_trades"] = list(reversed(merged[-50:]))
            # Signals fired today
            import time as _t
            day_ago = _t.time() - 86400
            stats["signals_today"] = sum(1 for s in _signal_log if s.get("ts", 0) > day_ago and s.get("action") != "skipped")
            # Last 50 signals for the live feed (most recent first)
            stats["recent_signals"] = list(reversed(_signal_log[-50:]))

            # In live mode: replace paper balance with real wallet SOL balance
            if stats.get("live_mode"):
                try:
                    wallet_svc = runtime.get_service("wallet") if runtime else None
                    if wallet_svc:
                        real_bal = await wallet_svc.get_sol_balance()
                        if real_bal is not None:
                            stats["balance"] = round(real_bal, 4)
                except Exception:
                    pass  # fall back to paper balance if RPC fails

            return web.json_response(stats)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/copy-trade/stats", handle_copy_trade_stats)

    async def handle_frost_mirror_stats(_: web.Request) -> web.Response:
        try:
            from elizaos.plugins.solana.axiom_copy_trader import get_frost_mirror_stats
            return web.json_response(get_frost_mirror_stats())
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/frost-mirror/stats", handle_frost_mirror_stats)

    async def handle_lifecycle_rejects(request: web.Request) -> web.Response:
        """24h aggregate of monster-lifecycle scout rejection reasons.

        Returns:
          window_hours: 24
          cycles: total cycles in window
          totals: {age: N, liq: N, ...}  — sum across the window
          per_cycle_avg: {age: float, liq: float, ...}
          recent: last 60 snapshots for time-series rendering
        """
        try:
            import json as _json, os as _os, time as _time
            from collections import defaultdict as _dd
            path = _os.path.join(_os.path.dirname(__file__), "lifecycle_rejects_24h.json")
            if not _os.path.exists(path):
                return web.json_response({"window_hours": 24, "cycles": 0,
                                          "totals": {}, "per_cycle_avg": {}, "recent": []})
            with open(path) as f:
                snapshots = _json.load(f) or []
            cutoff = _time.time() - 24 * 3600
            recent = [s for s in snapshots if (s.get("ts") or 0) >= cutoff]
            totals: dict = _dd(int)
            cands_total = 0
            entered_total = 0
            viral_fires_total = 0
            for s in recent:
                cands_total += int(s.get("candidates") or 0)
                entered_total += int(s.get("entered") or 0)
                viral_fires_total += int(s.get("viral_fires") or 0)
                for k, v in (s.get("rejects") or {}).items():
                    totals[k] += int(v)
            n = max(len(recent), 1)
            per_avg = {k: round(v / n, 2) for k, v in totals.items()}
            return web.json_response({
                "window_hours": 24,
                "cycles": len(recent),
                "candidates_total": cands_total,
                "entered_total": entered_total,
                "viral_fires_total": viral_fires_total,
                "totals": dict(totals),
                "per_cycle_avg": per_avg,
                "recent": recent[-60:],
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/lifecycle/rejects", handle_lifecycle_rejects)

    async def handle_creator_alpha(request: web.Request) -> web.Response:
        """Recent creator-alpha scout activity — direct creates, operator
        fundings, watched-child creates, and graduations."""
        try:
            from elizaos.plugins.solana import monster_signals as _ms
            recent = _ms.creator_alpha_recent_signals()
            return web.json_response({
                "recent": recent,
                "count": len(recent),
                "tracked_direct": len(_ms._creator_alpha_tracked_wallets()[0]),
                "tracked_operators": len(_ms._creator_alpha_tracked_wallets()[1]),
                "watched_children": len(_ms._creator_alpha_watched_children),
                "pending_mints": len(_ms._creator_alpha_pending_mints),
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/creator-alpha", handle_creator_alpha)

    async def handle_holder_guard_report(request: web.Request) -> web.Response:
        """Read-only diagnostic for holder_guard log-only performance.

        ?hours=48 (default) — window for both guard rejections and monster
        trades. Returns: window, summary (counts + false-positive rate of
        log-only blocks), per-rejection records, and monster trades closed
        in the window. Designed for the scheduled 48h analyzer agent which
        cannot SSH to this box.
        """
        try:
            import json as _json, os as _os, time as _time
            try:
                hours = float(request.query.get("hours") or 48)
            except Exception:
                hours = 48.0
            cutoff = _time.time() - hours * 3600

            from elizaos.plugins.solana import rejection_tracker as _rt
            store = _rt._load() if hasattr(_rt, "_load") else []
            recent = [r for r in store if r.get("ts", 0) >= cutoff]
            guard_recs = [
                r for r in recent
                if (r.get("filter_name") == "holder_guard")
                or str(r.get("reason", "")).startswith("holder_guard")
            ]

            checked = [r for r in guard_recs if r.get("outcome_checked")]
            pumped = sum(1 for r in checked if r.get("outcome_verdict") == "pumped")
            rugged = sum(1 for r in checked if r.get("outcome_verdict") == "rugged")
            flat   = sum(1 for r in checked if r.get("outcome_verdict") == "flat")
            unknown = sum(1 for r in checked if r.get("outcome_verdict") == "delisted_or_unknown")
            fp_rate = (pumped / len(checked)) if checked else None

            # Monster trades in the window
            mc_path = _os.path.join(
                _os.path.dirname(__file__), "monster_closed_trades.json"
            )
            monster_recent: list[dict] = []
            try:
                with open(mc_path) as f:
                    all_closed = _json.load(f) or []
                for t in all_closed:
                    if float(t.get("close_ts", 0)) >= cutoff:
                        monster_recent.append({
                            "token_name":      t.get("token_name"),
                            "mint":            t.get("mint"),
                            "signal_source":   t.get("signal_source"),
                            "close_reason":    t.get("close_reason"),
                            "final_pnl_pct":   t.get("final_pnl_pct"),
                            "final_pnl_sol":   t.get("final_pnl_sol"),
                            "peak_pnl_pct":    t.get("peak_pnl_pct"),
                            "metadata":        t.get("metadata"),
                            "close_ts":        t.get("close_ts"),
                        })
            except Exception:
                pass

            mwins = sum(1 for t in monster_recent if (t.get("final_pnl_sol") or 0) > 0)
            mtotal = len(monster_recent)
            mnet = round(sum(float(t.get("final_pnl_sol") or 0) for t in monster_recent), 4)

            # Plain-language verdict for the analyzer agent.
            if checked == []:
                verdict = (
                    "Insufficient data: no holder_guard rejections have outcome data yet. "
                    "Either no qualifying signals fired, or the 2h outcome-check window has "
                    "not elapsed for any rejection. Re-run the analyzer in 24h."
                )
            elif fp_rate is not None and fp_rate < 0.15:
                verdict = (
                    f"FLIP recommended: false-positive rate {fp_rate:.1%} "
                    f"({pumped}/{len(checked)} blocked tokens pumped ≥+50%). "
                    "Set HOLDER_GUARD_ENFORCE=true."
                )
            elif fp_rate is not None and fp_rate > 0.30:
                verdict = (
                    f"DO NOT FLIP: false-positive rate {fp_rate:.1%} "
                    f"({pumped}/{len(checked)} blocked tokens pumped). "
                    "Loosen thresholds (raise top_10_max_pct above 35%) before enforcing."
                )
            else:
                verdict = (
                    f"INCONCLUSIVE: false-positive rate {fp_rate:.1%} ({pumped}/{len(checked)}). "
                    "Collect another 24-48h before deciding."
                )

            from elizaos.plugins.solana.holder_guard import config as _hg_cfg
            return web.json_response({
                "window_hours": hours,
                "guard_enforce": _hg_cfg.HOLDER_GUARD_ENFORCE,
                "summary": {
                    "guard_rejections_total":   len(guard_recs),
                    "rejections_with_outcomes": len(checked),
                    "would_have_blocked_pumped": pumped,
                    "would_have_blocked_rugged": rugged,
                    "would_have_blocked_flat":   flat,
                    "would_have_blocked_unknown": unknown,
                    "false_positive_rate":      fp_rate,
                    "monster_trades_closed":    mtotal,
                    "monster_wins":             mwins,
                    "monster_net_sol":          mnet,
                },
                "verdict": verdict,
                "guard_records": guard_recs[-30:],  # last 30 for inspection
                "monster_trades": monster_recent,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/holder-guard/report", handle_holder_guard_report)

    async def handle_near_miss_report(request: web.Request) -> web.Response:
        """Daily near-miss analyzer — calibration check on filter thresholds.

        For each rejection in the window, compute margin = |value-threshold| / threshold.
        Bucket by margin × outcome to surface (a) close-call pumps that suggest
        filters are too tight, and (b) far-margin pumps that suggest a missing
        lifecycle lane (e.g. post-consolidation replay).

        ?hours=24 (default).
        """
        try:
            import json as _json, os as _os, time as _time
            from collections import defaultdict
            try:
                hours = float(request.query.get("hours") or 24)
            except Exception:
                hours = 24.0
            cutoff = _time.time() - hours * 3600
            rej_path = _os.path.join(_os.path.dirname(__file__), "rejected_tokens.json")
            with open(rej_path) as f:
                rejected = _json.load(f) or []
            last = [r for r in rejected if isinstance(r, dict) and r.get("ts", 0) >= cutoff]

            def _margin(r):
                fv, th = r.get("filter_value"), r.get("threshold")
                if not isinstance(fv, (int, float)) or not isinstance(th, (int, float)) or th == 0:
                    return None
                return abs(fv - th) / abs(th) * 100.0

            enriched = []
            for r in last:
                m = _margin(r)
                if m is None:
                    continue
                enriched.append({
                    "mint":         r.get("mint", ""),
                    "filter":       r.get("filter_name"),
                    "reason":       r.get("reason"),
                    "value":        r.get("filter_value"),
                    "threshold":    r.get("threshold"),
                    "margin_pct":   round(m, 2),
                    "verdict":      r.get("outcome_verdict"),
                    "outcome_pct":  r.get("outcome_price_change_pct"),
                    "strategy":     r.get("strategy"),
                    "ts":           r.get("ts"),
                })

            # Bucket by margin
            buckets_def = [
                ("0-10%",   0,    10),
                ("10-25%",  10,   25),
                ("25-50%",  25,   50),
                ("50-100%", 50,  100),
                (">100%",   100, 1e9),
            ]
            buckets = {}
            for label, lo, hi in buckets_def:
                items = [e for e in enriched if lo <= e["margin_pct"] < hi and e["verdict"]]
                buckets[label] = {
                    "checked": len(items),
                    "pumped":  sum(1 for x in items if x["verdict"] == "pumped"),
                    "rugged":  sum(1 for x in items if x["verdict"] == "rugged"),
                    "flat":    sum(1 for x in items if x["verdict"] == "flat"),
                }
                if buckets[label]["checked"]:
                    buckets[label]["fp_rate"] = round(
                        buckets[label]["pumped"] / buckets[label]["checked"] * 100, 1
                    )

            # Per-filter close-call stats (margin <= 25%)
            close = [e for e in enriched if e["margin_pct"] <= 25]
            by_filter = defaultdict(list)
            for e in close:
                by_filter[e["filter"]].append(e)
            per_filter = []
            for f, items in sorted(by_filter.items(), key=lambda kv: -len(kv[1])):
                per_filter.append({
                    "filter": f,
                    "n":      len(items),
                    "pumped": sum(1 for x in items if x["verdict"] == "pumped"),
                    "rugged": sum(1 for x in items if x["verdict"] == "rugged"),
                    "avg_margin_pct": round(sum(x["margin_pct"] for x in items) / len(items), 1) if items else 0,
                })

            # Closest 30 calls
            near_misses = sorted(close, key=lambda x: x["margin_pct"])[:30]

            # Far-margin pumps (the actual missed alpha)
            far_pumps = [
                e for e in enriched
                if e["margin_pct"] > 100 and e["verdict"] == "pumped"
            ]
            far_pumps.sort(key=lambda x: -(x["outcome_pct"] or 0))

            # Plain-language verdict
            close_pumped = sum(1 for e in close if e["verdict"] == "pumped")
            close_checked = sum(1 for e in close if e["verdict"])
            far_pump_count = len(far_pumps)
            if close_checked == 0 and far_pump_count == 0:
                verdict = "Insufficient data — no rejections have outcome verdicts yet for this window."
            elif close_pumped == 0 and far_pump_count > 0:
                verdict = (
                    f"Filters calibrated correctly at boundaries (0/{close_checked} close-calls pumped). "
                    f"{far_pump_count} far-margin rejections pumped — these are tokens entirely outside "
                    "scout windows, likely post-consolidation runners. Consider a new scout lane."
                )
            elif close_pumped > 0:
                verdict = (
                    f"Close calls leaking pumps: {close_pumped}/{close_checked} pumped. "
                    "Investigate loosening threshold(s) on the affected filter(s)."
                )
            else:
                verdict = f"Stable — {close_checked} close calls evaluated, none pumped, no far-margin pumps."

            return web.json_response({
                "window_hours":         hours,
                "total_rejections":     len(last),
                "rejections_with_margins": len(enriched),
                "buckets_by_margin":    buckets,
                "per_filter_close_calls": per_filter,
                "near_misses":          near_misses,
                "far_margin_pumps":     far_pumps[:30],
                "verdict":              verdict,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/near-miss-report", handle_near_miss_report)

    async def handle_copy_trade_close(request: web.Request) -> web.Response:
        try:
            import aiohttp as _aiohttp
            mint = request.match_info["mint"]
            from elizaos.plugins.solana.axiom_copy_trader import (
                _close_paper_position, _paper_positions
            )
            if mint not in _paper_positions:
                return web.json_response({"error": "Position not found"}, status=404)
            async with _aiohttp.ClientSession() as _sess:
                await _close_paper_position(mint, "manual_close", _sess, runtime)
            return web.json_response({"ok": True, "mint": mint})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_post("/api/copy-trade/close/{mint}", handle_copy_trade_close)

    async def handle_copy_trade_pause(request: web.Request) -> web.Response:
        try:
            from elizaos.plugins.solana import live_config as _lc_pause
            body = await request.json() if request.content_length else {}
            paused = bool(body.get("paused", True))
            # Set BOTH flags so one button pauses every strategy, not just copy-trade.
            # Monster/etc. entry paths read trading_paused; axiom_copy_trader still
            # reads copy_trade_paused for backwards-compat.
            _lc_pause.set_value("copy_trade_paused", paused, changed_by="dashboard", reason="manual pause button")
            _lc_pause.set_value("trading_paused",    paused, changed_by="dashboard", reason="manual pause button")
            state = "PAUSED" if paused else "RESUMED"
            print(f"[pause] ⏸️  All trading {state} via dashboard button (copy_trade_paused + trading_paused)")
            return web.json_response({"ok": True, "paused": paused})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_post("/api/copy-trade/pause", handle_copy_trade_pause)

    async def handle_wallet_promotion(_: web.Request) -> web.Response:
        """Surface the wallet_promotion report so the dashboard can show
        promote-ready candidates for the 4th WATCHED_WALLETS slot."""
        try:
            from elizaos.plugins.solana.wallet_promotion import write_report
            from elizaos.plugins.solana.axiom_copy_trader import WATCHED_WALLETS
            payload = write_report()
            payload["watched"] = list(WATCHED_WALLETS.keys())
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/wallet-promotion", handle_wallet_promotion)

    async def handle_brain_memory(request: web.Request) -> web.Response:
        """Return compact summaries for all three brains so the dashboard can
        show side-by-side live decisions, learned patterns, and session stats.
        Optional query param: ?mint=<mint> filters recent_decisions to that
        position only (for the per-trade 'brains waking up' view)."""
        try:
            from elizaos.plugins.solana.brain_memory import load_brain_memory, BRAIN_FILES
            mint_filter = request.query.get("mint", "").strip()
            mint_prefix = mint_filter[:12] if mint_filter else None

            # Per-tier health (down/idle/ok) so the panel can show degraded
            # status instead of an empty list when an API is rate-limited or
            # over its spending cap. Tracked in trade_monitor._tier_health.
            tier_health = {}
            try:
                from elizaos.plugins.solana.trade_monitor import get_tier_health
                tier_health = get_tier_health()
            except Exception:
                pass

            brains = {}
            for brain in BRAIN_FILES.keys():
                mem = load_brain_memory(brain)
                decisions = mem.get("recent_decisions", [])
                if mint_prefix:
                    decisions = [d for d in decisions if d.get("mint") == mint_prefix]
                brains[brain] = {
                    "last_updated":     mem.get("last_updated"),
                    "session_stats":    mem.get("session_stats", {}),
                    "learned_patterns": (mem.get("learned_patterns") or [])[:10],
                    "historical_summary": mem.get("historical_summary") or {},
                    "recent_decisions": decisions[-20:][::-1],  # newest first
                    "decision_count":   len(mem.get("recent_decisions", [])),
                    "tier_health":      tier_health.get(brain) or {},
                }
            return web.json_response({
                "brains": brains,
                "mint_filter": mint_filter or None,
            })
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    app.router.add_get("/api/brain-memory", handle_brain_memory)

    # ── Helius webhook receiver: fresh PumpSwap pool creations ────────────
    # When a token graduates from the pump.fun bonding curve, a CREATE_POOL
    # event fires on the PumpSwap program. Helius pushes the event to this
    # endpoint within seconds; we extract the mint and queue it for the
    # lifecycle scout to evaluate at next cycle.
    #
    # Auth: the X-Helius-Auth header must match HELIUS_WEBHOOK_AUTH env var.
    # If env unset, the endpoint accepts unauthenticated POSTs but logs a
    # warning — set the env in production.
    PUMP_AMM_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

    async def handle_helius_pumpswap_grad(request: web.Request) -> web.Response:
        try:
            expected_auth = os.getenv("HELIUS_WEBHOOK_AUTH", "").strip()
            if expected_auth:
                provided = request.headers.get("Authorization", "").strip()
                if provided != expected_auth:
                    print(f"[helius-webhook] ❌ auth mismatch — provided header rejected")
                    return web.json_response({"error": "unauthorized"}, status=401)
            payload = await request.json()
            # Helius enhanced webhooks send a list of events. Each event has
            # `description`, `type`, `accountData`, `tokenTransfers`, etc.
            events = payload if isinstance(payload, list) else [payload]
            from elizaos.plugins.solana.monster_signals import push_fresh_grad

            queued = 0
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                # Only act on pool-creation events touching the PumpSwap program.
                # Defensive: accept either explicit type=CREATE_POOL or any
                # event involving the PumpSwap program with token transfers.
                ev_type = (ev.get("type") or "").upper()
                instructions = ev.get("instructions") or []
                touches_pump_amm = any(
                    (i.get("programId") == PUMP_AMM_PROGRAM_ID)
                    for i in instructions
                ) or any(
                    (acc.get("account") == PUMP_AMM_PROGRAM_ID)
                    for acc in (ev.get("accountData") or [])
                )
                if ev_type not in ("CREATE_POOL", "ANY", "UNKNOWN") and not touches_pump_amm:
                    continue
                # Extract the mint — it's the non-WSOL side of tokenTransfers,
                # or the first non-system account in accountData.
                mint = None
                for tt in (ev.get("tokenTransfers") or []):
                    candidate = tt.get("mint")
                    if candidate and candidate not in (
                        "So11111111111111111111111111111111111111112",  # WSOL
                    ):
                        mint = candidate
                        break
                if not mint:
                    continue
                pool = ""
                # Best-effort pool address extraction from accountData; the
                # exact field name depends on Helius enhanced parser version.
                for acc in (ev.get("accountData") or []):
                    addr = acc.get("account") or ""
                    if addr and addr != PUMP_AMM_PROGRAM_ID and len(addr) == 44:
                        pool = addr
                        break
                on_chain_ts = ev.get("timestamp") or ev.get("blockTime")
                push_fresh_grad(mint, pool, float(on_chain_ts) if on_chain_ts else None)
                queued += 1
                print(f"[helius-webhook] 📬 fresh grad queued: mint={mint[:8]}... "
                      f"pool={pool[:8] if pool else '?'} ts={on_chain_ts}")
            return web.json_response({"queued": queued})
        except Exception as exc:
            print(f"[helius-webhook] error: {exc}")
            # Return 200 so Helius doesn't retry-storm on a parse bug — we'd
            # rather drop one event than have N replays compound the issue.
            return web.json_response({"error": str(exc), "queued": 0})

    app.router.add_post("/api/webhooks/helius/pumpswap-grad", handle_helius_pumpswap_grad)

    app.router.add_get("/ws", handle_ws)

    if os.path.isdir(dashboard_dist):
        app.router.add_get("/{path:.*}", handle_spa)
