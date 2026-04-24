"""holder_guard.guard — orchestrator: public HolderGuard class.

Callers:
  - monster_signals scouts call evaluate_entry() before open_monster_position
  - strategy_e_monster's monitor loop calls evaluate_exit() on each poll

Log-only mode (HOLDER_GUARD_ENFORCE=false, the default) writes every would-be
block or exit to stdout and to rejection_tracker, but doesn't actually veto.
This lets us measure false-positive rate before trusting the guard live.
"""
from __future__ import annotations

from typing import Any

import aiohttp

from elizaos.plugins.solana import rejection_tracker as rt

from . import cex_registry
from . import config as cfg
from . import flow, hard_block, sweet_spot
from .fetcher import _rpc, build_snapshot
from .models import GuardDecision, HolderSnapshot


class HolderGuard:
    """Single entry point for entry + exit guard checks.

    Stateless except for the flow-module's in-memory top-10 history. Safe to
    instantiate once per runtime and call concurrently from multiple scout
    tasks.
    """

    async def evaluate_entry(
        self,
        session: aiohttp.ClientSession,
        mint: str,
        *,
        dex_pair: dict | None = None,
        unique_holders: int | None = None,
        holder_growth_per_min: float | None = None,
        scout: str = "",
    ) -> GuardDecision:
        """Build snapshot and run hard_block + sweet_spot.

        Returns a GuardDecision where:
          hard_block=True           → callers MUST skip entry (when enforcing)
          sweet_spot_pass=False     → token is watchlist-worthy but not ideal
          sweet_spot_pass=True      → enter with confidence

        In log-only mode (HOLDER_GUARD_ENFORCE=false), the caller should still
        respect its existing logic and only log the guard's verdict.
        """
        if not cfg.HOLDER_GUARD_ENABLED:
            return GuardDecision(mint=mint)

        snap = await build_snapshot(
            session, mint,
            dex_pair=dex_pair,
            unique_holders=unique_holders,
            holder_growth_per_min=holder_growth_per_min,
        )

        hb = hard_block.evaluate_hard_block(snap)
        if hb.hard_block:
            # Keep block_reasons on the decision; merge sweet_spot as not evaluated.
            _log_block(mint, hb, scout)
            return hb

        ss = sweet_spot.evaluate_sweet_spot(snap)
        if not ss.sweet_spot_pass:
            _log_watchlist(mint, ss, scout)
        else:
            print(
                f"[holder-guard] ✅ {mint[:8]} sweet-spot PASS "
                f"top10={snap.top10_pct}% dev={snap.dev_holding_pct}% "
                f"holders={snap.unique_holders} growth={snap.holder_growth_per_min_pct}%/min"
            )
        return ss

    async def evaluate_exit(
        self,
        session: aiohttp.ClientSession,
        mint: str,
        *,
        dex_pair: dict | None = None,
        unique_holders: int | None = None,
        holder_growth_per_min: float | None = None,
    ) -> GuardDecision:
        """Holder-flow check for an open position.

        Callers integrate this alongside the price-based exit logic:
          - If decision.should_exit → exit immediately (when enforcing)
          - If decision.hold_override → suppress price-based exits
        """
        if not cfg.HOLDER_GUARD_ENABLED:
            return GuardDecision(mint=mint)

        snap = await build_snapshot(
            session, mint,
            dex_pair=dex_pair,
            unique_holders=unique_holders,
            holder_growth_per_min=holder_growth_per_min,
        )
        d = flow.evaluate_exit(snap)

        # CEX mover check — runs alongside flow
        cex_decision = await _check_cex_mover(session, snap)
        if cex_decision and cfg.CEX_MOVE_AUTO_EXIT:
            d.should_exit = True
            d.exit_reason = cex_decision

        if d.should_exit:
            _log_exit(mint, d)
        return d

    def on_position_close(self, mint: str) -> None:
        """Purge per-mint state when a position closes."""
        flow.purge(mint)

    def is_enforcing(self) -> bool:
        return cfg.HOLDER_GUARD_ENFORCE


async def _check_cex_mover(session: aiohttp.ClientSession, snap: HolderSnapshot) -> str | None:
    """Return an exit reason string if dev wallet moved tokens to a CEX, else None.

    Checks only the dev wallet's most recent transactions. Top-holder CEX
    detection is a later enhancement (needs per-holder signature polling).
    """
    if not snap.dev_wallet:
        return None
    try:
        resp = await _rpc(session, "getSignaturesForAddress", [snap.dev_wallet, {"limit": 5}])
        sigs = resp.get("result") or []
    except Exception:
        return None

    for sig in sigs[:3]:  # only recent
        sig_str = sig.get("signature")
        if not sig_str:
            continue
        try:
            tx_resp = await _rpc(session, "getTransaction", [sig_str, {"maxSupportedTransactionVersion": 0}])
            tx = tx_resp.get("result") or {}
            msg = tx.get("transaction", {}).get("message", {})
            accounts = msg.get("accountKeys") or []
            for acc in accounts:
                addr = acc.get("pubkey") if isinstance(acc, dict) else acc
                if addr and cex_registry.is_cex_address(addr):
                    return f"dev_to_cex:{cex_registry.label(addr)}"
        except Exception:
            continue
    return None


def _log_block(mint: str, d: GuardDecision, scout: str) -> None:
    mode = "ENFORCED" if cfg.HOLDER_GUARD_ENFORCE else "LOG-ONLY"
    print(f"[holder-guard] 🛑 {mode} BLOCK {mint[:8]} — {'; '.join(d.block_reasons)}")
    try:
        rt.record(
            mint=mint,
            reason="holder_guard_hard_block",
            filter_name="holder_guard",
            filter_value="; ".join(d.block_reasons),
            strategy=scout or "holder_guard",
            extra={
                "top10_pct": d.snapshot.top10_pct if d.snapshot else None,
                "dev_pct": d.snapshot.dev_holding_pct if d.snapshot else None,
                "lp_burned_pct": d.snapshot.lp_burned_pct if d.snapshot else None,
                "mint_authority_renounced": d.snapshot.mint_authority_renounced if d.snapshot else None,
                "freeze_authority_renounced": d.snapshot.freeze_authority_renounced if d.snapshot else None,
                "enforced": cfg.HOLDER_GUARD_ENFORCE,
            },
        )
    except Exception:
        pass


def _log_watchlist(mint: str, d: GuardDecision, scout: str) -> None:
    print(f"[holder-guard] 👁️  WATCHLIST {mint[:8]} — missing: {'; '.join(d.failing_gates)}")


def _log_exit(mint: str, d: GuardDecision) -> None:
    mode = "ENFORCED" if cfg.HOLDER_GUARD_ENFORCE else "LOG-ONLY"
    print(f"[holder-guard] 🚨 {mode} HOLDER-FLOW EXIT {mint[:8]} — {d.exit_reason}")
