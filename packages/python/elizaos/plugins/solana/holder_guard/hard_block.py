"""holder_guard.hard_block — non-negotiable entry vetoes.

Deterministic rule layer that runs BEFORE any LLM/classifier. If any rule
fires, return GuardDecision(hard_block=True, block_reasons=[...]).

Optional fields (snipers, bundlers, audit, fresh wallets) are only checked
when their data is present — missing data is not a rejection.
"""
from __future__ import annotations

from . import config as cfg
from .models import GuardDecision, HolderSnapshot


def evaluate_hard_block(snapshot: HolderSnapshot) -> GuardDecision:
    d = GuardDecision(mint=snapshot.mint, snapshot=snapshot)

    # Top-10 concentration
    if snapshot.top10_pct is not None and snapshot.top10_pct > cfg.TOP_10_MAX_PCT:
        d.block_reasons.append(
            f"top10={snapshot.top10_pct:.1f}% > {cfg.TOP_10_MAX_PCT}%"
        )

    # Dev wallet %
    if snapshot.dev_holding_pct is not None and snapshot.dev_holding_pct > cfg.DEV_HOLDING_MAX_PCT:
        d.block_reasons.append(
            f"dev={snapshot.dev_holding_pct:.2f}% > {cfg.DEV_HOLDING_MAX_PCT}%"
        )

    # LP burn
    if cfg.REQUIRE_LP_BURNED:
        if snapshot.lp_burned_pct is not None and snapshot.lp_burned_pct < 99.0:
            d.block_reasons.append(
                f"lp_burned={snapshot.lp_burned_pct:.1f}% (need ≥99%)"
            )

    # Mint / freeze authority
    if cfg.REQUIRE_MINT_RENOUNCED and snapshot.mint_authority_renounced is False:
        d.block_reasons.append("mint_authority not renounced")
    if cfg.REQUIRE_FREEZE_RENOUNCED and snapshot.freeze_authority_renounced is False:
        d.block_reasons.append("freeze_authority not renounced")

    # Optional paid-data fields — only evaluated if present
    if snapshot.snipers_pct is not None and snapshot.snipers_pct > cfg.SNIPERS_HOLDING_MAX_PCT:
        d.block_reasons.append(
            f"snipers={snapshot.snipers_pct:.1f}% > {cfg.SNIPERS_HOLDING_MAX_PCT}%"
        )
    if snapshot.bundlers_pct is not None and snapshot.bundlers_pct > cfg.BUNDLERS_HOLDING_MAX_PCT:
        d.block_reasons.append(
            f"bundlers={snapshot.bundlers_pct:.1f}% > {cfg.BUNDLERS_HOLDING_MAX_PCT}%"
        )
    if snapshot.audit_score is not None and snapshot.audit_score < cfg.AUDIT_SCORE_MIN:
        d.block_reasons.append(
            f"audit={snapshot.audit_score}/8 < {cfg.AUDIT_SCORE_MIN}"
        )
    if snapshot.fresh_pct is not None and snapshot.fresh_pct > cfg.FRESH_WALLETS_IN_TOP_20_MAX_PCT:
        d.block_reasons.append(
            f"fresh_wallets={snapshot.fresh_pct:.1f}% > {cfg.FRESH_WALLETS_IN_TOP_20_MAX_PCT}%"
        )

    d.hard_block = bool(d.block_reasons)
    return d
