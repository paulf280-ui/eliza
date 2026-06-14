"""Strategy E — Monster Token Strategy.

Completely separate from copy-trade (axiom_copy_trader.py). Own position pool,
own state files, own execution path. Can re-enter a mint that copy-trade has
already traded (does NOT consult traded_mints.json).

Exit spec agreed 2026-04-19:
  - TP1 at +100% → sell 50% of position (exact capital recovery)
  - Pre-TP1 hard floor: exit if pnl < -40% (rug protection)
  - Post-TP1: NO price-based SL — brain-managed event-driven exits only
  - Event triggers: liq-drop >40% in 5min, buy-ratio <30% sustained 10min,
    dev/top-5-holder coordinated sell, brain emergency signal
  - Flat gate: pnl stays in [-5%, +5%] for 60min pre-TP1 → exit (opportunity cost)
  - Max 2 concurrent monster positions
  - Size: 0.25 SOL per trade (same as copy-trade default)

Entry signals (to be wired separately, see TODO in monster_signal_sources):
  1. Cluster-Confirm Scout — 45% retro hit rate on the 40-monster corpus
  2. Serial-Deployer Sniper — 4 whitelisted creator wallets
  3. Lifecycle Scout — PumpSwap + Meteora pool within 180min + top1<10%
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

# ─── Config ───────────────────────────────────────────────────────────────
# 2026-05-03 hard-TP testing era: +100% TP → full 100% exit, close position.
# Clean data gathering for 2-position scaling validation.
# Every 0.2 SOL trade hitting +100% = +0.2 SOL profit (doubled stake).
# 30-40% range tokens: Groq evaluates MC/holders/liq trends, auto-exits if declining.
MONSTER_TP1_GAIN_PCT     = 20.0    # Hard TP at +20% — full exit, bank the profit
MONSTER_TP1_SELL_FRACTION = 1.0    # sell 100% at TP, no moonbag
MONSTER_PRE_TP1_FLOOR_PCT = -25.0  # catastrophic floor only — safety rail
MONSTER_BE_TRAIL_ACTIVATE_PCT = 20.0  # legacy — unused
MONSTER_BE_TRAIL_TARGET_PCT   = 10.0  # legacy — unused
MONSTER_BE_TRAIL_ENABLED      = False # legacy — unused
MONSTER_FLAT_TIMEOUT_SECS = 0  # DISABLED — flat_gate was misfiring
MONSTER_FLAT_ZONE_PCT     = 5.0    # ±5% = "flat" (around break-even)

# Stalled-winner gate: DISABLED in new strategy
MONSTER_STALLED_WINNER_LOW_PCT  = 12.0  # legacy
MONSTER_STALLED_WINNER_HIGH_PCT = 18.0  # legacy
MONSTER_STALLED_WINNER_SECS     = 0  # DISABLED

MONSTER_MAX_CONCURRENT    = 2      # 2 slots — diversified positions (test multi-runner scenario)
MONSTER_DEFAULT_SIZE_SOL  = 0.2    # 0.2 SOL per trade × 2 positions = 0.4 SOL max in use (conservative per-trade, diversified)

# Don't re-enter a mint that recently lost. Learned from MIM/hijabunc re-entry
# bleed 2026-04-20 (lost -100%, re-entered at lower liq, lost again at -56/-36/-25).
LOSER_COOLDOWN_SECS       = 24 * 3600  # 24h
LOSER_COOLDOWN_PNL_PCT    = -10.0      # only block if we lost more than 10%

# Post-TP1 event-driven SL thresholds
LIQ_PULL_PCT_5MIN         = 40.0   # liq dropped >40% in 5 min → exit
BUY_RATIO_FLOOR           = 30.0   # sustained <30% for 10min → exit
BUY_RATIO_SUSTAIN_SECS    = 600    # 10 min

# Price refresh cadence
MONITOR_INTERVAL_SECS     = 6   # 6s ticks — TP fires much faster, peak lasts seconds on BC

# ─── Feature flags (env-driven, default SAFE: disabled / paper-only) ────
def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")

MONSTER_STRATEGY_ENABLED = _env_on("MONSTER_STRATEGY_ENABLED", "false")
MONSTER_PAPER_ONLY       = _env_on("MONSTER_PAPER_ONLY", "true")

# ─── State files (own files — do NOT share with copy-trade) ─────────────
_BASE = Path(__file__).parent


async def _get_real_sell_pnl(
    session: aiohttp.ClientSession,
    mint: str,
    sol_spent: float,
    close_ts_approx: float,
) -> tuple[float, float] | None:
    """Find the actual on-chain swap transaction and return (real_pnl_pct, sol_received).

    Uses Helius Enhanced Transactions API — same data source Phantom uses.
    Returns None if the transaction cannot be found within the time window.

    This replaces the stale DexScreener `current_price` used for manual closes,
    which can be wrong by ±6% when price slips on a thin-liquidity pool.
    """
    helius_key = os.getenv("HELIUS_API_KEY", "")
    wallet     = os.getenv("WALLET_PUBLIC_KEY") or os.getenv("SOLANA_PUBLIC_KEY", "")
    if not helius_key or not wallet or sol_spent <= 0:
        return None
    try:
        async with session.get(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params={"api-key": helius_key, "type": "SWAP", "limit": "10"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            txns = await r.json()

        for t in (txns or []):
            ts = t.get("timestamp", 0)
            if abs(ts - close_ts_approx) > 300:   # within 5 min
                continue

            # Confirm this transaction moved our token OUT
            token_out = any(
                mint[:20] in (tt.get("mint") or "")
                and (tt.get("fromUserAccount") or "")[:12] == wallet[:12]
                for tt in (t.get("tokenTransfers") or [])
            )
            if not token_out:
                continue

            # Sum SOL received INTO our wallet from native transfers
            sol_received = sum(
                nt.get("amount", 0) / 1e9
                for nt in (t.get("nativeTransfers") or [])
                if (nt.get("toUserAccount") or "")[:12] == wallet[:12]
            )
            if sol_received <= 0:
                continue

            real_pnl_pct = (sol_received / sol_spent - 1.0) * 100.0
            print(
                f"[monster] 📊 on-chain P&L confirmed: received={sol_received:.4f} SOL "
                f"spent={sol_spent:.4f} SOL  real_pnl={real_pnl_pct:+.2f}% "
                f"(tx={t.get('signature','')[:20]}...)"
            )
            return real_pnl_pct, sol_received

    except Exception as _e:
        print(f"[monster] Helius P&L lookup failed: {_e}")
    return None
MONSTER_POSITIONS_FILE    = _BASE / "monster_positions.json"
MONSTER_CLOSED_TRADES_FILE = _BASE / "monster_closed_trades.json"

# In-memory state — lives in this module ONLY
_monster_positions: dict[str, dict] = {}
_monster_closed: list[dict] = []

# Per-mint AI cascade + price-snapshot feed for monster positions, so the 3-brain
# stack (Groq 30s / Gemini 2min / Opus 3min + emergency) rates every open monster
# trade on the same cadence copy-trade uses. Built lazily on first tick; torn
# down when the position closes.
_monster_cascades: dict[str, Any] = {}
_monster_feeds: dict[str, Any] = {}


def _load_state() -> None:
    global _monster_positions, _monster_closed
    if MONSTER_POSITIONS_FILE.exists():
        try:
            _monster_positions = json.loads(MONSTER_POSITIONS_FILE.read_text())
        except Exception:
            _monster_positions = {}
    if MONSTER_CLOSED_TRADES_FILE.exists():
        try:
            _monster_closed = json.loads(MONSTER_CLOSED_TRADES_FILE.read_text())
        except Exception:
            _monster_closed = []


def _save_state() -> None:
    MONSTER_POSITIONS_FILE.write_text(json.dumps(_monster_positions, indent=2))
    # Before writing closed trades, preserve any records that were manually
    # corrected with real on-chain data (marked by _on_chain_note). This
    # prevents blockchain-verified P&L from being overwritten by the
    # in-memory ghost_purge/-100% values every time _save_state fires.
    try:
        if MONSTER_CLOSED_TRADES_FILE.exists():
            _on_chain_corrections: dict[str, dict] = {}
            for rec in json.loads(MONSTER_CLOSED_TRADES_FILE.read_text()):
                if rec.get("_on_chain_note") and rec.get("mint"):
                    _on_chain_corrections[rec["mint"]] = rec
            if _on_chain_corrections:
                for i, rec in enumerate(_monster_closed):
                    if rec.get("mint") in _on_chain_corrections:
                        _monster_closed[i] = _on_chain_corrections[rec["mint"]]
    except Exception:
        pass
    MONSTER_CLOSED_TRADES_FILE.write_text(json.dumps(_monster_closed, indent=2))


def open_positions() -> dict[str, dict]:
    return dict(_monster_positions)


# ─── Compound tier auto-scaler ───────────────────────────────────────────────
# Tiers (wallet balance → trade size):
#   < 1.5 SOL  → 0.50 SOL/trade   (build to trigger)
#   1.5–2.5    → 0.75 SOL/trade   (compounding phase 1)
#   ≥ 2.5      → 1.00 SOL/trade   (compounding phase 2)
#   ≥ 5.0      → BANK ALERT       (bank 3.5, reset to 1.5)
_COMPOUND_TIERS: list[tuple[float, float]] = [
    (2.5, 1.00),
    (1.5, 0.75),
    (0.0, 0.50),
]
_COMPOUND_BANK_SOL  = 5.0   # trigger bank alert
_COMPOUND_RESET_SOL = 1.5   # keep this after banking
_compound_last_check: float = 0.0
_compound_current_size: float = 0.0   # 0 = unknown, forces first-run update


async def _check_compound_tier(session: aiohttp.ClientSession) -> None:
    """Read wallet SOL balance and auto-scale trade size if tier changed.
    Runs at most once every 5 minutes to avoid excess RPC calls.
    """
    # PERMANENTLY DISABLED 2026-06-13. This auto-scaler overwrote the
    # dashboard-set trade size every 5 min (forcing it back to the 0.50 tier),
    # which is the "drift" the user reported. Trade size is now controlled ONLY
    # from the dashboard and stays exactly where it is set. Do not re-enable
    # without an explicit request — re-instate the tiered logic below if so.
    return

    global _compound_last_check, _compound_current_size  # noqa: unreachable
    import time as _t, os as _os
    now = _t.time()
    if now - _compound_last_check < 300:
        return
    _compound_last_check = now

    # Fetch SOL balance via RPC
    wallet_sol = 0.0
    try:
        rpc = _os.getenv("SOLANA_RPC_URL", "")
        pk  = _os.getenv("WALLET_PUBLIC_KEY") or _os.getenv("SOLANA_PUBLIC_KEY", "")
        if rpc and pk:
            async with session.post(
                rpc,
                json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                      "params": [pk, {"commitment": "confirmed"}]},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    wallet_sol = ((d.get("result") or {}).get("value", 0)) / 1e9
    except Exception:
        return

    if wallet_sol <= 0:
        return

    # Bank alert
    if wallet_sol >= _COMPOUND_BANK_SOL:
        bank_amount = round(wallet_sol - _COMPOUND_RESET_SOL, 3)
        print(
            f"\n[compound] 🏦🏦🏦 BANK NOW — wallet={wallet_sol:.3f} SOL reached "
            f"{_COMPOUND_BANK_SOL} SOL target!\n"
            f"[compound] 👉 WITHDRAW {bank_amount} SOL to cold wallet, keep {_COMPOUND_RESET_SOL} SOL here.\n"
        )

    # Determine target tier
    target_size = 0.50
    for threshold, size in _COMPOUND_TIERS:
        if wallet_sol >= threshold:
            target_size = size
            break

    if target_size == _compound_current_size:
        return  # no change

    # Apply tier update to all three trade size keys
    prev = _compound_current_size
    _compound_current_size = target_size
    try:
        from elizaos.plugins.solana import live_config as _lc
        for key in ("lifecycle_size_sol", "monster_default_size_sol", "creator_alpha_size_sol"):
            _lc.set_value(key, target_size, changed_by="compound_tier",
                          reason=f"wallet={wallet_sol:.3f}SOL")
        arrow = "⬆️ UP" if target_size > prev else "⬇️ DOWN"
        print(
            f"[compound] {arrow} TIER CHANGE: wallet={wallet_sol:.3f} SOL  "
            f"{prev:.2f}→{target_size:.2f} SOL/trade"
        )
    except Exception as _ce:
        print(f"[compound] tier update failed: {_ce}")


def get_max_concurrent() -> int:
    """Live max concurrent positions — reads live_config first, falls back to module constant."""
    try:
        from elizaos.plugins.solana import live_config as _lc
        return int(_lc.get("monster_max_concurrent", MONSTER_MAX_CONCURRENT))
    except Exception:
        return MONSTER_MAX_CONCURRENT


def get_default_size_sol() -> float:
    """Live trade size — reads live_config first, falls back to module constant."""
    try:
        from elizaos.plugins.solana import live_config as _lc
        return float(_lc.get("monster_default_size_sol", MONSTER_DEFAULT_SIZE_SOL))
    except Exception:
        return MONSTER_DEFAULT_SIZE_SOL


def _is_creator_alpha_source(source: str | None) -> bool:
    return bool(source) and (source.startswith("creator_alpha"))


def _is_lifecycle_source(source: str | None) -> bool:
    return bool(source) and source.startswith(("lifecycle", "pumpswap_grad", "graduation"))


def _is_velocity_source(source: str | None) -> bool:
    return bool(source) and source.startswith("velocity")


def get_size_for_source(source: str | None) -> float:
    """Return the trade size for a given signal_source."""
    if _is_creator_alpha_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("creator_alpha_size_sol", 0.10))
        except Exception:
            return 0.10
    if _is_lifecycle_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("lifecycle_size_sol", 0.10))
        except Exception:
            return 0.10
    if _is_velocity_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("velocity_size_sol", 0.20))
        except Exception:
            return 0.20
    return get_default_size_sol()


def get_floor_for_source(source: str | None) -> float:
    """SL floor: -25% for all sources. Velocity returns None (no hard SL)."""
    if _is_velocity_source(source):
        return None  # type: ignore[return-value]  — velocity has no hard SL
    if _is_creator_alpha_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("creator_alpha_floor_pct", -25.0))
        except Exception:
            return -25.0
    if _is_lifecycle_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("lifecycle_floor_pct", -25.0))
        except Exception:
            return -25.0
    return MONSTER_PRE_TP1_FLOOR_PCT


def get_tp1_mult_for_source(source: str | None) -> float:
    """TP1 trigger multiple. Velocity targets +200% = 3.0×."""
    if _is_velocity_source(source):
        return 3.0   # +200% = 3× price — full exit, no moonbag
    if _is_creator_alpha_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("creator_alpha_tp1_mult", 2.0))
        except Exception:
            return 2.0
    if _is_lifecycle_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("lifecycle_tp1_mult", 1.40))
        except Exception:
            return 1.40
    return 1.0 + (MONSTER_TP1_GAIN_PCT / 100.0)


def get_tp1_sell_frac_for_source(source: str | None) -> float:
    """Fraction sold at TP1 — creator_alpha sells half (moonbag), default sells all."""
    if _is_creator_alpha_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            return float(_lc.get("creator_alpha_tp1_sell_frac", 0.5))
        except Exception:
            return 0.5
    return MONSTER_TP1_SELL_FRACTION


def can_open_new_position(source: str | None = None) -> bool:
    """Slot check — each strategy has its own slot pool so they don't compete."""
    if _is_velocity_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            cap = int(_lc.get("velocity_max_concurrent", 1))
        except Exception:
            cap = 1
        vel_count = sum(
            1 for pos in _monster_positions.values()
            if _is_velocity_source(pos.get("signal_source"))
        )
        return vel_count < cap
    if _is_creator_alpha_source(source):
        try:
            from elizaos.plugins.solana import live_config as _lc
            cap = int(_lc.get("creator_alpha_max_concurrent", 3))
        except Exception:
            cap = 3
        ca_count = sum(
            1 for pos in _monster_positions.values()
            if _is_creator_alpha_source(pos.get("signal_source"))
        )
        return ca_count < cap
    # Standard slot pool — lifecycle_quiet and others
    other_count = sum(
        1 for pos in _monster_positions.values()
        if not _is_creator_alpha_source(pos.get("signal_source"))
        and not _is_velocity_source(pos.get("signal_source"))
    )
    return other_count < get_max_concurrent()


