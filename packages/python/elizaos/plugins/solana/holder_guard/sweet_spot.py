"""holder_guard.sweet_spot — positive-profile acceptance gate.

Runs AFTER hard_block passes. Only tokens matching the observed monster
profile (AINI / SAM / LARP shape) enter consider state. Tokens that fail
any rule are watchlisted — logged but not entered.

Like hard_block, optional paid-data fields are only evaluated when present.
"""
from __future__ import annotations

from . import config as cfg
from .models import GuardDecision, HolderSnapshot


def evaluate_sweet_spot(snapshot: HolderSnapshot) -> GuardDecision:
    d = GuardDecision(mint=snapshot.mint, snapshot=snapshot)

    # Top-10 must be in the runner range
    if snapshot.top10_pct is None:
        d.failing_gates.append("top10 unknown")
    elif snapshot.top10_pct < cfg.TOP_10_MIN_PCT_SWEET:
        d.failing_gates.append(
            f"top10={snapshot.top10_pct:.1f}% < {cfg.TOP_10_MIN_PCT_SWEET}% (too distributed — liquidity proxy)"
        )
    elif snapshot.top10_pct > cfg.TOP_10_MAX_PCT_SWEET:
        d.failing_gates.append(
            f"top10={snapshot.top10_pct:.1f}% > {cfg.TOP_10_MAX_PCT_SWEET}% (too concentrated)"
        )

    # Stricter dev cap for sweet spot
    if snapshot.dev_holding_pct is not None and snapshot.dev_holding_pct > cfg.DEV_HOLDING_MAX_PCT_SWEET:
        d.failing_gates.append(
            f"dev={snapshot.dev_holding_pct:.2f}% > {cfg.DEV_HOLDING_MAX_PCT_SWEET}%"
        )

    # Authorities must be renounced (stricter than hard-block: unknown = fail)
    if not snapshot.mint_authority_renounced:
        d.failing_gates.append("mint_authority not confirmed renounced")
    if not snapshot.freeze_authority_renounced:
        d.failing_gates.append("freeze_authority not confirmed renounced")

    # LP must be burned
    if snapshot.lp_burned_pct is None or snapshot.lp_burned_pct < 99.0:
        d.failing_gates.append(
            f"lp_burned={snapshot.lp_burned_pct}% (need 100%)"
        )

    # Holder count + growth
    if snapshot.unique_holders is None or snapshot.unique_holders < cfg.MIN_UNIQUE_HOLDERS:
        d.failing_gates.append(
            f"holders={snapshot.unique_holders} < {cfg.MIN_UNIQUE_HOLDERS}"
        )
    if (
        snapshot.holder_growth_per_min_pct is None
        or snapshot.holder_growth_per_min_pct < cfg.MIN_HOLDER_GROWTH_PER_MIN_PCT
    ):
        d.failing_gates.append(
            f"holder_growth={snapshot.holder_growth_per_min_pct}%/min < "
            f"{cfg.MIN_HOLDER_GROWTH_PER_MIN_PCT}%/min"
        )

    # Buys outpace sells
    if cfg.REQUIRE_BUYS_OUTPACE_SELLS:
        if snapshot.buy_count_h1 is not None and snapshot.sell_count_h1 is not None:
            if snapshot.buy_count_h1 <= snapshot.sell_count_h1:
                d.failing_gates.append(
                    f"buys={snapshot.buy_count_h1} ≤ sells={snapshot.sell_count_h1}"
                )

    # Optional paid-data fields — stricter thresholds, still skip if None
    if snapshot.snipers_pct is not None and snapshot.snipers_pct > cfg.SNIPERS_HOLDING_MAX_PCT_SWEET:
        d.failing_gates.append(
            f"snipers={snapshot.snipers_pct:.2f}% > {cfg.SNIPERS_HOLDING_MAX_PCT_SWEET}%"
        )
    if snapshot.bundlers_pct is not None and snapshot.bundlers_pct > cfg.BUNDLERS_HOLDING_MAX_PCT_SWEET:
        d.failing_gates.append(
            f"bundlers={snapshot.bundlers_pct:.2f}% > {cfg.BUNDLERS_HOLDING_MAX_PCT_SWEET}%"
        )
    if snapshot.audit_score is not None and snapshot.audit_score < cfg.AUDIT_SCORE_MIN_SWEET:
        d.failing_gates.append(
            f"audit={snapshot.audit_score}/8 < {cfg.AUDIT_SCORE_MIN_SWEET}/8"
        )

    d.sweet_spot_pass = not d.failing_gates
    return d
