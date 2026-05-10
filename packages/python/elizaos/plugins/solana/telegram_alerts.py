"""
telegram_alerts.py — Telegram push notifications + command handler for TraderBot.

Sends alerts for:  position opened/closed, circuit breaker triggered/reset, bot startup
Commands:          /status  /positions  /pause  /resume  /config  /help
"""
import asyncio
import os
import time
from typing import Any

import aiohttp

# ── Credentials (hardcoded defaults, override via env vars) ─────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8319659332:AAE55XkKV7ICddUZtsyVHAla54UkP5cTOhI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7794593557")

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Global runtime reference (set by start_telegram_bot)
_runtime_ref: Any = None

# Polling offset
_poll_offset: int = 0


# ── Core send function ───────────────────────────────────────────────────────

async def send_alert(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to the hardcoded chat. Fire-and-forget — never raises."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                f"{_BASE_URL}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        print(f"[telegram] send failed: {exc}")


# ── Alert formatters ─────────────────────────────────────────────────────────

def _sol(v: float) -> str:
    return f"{v:.4f} SOL"


def fmt_position_opened(trade: dict) -> str:
    mint   = trade.get("mint", "?")
    name   = trade.get("meta", {}).get("name") or trade.get("name", mint[:8])
    symbol = trade.get("meta", {}).get("symbol") or trade.get("symbol", "?")
    dex    = (trade.get("dex") or "?").upper()
    entry  = trade.get("entry_price_sol", 0.0)
    spent  = trade.get("entry_sol_spent", 0.0)
    score  = trade.get("score", 0)
    grok   = trade.get("grok_confirmed", False)
    tp_mul = trade.get("tp1_mult", 1.4)
    sl_pct = trade.get("stop_loss_pct", 0.09)
    label  = trade.get("label", "")
    grok_tag  = " ⚡ GROK CONFIRMED" if grok else ""
    label_tag = f" [{'HARVESTER' if label == 'A' else 'MONSTER HUNTER'}]" if label else ""
    return (
        f"🟢 <b>POSITION OPENED{grok_tag}{label_tag}</b>\n"
        f"<b>{name}</b> ({symbol}) on {dex}\n"
        f"<code>{mint[:20]}...</code>\n\n"
        f"Entry: <code>{entry:.2e} SOL</code>  |  Spent: <b>{_sol(spent)}</b>\n"
        f"Score: {score}  |  TP: +{(tp_mul-1)*100:.0f}%  |  SL: -{sl_pct*100:.0f}%"
    )


def fmt_position_closed(trade: dict) -> str:
    mint     = trade.get("mint", "?")
    name     = trade.get("meta", {}).get("name") or trade.get("name", mint[:8])
    symbol   = trade.get("meta", {}).get("symbol") or trade.get("symbol", "?")
    dex      = (trade.get("dex") or "?").upper()
    pnl_pct  = float(trade.get("pnl_pct") or 0)
    pnl_sol  = float(trade.get("pnl_sol") or 0)
    reason   = trade.get("reason", "?")
    hold_s   = float(trade.get("hold_secs") or 0)
    hold_str = f"{int(hold_s//60)}m{int(hold_s%60):02d}s"
    label    = trade.get("label", "")

    if pnl_pct > 0:
        icon = "💰"
        sign = "+"
    elif reason in ("stop_loss", "early_stop_loss", "rug_stop_loss"):
        icon = "🔴"
        sign = ""
    else:
        icon = "⚪"
        sign = ""

    label_tag    = f" [{'HARVESTER' if label == 'A' else 'MONSTER HUNTER'}]" if label else ""
    reason_clean = reason.replace("_", " ").title()
    return (
        f"{icon} <b>POSITION CLOSED{label_tag}</b>\n"
        f"<b>{name}</b> ({symbol}) on {dex}\n"
        f"<code>{mint[:20]}...</code>\n\n"
        f"P&L: <b>{sign}{pnl_pct:.1f}%</b>  (<code>{sign}{_sol(pnl_sol)}</code>)\n"
        f"Hold: {hold_str}  |  Reason: {reason_clean}"
    )


def fmt_partial_sell(trade: dict) -> str:
    mint    = trade.get("mint", "?")
    name    = trade.get("meta", {}).get("name") or trade.get("name", mint[:8])
    symbol  = trade.get("meta", {}).get("symbol") or trade.get("symbol", "?")
    pnl_pct = float(trade.get("pnl_pct") or 0)
    pct_sold = float(trade.get("pct_sold") or 50)
    return (
        f"🎯 <b>TP1 HIT — Partial Sell</b>\n"
        f"<b>{name}</b> ({symbol})\n"
        f"Sold {pct_sold:.0f}% at <b>+{pnl_pct:.1f}%</b> — moonbag running"
    )


def fmt_circuit_breaker_on(reason: str = "") -> str:
    return (
        f"🚨 <b>CIRCUIT BREAKER TRIGGERED</b>\n"
        f"Trading halted.\n"
        f"Reason: {reason or 'consecutive losses / daily loss limit'}\n\n"
        f"Use /resume to manually reset, or Jarvis will auto-assess in ~5 min."
    )


def fmt_circuit_breaker_off(reason: str = "") -> str:
    return (
        f"✅ <b>CIRCUIT BREAKER RESET</b>\n"
        f"Trading resumed.\n"
        f"Reason: {reason or 'auto-reset'}"
    )


def fmt_startup(wallet_sol: float = 0.0) -> str:
    import os as _os_tg
    from datetime import datetime, timezone
    utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
    try:
        from elizaos.plugins.solana import live_config as _lc_tg
        _strats = []
        # Monster lifecycle strategy (primary)
        _monster_on = _os_tg.getenv("MONSTER_STRATEGY_ENABLED", "false").lower() in ("true", "1", "yes")
        if _monster_on:
            _paper_only = _os_tg.getenv("MONSTER_PAPER_ONLY", "false").lower() in ("true", "1", "yes")
            _strats.append(f"Monster{'(paper)' if _paper_only else ''}")
        # Creator-alpha strategy
        _ca_on = _os_tg.getenv("CREATOR_ALPHA_ENABLED", "false").lower() in ("true", "1", "yes")
        if _ca_on:
            _strats.append("Creator-Alpha")
        # Legacy strategies
        if _lc_tg.get("strategy_b_enabled", False): _strats.append("B")
        if _lc_tg.get("strategy_c_enabled", False): _strats.append("C")
        if _lc_tg.get("strategy_d_enabled", False): _strats.append("D")

        strat_str = " + ".join(_strats) if _strats else "none"

        # Mode: LIVE unless explicitly in paper mode
        _paper = _os_tg.getenv("MONSTER_PAPER_ONLY", "false").lower() in ("true", "1", "yes")
        mode_str = "PAPER" if _paper else "LIVE"
    except Exception:
        strat_str = "unknown"
        mode_str  = "unknown"
    return (
        f"🤖 <b>TraderBot started</b> — {utc}\n"
        f"Real wallet: <b>{_sol(wallet_sol)}</b>\n"
        f"Mode: <b>{mode_str}</b>\n"
        f"Active strategies: {strat_str}"
    )


# ── Command handlers ─────────────────────────────────────────────────────────

async def _cmd_status() -> str:
    if _runtime_ref is None:
        return "Bot not initialised."
    try:
        from elizaos.plugins.solana import live_config as lc
        pos_mgr = _runtime_ref.get_service("position_manager")
        wallet_svc = _runtime_ref.get_service("wallet")

        balance = 0.0
        if wallet_svc:
            try:
                balance = await asyncio.wait_for(wallet_svc.get_sol_balance(), timeout=5)
            except Exception:
                pass

        risk = pos_mgr.get_risk_summary() if pos_mgr else {}
        open_count = risk.get("open_position_count", 0)
        daily_pnl  = risk.get("daily_pnl_sol", 0.0)
        consec     = risk.get("consecutive_losses", 0)
        broken     = risk.get("circuit_broken", False)
        status_str = "🚨 PAUSED" if broken else "✅ TRADING"

        return (
            f"📊 <b>TraderBot Status</b>\n\n"
            f"Status: {status_str}\n"
            f"Wallet: <b>{_sol(balance)}</b>\n"
            f"Open positions: {open_count}\n"
            f"Daily P&L: <b>{daily_pnl:+.4f} SOL</b>\n"
            f"Consecutive losses: {consec}\n"
            f"Buy size: A2={_sol(lc.get('a2_buy_sol',0.24))}  B={_sol(lc.get('strategy_b_buy_sol',0.24))}"
        )
    except Exception as exc:
        return f"Status error: {exc}"


async def _cmd_positions() -> str:
    if _runtime_ref is None:
        return "Bot not initialised."
    try:
        pos_mgr = _runtime_ref.get_service("position_manager")
        if pos_mgr is None:
            return "Position manager not available."
        positions = pos_mgr.serialize_positions()
        if not positions:
            return "No open positions."

        lines = ["📋 <b>Open Positions</b>\n"]
        for mint, pos in positions.items():
            name   = pos.get("name", mint[:8])
            dex    = (pos.get("dex") or "?").upper()
            pnl    = float(pos.get("pnl_pct") or 0)
            pnl_sol = float(pos.get("pnl_sol") or 0)
            icon   = "📈" if pnl >= 0 else "📉"
            lines.append(
                f"{icon} <b>{name}</b> ({dex})\n"
                f"   P&L: <b>{pnl:+.1f}%</b> ({pnl_sol:+.4f} SOL)\n"
                f"   <code>{mint[:20]}...</code>"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Positions error: {exc}"


async def _cmd_pause() -> str:
    if _runtime_ref is None:
        return "Bot not initialised."
    try:
        pos_mgr = _runtime_ref.get_service("position_manager")
        if pos_mgr is None:
            return "Position manager not available."
        if pos_mgr.circuit_broken:
            return "Already paused."
        pos_mgr.trigger_circuit_breaker("Manual pause via Telegram /pause")
        return "🚨 Trading paused. Use /resume to restart."
    except Exception as exc:
        return f"Pause error: {exc}"


async def _cmd_resume() -> str:
    if _runtime_ref is None:
        return "Bot not initialised."
    try:
        pos_mgr = _runtime_ref.get_service("position_manager")
        if pos_mgr is None:
            return "Position manager not available."
        if not pos_mgr.circuit_broken:
            return "Already trading (not paused)."
        pos_mgr.force_reset_circuit_breaker("Manual resume via Telegram /resume")
        return "✅ Trading resumed."
    except Exception as exc:
        return f"Resume error: {exc}"


async def _cmd_config() -> str:
    try:
        from elizaos.plugins.solana import live_config as lc
        cfg = lc.all_config()
        sl_b   = cfg.get("pumpswap_stop_loss_pct", 0.07)
        sl_g   = cfg.get("stop_loss_pct", 0.09)
        tp_b   = cfg.get("tp1_mult", 1.4)
        tp_a2  = cfg.get("pf_tp1_mult", 1.6)
        tp_grok= cfg.get("grok_tp_mult", 1.8)
        paper  = cfg.get("paper_trading", False)
        max_pos= cfg.get("max_concurrent_positions", 1)
        a2_en  = cfg.get("strategy_a2_enabled", True)
        b_en   = cfg.get("strategy_b_enabled", True)
        a2_sol = cfg.get("a2_buy_sol", 0.24)
        b_sol  = cfg.get("strategy_b_buy_sol", 0.24)
        floor  = cfg.get("a2_min_real_sol", 1.6)
        return (
            f"⚙️ <b>Live Config</b>\n\n"
            f"Paper trading: {'YES ⚠️' if paper else 'NO (live)'}\n"
            f"Max positions: {max_pos}\n\n"
            f"<b>Strategy A2</b> (Ghost Rider): {'ON' if a2_en else 'OFF'}\n"
            f"  Size: {_sol(a2_sol)}  |  Floor: {floor:.1f} SOL\n"
            f"  TP: +{(tp_a2-1)*100:.0f}%  |  SL: -{sl_g*100:.0f}%\n\n"
            f"<b>Strategy B</b> (Grad snipe): {'ON' if b_en else 'OFF'}\n"
            f"  Size: {_sol(b_sol)}\n"
            f"  TP: +{(tp_b-1)*100:.0f}%  |  SL: -{sl_b*100:.0f}%\n\n"
            f"Grok TP mult: +{(tp_grok-1)*100:.0f}%"
        )
    except Exception as exc:
        return f"Config error: {exc}"


def _cmd_help() -> str:
    return (
        "🤖 <b>TraderBot Commands</b>\n\n"
        "/status    — wallet + P&L + circuit breaker state\n"
        "/positions — open positions with current P&L\n"
        "/config    — live strategy config\n"
        "/pause     — halt all trading immediately\n"
        "/resume    — resume trading after pause\n"
        "/help      — this message"
    )


# ── Polling loop ─────────────────────────────────────────────────────────────

async def _send_reply(chat_id: int | str, text: str) -> None:
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                f"{_BASE_URL}/sendMessage",
                json={
                    "chat_id": str(chat_id),
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        print(f"[telegram] reply failed: {exc}")


async def _handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip().lower()

    # Only respond to the authorised chat
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    if text in ("/status", "status"):
        reply = await _cmd_status()
    elif text in ("/positions", "positions", "/pos"):
        reply = await _cmd_positions()
    elif text in ("/pause", "pause"):
        reply = await _cmd_pause()
    elif text in ("/resume", "resume"):
        reply = await _cmd_resume()
    elif text in ("/config", "config"):
        reply = await _cmd_config()
    elif text in ("/help", "help", "/start"):
        reply = _cmd_help()
    else:
        return  # Ignore unknown messages silently

    await _send_reply(chat_id, reply)


async def _polling_loop() -> None:
    global _poll_offset
    print("[telegram] Command polling started")
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=35)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{_BASE_URL}/getUpdates",
                    params={"offset": _poll_offset, "timeout": 30, "limit": 10},
                ) as resp:
                    data = await resp.json()

            updates = data.get("result", [])
            for update in updates:
                uid = update.get("update_id", 0)
                if uid >= _poll_offset:
                    _poll_offset = uid + 1
                asyncio.create_task(_handle_update(update))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[telegram] polling error: {exc}")
            await asyncio.sleep(5)


# ── Public startup ────────────────────────────────────────────────────────────

async def start_telegram_bot(runtime: Any, wallet_sol: float = 0.0) -> asyncio.Task:
    """
    Call once from main() after the bot is fully initialised.
    Returns the polling task (store it to prevent GC).
    """
    global _runtime_ref
    _runtime_ref = runtime

    # Send startup notification
    await send_alert(fmt_startup(wallet_sol))

    # Start command polling in background
    task = asyncio.create_task(_polling_loop())
    return task