def _recent_loss_on_mint(mint: str) -> dict | None:
    now = time.time()
    for rec in reversed(_monster_closed):
        if rec.get("mint") != mint:
            continue
        close_ts = float(rec.get("close_ts") or 0)
        if now - close_ts > LOSER_COOLDOWN_SECS:
            return None
        pnl = rec.get("final_pnl_pct")
        if pnl is not None and pnl <= LOSER_COOLDOWN_PNL_PCT:
            return rec
        return None
    return None


# ─── Entry ───────────────────────────────────────────────────────────────
async def open_monster_position(
    mint: str,
    token_name: str,
    signal_source: str,  # "cluster_confirm" | "serial_deployer" | "lifecycle" | "creator_alpha_*"
    sol_size: float,
    session: aiohttp.ClientSession,
    runtime: Any,
    metadata: dict | None = None,
    pool: str | None = None,  # explicit pool override; None = auto-detect via DexScreener
    cluster_data: dict | None = None,  # result of cluster_check — injected into AI prompts
) -> bool:
    """Open a new monster position. Returns True on success.

    Golden rule: checks traded_mints.json — NEVER re-enters previously traded tokens.
    Respects creator-alpha concurrent-position cap and holder-guard safety gates.
    """
    # Master pause check — respects dashboard pause button. Added 2026-04-24
    # after monster traded (AINI, TRADE) while user had the bot paused; the
    # old button only set copy_trade_paused which this path never read.
    try:
        from elizaos.plugins.solana import live_config as _lc_pause
        if bool(_lc_pause.get("trading_paused", False)):
            print(f"[monster] ⏸️  trading paused — skip {token_name} ({mint[:8]})")
            return False
    except Exception:
        pass

    if mint in _monster_positions:
        print(f"[monster] already holding {mint[:8]} — skip")
        return False
    if not can_open_new_position(signal_source):
        ca = _is_creator_alpha_source(signal_source)
        if ca:
            from elizaos.plugins.solana import live_config as _lc
            cap = int(_lc.get("creator_alpha_max_concurrent", 3))
            print(f"[monster] creator-alpha slot pool full (cap={cap}) — skip {token_name}")
        else:
            print(f"[monster] slot pool full (cap={get_max_concurrent()}) — skip {token_name}")
        return False

    # Golden rule: never re-enter a token we've already traded
    try:
        import json as _json_traded
        _traded_path = _BASE / "traded_mints.json"
        if _traded_path.exists():
            with open(_traded_path) as f:
                _traded = _json_traded.load(f)
                if mint in _traded:
                    print(f"[monster] 🚫 GOLDEN RULE: {token_name} ({mint[:8]}) already traded — no re-entry")
                    return False
    except Exception:
        pass

    # Holder-guard entry check — two enforcement tiers:
    #
    # ALWAYS BLOCK (regardless of HOLDER_GUARD_ENFORCE flag):
    #   - top10 concentration > 35%  (67% top10 = S&P500 rug, certain dump)
    #   - dev holding > 5%            (dev holds 67% = they will dump on you)
    #   These are absolute rug indicators. No legitimate quality token has these.
    #
    # LOG-ONLY (respects HOLDER_GUARD_ENFORCE, currently False):
    #   - bundle_bot detected          (some good tokens have creation-slot buyers)
    #   - Other soft signals
    try:
        from elizaos.plugins.solana.holder_guard import HolderGuard, config as _hg_cfg
        _guard = HolderGuard()
        _g_dec = await _guard.evaluate_entry(session, mint, scout=signal_source)
        if _g_dec.hard_block:
            _reasons = _g_dec.block_reasons or []
            # Check for CONCENTRATION blocks — always enforce these
            _concentration_block = any(
                "top10=" in r or "dev=" in r or "top10_pct" in r
                for r in _reasons
            )
            if _concentration_block:
                print(f"[monster] 🛑 CONCENTRATION BLOCK {token_name} ({mint[:8]}) "
                      f"— {'; '.join(_reasons)}")
                return False
            # All other blocks (bundle_bot etc.) respect ENFORCE flag
            elif _hg_cfg.HOLDER_GUARD_ENFORCE:
                print(f"[monster] 🛑 holder-guard veto on {token_name} ({mint[:8]}) — {'; '.join(_reasons)}")
                return False
    except Exception as _hg_err:
        print(f"[holder-guard] entry-check failure (non-fatal): {_hg_err}")

    # Loser cooldown: don't re-enter a mint that lost badly within the last 24h.
    last_loss = _recent_loss_on_mint(mint)
    if last_loss:
        hrs = (time.time() - float(last_loss.get("close_ts") or 0)) / 3600
        print(f"[monster] 🚫 {token_name} ({mint[:8]}) lost {last_loss.get('final_pnl_pct'):+.0f}% "
              f"{hrs:.1f}h ago — loser cooldown active, skip")
        return False

    mode = "PAPER" if MONSTER_PAPER_ONLY else "LIVE"
    print(f"[monster] 🎯 OPENING ({mode}) {token_name} ({mint[:8]}) {sol_size:.3f} SOL — src={signal_source}")

    # Post to cabal-hunter alerts channel whenever we enter a token.
    # This creates an audit trail of all trades and correlates entries with risk signals.
    try:
        import asyncio as _asyncio_alert
        from elizaos.plugins.solana.cluster_check import get_cluster_map as _get_cabal
        from elizaos.plugins.solana.deployer_check import (
            blend_deployer_into_score as _blend,
            get_deployer_report as _get_deployer,
        )
        from elizaos.plugins.solana.cabal_telegram import send_cabal_alert as _tg_alert_post
        from elizaos.plugins.solana.cabal_cache import save_result as _cache_save_alert

        async def _post_entry_alert():
            try:
                # Full signal set at entry — cabal + deployer + wash + cohort +
                # exit-liquidity. Non-blocking (runs after the buy). Stored on the
                # position so it persists into the closed-trade record → the
                # signal-vs-PnL proof dataset (Phase 1 dogfooding).
                from elizaos.plugins.solana.wash_check import get_wash_analysis as _wash
                from elizaos.plugins.solana.cohort_pnl import get_cohort_pnl as _cohorts
                from elizaos.plugins.solana.liquidity_check import get_liquidity_sim as _liq
                cabal_result, _dep, _wash_r, _cohort_r, _liq_r = await _asyncio_alert.gather(
                    _get_cabal(session, mint, time.time()),
                    _get_deployer(session, mint),
                    _wash(session, mint),
                    _cohorts(session, mint, time.time()),
                    _liq(session, mint),
                )
                _blend(cabal_result, _dep)  # same score/risk the bubble map shows
                sym = token_name or mint[:8]

                # Persist the entry signal snapshot onto the position
                _sni = {c.get("cohort"): c for c in (_cohort_r.get("cohorts") or [])}
                _sells = _liq_r.get("sells") or []
                _impact10 = next((s["impact_pct"] for s in _sells if s.get("sol") == 10), None)
                signals = {
                    "cabal_score":      cabal_result.get("cabal_score"),
                    "risk":             cabal_result.get("risk"),
                    "time_sync":        cabal_result.get("time_sync"),
                    "coordinated_exit": cabal_result.get("coordinated_exit"),
                    "deployer_verdict": (_dep or {}).get("verdict"),
                    "deployer_dead_pct": (_dep or {}).get("dead_pct"),
                    "wash_score":       _wash_r.get("wash_score"),
                    "wash_volume_pct":  _wash_r.get("wash_volume_pct"),
                    "snipers_hold_pct": (_sni.get("Snipers") or {}).get("current_pct_of_buy"),
                    "insiders_hold_pct": (_sni.get("Insiders") or {}).get("current_pct_of_buy"),
                    "liquidity_usd":    _liq_r.get("liquidity_usd"),
                    "exit_impact_10sol_pct": _impact10,
                    "captured_at":      time.time(),
                }
                if mint in _monster_positions:
                    _monster_positions[mint]["cabal_signals"] = signals
                    try:
                        _save_state()
                    except Exception:
                        pass
                print(f"[cabal-signals] {sym}: score={signals['cabal_score']} "
                      f"wash={signals['wash_score']} dep={signals['deployer_verdict']} "
                      f"exit10={signals['exit_impact_10sol_pct']}%")

                # Cache for bubble map
                try:
                    _cache_save_alert(mint, sym, cabal_result, time.time())
                except Exception:
                    pass
                # Always post to alerts channel (not just HIGH/MEDIUM) — audit trail of all entries
                await _tg_alert_post(mint, sym, cabal_result, session, force_post=True)
            except Exception as _alert_err:
                print(f"[cabal-alert] entry post failed for {mint[:8]}: {_alert_err}")

        # Fire async, don't block position opening on Telegram
        try:
            loop = _asyncio_alert.get_event_loop()
            loop.create_task(_post_entry_alert())
        except Exception:
            pass
    except Exception:
        pass

    # Determine pool: explicit override (e.g. creator_alpha bonding-curve buy)
    # OR auto-detect via DexScreener.
    if pool is None:
        pool = "pump-amm"
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    pairs = d.get("pairs") or []
                    for p in pairs:
                        if p.get("dexId") in ("pump-amm", "pumpswap"):
                            pool = "pump-amm"
                            break
                        if p.get("dexId") == "pumpfun":
                            pool = "pump"
        except Exception:
            pass

    # Execute the buy via the pump service (skipped in paper mode)
    buy_sig: str | None = None
    entry_price: float = 0.0
    tokens_received_raw: int = 0
    if not MONSTER_PAPER_ONLY:
        try:
            pump_svc = runtime.get_service("token_data") if runtime else None
            if not pump_svc:
                print("[monster] no pump service — cannot buy")
                return False

            # ── Dynamic slippage from wallet headroom ────────────────────
            # PumpPortal builds pump-amm buys with max_amount_in =
            # sol_size × (1 + slippage). The wallet must hold that full
            # amount even though only the actual fill is spent. With a
            # 0.77 SOL wallet and 0.6 SOL trade, 50% slippage requires
            # 0.9 SOL on hand → buy fails with System Program 0x1
            # (insufficient lamports). Cap slippage at affordable.
            wallet_svc_pre = runtime.get_service("wallet") if runtime else None
            wallet_sol = 0.0
            if wallet_svc_pre is not None:
                try:
                    wallet_sol = await wallet_svc_pre.get_sol_balance()
                except Exception as _bal_err:
                    print(f"[monster] wallet balance fetch error pre-buy: {_bal_err}")
            # Wallet service in read-only mode returns 0 — fall back to direct RPC
            if wallet_sol == 0.0:
                try:
                    import aiohttp as _aio_wb, os as _os_wb
                    _rpc = _os_wb.getenv("SOLANA_RPC_URL", "")
                    _pk  = _os_wb.getenv("WALLET_PUBLIC_KEY") or _os_wb.getenv("SOLANA_PUBLIC_KEY", "")
                    if _rpc and _pk:
                        async with _aio_wb.ClientSession() as _s:
                            async with _s.post(_rpc,
                                json={"jsonrpc":"2.0","id":1,"method":"getBalance","params":[_pk,{"commitment":"confirmed"}]},
                                timeout=_aio_wb.ClientTimeout(total=5)) as _r:
                                if _r.status == 200:
                                    _d = await _r.json()
                                    wallet_sol = (_d.get("result") or {}).get("value", 0) / 1e9
                                    print(f"[monster] 💰 RPC balance fallback: {wallet_sol:.4f} SOL")
                except Exception as _rpc_err:
                    print(f"[monster] RPC balance fallback failed: {_rpc_err}")
            BUFFER_SOL = 0.02  # rent + Jito tip + signature fees + margin
            affordable = wallet_sol - BUFFER_SOL
            if affordable < sol_size:
                print(
                    f"[monster] 🛑 wallet {wallet_sol:.4f} SOL < {sol_size + BUFFER_SOL:.4f} "
                    f"required ({sol_size:.3f} buy + {BUFFER_SOL:.3f} buffer) — skip {token_name}"
                )
                return False
            max_slip = affordable / sol_size - 1.0
            slippage = min(0.50, max(0.10, max_slip))
            print(
                f"[monster] 💰 wallet={wallet_sol:.4f} SOL, sizing buy at {sol_size:.3f} "
                f"with slippage={slippage * 100:.1f}% (max-affordable={max_slip * 100:.1f}%)"
            )

            try:
                sig = await pump_svc.buy(mint, sol_size, slippage=slippage, pool=pool)
            except Exception as e1:
                if "400" in str(e1) or "Bad Request" in str(e1):
                    alt = "pump" if pool == "pump-amm" else "pump-amm"
                    print(f"[monster] pool={pool} rejected — retry with {alt}")
                    sig = await pump_svc.buy(mint, sol_size, slippage=slippage, pool=alt)
                    pool = alt
                else:
                    raise
            buy_sig = str(sig)
        except Exception as e:
            print(f"[monster] buy failed: {e}")
            return False

        # ── Post-buy landing verification ────────────────────────────────
        # PumpPortal returns sig on 200 but tx may never land. Mirror the
        # copy-trade pattern: poll get_token_balances() up to ~12s until
        # the mint shows non-zero balance. If it never lands, DO NOT save
        # the position — a phantom entry blocks the slot pool and corrupts
        # the learning data with fake trades.
        wallet_svc = runtime.get_service("wallet") if runtime else None
        if wallet_svc is not None:
            tokens_received_raw = 0
            for attempt in range(4):  # 4 × 3s = 12s max
                await asyncio.sleep(3)
                try:
                    bals = await wallet_svc.get_token_balances()
                    tokens_received_raw = next(
                        (int(t["raw_amount"]) for t in bals if t.get("mint") == mint), 0
                    )
                    if tokens_received_raw > 0:
                        break
                except Exception as _bal_err:
                    print(f"[monster] balance check attempt {attempt + 1} error: {_bal_err}")
            if tokens_received_raw <= 0:
                print(
                    f"[monster] ❌ buy sig {buy_sig[:16]}... reported success but "
                    f"tokens NEVER landed for {token_name} — aborting (no phantom position)"
                )
                return False
        else:
            print("[monster] ⚠️  no wallet service — cannot verify landing, skipping save")
            return False
    else:
        buy_sig = "paper"

    # Fetch entry price (best effort)
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status == 200:
                d = await r.json()
                pairs = sorted(
                    d.get("pairs") or [],
                    key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                    reverse=True,
                )
                if pairs:
                    entry_price = float(pairs[0].get("priceNative") or pairs[0].get("priceUsd") or 0)
    except Exception:
        pass

    # Bonding-curve fallback: when buying brand-new pump.fun tokens via
    # PumpPortal pool="pump", DexScreener typically hasn't indexed yet so
    # entry_price comes back 0. Derive it from actual on-chain fill:
    # entry_price (SOL/token) = sol_spent / tokens_received.
    # Without this fix, TP1/floor checks silently no-op forever because
    # evaluate_exit guards on entry_price > 0. See 5fctAP4J 2026-05-02:
    # bot caught BC entry correctly but missed +521% peak because no exit
    # check could compute pnl_pct with entry_price=0.
    if entry_price <= 0 and tokens_received_raw > 0:
        # Bonding-curve safety net: pump.fun tokens use 6 decimals.
        # entry_price (SOL/token) = sol_spent / tokens_received_ui.
        try:
            tokens_ui = float(tokens_received_raw) / 1_000_000.0
            if tokens_ui > 0:
                entry_price = float(sol_size) / tokens_ui
                print(f"[monster] 💰 entry_price derived from fill: "
                      f"{sol_size} SOL / {tokens_ui:,.0f} tokens = {entry_price:.3e} SOL/token")
        except Exception as _ep_err:
            print(f"[monster] ⚠️  entry-price derivation failed: {_ep_err}")

    _monster_positions[mint] = {
        "token_name":       token_name,
        "signal_source":    signal_source,
        "metadata":         metadata or {},
        "entry_price":      entry_price,
        "sol_spent":        sol_size,
        "entry_ts":         time.time(),
        "buy_sig":          buy_sig,
        "pool":             pool,
        "tokens_received_raw": tokens_received_raw,  # 0 in paper; verified >0 in live
        # Cluster/bubble-map data — used by AI brains (Groq/Gemini/Claude) and dashboard
        "cluster_data":     cluster_data,
        # Full cabal-hunter signal snapshot at entry (filled async by _post_entry_alert).
        # Flows into the closed-trade record → the signal-vs-PnL proof dataset.
        "cabal_signals":    {},
        # Lifecycle flags
        "tp1_fired":        False,
        "remaining_fraction": 1.0,
        "locked_sol":       0.0,
        "peak_pnl_pct":     0.0,
        "peak_price":       entry_price,
        # For flat-gate detection
        "first_in_flat_zone_ts": None,
        # For liq-pull detection (filled by monitor loop)
        "liq_checkpoints":  [],
        "partial_exits":    [],
    }
    _save_state()
    print(f"[monster] ✅ entered {token_name} sig={buy_sig[:16] if buy_sig else 'none'}... price={entry_price}")

    # ── Telegram buy alert ──
    try:
        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_buy
        mode_tag = "PAPER" if MONSTER_PAPER_ONLY else "LIVE"
        meta_str = ""
        if metadata:
            mc = metadata.get("mcap_usd") or metadata.get("mc_usd")
            liq = metadata.get("liq_usd")
            age = metadata.get("age_min")
            bits = []
            if mc: bits.append(f"MC ${float(mc)/1000:.0f}k")
            if liq: bits.append(f"Liq ${float(liq)/1000:.0f}k")
            if age is not None: bits.append(f"age {int(age)}m")
            if bits:
                meta_str = " • " + "  ".join(bits)
        sig_short = f"{buy_sig[:10]}..." if buy_sig and buy_sig != "paper" else buy_sig
        await _tg_buy(
            f"🟢 <b>MONSTER BUY ({mode_tag})</b>\n"
            f"<b>{token_name}</b> via <b>{signal_source}</b>\n"
            f"<code>{mint[:20]}...</code>\n\n"
            f"Size: <b>{sol_size:.3f} SOL</b>  •  Entry: <code>{entry_price:.2e}</code>{meta_str}\n"
            f"Sig: <code>{sig_short}</code>"
        )
    except Exception as _tg_err:
        print(f"[monster] telegram buy alert failed: {_tg_err}")

    # ── Catalyst lookup (Layer 4b) ──
    # Fire non-blocking social-buzz check right after open. Result is written
    # onto pos["catalyst"] within ~15s and shows up in the brain context on
    # the next cascade tick. We never block the buy on this.
    try:
        import asyncio as _asyncio
        _asyncio.get_event_loop().create_task(
            _populate_catalyst(mint, token_name, runtime)
        )
    except Exception as _cat_err:
        print(f"[monster] catalyst task spawn failed: {_cat_err}")

    return True


