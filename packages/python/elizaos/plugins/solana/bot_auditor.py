"""bot_auditor.py — Hourly internal audit system.

Runs every 60 minutes and verifies:
  - All config values match expected strategy settings
  - API endpoints (Groq, Helius, DexScreener) are reachable
  - Creator-alpha playbook has correct operator/creator counts
  - Brain flags are correct (Groq enabled, Gemini/Claude disabled)
  - Strategy flags (live mode, monster enabled, not paused)
  - Wallet balance sufficient for active strategy
  - Gate code integrity (direction/traction/MC/dead-token/age)
  - Recent trade performance summary

Results are printed to logs every hour with PASS/FAIL for each check.
Any failures trigger a dashboard alert. Accessible via GET /api/audit.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

_DIR = Path(__file__).parent
_last_audit_result: dict = {}
_last_audit_ts: float = 0.0

# ── Expected values for the current strategy (hard-TP era, 2026-05-04+) ──
_EXPECTED = {
    "monster_default_size_sol":      (0.2,    "0.2 SOL per trade"),
    "creator_alpha_size_sol":        (0.2,    "0.2 SOL per trade (creator-alpha)"),
    "monster_max_concurrent":        (2,      "2 concurrent positions"),
    "creator_alpha_max_concurrent":  (2,      "2 concurrent positions (creator-alpha)"),
    "creator_alpha_floor_pct":       (-25.0,  "-25% catastrophic floor"),
    "creator_alpha_tp1_mult":        (2.0,    "+100% TP target"),
    "creator_alpha_tp1_sell_frac":   (1.0,    "100% full exit at TP"),
    "creator_alpha_max_entry_mc_usd": (20_000, "$20K MC gate"),
    "trading_paused":                (False,  "trading must NOT be paused"),
}

_EXPECTED_PLAYBOOK = {
    "operators":        46,
    "direct_creators":  25,
    "playbook_version": 2,
}

_MIN_WALLET_SOL = 0.42  # 2 trades × 0.2 SOL × 1.05 buffer


async def run_audit(session: aiohttp.ClientSession | None = None) -> dict:
    """Run the full audit. Returns a structured result dict."""
    global _last_audit_result, _last_audit_ts

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    passed: list[str] = []
    failed: list[str] = []
    warnings: list[str] = []
    ts = datetime.now(tz=timezone.utc)

    # ── 1. CONFIG AUDIT ────────────────────────────────────────────────────────
    try:
        from elizaos.plugins.solana import live_config as lc
        for key, (expected, label) in _EXPECTED.items():
            actual = lc.get(key)
            if actual is None:
                warnings.append(f"Config {key} not set — using default (expected {expected})")
            elif actual != expected:
                failed.append(f"Config DRIFT: {key}={actual} (expected {expected} — {label})")
            else:
                passed.append(f"{key}={actual} ✓")
    except Exception as e:
        failed.append(f"Config audit error: {e}")

    # ── 2. ENVIRONMENT FLAGS ───────────────────────────────────────────────────
    monster_enabled = os.getenv("MONSTER_STRATEGY_ENABLED", "false").lower() in ("true","1","yes")
    paper_only      = os.getenv("MONSTER_PAPER_ONLY", "true").lower() in ("true","1","yes")
    groq_key        = os.getenv("GROQ_API_KEY", "")
    helius_key      = os.getenv("HELIUS_API_KEY", "")

    if not monster_enabled:
        failed.append("ENV: MONSTER_STRATEGY_ENABLED is not true — bot will not trade!")
    else:
        passed.append("MONSTER_STRATEGY_ENABLED=true ✓")

    if paper_only:
        failed.append("ENV: MONSTER_PAPER_ONLY=true — bot is in paper mode, no real trades!")
    else:
        passed.append("MONSTER_PAPER_ONLY=false (live mode) ✓")

    if not groq_key:
        failed.append("ENV: GROQ_API_KEY missing — brain is blind!")
    else:
        passed.append("GROQ_API_KEY set ✓")

    if not helius_key:
        failed.append("ENV: HELIUS_API_KEY missing — operator monitoring dead!")
    else:
        passed.append("HELIUS_API_KEY set ✓")

    # ── 3. BRAIN FLAGS (Groq only, Gemini/Claude disabled) ────────────────────
    try:
        trade_monitor_path = _DIR / "trade_monitor.py"
        monitor_code = trade_monitor_path.read_text()
        if "GEMINI_ENABLED = False" in monitor_code:
            passed.append("Gemini brain DISABLED ✓")
        else:
            failed.append("BRAIN: Gemini may be enabled — expected GEMINI_ENABLED = False")
        if "CLAUDE_ENABLED = False" in monitor_code:
            passed.append("Claude brain DISABLED ✓")
        else:
            failed.append("BRAIN: Claude may be enabled — expected CLAUDE_ENABLED = False")
        if "15.0 <= pnl_pct <= 80.0" in monitor_code or "15.0 <= pnl_pct" in monitor_code:
            passed.append("Groq evaluation window 15-80% ✓")
        else:
            warnings.append("BRAIN: Groq evaluation window may have changed — check trade_monitor.py")
    except Exception as e:
        warnings.append(f"Brain flag check error: {e}")

    # ── 4. GATE CODE INTEGRITY ─────────────────────────────────────────────────
    try:
        signals_code = (_DIR / "monster_signals.py").read_text()
        gates = {
            "DEAD-TOKEN-GATE":  "DEAD-TOKEN-GATE" in signals_code,
            "AGE-GATE":         "AGE-GATE" in signals_code,
            "MC-GATE":          "MC-GATE" in signals_code,
            "DIRECTION-GATE":   "DIRECTION-GATE" in signals_code,
            "TRACTION-GATE":    "TRACTION-GATE" in signals_code,
        }
        for gate, present in gates.items():
            if present:
                passed.append(f"Gate {gate} present ✓")
            else:
                failed.append(f"Gate {gate} MISSING — code may have regressed!")

        # Check stagnation exit in strategy_e_monster.py
        monster_code = (_DIR / "strategy_e_monster.py").read_text()
        if "stagnant_no_momentum" in monster_code:
            passed.append("Stagnation exit (10min/peak<15%) active ✓")
        else:
            failed.append("Stagnation exit MISSING from strategy_e_monster.py!")
    except Exception as e:
        warnings.append(f"Gate integrity check error: {e}")

    # ── 5. PLAYBOOK INTEGRITY ──────────────────────────────────────────────────
    try:
        playbook = json.loads((_DIR / "creator_alpha_playbook.json").read_text())
        ops      = len(playbook.get("operators", []))
        creators = sum(len(v) for v in playbook.get("direct_creators", {}).values())
        version  = playbook.get("playbook_version", 0)

        if ops < _EXPECTED_PLAYBOOK["operators"]:
            failed.append(f"Playbook: only {ops} operators (expected {_EXPECTED_PLAYBOOK['operators']}) — may have regressed!")
        else:
            passed.append(f"Playbook: {ops} operators ✓")

        if creators < _EXPECTED_PLAYBOOK["direct_creators"]:
            failed.append(f"Playbook: only {creators} direct creators (expected {_EXPECTED_PLAYBOOK['direct_creators']})")
        else:
            passed.append(f"Playbook: {creators} direct creators ✓")

        if version < _EXPECTED_PLAYBOOK["playbook_version"]:
            warnings.append(f"Playbook version {version} — expected v{_EXPECTED_PLAYBOOK['playbook_version']}")
        else:
            passed.append(f"Playbook v{version} ✓")

        # Check no bundler wallets accidentally in operators
        noise = playbook.get("filtered_out_noise", {})
        bundlers = noise.get("bundler_clusters", [])
        op_wallets = {op["wallet"] for op in playbook.get("operators", [])}
        crossed = op_wallets & set(bundlers)
        if crossed:
            failed.append(f"Playbook: bundler wallet(s) in operators list: {crossed}")
        else:
            passed.append("No bundler wallets in operator list ✓")
    except Exception as e:
        warnings.append(f"Playbook check error: {e}")

    # ── 6. API REACHABILITY ────────────────────────────────────────────────────
    # DexScreener
    try:
        async with session.get(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                passed.append("DexScreener API reachable ✓")
            else:
                failed.append(f"DexScreener returned HTTP {r.status}")
    except Exception as e:
        failed.append(f"DexScreener unreachable: {e}")

    # Helius RPC
    if helius_key:
        try:
            async with session.post(
                f"https://mainnet.helius-rpc.com/?api-key={helius_key}",
                json={"jsonrpc":"2.0","id":1,"method":"getHealth"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    passed.append("Helius RPC reachable ✓")
                else:
                    failed.append(f"Helius RPC returned HTTP {r.status}")
        except Exception as e:
            failed.append(f"Helius RPC unreachable: {e}")

    # Groq API
    if groq_key:
        try:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":"ping"}],"max_tokens":1},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status == 200:
                    passed.append("Groq API responding ✓")
                elif r.status == 429:
                    failed.append("Groq API: RATE LIMITED (429) — brain evaluations failing!")
                elif r.status == 401:
                    failed.append("Groq API: INVALID KEY (401) — check GROQ_API_KEY")
                else:
                    warnings.append(f"Groq API returned HTTP {r.status}")
        except Exception as e:
            warnings.append(f"Groq API check error: {e}")

    # ── 7. WALLET BALANCE ──────────────────────────────────────────────────────
    try:
        rpc_url = os.getenv("HELIUS_RPC_URL") or os.getenv("SOLANA_RPC_URL", "")
        if rpc_url:
            wallet_key = os.getenv("SOLANA_PUBLIC_KEY", "")
            if wallet_key:
                async with session.post(
                    rpc_url,
                    json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[wallet_key]},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        lamports = (data.get("result") or {}).get("value", 0)
                        sol = lamports / 1e9
                        if sol < _MIN_WALLET_SOL:
                            failed.append(f"Wallet balance {sol:.4f} SOL — below {_MIN_WALLET_SOL} SOL minimum for 2 active trades!")
                        else:
                            passed.append(f"Wallet balance: {sol:.4f} SOL ✓")
    except Exception as e:
        warnings.append(f"Wallet balance check error: {e}")

    # ── 8. RECENT TRADE PERFORMANCE SUMMARY ───────────────────────────────────
    try:
        closed_path = _DIR / "monster_closed_trades.json"
        if closed_path.exists():
            closed = json.loads(closed_path.read_text())
            recent = sorted(closed, key=lambda x: x.get("close_ts", 0), reverse=True)[:10]
            if recent:
                wins   = [t for t in recent if t.get("final_pnl_pct", 0) > 5]
                losses = [t for t in recent if t.get("final_pnl_pct", 0) < -5]
                flat   = [t for t in recent if -5 <= t.get("final_pnl_pct", 0) <= 5]
                net_sol = sum(t.get("locked_sol", 0) - t.get("sol_spent", 0) for t in recent)
                best = max(recent, key=lambda x: x.get("final_pnl_pct", 0))
                summary = (
                    f"Last {len(recent)} trades: {len(wins)}W/{len(flat)}flat/{len(losses)}L "
                    f"net={net_sol:+.4f} SOL | best={best.get('final_pnl_pct',0):+.1f}%"
                )
                if net_sol < -0.2:
                    warnings.append(f"Performance: {summary} ⚠ net loss >0.2 SOL on last {len(recent)} trades")
                else:
                    passed.append(f"Performance: {summary} ✓")
    except Exception as e:
        warnings.append(f"Performance summary error: {e}")

    # ── 9. OPEN POSITIONS SANITY ───────────────────────────────────────────────
    try:
        pos_path = _DIR / "monster_positions.json"
        if pos_path.exists():
            positions = json.loads(pos_path.read_text())
            now = time.time()
            stale = [
                mint for mint, pos in positions.items()
                if now - float(pos.get("entry_ts", now)) > 4 * 3600
            ]
            if stale:
                failed.append(f"STALE POSITIONS: {len(stale)} positions open >4h — {[s[:12] for s in stale]}")
            elif positions:
                passed.append(f"{len(positions)} open position(s), all within 4h ✓")
            else:
                passed.append("No open positions (clean slate) ✓")
    except Exception as e:
        warnings.append(f"Open positions check error: {e}")

    # ── Summary ────────────────────────────────────────────────────────────────
    if owns_session:
        await session.close()

    result = {
        "ts":       ts.isoformat(),
        "passed":   passed,
        "failed":   failed,
        "warnings": warnings,
        "score":    f"{len(passed)}/{len(passed)+len(failed)+len(warnings)}",
        "status":   "FAIL" if failed else ("WARN" if warnings else "PASS"),
    }

    _last_audit_result = result
    _last_audit_ts = time.time()

    # Print structured report
    print()
    print(f"[audit] ═══════ HOURLY AUDIT {ts.strftime('%H:%M UTC')} ═══════")
    print(f"[audit] Status: {result['status']} | Score: {result['score']}")
    if failed:
        for f in failed:
            print(f"[audit] ❌ FAIL: {f}")
    if warnings:
        for w in warnings:
            print(f"[audit] ⚠️  WARN: {w}")
    for p in passed:
        print(f"[audit] ✅ {p}")
    print(f"[audit] ════════════════════════════════════════")
    print()

    return result


def get_last_audit() -> dict:
    """Return the most recent audit result (for dashboard API)."""
    return _last_audit_result


async def audit_loop() -> None:
    """Background task: run audit every 60 minutes."""
    await asyncio.sleep(30)  # small delay after startup before first audit
    while True:
        try:
            await run_audit()
        except Exception as exc:
            print(f"[audit] audit run failed: {exc}")
        await asyncio.sleep(3600)  # 1 hour
