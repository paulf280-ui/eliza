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
# 2026-04-26 hard-TP era: 1 slot × 0.6 SOL, full exit at +20%, no moonbag.
# Brains rule the band between catastrophic floor (-25%) and TP (+20%).
MONSTER_TP1_GAIN_PCT     = 20.0    # hard TP — full exit, no moonbag
MONSTER_TP1_SELL_FRACTION = 1.0    # sell 100% at TP (no 25% tail to rug)
MONSTER_PRE_TP1_FLOOR_PCT = -25.0  # catastrophic floor only — brains manage between -25 and +20
MONSTER_BE_TRAIL_ACTIVATE_PCT = 20.0  # legacy — unused while TP1 is full exit
MONSTER_BE_TRAIL_TARGET_PCT   = 10.0  # legacy — unused while TP1 is full exit
MONSTER_BE_TRAIL_ENABLED      = False # legacy — unused while TP1 is full exit
MONSTER_FLAT_TIMEOUT_SECS = 60 * 60  # 60 min pre-TP with pnl in flat zone → exit
MONSTER_FLAT_ZONE_PCT     = 5.0    # ±5% = "flat" (around break-even)

# Stalled-winner gate: pnl camped near TP without breaking through → bank it.
# Triggers at 2026-04-26 user request: if we're already +12-18% and idling
# for 10 min, take what we've got rather than waiting for a +20% break that
# may never come. Bypassed by an active brain HOLD vote at conf ≥ 0.75.
MONSTER_STALLED_WINNER_LOW_PCT  = 12.0  # bottom of stalled-winner band
MONSTER_STALLED_WINNER_HIGH_PCT = 18.0  # top of stalled-winner band (just below TP)
MONSTER_STALLED_WINNER_SECS     = 10 * 60  # 10 min in band → exit

MONSTER_MAX_CONCURRENT    = 1      # 1 slot — single concentrated position
MONSTER_DEFAULT_SIZE_SOL  = 0.45   # one trade × 0.45 SOL — leaves headroom for full 50% slippage on 0.77 SOL wallet

# Don't re-enter a mint that recently lost. Learned from MIM/hijabunc re-entry
# bleed 2026-04-20 (lost -100%, re-entered at lower liq, lost again at -56/-36/-25).
LOSER_COOLDOWN_SECS       = 24 * 3600  # 24h
LOSER_COOLDOWN_PNL_PCT    = -10.0      # only block if we lost more than 10%

# Post-TP1 event-driven SL thresholds
LIQ_PULL_PCT_5MIN         = 40.0   # liq dropped >40% in 5 min → exit
BUY_RATIO_FLOOR           = 30.0   # sustained <30% for 10min → exit
BUY_RATIO_SUSTAIN_SECS    = 600    # 10 min

# Price refresh cadence
MONITOR_INTERVAL_SECS     = 15