async def _populate_catalyst(mint: str, token_name: str, runtime: Any) -> None:
    """Query social_monitor for buzz/catalyst context and attach to the position."""
    try:
        svc = runtime.get_service("social_monitor") if runtime else None
        if not svc:
            return
        # The check_token_buzz API: (mint, name, symbol) → (active, confidence, reason)
        symbol = token_name if len(token_name) <= 12 else token_name[:12]
        active, confidence, reason = await svc.check_token_buzz(mint, token_name, symbol)
        pos = _monster_positions.get(mint)
        if not pos:
            return
        pos["catalyst"] = {
            "active":     bool(active),
            "confidence": int(confidence),
            "reason":     str(reason)[:200],
            "fetched_ts": time.time(),
        }
        _save_state()
        print(f"[monster] 🔎 catalyst {token_name}: active={active} conf={confidence} reason={reason[:80]}")
    except Exception as exc:
        print(f"[monster] catalyst fetch failed for {mint[:8]}: {exc}")


# ─── Exit decision ────────────────────────────────────────────────────────
def evaluate_exit(pos: dict, current_price: float, current_liq: float | None,
                  current_buy_ratio: float | None) -> tuple[str | None, float]:
    """Given a position + current market data, return (reason, sell_fraction) or (None, 0.0).

    reason is a short string describing WHY we're exiting. sell_fraction is of the
    CURRENT holdings (already net of prior partials) — e.g. 0.5 sells half of what's left.
    """
    entry = pos.get("entry_price") or 0.0
    if entry <= 0 or current_price <= 0:
        return None, 0.0
    pnl_pct = ((current_price / entry) - 1.0) * 100
    tp1_fired = pos.get("tp1_fired", False)
    now = time.time()

    # ── VELOCITY strategy: +200% TP, no SL, emergency exits only ─────────────
    # Velocity positions target a clean 3× (200% gain) full exit.
    # No hard SL — the token needs room to breathe and develop.
    # Only three exits are allowed: hit +200%, liquidity collapses, or stagnation.
    src = pos.get("signal_source")
    if _is_velocity_source(src):
        # TP: hit 200% (+3× entry price)
        if pnl_pct >= 200.0:
            return f"velocity_tp200_{pnl_pct:.0f}pct", 1.0

        # Emergency: liquidity collapsed >60% from entry value
        entry_liq = float(pos.get("liq_usd_at_entry") or 0)
        if current_liq is not None and entry_liq > 0:
            if current_liq < entry_liq * 0.40:
                return f"velocity_liq_collapse_${int(current_liq)}", 1.0

        # Stagnation: still open after 6h AND price below entry
        age_h = (now - float(pos.get("entry_ts") or now)) / 3600
        if age_h > 6.0 and pnl_pct < 0:
            return f"velocity_stagnant_{age_h:.1f}h_pnl{pnl_pct:.0f}pct", 1.0

        # Volume distribution: strong sell signal (inherited from standard logic)
        _sell_ratio = pos.get("_current_sell_ratio_m5")
        _holder_delta = pos.get("_holder_delta_5min")
        if _sell_ratio is not None and _sell_ratio > 0.75 and pnl_pct > 20.0:
            return f"velocity_distribution_sr{int(_sell_ratio*100)}pct_pnl{int(pnl_pct)}pct", 1.0
        if (_holder_delta is not None and _holder_delta < -30
                and _sell_ratio is not None and _sell_ratio > 0.60):
            return f"velocity_holder_exit_delta{int(_holder_delta)}_pnl{int(pnl_pct)}pct", 1.0

        # All clear — hold and let it develop
        return None, 0.0

    # Source-aware TP/SL — creator_alpha gets +100% TP1 / -75% floor / 50% sell-frac
    # while everything else uses the standard +20% TP / -25% floor / 100% sell.
    tp1_mult = get_tp1_mult_for_source(src)
    tp1_gain_pct = (tp1_mult - 1.0) * 100.0
    # Groq TP veto can raise the target on a single position (see monitor loop)
    if pos.get("_tp_override_pct") is not None:
        tp1_gain_pct = float(pos["_tp_override_pct"])
    tp1_sell_frac = get_tp1_sell_frac_for_source(src)
    floor_pct = get_floor_for_source(src)

    # ── Hard TP at +40% — paper test configuration ────────────────────────
    # Exit 100% of position when P&L reaches +40%. SL is lifecycle_floor_pct
    # (-45%). Paper testing with hard TP to validate entry quality first.
    if not tp1_fired and pnl_pct >= tp1_gain_pct:
        return f"tp_hard_{pnl_pct:.0f}pct", tp1_sell_frac

    # ── SL-zone signal log — bounce watch ─────────────────────────────────
    # When within 15pp of the SL floor, log all key signals so we can see
    # if a bounce is forming before the stop fires.
    if not tp1_fired and floor_pct < pnl_pct < (floor_pct + 15.0):
        _br_log = current_buy_ratio or 0
        _sr_log = int((_sell_ratio or 0) * 100)
        _hd_log = _holder_delta or 0
        _liq_log = int(current_liq) if current_liq else 0
        print(f"[monster] ⚠️  SL ZONE {pnl_pct:+.1f}% "
              f"(SL={floor_pct:.0f}%) | "
              f"buy={_br_log:.0f}% sell={_sr_log}% "
              f"holders_5m={_hd_log:+d} liq=${_liq_log:,}")

    # ── Volume-based distribution exit ───────────────────────────────────
    peak_pnl = float(pos.get("peak_pnl_pct") or 0.0)
    _sell_ratio = pos.get("_current_sell_ratio_m5")
    _lower_lows = int(pos.get("_consecutive_lower_lows") or 0)
    _holder_delta = pos.get("_holder_delta_5min")  # negative = holders leaving

    # Path 1: Strong dump — heavy sell volume, no lower lows needed
    if (_sell_ratio is not None and _sell_ratio > 0.72 and pnl_pct > 10.0):
        tag = f"strong_sell_exit_sr{int(_sell_ratio*100)}pct_pnl{int(pnl_pct)}pct"
        print(f"[monster] 🚨 STRONG SELL SIGNAL — sell_ratio={_sell_ratio:.0%}, pnl={pnl_pct:+.1f}%")
        return tag, 1.0

    # Path 2: Holders leaving + sell pressure together (Discomorphism pattern)
    if (_holder_delta is not None and _holder_delta < -25
            and _sell_ratio is not None and _sell_ratio > 0.58
            and pnl_pct > 5.0):
        tag = f"holder_exit_delta{int(_holder_delta)}_sr{int(_sell_ratio*100)}pct_pnl{int(pnl_pct)}pct"
        print(f"[monster] 👥 HOLDER DECLINE EXIT — holders_5min={_holder_delta}, sell_ratio={_sell_ratio:.0%}")
        return tag, 1.0

    # Path 3: Confirmed distribution (sell ratio + 3 lower lows)
    # Holder bypass: if holders are GROWING during the sell-off, this is a
    # staircase consolidation, not distribution. The token is dipping to
    # let weak hands out while strong hands accumulate — hold.
    # MIRA: between steps 2 and 3, price fell 33% but holders kept growing.
    # Path 3 would have fired on price action alone and missed the +383% run.
    if (_sell_ratio is not None and _sell_ratio > 0.65
            and _lower_lows >= 3 and pnl_pct > 5.0):
        if _holder_delta is not None and _holder_delta > 0:
            pass  # holders accumulating during dip — staircase, not distribution
        else:
            tag = f"distribution_exit_sr{int(_sell_ratio*100)}pct_{_lower_lows}lows_pnl{int(pnl_pct)}pct"
            return tag, 1.0

    # ── Trailing gain floor (staircase exit) ──────────────────────────────
    # Activates once peak_pnl > 50%. Floor = 50% of peak.
    # Fires ONLY when: floor breached + sell_ratio > 65% + holders declining.
    # All three required — any single signal can be a normal staircase dip.
    # MIRA from ideal entry ($86K):
    #   Step 2 peak +220% → floor +110%. Dip to +115% = above floor → HOLD ✓
    #   Step 3 peak +383% → floor +192%. Price drops from $416K, sell spikes,
    #   holders start leaving → EXIT at ~$251K MC = +192% banked.
    # From actual entry ($156K):
    #   Peak +167% → floor +84%. Exit ~$286K = +84% on 0.4 SOL = 0.34 SOL.
    if peak_pnl >= 50.0:
        _tgf_floor = peak_pnl * 0.50
        if (pnl_pct <= _tgf_floor
                and pnl_pct > 5.0
                and _sell_ratio is not None and _sell_ratio > 0.65
                and (_holder_delta is None or _holder_delta < -10)):
            tag = f"trailing_floor_pnl{pnl_pct:.0f}pct_peak{peak_pnl:.0f}pct"
            print(f"[monster] 📉 TRAILING FLOOR — pnl={pnl_pct:+.1f}% "
                  f"≤ floor={_tgf_floor:.1f}% (50% of peak +{peak_pnl:.0f}%)")
            return tag, 1.0

    # ── Low-liq emergency — token market has collapsed ────────────────────
    # Below $1.5K liquidity you cannot exit at a reasonable price.
    # Bullseye: rugged to $3.25K liq while DexScreener returned 0 price
    # for 30min; by the time price was readable it was -97%.
    # Gate on age > 5min to avoid false fire on fresh grads (slow liq build).
    _pos_age_liq = time.time() - float(pos.get("entry_ts") or time.time())
    if (current_liq is not None and 0 < current_liq < 1500 and _pos_age_liq > 300):
        return f"liq_emergency_${int(current_liq)}", 1.0

    # ── Catastrophic floor → full exit ────────────────────────────────────
    # -25% for all lifecycle sources (tightened from -45% — TREAL/WOJCUP proved
    # that allowing -45% drawdowns destroys more than the brain recovers)
    if not tp1_fired and pnl_pct <= floor_pct:
        return f"pre_tp1_floor_{pnl_pct:.0f}pct", 1.0

    # ── Slow bleed detector ──────────────────────────────────────────────
    # Catches coordinated slow-drain rugs that the -25% floor misses until
    # the final flush. Painmaxxin 2026-06-03: sat -8% to -10% for 70 min
    # while top-10 holders quietly drained, then gapped to -30% in one tick.
    # Rule: if below -8% for 20 consecutive minutes without recovering to
    # -2%, exit. Recovery above -2% resets the clock.
    if not tp1_fired and pnl_pct <= -8.0:
        if pos.get("_slow_bleed_since") is None:
            pos["_slow_bleed_since"] = now
        elif now - pos["_slow_bleed_since"] >= 20 * 60:
            mins = round((now - pos["_slow_bleed_since"]) / 60)
            return f"slow_bleed_{mins}min_at_{pnl_pct:.0f}pct", 1.0
    elif pnl_pct >= -2.0 and pos.get("_slow_bleed_since") is not None:
        pos["_slow_bleed_since"] = None  # recovered meaningfully — reset

    # ── Time-based stagnation exit ───────────────────────────────────────
    # If a position has been open >15 minutes and peak PnL never exceeded 20%,
    # the token never had real momentum — bank whatever small profit/loss exists
    # and free the slot for the next genuine signal.
    #
    # Data: Trade #2 sat for 4h20m at +1.3% peak, blocking a slot all night.
    # Trades #8, #9, #10 sat 1-8 minutes at sub-5% peaks, wasting entries.
    # None of them were going to hit 100% TP. Exit early, free the slot.
    #
    # Safe for winners: Homunculus (+118%) peaked at +118% within 7 min —
    # peak_pnl_pct exceeds 20% immediately so this gate never fires on runners.
    #
    # Buy-pressure bypass: PLANDEMIC 2026-05-07 peaked at +9.5% then dipped to
    # -13.8% at 10min — stagnation fired, then token ran +30%+. The token had
    # strong buy ratio throughout. Skip stagnation when buyers are still active
    # (buy_ratio > 45%) — a shake-out looks different to a dead token.
    if not tp1_fired:
        age_secs = now - float(pos.get("entry_ts") or now)
        peak_pnl = float(pos.get("peak_pnl_pct") or 0.0)
        if age_secs > 3600 and peak_pnl < 10.0:  # 60 min — monsters need time to develop
            if current_buy_ratio is not None and current_buy_ratio > 45.0:
                pass  # buyers still active — skip stagnation, give it more time
            else:
                return f"stagnant_no_momentum_{int(age_secs//60)}min_peak{peak_pnl:.1f}pct", 1.0

    # ── Pre-TP flat gate: DISABLED (was misfiring, closing too early)
    # New strategy: let TP (+100%) and Groq evaluator (15-80% range) handle exits.

    # ── Post-TP1 lottery ticket state machine ───────────────────────────────
    # The 10% bag is the "lottery play" — user can watch on phone and manually close.
    # We track the post-TP1 running low and only "arm" safety rails once the price
    # recovers +50% off that low (V-recovery confirming Leg 2). During DORMANT,
    # exits are suppressed — we let the bag breathe. Only safety rails are active:
    #   - liq < $3k = stuck bag emergency
    #   - 8h dormant cap = don't hold forgotten bags across trading sessions
    if tp1_fired:
        pos["post_tp1_low"] = min(
            float(pos.get("post_tp1_low") or current_price),
            current_price,
        )
        pos["tp1_fired_ts"] = pos.get("tp1_fired_ts") or now
        _low = float(pos["post_tp1_low"])
        moonbag_armed = current_price >= _low * 1.5 if _low > 0 else False
        pos["moonbag_armed"] = moonbag_armed
        if _low > 0:
            _vr = current_price / _low
            pos["v_recovery_max"] = max(float(pos.get("v_recovery_max") or 0.0), _vr)

        # Safety rail 1 — liquidity collapse (always active)
        if current_liq is not None and current_liq < 3_000:
            return f"moonbag_liq_collapse_${current_liq:.0f}", 1.0

        # Safety rail 2 — 8h dormant cap
        if not moonbag_armed:
            dormant_secs = now - float(pos["tp1_fired_ts"])
            if dormant_secs >= 8 * 3600:
                return f"moonbag_dormant_{int(dormant_secs/3600)}h", 1.0
            return None, 0.0  # DORMANT — ignore all other exits

        # ── ARMED — V-recovery confirmed, event-driven exits re-enable ──
        # Liquidity pull: >40% drop over 5 min
        checkpoints = pos.get("liq_checkpoints") or []
        if current_liq is not None:
            checkpoints.append({"ts": now, "liq": current_liq})
            pos["liq_checkpoints"] = [c for c in checkpoints if now - c["ts"] <= 600]
            five_min_ago = [c for c in pos["liq_checkpoints"] if now - c["ts"] >= 300]
            if five_min_ago:
                peak_liq_5min = max(c["liq"] for c in five_min_ago)
                if peak_liq_5min > 0:
                    drop_pct = (peak_liq_5min - current_liq) / peak_liq_5min * 100
                    if drop_pct >= LIQ_PULL_PCT_5MIN:
                        return f"liq_pull_{drop_pct:.0f}pct", 1.0

        # Buy ratio sustained below 30% for 10 min
        if current_buy_ratio is not None:
            below_since = pos.get("buy_ratio_below_since")
            if current_buy_ratio < BUY_RATIO_FLOOR:
                if below_since is None:
                    pos["buy_ratio_below_since"] = now
                elif (now - below_since) >= BUY_RATIO_SUSTAIN_SECS:
                    return f"buy_ratio_fade_{current_buy_ratio:.0f}pct_10min", 1.0
            else:
                pos["buy_ratio_below_since"] = None

    return None, 0.0


# ─── Execution helpers ────────────────────────────────────────────────────
async def _execute_monster_sell(mint: str, sell_fraction: float, runtime: Any) -> tuple[bool, str | None, float]:
    """Sell a fraction of the monster position. Returns (ok, sig, est_sol_received).

    In MONSTER_PAPER_ONLY mode this does no on-chain call — returns an estimated
    SOL-out based on mark-to-market price. Live mode uses the token_data service
    (PumpFunService.sell) to place the sell via pumpportal.
    """
    pos = _monster_positions.get(mint)
    if not pos:
        return False, None, 0.0
    entry = pos.get("entry_price") or 0.0
    current = pos.get("current_price") or entry
    sol_spent_orig = pos.get("sol_spent", 0.0)
    remaining_frac = float(pos.get("remaining_fraction", 1.0))
    est_sol_out = sol_spent_orig * remaining_frac * sell_fraction * (
        (current / entry) if entry > 0 and current > 0 else 1.0
    )

    if MONSTER_PAPER_ONLY:
        return True, "paper", est_sol_out

    try:
        pump_svc = runtime.get_service("token_data")
        wallet_svc = runtime.get_service("wallet")
        if not pump_svc or not wallet_svc:
            return False, None, 0.0
        # Look up our wallet balance for this mint — use UI token count, not
        # raw lamport-style integer. PumpPortal `/trade-local` expects either a
        # UI-token number or a "100%" string when denominatedInSol=false; it
        # rejects raw-decimal integers with HTTP 400 Bad Request.
        balances = await wallet_svc.get_token_balances()
        token_entry = next((t for t in balances if t.get("mint") == mint), None)
        if not token_entry:
            print(f"[monster] zero balance for {mint[:8]} — nothing to sell")
            return False, None, 0.0
        ui_total = float(token_entry.get("amount") or 0)
        if ui_total <= 0:
            print(f"[monster] zero balance for {mint[:8]} — nothing to sell")
            return False, None, 0.0

        # Full exits use "100%" (same pattern copy-trade uses successfully).
        # Partial exits send the exact UI-token count we want to sell.
        sell_amount: float | str
        if sell_fraction >= 0.999:
            sell_amount = "100%"
        else:
            sell_amount = ui_total * sell_fraction
            if sell_amount <= 0:
                return False, None, 0.0

        # ── Pre-sell: check if token has GRADUATED since entry ───────────────
        # If entered on BC (pool=pump) but now on PumpSwap, sell on AMM.
        # AMM slippage is far lower than BC slippage for 0.2 SOL sells.
        cfg_pool = pos.get("pool", "pump-amm")
        try:
            import aiohttp as _aio_ps
            async with _aio_ps.ClientSession() as _ps_sess:
                async with _ps_sess.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                    timeout=_aio_ps.ClientTimeout(total=4),
                ) as _ps_r:
                    if _ps_r.status == 200:
                        _ps_pairs = (await _ps_r.json()).get("pairs") or []
                        _has_amm = any(
                            p.get("dexId") in ("pump-amm","pumpswap","raydium")
                            for p in _ps_pairs
                        )
                        _best_ps = max(
                            _ps_pairs,
                            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                            default=None,
                        )
                        _fresh_price = float(_best_ps.get("priceNative") or 0) if _best_ps else 0
                        _entry_p = pos.get("entry_price") or 0
                        if _fresh_price > 0 and _entry_p > 0:
                            _fresh_pnl = (_fresh_price / _entry_p - 1) * 100
                            print(f"[monster] 🔄 pre-sell check {mint[:8]}: "
                                  f"fresh_pnl={_fresh_pnl:+.1f}% "
                                  f"(decision was {(pos.get('current_price',_entry_p)/_entry_p-1)*100:+.1f}%)")
                            if _has_amm and cfg_pool == "pump":
                                cfg_pool = "pump-amm"
                                print(f"[monster] 📈 token graduated — upgrading sell to pump-amm pool")
        except Exception:
            pass  # pre-sell check failed — continue with original pool

        pool_order = [cfg_pool, "pump-amm" if cfg_pool == "pump" else "pump"]
        slippage_ladder = [50, 70, 90]
        last_err = ""
        for attempt_idx, slip_pct in enumerate(slippage_ladder):
            try_pool = pool_order[min(attempt_idx, len(pool_order) - 1)]
            try:
                sig = await pump_svc._pumpportal_trade(
                    action="sell",
                    mint=mint,
                    amount=sell_amount,
                    denominated_in_sol=False,
                    slippage_pct=slip_pct,
                    pool=try_pool,
                )
                print(f"[monster] SELL TX {pos.get('token_name', mint[:8])} "
                      f"amount={sell_amount} pool={try_pool} slip={slip_pct}% sig={str(sig)[:20]}...")

                # Read ACTUAL SOL received from wallet balance change.
                # The est_sol_out is a mark-to-market estimate that ignores
                # bonding-curve slippage and can be wildly wrong (e.g. we
                # estimated +0.3155 SOL but only received 0.034 SOL on-chain).
                # Wait for the tx to land then read the real balance delta.
                actual_sol_out = est_sol_out  # fallback if balance read fails
                try:
                    bal_before = await wallet_svc.get_sol_balance()
                    await asyncio.sleep(4)  # wait for tx to land on-chain
                    bal_after  = await wallet_svc.get_sol_balance()
                    delta = bal_after - bal_before
                    if delta > 0.0001:
                        actual_sol_out = delta
                        print(f"[monster] 💰 actual SOL received: {actual_sol_out:.4f} "
                              f"(estimate was {est_sol_out:.4f}, "
                              f"diff={actual_sol_out-est_sol_out:+.4f})")
                    else:
                        print(f"[monster] ⚠️  balance check inconclusive "
                              f"(delta={delta:.4f}) — using estimate {est_sol_out:.4f}")
                except Exception as _bal_err:
                    print(f"[monster] balance read failed ({_bal_err}) — using estimate")

                return True, str(sig), actual_sol_out
            except Exception as e:
                last_err = str(e)
                print(f"[monster] sell attempt {attempt_idx+1}/3 failed ({try_pool}, slip={slip_pct}%): {last_err[:150]}")
                continue
        print(f"[monster] all sell attempts exhausted for {mint[:8]}: {last_err[:200]}")
        return False, None, 0.0
    except Exception as e:
        print(f"[monster] sell error: {e}")
        return False, None, 0.0