# ─── Feature flags (env-driven, default SAFE: disabled / paper-only) ────
def _env_on(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")

MONSTER_STRATEGY_ENABLED = _env_on("MONSTER_STRATEGY_ENABLED", "false")
MONSTER_PAPER_ONLY       = _env_on("MONSTER_PAPER_ONLY", "true")

# ─── State files (own files — do NOT share with copy-trade) ─────────────
_BASE = Path(__file__).parent
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
    MONSTER_CLOSED_TRADES_FILE.write_text(json.dumps(_monster_closed, indent=2))


def open_positions() -> dict[str, dict]:
    return dict(_monster_positions)


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


def can_open_new_position() -> bool:
    return len(_monster_positions) < get_max_concurrent()


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
    signal_source: str,  # "cluster_confirm" | "serial_deployer" | "lifecycle"
    sol_size: float,
    session: aiohttp.ClientSession,
    runtime: Any,
    metadata: dict | None = None,
) -> bool:
    """Open a new monster position. Returns True on success.

    Does NOT consult traded_mints.json — monster strategy can re-enter any mint.
    Respects its OWN concurrent-position cap only.
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
    if not can_open_new_position():
        print(f"[monster] slot pool full ({len(_monster_positions)}/{get_max_concurrent()}) — skip {token_name}")
        return False

    # Holder-guard entry check. Log-only by default (HOLDER_GUARD_ENFORCE=false);
    # when enforcing, a hard-block decision returns False here and the scout's
    # attempt to open is vetoed. TRADE-class failures land in block_reasons.
    try:
        from elizaos.plugins.solana.holder_guard import HolderGuard, config as _hg_cfg
        _guard = HolderGuard()
        _g_dec = await _guard.evaluate_entry(session, mint, scout=signal_source)
        if _g_dec.hard_block and _hg_cfg.HOLDER_GUARD_ENFORCE:
            print(f"[monster] 🛑 holder-guard veto on {token_name} ({mint[:8]}) — {'; '.join(_g_dec.block_reasons)}")
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

    # Determine pool (pump bonding curve vs pump-amm graduated)
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
                    return False
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

    # ── Hard TP at +20% → full exit ──────────────────────────────────────
    if not tp1_fired and pnl_pct >= MONSTER_TP1_GAIN_PCT:
        return "tp_hard_20pct", MONSTER_TP1_SELL_FRACTION

    # ── Stalled-winner exit: camped in [+12%, +18%] for 10min → bank ─────
    # User-requested 2026-04-26: if we're up ~+15% but the move stalls
    # without breaking through to the +20% TP, take the profit rather than
    # sit there hoping. Resets if pnl exits the band in either direction.
    if not tp1_fired:
        in_stalled_zone = (
            MONSTER_STALLED_WINNER_LOW_PCT <= pnl_pct <= MONSTER_STALLED_WINNER_HIGH_PCT
        )
        first_stalled = pos.get("first_in_stalled_winner_ts")
        if in_stalled_zone:
            if first_stalled is None:
                pos["first_in_stalled_winner_ts"] = now
            elif (now - first_stalled) >= MONSTER_STALLED_WINNER_SECS:
                return f"stalled_winner_{pnl_pct:.0f}pct_{int((now - first_stalled) / 60)}min", 1.0
        else:
            pos["first_in_stalled_winner_ts"] = None

    # ── Catastrophic floor: -25% → full exit (brains rule above this) ────
    if not tp1_fired and pnl_pct <= MONSTER_PRE_TP1_FLOOR_PCT:
        return f"pre_tp1_floor_{pnl_pct:.0f}pct", 1.0

    # ── Pre-TP flat gate: flat for 60min → full exit ────────────────────
    if not tp1_fired:
        in_flat = abs(pnl_pct) <= MONSTER_FLAT_ZONE_PCT
        first = pos.get("first_in_flat_zone_ts")
        if in_flat:
            if first is None:
                pos["first_in_flat_zone_ts"] = now
            elif (now - first) >= MONSTER_FLAT_TIMEOUT_SECS:
                return f"flat_gate_{int((now - first) / 60)}min", 1.0
        else:
            pos["first_in_flat_zone_ts"] = None

    # ── Post-TP1 moonbag state machine ────────────────────────────────────
    # The 25% bag rides out the cooldown. We track the post-TP1 running low
    # and only "arm" the brain cascade once the price recovers +50% off that
    # low (V-recovery confirming Leg 2). During DORMANT, discretionary exits
    # are suppressed — we let the bag breathe. Only safety rails are active:
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

        # Pool ladder: try configured pool first, then the other PumpPortal
        # option. Slippage ladder 50 → 70 → 90 so panic exits still land when
        # the book thins out.
        cfg_pool = pos.get("pool", "pump-amm")
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
                return True, str(sig), est_sol_out
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
        close_rec = {
            **pos,
            "mint": mint,
            "close_reason": reason,
            "close_ts": now,
            "close_price": current_price,
            "final_pnl_sol": final_pnl_sol,
            "final_pnl_pct": final_pnl_pct,
            "last_exit_pnl_pct": pnl_pct,  # preserve last-exit detail for forensics
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
    _save_state()


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

    GHOST_PURGE_TICKS = 3  # 3 ticks × 15s = 45s of zero on-chain balance → purge

    while True:
        try:
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
                                _final_sol = _locked - _spent if _had_partials else -_spent
                                _final_pct = (
                                    (_final_sol / _spent * 100.0) if _spent > 0 and _had_partials
                                    else -100.0
                                )
                                close_rec = {
                                    **pos,
                                    "mint": mint,
                                    "close_reason": (
                                        "externally_closed" if _had_partials else "ghost_purge"
                                    ),
                                    "close_ts": now,
                                    "close_price": entry_price,  # last known
                                    "final_pnl_sol": _final_sol,
                                    "final_pnl_pct": _final_pct,
                                }
                                _monster_closed.append(close_rec)
                            except Exception:
                                pass
                            del _monster_positions[mint]
                            _monster_cascades.pop(mint, None)
                            _monster_feeds.pop(mint, None)
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

                if current_price > 0:
                    pos["current_price"] = current_price
                    pnl_pct = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100
                    if pnl_pct > (pos.get("peak_pnl_pct") or 0):
                        pos["peak_pnl_pct"] = pnl_pct
                        pos["peak_price"] = current_price

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
                        cascade._mint = mint
                        cascade._token_name = pos.get("token_name", mint[:8])
                        cascade._last_pnl_pct = ((current_price / (pos["entry_price"] or 1)) - 1.0) * 100

                        decision = await cascade.evaluate(mint, pos, feed, session)
                        # ── Monster AI-exit gate ─────────────────────────────
                        # The brains rate every tick (already recorded to brain
                        # memory for dashboard visibility), but we only ACT on a
                        # SELL when the position is actually in trouble. Monster
                        # positions have a deterministic -40% hard floor and
                        # +100% TP1 — we don't want Groq/Gemini yanking a +24%
                        # runner just because 5m change dipped. AI is the
                        # emergency brake, not the primary exit.
                        if decision and decision.action == "SELL":
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
                            min_conf = 0.70
                            if (tier != "claude"
                                and peak_pnl >= 5.0
                                and cur_pnl > -10.0):
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
                                    claude_veto, claude_veto_conf = _bm_v.claude_recently_holding(mint, 600)
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

                            if ai_allowed:
                                ai_reason = f"ai_{tier}_{conf:.2f}_{gate_reason}"
                                print(f"[monster] 🧠 AI EXIT ALLOWED {pos.get('token_name', mint[:8])} "
                                      f"tier={tier} conf={conf:.2f} pnl={cur_pnl:+.1f}% peak={peak_pnl:+.1f}% "
                                      f"gate={gate_reason} why={decision.reason[:80]}")
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