async def _apply_exit(mint: str, reason: str, sell_fraction: float, runtime: Any,
                       current_price: float) -> None:
    pos = _monster_positions.get(mint)
    if not pos:
        return
    ok, sig, sol_out = await _execute_monster_sell(mint, sell_fraction, runtime)
    if not ok:
        print(f"[monster] exit failed mint={mint[:8]} reason={reason} — will retry next tick")
        return
    is_full = sell_fraction >= 0.999
    now = time.time()
    pnl_pct = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100

    if sell_fraction >= 0.5 and reason.startswith("tp1"):
        pos["tp1_fired"] = True
    pos["remaining_fraction"] = max(0.0, pos["remaining_fraction"] * (1 - sell_fraction))
    pos["locked_sol"] = (pos.get("locked_sol") or 0.0) + sol_out
    pos.setdefault("partial_exits", []).append({
        "ts": now,
        "reason": reason,
        "sell_fraction": sell_fraction,
        "sol_received": sol_out,
        "pnl_pct_at_exit": pnl_pct,
        "sig": sig,
    })
    print(f"[monster] 📤 {reason} sold {sell_fraction*100:.0f}% of {pos['token_name']} "
          f"pnl={pnl_pct:+.1f}% sol_out={sol_out:.4f} sig={str(sig)[:16] if sig else 'paper'}")

    # ── Telegram sell alert (partial or full) ──
    try:
        from elizaos.plugins.solana.telegram_alerts import send_alert as _tg_sell
        mode_tag = "PAPER" if MONSTER_PAPER_ONLY else "LIVE"
        will_close = is_full or pos["remaining_fraction"] <= 0.001
        if will_close:
            icon = "💰" if pnl_pct > 0 else ("🔴" if pnl_pct <= -10 else "⚪")
            final_pnl_sol = (pos.get("locked_sol") or 0.0) - (pos.get("sol_spent") or 0.0)
            hold_s = max(0.0, now - float(pos.get("entry_ts") or now))
            hold_str = f"{int(hold_s//60)}m{int(hold_s%60):02d}s"
            peak = pos.get("peak_pnl_pct") or 0.0
            body = (
                f"{icon} <b>MONSTER CLOSE ({mode_tag})</b>\n"
                f"<b>{pos['token_name']}</b>  •  reason: <code>{reason}</code>\n"
                f"<code>{mint[:20]}...</code>\n\n"
                f"Final P&L: <b>{pnl_pct:+.1f}%</b>  ({final_pnl_sol:+.4f} SOL)\n"
                f"Hold: {hold_str}  •  Peak: {peak:+.1f}%"
            )
        else:
            body = (
                f"🎯 <b>MONSTER PARTIAL ({mode_tag})</b>\n"
                f"<b>{pos['token_name']}</b>  •  reason: <code>{reason}</code>\n"
                f"Sold <b>{sell_fraction*100:.0f}%</b> at <b>{pnl_pct:+.1f}%</b>\n"
                f"Out: <b>{sol_out:.4f} SOL</b>  •  remaining: {pos['remaining_fraction']*100:.0f}%"
            )
        await _tg_sell(body)
    except Exception as _tg_err:
        print(f"[monster] telegram sell alert failed: {_tg_err}")

    if is_full or pos["remaining_fraction"] <= 0.001:
        # Net P&L across ALL partial exits, not just the last one. Previously
        # `final_pnl_pct` was set to the last exit's pnl_pct (exit-price vs
        # entry-price) which misreported winning trades as losers — ETF 2026-04-22
        # banked +0.056 SOL net but recorded -5.3% because its moonbag exited red.
        final_pnl_sol = pos["locked_sol"] - pos["sol_spent"]
        _sol_spent = float(pos.get("sol_spent") or 0.0)
        final_pnl_pct = (final_pnl_sol / _sol_spent * 100.0) if _sol_spent > 0 else pnl_pct
        # Extract entry metrics from metadata for performance analysis
        _meta = pos.get("metadata") or {}
        close_rec = {
            **pos,
            "mint": mint,
            "close_reason": reason,
            "close_ts": now,
            "close_price": current_price,
            "final_pnl_sol": final_pnl_sol,
            "final_pnl_pct": final_pnl_pct,
            "last_exit_pnl_pct": pnl_pct,  # preserve last-exit detail for forensics
            # Entry quality signals (2026-05-03: holder-count correlation analysis)
            "holders_at_entry": _meta.get("holders_at_entry"),
            "mc_usd_at_entry": _meta.get("mc_usd") or _meta.get("mcap_usd"),
            "liq_usd_at_entry": _meta.get("liq_usd"),
            "top1_pct_at_entry": _meta.get("top1_pct"),
            "top10_pct_at_entry": _meta.get("top10_pct"),
        }
        _monster_closed.append(close_rec)

        # ── Feed the learning engine so the brains build per-strategy rules ──
        try:
            from elizaos.plugins.solana import learning_engine as _le
            meta = pos.get("metadata") or {}
            signal_source = pos.get("signal_source") or "unknown"
            strategy_tag = f"monster_{signal_source}"  # monster_lifecycle / monster_cluster_confirm / monster_serial_deployer
            hold_mins = max(0.0, (now - float(pos.get("entry_ts") or now)) / 60.0)
            tp1_hit = bool(pos.get("tp1_fired", False))
            age_min_meta = meta.get("age_min")
            # Post-TP1 drawdown depth for moonbag learning
            _post_tp1_low = pos.get("post_tp1_low")
            _entry_px = pos.get("entry_price") or 0.0
            _post_tp1_low_pnl = (
                round((float(_post_tp1_low) / _entry_px - 1) * 100, 2)
                if _post_tp1_low and _entry_px > 0 else None
            )
            _catalyst = pos.get("catalyst") or {}
            _le.record_trade_exit(mint, {
                "token_name":    pos.get("token_name", mint[:8]),
                "wallet":        "monster",
                "strategy":      strategy_tag,
                "signal_source": signal_source,
                "narrative":     signal_source,
                "reason":        reason,
                "ts":            now,
                "hold_mins":     hold_mins,
                "entry_price":   _entry_px,
                "exit_price":    current_price,
                "pnl_pct":       final_pnl_pct,
                "pnl_sol":       final_pnl_sol,
                "peak_pnl_pct":  pos.get("peak_pnl_pct") or 0.0,
                "tp1_hit":       tp1_hit,
                "tp2_hit":       False,
                "locked_sol":    pos.get("locked_sol") or 0.0,
                "_entry_mc":     meta.get("mcap_usd") or meta.get("mc_usd"),
                "_entry_liq":    meta.get("liq_usd"),
                "_entry_holders": meta.get("holders_at_entry"),
                # ── Entry-timing fingerprint (for monster learning) ──
                "h1_change_at_entry":     meta.get("h1") or meta.get("h1_change_pct") or meta.get("h1_change"),
                "h6_change_at_entry":     meta.get("h6"),
                "h24_change_at_entry":    meta.get("h24"),
                "m5_change_at_entry":     meta.get("m5") or meta.get("m5_change_pct") or meta.get("m5_change"),
                "age_hours_at_entry":     (float(age_min_meta) / 60.0) if age_min_meta is not None else None,
                "buy_ratio_at_entry":     meta.get("buy_ratio") or meta.get("buy_ratio_pct"),
                "top1_pct_at_entry":      meta.get("top1_pct"),
                "top10_pct_at_entry":     meta.get("top10_pct"),
                # ── Expanded entry signals (2026-04-22) ──
                "momentum_ratio_at_entry": (
                    float(meta.get("h6")) / float(meta.get("h1"))
                    if (meta.get("h1") and float(meta.get("h1") or 0) > 0 and meta.get("h6")) else None
                ),
                "mcap_velocity_at_entry": (
                    float(meta.get("mcap_usd") or meta.get("mc_usd") or 0) / float(age_min_meta)
                    if age_min_meta and float(age_min_meta) > 0
                       and (meta.get("mcap_usd") or meta.get("mc_usd")) else None
                ),
                "pct_off_peak_at_entry":  meta.get("pct_off_peak_at_entry"),
                # ── Catalyst + Moonbag lifecycle ──
                "catalyst_active":     _catalyst.get("active"),
                "catalyst_confidence": _catalyst.get("confidence"),
                "moonbag_armed":       pos.get("moonbag_armed"),
                "post_tp1_low_pnl_pct": _post_tp1_low_pnl,
                "v_recovery_max":      pos.get("v_recovery_max"),
            })
        except Exception as _le_err:
            print(f"[monster] learning_engine record failed: {_le_err}")

        # ── Dev reputation: record outcome per creator wallet ──
        # Builds the proven/whitelisted/blacklisted tier ladder over time.
        # Auto-promotes after 2 wins (zero rugs) → PROVEN; auto-blacklists
        # after 2 confirmed rugs. Classification thresholds match
        # dev_reputation: pnl ≤ -50% in <120s = rug, ≥ +5% = win, else loss.
        try:
            _meta = pos.get("metadata") or {}
            _creator_wallet = _meta.get("creator")
            if _creator_wallet:
                from elizaos.plugins.solana.dev_reputation import get_reputation
                _hold_s = max(0.0, now - float(pos.get("entry_ts") or now))
                if final_pnl_pct <= -50.0 and _hold_s < 120:
                    _outcome_tag = "rug"
                elif final_pnl_pct >= 5.0:
                    _outcome_tag = "win"
                else:
                    _outcome_tag = "loss"
                get_reputation().record_outcome(
                    wallet=_creator_wallet,
                    mint=mint,
                    outcome=_outcome_tag,
                    pnl_pct=final_pnl_pct / 100.0,  # dev_reputation uses fraction not percent
                    hold_secs=_hold_s,
                    dex=pos.get("dex", "pump-amm"),
                )
        except Exception as _rep_err:
            print(f"[monster] dev_reputation record failed: {_rep_err}")

        # Resolve all PENDING brain decisions for this mint so each brain
        # builds real learned_patterns from its own track record. Without
        # this, brains stay in cold-start and show only 2 seed patterns.
        try:
            from elizaos.plugins.solana import brain_memory as _bm
            r = reason.lower()
            outcome_tag = (
                "TP_HIT"      if "tp" in r else
                "SL_HIT"      if "floor" in r or "sl" in r or "stop" in r or "pre_tp1" in r else
                "WALLET_EXIT" if "wallet" in r or "be_trail" in r else
                "TIMEOUT"     if "flat_gate" in r else
                "MANUAL"
            )
            _bm.resolve_outcome_all_brains(mint, outcome_tag, final_pnl_pct)
        except Exception as _bm_err:
            print(f"[monster] brain_memory resolve failed: {_bm_err}")

        del _monster_positions[mint]
        # Drop any AI cascade / price feed we built for this mint.
        _monster_cascades.pop(mint, None)
        _monster_feeds.pop(mint, None)
        # Release holder-flow snapshots for this mint.
        try:
            from elizaos.plugins.solana.holder_guard import flow as _hg_flow
            _hg_flow.purge(mint)
        except Exception:
            pass

        # ── Golden rule: persist to traded_mints.json so re-entry is blocked
        # across restarts. The in-memory _recently_signalled check (monster_signals)
        # is wiped on every restart — without this write the same mint can be
        # re-entered immediately after a bot restart. HANTA re-entry 2026-05-07.
        try:
            import json as _json_gr
            _traded_path = _BASE / "traded_mints.json"
            _existing: list = []
            if _traded_path.exists():
                with open(_traded_path) as _f:
                    _existing = _json_gr.load(_f)
            if mint not in _existing:
                _existing.append(mint)
                with open(_traded_path, "w") as _f:
                    _json_gr.dump(_existing, _f)
                print(f"[monster] 🔒 golden rule: {mint[:20]} added to traded_mints.json")
        except Exception as _gr_err:
            print(f"[monster] traded_mints write failed: {_gr_err}")

    _save_state()


# ─── Helius DAS price fallback ─────────────────────────────────────────────
# Used when DexScreener returns no pair data for a mint (common for freshly
# graduated pump-amm tokens in the first 5-15 minutes). Helius DAS sources
# price from Jupiter, updates every ~5s, and resolves via our existing RPC
# key — no extra cost. Returns SOL-denominated price or 0.0 on failure.
_SOL_MINT = "So11111111111111111111111111111111111111112"
_helius_sol_price_cache: tuple[float, float] = (0.0, 0.0)  # (price_usd, fetched_ts)

async def _helius_price_sol(session: aiohttp.ClientSession, mint: str) -> float:
    """Fetch SOL-denominated price via Helius DAS getAsset. ~60ms latency."""
    global _helius_sol_price_cache
    import os as _os
    rpc_url = _os.getenv("SOLANA_RPC_URL", "")
    if not rpc_url:
        return 0.0
    try:
        import time as _t
        now = _t.time()
        # Re-use cached SOL price for 10s to halve RPC calls
        sol_usd = _helius_sol_price_cache[0]
        if not sol_usd or now - _helius_sol_price_cache[1] > 10:
            async with session.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getAsset",
                      "params": {"id": _SOL_MINT}},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    pi = ((d.get("result") or {}).get("token_info") or {}).get("price_info") or {}
                    sol_usd = float(pi.get("price_per_token") or 0)
                    if sol_usd:
                        _helius_sol_price_cache = (sol_usd, now)
        if not sol_usd:
            return 0.0
        async with session.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset",
                  "params": {"id": mint}},
            timeout=aiohttp.ClientTimeout(total=3),
        ) as r:
            if r.status != 200:
                return 0.0
            d = await r.json()
            pi = ((d.get("result") or {}).get("token_info") or {}).get("price_info") or {}
            token_usd = float(pi.get("price_per_token") or 0)
            if not token_usd:
                return 0.0
            return token_usd / sol_usd
    except Exception:
        return 0.0


# ─── Monitor loop ─────────────────────────────────────────────────────────
async def monitor_positions_loop(runtime: Any, session: aiohttp.ClientSession) -> None:
    """Continuously evaluate open monster positions and fire exits as needed.

    Two parallel evaluators run per tick:
      1. `evaluate_exit` — deterministic rules (TP1, pre-TP1 floor, flat gate,
         post-TP1 liq pull, buy-ratio fade). Fast, always-on.
      2. AICascade — Groq / Gemini / Opus rate each open monster position on
         the same cadence copy-trade gets (30s / 2min / 3min + emergency).
         Needs a `PriceSnapshot` fed every tick so atr, chg_30s, liq_chg_5min,
         and holder_delta_5min populate.
    """
    # Lazy import to avoid circular refs (trade_monitor imports things that may
    # import us back through the strategy registry).
    from elizaos.plugins.solana import trade_monitor as _tm
    from elizaos.plugins.solana import live_config as _lc_audit

    GHOST_PURGE_TICKS = 3  # 3 ticks × 15s = 45s of zero on-chain balance → purge

    # ── Startup golden-rules audit ─────────────────────────────────────────
    # Printed once at startup so every restart is self-documenting.
    # Any drift in critical settings is immediately visible in the logs.
    _tm_count = len(json.load(open(_BASE / "traded_mints.json"))) if (_BASE / "traded_mints.json").exists() else 0
    print(
        f"\n[golden-rules] ══ STARTUP AUDIT ══\n"
        f"  mode            : {'PAPER' if MONSTER_PAPER_ONLY else 'LIVE ✅'}\n"
        f"  trading_paused  : {_lc_audit.get('trading_paused', '?')}  (must be False)\n"
        f"  copy_trade      : {_lc_audit.get('copy_trade_enabled', '?')}  (must be False)\n"
        f"  max_concurrent  : {_lc_audit.get('monster_max_concurrent', '?')}  (must be 1)\n"
        f"  lifecycle_size  : {_lc_audit.get('lifecycle_size_sol', '?')} SOL  (compound-managed)\n"
        f"  lifecycle_floor : {_lc_audit.get('lifecycle_floor_pct', '?')}%  (must be -25.0)\n"
        f"  lifecycle_tp    : ×{_lc_audit.get('lifecycle_tp1_mult', '?')}  (must be 1.2 = +20% TP)\n"
        f"  liq/mc gate     : 12%  (hardcoded, blocks structural rugs)\n"
        f"  traded_mints    : {_tm_count} blacklisted  (NEVER wipe)\n"
        f"  compound tiers  : <1.5→0.5 | 1.5-2.5→0.75 | ≥2.5→1.0 SOL/trade\n"
        f"  bank target     : 5.0 SOL → keep 1.5 | bank 2.5 private | 1.0 new strategy\n"
        f"[golden-rules] ══════════════════\n"
    )

    while True:
        try:
            # Auto-scale trade size based on wallet balance (runs every 5 min).
            await _check_compound_tier(session)

            # One wallet balance snapshot per tick, reused across all positions.
            wallet_balances_by_mint: dict[str, int] = {}
            if not MONSTER_PAPER_ONLY and runtime is not None:
                try:
                    wallet_svc = runtime.get_service("wallet")
                    if wallet_svc is not None:
                        bals = await wallet_svc.get_token_balances()
                        wallet_balances_by_mint = {
                            str(b.get("mint")): int(b.get("raw_amount") or 0)
                            for b in bals
                            if b.get("mint")
                        }
                except Exception as _wb_err:
                    print(f"[monster] wallet balance snapshot error: {_wb_err}")

            for mint in list(_monster_positions.keys()):
                pos = _monster_positions.get(mint)
                if not pos:
                    continue

                # ── Ghost-position detector ───────────────────────────────
                # The tokens_received_raw at buy-time claims we hold SPL — if
                # the wallet snapshot says otherwise for GHOST_PURGE_TICKS in
                # a row, the position is a ghost (tokens never settled or got
                # moved). Purge it so the slot pool doesn't deadlock. Only
                # runs in LIVE mode — paper positions have tokens_received_raw=0
                # by construction.
                if (
                    not MONSTER_PAPER_ONLY
                    and wallet_balances_by_mint  # don't purge on a failed RPC snapshot
                    and int(pos.get("tokens_received_raw") or 0) > 0
                ):
                    on_chain = int(wallet_balances_by_mint.get(mint, 0))
                    if on_chain <= 0:
                        pos["ghost_ticks"] = int(pos.get("ghost_ticks", 0)) + 1
                        tn = pos.get("token_name", mint[:8])
                        print(f"[monster] 👻 ghost check {tn} ({mint[:8]}) "
                              f"claimed_raw={pos.get('tokens_received_raw')} "
                              f"on_chain_raw=0 streak={pos['ghost_ticks']}/{GHOST_PURGE_TICKS}")
                        if pos["ghost_ticks"] >= GHOST_PURGE_TICKS:
                            print(f"[monster] ❌ GHOST PURGE {tn} ({mint[:8]}) — tokens "
                                  f"not on-chain for {pos['ghost_ticks']} ticks. Freeing slot.")
                            # Account for any SOL already recovered via partial_exits.
                            # CATEROID 2026-04-22 previously recorded -100% / -0.3 SOL
                            # after TP1 had banked 0.325 SOL + position_manager sold
                            # the moonbag externally — real net was ~+0.07 SOL.
                            try:
                                now = time.time()
                                entry_price = float(pos.get("entry_price") or 0.0)
                                _spent  = float(pos.get("sol_spent") or 0.0)
                                _locked = float(pos.get("locked_sol") or 0.0)
                                # If tokens vanished but we've already locked ≥ spent,
                                # it's an externally-closed (manual / stall / Jupiter)
                                # sale, not a phantom rug. Use the net we know.
                                _had_partials = bool(pos.get("partial_exits"))
                                # Use the last monitored price as exit price — it's
                                # updated every 6s so is close to the actual manual
                                # sell price. Much better than showing -100% / entry
                                # price when the user manually closed on Phantom.
                                _last_known_price = float(pos.get("current_price") or entry_price)
                                if _had_partials:
                                    _final_sol = _locked - _spent
                                    _final_pct = (_final_sol / _spent * 100.0) if _spent > 0 else -100.0
                                    _close_reason = "externally_closed"
                                    _close_price = _last_known_price
                                elif _last_known_price > 0 and entry_price > 0:
                                    # Manual sell detected — look up the real on-chain execution
                                    # price via Helius Enhanced API (same source as Phantom).
                                    # Falls back to stale DexScreener price if Helius unavailable.
                                    _close_reason = "manual_close"
                                    _real = await _get_real_sell_pnl(
                                        session, mint, _spent, now
                                    )
                                    if _real is not None:
                                        _final_pct, _final_sol_recv = _real
                                        _final_sol  = _final_sol_recv - _spent
                                        _close_price = entry_price * (1.0 + _final_pct / 100.0)
                                    else:
                                        # Helius lookup failed — fall back to DexScreener price
                                        _final_pct = (_last_known_price / entry_price - 1.0) * 100.0
                                        _final_sol = _spent * (_final_pct / 100.0)
                                        _close_price = _last_known_price
                                        print(f"[monster] 👤 manual close {tn}: "
                                              f"est pnl={_final_pct:+.1f}% (DexScreener fallback)")
                                else:
                                    _final_sol = -_spent
                                    _final_pct = -100.0
                                    _close_reason = "ghost_purge"
                                    _close_price = entry_price
                                close_rec = {
                                    **pos,
                                    "mint": mint,
                                    "close_reason": _close_reason,
                                    "close_ts": now,
                                    "close_price": _close_price,
                                    "final_pnl_sol": _final_sol,
                                    "final_pnl_pct": _final_pct,
                                }
                                _monster_closed.append(close_rec)
                            except Exception:
                                pass
                            del _monster_positions[mint]
                            _monster_cascades.pop(mint, None)
                            _monster_feeds.pop(mint, None)
                            # Golden rule: manual/external closes must also block re-entry.
                            try:
                                import json as _json_mc
                                _tp = _BASE / "traded_mints.json"
                                _ex: list = json.load(open(_tp)) if _tp.exists() else []
                                if mint not in _ex:
                                    _ex.append(mint)
                                    with open(_tp, "w") as _f:
                                        _json_mc.dump(_ex, _f)
                                    print(f"[monster] 🔒 golden rule: {mint[:20]} added (manual/external close)")
                            except Exception as _gr2:
                                print(f"[monster] traded_mints write failed (manual): {_gr2}")
                            _save_state()
                            continue
                    else:
                        if pos.get("ghost_ticks"):
                            pos["ghost_ticks"] = 0

                # Pull the richest-liquidity pair snapshot from DexScreener.
                current_price = 0.0
                current_liq: float | None = None
                current_buy_ratio: float | None = None
                current_mc: float | None = None
                vol_h1_v = 0.0
                buys_h1 = 0
                sells_h1 = 0
                _pair_snapshot: dict = {}
                try:
                    async with session.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        if r.status == 200:
                            d = await r.json()
                            pairs = sorted(
                                d.get("pairs") or [],
                                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                                reverse=True,
                            )
                            if pairs:
                                p0 = pairs[0]
                                _pair_snapshot = p0
                                current_price = float(p0.get("priceNative") or 0)
                                current_liq = float((p0.get("liquidity") or {}).get("usd") or 0)
                                current_mc = float(p0.get("marketCap") or p0.get("fdv") or 0) or None
                                txns_h1 = (p0.get("txns") or {}).get("h1") or {}
                                buys_h1 = int(txns_h1.get("buys") or 0)
                                sells_h1 = int(txns_h1.get("sells") or 0)
                                total = buys_h1 + sells_h1
                                current_buy_ratio = (buys_h1 / total * 100) if total else None
                                vol_h1_v = float((p0.get("volume") or {}).get("h1") or 0)
                except Exception:
                    pass

                # ── Helius DAS price fallback ──────────────────────────────
                # DexScreener often has no pair data for the first 5-15 min
                # after graduation. During this window current_price stays 0
                # and evaluate_exit can't fire — positions drift to the floor
                # completely unmonitored (L2.0, pWRG/AND-M post-mortem).
                # Helius DAS (Jupiter-sourced, ~5s freshness, ~60ms latency)
                # fills the gap using our existing Helius RPC key.
                _used_helius_price = False
                if current_price == 0:
                    _h_price = await _helius_price_sol(session, mint)
                    if _h_price > 0:
                        current_price = _h_price
                        _used_helius_price = True

                # ── No-price emergency exit ──────────────────────────────
                # Bullseye 2026-05-19: token rugged in minutes. DexScreener
                # and Helius both returned price=0 for ~30 minutes during
                # the collapse. evaluate_exit guards on price=0 → returns
                # None → SL never evaluated → eventual exit at -97%.
                #
                # Fix: after 3 consecutive ticks (18s) with no price AND
                # position is > 90s old (not a fresh grad DexScreener gap),
                # force exit at the last known price. A legitimate live
                # token should NEVER be invisible to both APIs for 18s.
                _pos_age_secs = time.time() - float(pos.get("entry_ts") or time.time())
                if current_price == 0 and _pos_age_secs > 90:
                    pos["_no_price_ticks"] = int(pos.get("_no_price_ticks", 0)) + 1
                    if pos["_no_price_ticks"] >= 3:
                        _last_px = float(pos.get("current_price") or pos.get("entry_price") or 0)
                        _nm = pos.get("token_name", mint[:8])
                        print(f"[monster] 🚨 NO-PRICE EMERGENCY {_nm} ({mint[:8]}) — "
                              f"{pos['_no_price_ticks']} ticks no price data — force exit at last known")
                        await _apply_exit(
                            mint,
                            f"no_price_rug_{pos['_no_price_ticks']}ticks",
                            1.0, runtime, _last_px,
                        )
                        continue
                else:
                    if current_price > 0:
                        pos["_no_price_ticks"] = 0  # reset on good data

                if current_price > 0:
                    pos["current_price"] = current_price
                    pnl_pct = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100
                    if pnl_pct > (pos.get("peak_pnl_pct") or 0):
                        pos["peak_pnl_pct"] = pnl_pct
                        pos["peak_price"] = current_price
                    if _used_helius_price:
                        print(f"[monster] 📡 helius price {pos.get('token_name', mint[:8])}: "
                              f"{current_price:.3e} SOL pnl={pnl_pct:+.1f}% (dexscreener blind)")

                    # ── Volume exit signal tracking ──────────────────────────
                    # Track consecutive lower lows + sell ratio for the
                    # distribution detection exit (replaces trailing stop).
                    # When sell_ratio_m5 > 65% AND 3 consecutive lower lows
                    # AND we're in profit → distribution event confirmed, exit.
                    try:
                        _price_hist = pos.setdefault("_price_history", [])
                        _price_hist.append(current_price)
                        if len(_price_hist) > 6:
                            _price_hist.pop(0)
                        _lower_lows = 0
                        for _idx in range(len(_price_hist)-1, 0, -1):
                            if _price_hist[_idx] < _price_hist[_idx-1]:
                                _lower_lows += 1
                            else:
                                break
                        pos["_consecutive_lower_lows"] = _lower_lows
                        # Sell ratio from buy_ratio (current_buy_ratio is buys/total)
                        if current_buy_ratio is not None:
                            pos["_current_sell_ratio_m5"] = round(1.0 - (current_buy_ratio / 100.0), 3)
                    except Exception:
                        pass

                    # Holder count delta — powers exit Path 2 (holder_exit).
                    # DexScreener info.holders updates every ~30s. Keep 10 min
                    # of snapshots and compute the true 5-minute window delta.
                    try:
                        _h_raw = (_pair_snapshot.get("info") or {}).get("holders")
                        if _h_raw is not None:
                            _h_cur = int(_h_raw)
                            _hsnaps = pos.setdefault("_holder_snapshots", [])
                            _hnow = time.time()
                            _hsnaps.append({"ts": _hnow, "h": _h_cur})
                            pos["_holder_snapshots"] = [s for s in _hsnaps if _hnow - s["ts"] <= 600]
                            # Compare against snapshot 4.5-5.5 min old for true 5-min delta
                            _h5 = [s for s in pos["_holder_snapshots"] if _hnow - s["ts"] >= 270]
                            if _h5:
                                _h_oldest = min(_h5, key=lambda s: s["ts"])
                                pos["_holder_delta_5min"] = _h_cur - _h_oldest["h"]
                            elif len(pos["_holder_snapshots"]) >= 2:
                                _h_oldest = pos["_holder_snapshots"][0]
                                pos["_holder_delta_5min"] = _h_cur - _h_oldest["h"]
                    except Exception:
                        pass

                # ── 1. Deterministic rules (TP1, floor, flat, post-TP1 events) ──
                reason, frac = evaluate_exit(pos, current_price, current_liq, current_buy_ratio)

                # ── 1b. Holder-flow guard — can force exit on top-10 jump /
                # holder drop, or suppress short-term price noise exits when
                # accumulation is still healthy (hold_override).
                try:
                    from elizaos.plugins.solana.holder_guard import HolderGuard, config as _hg_cfg
                    _hg = HolderGuard()
                    _flow_dec = await _hg.evaluate_exit(session, mint)
                    if _flow_dec.should_exit and _hg_cfg.HOLDER_GUARD_ENFORCE:
                        reason = _flow_dec.exit_reason or "holder_flow_exit"
                        frac = 1.0
                    elif (
                        _flow_dec.hold_override
                        and _hg_cfg.HOLDER_GUARD_ENFORCE
                        and reason
                        and reason.startswith(("flat_gate", "be_trail"))
                    ):
                        print(f"[holder-guard] 🟢 HOLD override on {mint[:8]} — {_flow_dec.hold_reason} — suppressing {reason}")
                        reason, frac = None, 0.0
                except Exception as _hg_err:
                    print(f"[holder-guard] exit-check failure (non-fatal): {_hg_err}")

                # ── 2. AI cascade — feeds a snapshot every tick & runs tiers ─────
                ai_reason: str | None = None
                if current_price > 0:
                    try:
                        feed = _monster_feeds.get(mint)
                        if feed is None:
                            feed = _tm.DataFeed(
                                mint,
                                market_data_fn=lambda _m, _s: None,
                                pool_resolve_fn=lambda _m, _s: None,
                                runtime=runtime,
                            )
                            _monster_feeds[mint] = feed
                        feed.snapshots.append(_tm.PriceSnapshot(
                            ts=time.time(),
                            price=current_price,
                            mc=current_mc,
                            liq=current_liq,
                            vol_h1=vol_h1_v,
                            buys_h1=buys_h1,
                            sells_h1=sells_h1,
                            buy_sell_ratio=current_buy_ratio,
                            vol_mc_ratio=(vol_h1_v / current_mc) if (current_mc and current_mc > 0) else None,
                            holders=None,  # DexScreener doesn't expose; holder_delta_5min stays None
                        ))

                        # Adapter keys so _build_context projects monster into
                        # the same shape copy-trade uses. Cheap to set every tick.
                        sig_src = pos.get("signal_source") or "monster"
                        pos.setdefault("wallet_name", sig_src)
                        pos["tp1_hit"] = bool(pos.get("tp1_fired", False))
                        pos["dex"] = pos.get("dex") or (
                            "pump-amm" if (pos.get("pool") or "").startswith("pump") else pos.get("pool") or "pump-amm"
                        )
                        pos["narrative"] = pos.get("narrative") or sig_src
                        pos["strategy"] = f"monster_{sig_src}"
                        pos["peak_price"] = pos.get("peak_price") or pos.get("entry_price")

                        cascade = _monster_cascades.get(mint)
                        if cascade is None:
                            cascade = _tm.AICascade()
                            _monster_cascades[mint] = cascade
                        # Labels used by brain_memory.record_decision for dashboard panels.
                        cascade._mint        = mint
                        cascade._token_name  = pos.get("token_name", mint[:8])
                        cascade._last_pnl_pct = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100
                        # Cluster/bubble-map data — injected into every brain prompt.
                        cascade._cluster_data = pos.get("cluster_data")

                        decision = await cascade.evaluate(mint, pos, feed, session)
                        # ── Monster AI-exit gate ─────────────────────────────
                        # The brains rate every tick (already recorded to brain
                        # memory for dashboard visibility), but we only ACT on a
                        # SELL when the position is actually in trouble. Monster
                        # positions have a deterministic -40% hard floor and
                        # +100% TP1 — we don't want Groq/Gemini yanking a +24%
                        # runner just because 5m change dipped. AI is the
                        # emergency brake, not the primary exit.
                        # Risk-level exit: act on observable categorisation, not
                        # binary SELL. Code triggers on HIGH; AI classifies the facts.
                        _risk_lvl = getattr(decision, "risk_level", "MEDIUM") if decision else "MEDIUM"
                        _is_high_risk = (_risk_lvl == "HIGH")
                        if decision and (decision.action == "SELL" or _is_high_risk):
                            cur_pnl = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100
                            peak_pnl = pos.get("peak_pnl_pct") or 0.0
                            drawdown_from_peak = peak_pnl - cur_pnl  # positive = we gave back gains
                            tp1_fired = bool(pos.get("tp1_fired", False))
                            tier = decision.tier
                            conf = decision.confidence

                            # Hard-TP era (2026-04-26): with TP=full-exit at +20%
                            # and floor at -25%, the brains rule the entire band
                            # in between. Any tier with conf ≥ 0.70 is enough —
                            # the catastrophic floor still catches anything they
                            # miss. Old multi-condition runner-protection gate
                            # (opus≥0.85, drawdown thresholds) is gone — it was
                            # the reason MEOWJESTY died at the floor on 2026-04-26
                            # despite Claude correctly calling SELL at -10.6%.
                            ai_allowed = False
                            gate_reason = ""

                            # Peak-protection threshold: if this position was a
                            # real winner at some point (peak ≥ +5%) AND we're
                            # not in catastrophic territory (pnl > -10%), require
                            # higher confidence from non-Claude tiers to exit.
                            # EWON 2026-05-01: peak +10.7%, Claude held at +4%,
                            # Groq sold at -2.1% with conf=0.85, then the price
                            # recovered to +18% which would have hit our TP. The
                            # deeper brain's HOLD signal deserves more weight on
                            # previously-winning positions. Claude SELLs always
                            # use the base 0.70 (we trust the depth tier).
                            # Velocity positions have NO hard SL — Groq is the primary
                            # exit authority. Lower threshold so he acts decisively
                            # on candle/volume signals without needing extreme confidence.
                            is_vel_pos = _is_velocity_source(pos.get("signal_source"))
                            min_conf = 0.65 if is_vel_pos else 0.70
                            if (not is_vel_pos
                                and tier != "claude"
                                and peak_pnl >= 5.0
                                and cur_pnl > -10.0
                                and drawdown_from_peak < 10.0):
                                # Only protect lifecycle positions with small drawdown.
                                # Velocity positions don't get this — they need Groq
                                # to be responsive when the candles turn bearish.
                                min_conf = 0.90

                            # Claude-HOLD veto on lower tiers — extends below
                            # the peak-protection band. PETS 2026-05-01 had
                            # peak=0.0% so peak-protection didn't fire, but
                            # Claude was on record HOLD at -1.9% and at -12.3%
                            # right before Groq sold at -19.5%. The depth tier's
                            # active HOLD deserves to override Groq even on
                            # never-winning positions. Catastrophic floor (-25%)
                            # is a separate exit and still fires regardless.
                            claude_veto = False
                            claude_veto_conf = 0.0
                            if tier != "claude":
                                try:
                                    from elizaos.plugins.solana import brain_memory as _bm_v
                                    # Velocity: tighter Claude veto window (120s vs 600s) —
                                    # Groq should be more decisive on velocity candle signals.
                                    veto_window = 120 if is_vel_pos else 600
                                    claude_veto, claude_veto_conf = _bm_v.claude_recently_holding(mint, veto_window)
                                except Exception:
                                    pass

                            if tp1_fired:
                                # tp1_fired implies a legacy partial-exit position
                                # opened before the hard-TP migration. Keep the
                                # old dormant-moonbag exemption out of caution.
                                _dormant_bag = not pos.get("moonbag_armed", False)
                                if _dormant_bag:
                                    _low = float(pos.get("post_tp1_low") or 0)
                                    _vr = (current_price / _low) if _low > 0 else 0.0
                                    print(f"[monster] 💤 dormant-bag ignored AI SELL "
                                          f"{pos.get('token_name', mint[:8])} tier={tier} conf={conf:.2f} "
                                          f"pnl={cur_pnl:+.1f}% v_recovery={_vr:.2f}× (need 1.50×)")
                                elif conf >= min_conf and not claude_veto:
                                    ai_allowed = True
                                    gate_reason = f"armed_moonbag_{tier}"
                            elif conf >= min_conf and not claude_veto:
                                ai_allowed = True
                                gate_reason = f"brain_primary_{tier}_pnl{cur_pnl:+.0f}"

                            # Risk-level fast path: if Groq/Gemini explicitly
                            # categorised danger as HIGH with ≥0.70 confidence,
                            # bypass peak-protect. The risk categorisation prompt
                            # requires MULTIPLE gates to confirm HIGH simultaneously —
                            # it's not triggered by a single uncertain signal.
                            if _is_high_risk and conf >= 0.70 and not tp1_fired:
                                ai_allowed = True
                                gate_reason = f"risk_high_{tier}_conf{conf:.2f}"

                            if ai_allowed:
                                ai_reason = f"ai_{tier}_{conf:.2f}_{gate_reason}"
                                risk_tag = f" risk={_risk_lvl}" if _is_high_risk else ""
                                print(f"[monster] 🧠 AI EXIT ALLOWED {pos.get('token_name', mint[:8])} "
                                      f"tier={tier} conf={conf:.2f} pnl={cur_pnl:+.1f}% peak={peak_pnl:+.1f}%"
                                      f"{risk_tag} gate={gate_reason} why={decision.reason[:80]}")
                            else:
                                if claude_veto:
                                    tag = f"🛡 CLAUDE-VETO (Claude HOLD conf={claude_veto_conf:.2f})"
                                elif min_conf > 0.70:
                                    tag = "🛡 PEAK-PROTECT"
                                else:
                                    tag = "🧠 below threshold"
                                print(f"[monster] {tag} AI sell signal blocked "
                                      f"{pos.get('token_name', mint[:8])} tier={tier} conf={conf:.2f} "
                                      f"pnl={cur_pnl:+.1f}% peak={peak_pnl:+.1f}% (need conf≥{min_conf:.2f}) "
                                      f"why={decision.reason[:80]}")
                    except Exception as _ai_err:
                        print(f"[monster] AI cascade error for {mint[:8]}: {_ai_err}")

                # ── Groq TP veto — one shot at the 20% hard TP ──────────────
                # If Groq sees strong momentum (volume, holders, buy pressure),
                # it can veto the 20% sell and raise the target to hold for more.
                # Only fires once per position (tp_groq_vetoed flag). If Groq is
                # unavailable or uncertain, default is to SELL and bank the profit.
                if (reason and reason.startswith("tp_hard")
                        and _is_lifecycle_source(pos.get("signal_source"))
                        and not pos.get("tp_groq_vetoed")):
                    _groq_key_tp = os.getenv("GROQ_API_KEY", "")
                    if _groq_key_tp:
                        try:
                            _meta = pos.get("metadata") or {}
                            _vol_m5 = float((_pair_snapshot.get("volume") or {}).get("m5") or 0)
                            _pc = _pair_snapshot.get("priceChange") or {}
                            _m5_chg = float(_pc.get("m5") or 0)
                            _h1_chg = float(_pc.get("h1") or 0)
                            _txns_m5 = (_pair_snapshot.get("txns") or {}).get("m5") or {}
                            _m5_buys = int(_txns_m5.get("buys") or 0)
                            _m5_sells = int(_txns_m5.get("sells") or 0)
                            _top10_entry = _meta.get("top10_pct") or 0
                            _age_min = (time.time() - float(pos.get("entry_ts") or time.time())) / 60
                            _peak = pos.get("peak_pnl_pct") or 0
                            _br = current_buy_ratio or 0
                            _veto_prompt = (
                                f"Monster trade just hit +20% TP. Bank it now or let it run?\n\n"
                                f"Token stats at TP trigger:\n"
                                f"  Age since entry: {_age_min:.0f}min | PNL: +20% | Peak: {_peak:.1f}%\n"
                                f"  Price m5: {_m5_chg:+.1f}% | Price h1: {_h1_chg:+.1f}%\n"
                                f"  Volume m5: ${_vol_m5:,.0f} | Volume h1: ${vol_h1_v:,.0f}\n"
                                f"  m5 buys: {_m5_buys} | m5 sells: {_m5_sells}\n"
                                f"  Buy ratio h1: {_br:.0f}% | Liquidity: ${current_liq:,.0f}\n"
                                f"  Top10 holders at entry: {_top10_entry:.1f}%\n\n"
                                f"HOLD only if ALL three are strong: (1) m5 buy count > 30 AND buys > sells, "
                                f"(2) price m5 positive or flat (not dumping), "
                                f"(3) top10 concentration reasonable (<14%). "
                                f"If any signal is weak or mixed, SELL and bank the profit. "
                                f"Default to SELL if uncertain.\n\n"
                                f"Reply JSON only: "
                                f'{{\"action\": \"sell\"|\"hold\", \"hold_until_pct\": 40, \"reason\": \"one sentence\"}}'
                            )
                            async with session.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                headers={"Authorization": f"Bearer {_groq_key_tp}", "Content-Type": "application/json"},
                                json={"model": "llama-3.3-70b-versatile",
                                      "messages": [{"role": "user", "content": _veto_prompt}],
                                      "temperature": 0.1, "max_tokens": 80},
                                timeout=aiohttp.ClientTimeout(total=4),
                            ) as _vr:
                                if _vr.status == 200:
                                    _vrd = await _vr.json()
                                    _vraw = (_vrd.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                                    import json as _jv
                                    _vclean = _vraw.strip().strip("```json").strip("```").strip()
                                    _vparsed = _jv.loads(_vclean)
                                    _vaction = (_vparsed.get("action") or "sell").lower()
                                    _vnew_tp = float(_vparsed.get("hold_until_pct") or 40)
                                    _vnew_tp = min(max(_vnew_tp, 25.0), 60.0)
                                    _vreason = _vparsed.get("reason", "")
                                    pos["tp_groq_vetoed"] = True
                                    if _vaction == "hold":
                                        pos["_tp_override_pct"] = _vnew_tp
                                        reason = None
                                        frac = 0.0
                                        print(f"[monster] 🚀 GROQ TP-HOLD {pos.get('token_name', mint[:8])}: "
                                              f"momentum strong — holding through +20% → target +{_vnew_tp:.0f}% | {_vreason}")
                                    else:
                                        print(f"[monster] ✅ GROQ TP-CONFIRM {pos.get('token_name', mint[:8])}: "
                                              f"banking +20% | {_vreason}")
                        except Exception as _ve:
                            print(f"[monster] ⚠ GROQ TP-VETO error {mint[:8]}: {_ve} — defaulting to SELL")

                # Rules win over AI on ties — rules are deterministic & cheap.
                if reason:
                    await _apply_exit(mint, reason, frac, runtime, current_price)
                elif ai_reason and current_price > 0:
                    await _apply_exit(mint, ai_reason, 1.0, runtime, current_price)

            _save_state()
        except Exception as e:
            print(f"[monster] monitor loop error: {e}")

        await asyncio.sleep(MONITOR_INTERVAL_SECS)


# Load state on import so hot reloads don't forget positions
_load_state()


# ─── Social-event exit hook ──────────────────────────────────────────────
# Called by monster_social_monitor when a Tier-1/Tier-2 social event fires.
# Event → (sell_fraction, reason_suffix) mapping per the scope doc.
SOCIAL_EVENT_ACTIONS: dict[str, tuple[float, str]] = {
    "tweet_deleted":        (1.0, "social_tweet_deleted"),
    "author_disavow":       (1.0, "social_author_disavow"),
    "engagement_collapse":  (1.0, "social_engagement_collapse"),
    "community_deleted":    (1.0, "social_community_deleted"),
    "community_drain":      (0.5, "social_community_drain"),
    "brain_emergency":      (1.0, "brain_emergency"),
}


async def on_social_event(mint: str, event: str, runtime: Any) -> bool:
    """Translate a social-monitor event into a monster-position exit.

    Safe to call for a mint that isn't in our pool — returns False in that case.
    """
    action = SOCIAL_EVENT_ACTIONS.get(event)
    if not action:
        print(f"[monster] unknown social event={event} — ignoring")
        return False
    pos = _monster_positions.get(mint)
    if not pos:
        return False
    frac, reason = action
    current_price = pos.get("current_price") or pos.get("entry_price") or 0.0
    await _apply_exit(mint, reason, frac, runtime, current_price)
    return True
