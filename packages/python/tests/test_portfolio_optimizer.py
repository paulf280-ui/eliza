"""
Tests for the Portfolio Optimizer and Capital Allocation Engine.

Run: python -m pytest packages/python/tests/test_portfolio_optimizer.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from elizaos.plugins.polymarket.portfolio.portfolio_optimizer import (
    PortfolioOptimizer,
    PortfolioState,
    PortfolioExposure,
    DrawdownState,
    CurrentPosition,
    CandidateTrade,
    RiskPolicy,
    AllocationReport,
    _allocation_score,
    _correlation_penalty,
    _liquidity_penalty,
    _uncertainty_penalty,
    _compute_base_size,
    _category_saturation,
    _suggest_reductions,
    _portfolio_risk_level,
    _time_bucket,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_state(
    total_capital=1000.0,
    available_capital=700.0,
    positions=None,
    cat_exposure=None,
    corr_exposure=None,
    drawdown_state="NORMAL",
    drawdown_pct=0.0,
) -> PortfolioState:
    return PortfolioState(
        total_capital=total_capital,
        available_capital=available_capital,
        current_positions=positions or [],
        exposure=PortfolioExposure(
            total_exposure=total_capital - available_capital,
            category_exposure=cat_exposure or {},
            correlated_exposure=corr_exposure or {},
            time_bucket_exposure={},
        ),
        drawdown=DrawdownState(
            current_drawdown_pct=drawdown_pct,
            state=drawdown_state,
        ),
    )


def make_candidate(
    market_id="m1",
    action="BUY_YES",
    edge=0.08,
    confidence=0.65,
    conviction="MEDIUM",
    risk_level=0.30,
    category="politics",
    correlation_group="us_elections",
    time_horizon="3d",
    liquidity_score=0.80,
    execution_difficulty=0.20,
) -> CandidateTrade:
    return CandidateTrade(
        market_id=market_id,
        action=action,
        edge=edge,
        confidence=confidence,
        conviction=conviction,
        risk_level=risk_level,
        category=category,
        correlation_group=correlation_group,
        time_horizon=time_horizon,
        liquidity_score=liquidity_score,
        execution_difficulty=execution_difficulty,
    )


def make_position(
    market_id="p1",
    category="politics",
    correlation_group="us_elections",
    size=30.0,
    conviction="MEDIUM",
    unrealized_pnl=0.0,
) -> CurrentPosition:
    return CurrentPosition(
        market_id=market_id,
        position_type="YES",
        size=size,
        entry_price=0.50,
        current_price=0.55,
        unrealized_pnl=unrealized_pnl,
        category=category,
        correlation_group=correlation_group,
        conviction=conviction,
    )


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

class TestAllocationScore:
    def test_high_edge_high_confidence_scores_well(self):
        t = make_candidate(edge=0.15, confidence=0.85, conviction="HIGH",
                           liquidity_score=0.90, execution_difficulty=0.10)
        score = _allocation_score(t)
        assert score > 0.50

    def test_low_edge_low_confidence_scores_low(self):
        t = make_candidate(edge=0.01, confidence=0.35, conviction="LOW",
                           liquidity_score=0.30, execution_difficulty=0.70)
        score = _allocation_score(t)
        assert score < 0.35

    def test_score_clamped_to_1(self):
        t = make_candidate(edge=1.0, confidence=1.0, conviction="EXTREME",
                           liquidity_score=1.0, execution_difficulty=0.0)
        score = _allocation_score(t)
        assert score <= 1.0

    def test_score_is_non_negative(self):
        t = make_candidate(edge=0.0, confidence=0.0, conviction="LOW",
                           liquidity_score=0.0, execution_difficulty=1.0)
        score = _allocation_score(t)
        assert score >= 0.0


class TestPenalties:
    def test_no_correlation_penalty_for_new_group(self):
        t = make_candidate(correlation_group="crypto_btc")
        positions = [make_position(correlation_group="us_elections")]
        penalty = _correlation_penalty(t, positions, [])
        assert penalty == 0.0

    def test_correlation_penalty_grows_with_exposure(self):
        t = make_candidate(correlation_group="us_elections")
        positions = [make_position(correlation_group="us_elections", size=120.0)]
        penalty = _correlation_penalty(t, positions, [])
        assert penalty >= 0.30

    def test_good_liquidity_no_penalty(self):
        t = make_candidate(liquidity_score=0.85, execution_difficulty=0.10)
        assert _liquidity_penalty(t) == 0.0

    def test_poor_liquidity_adds_penalty(self):
        t = make_candidate(liquidity_score=0.20, execution_difficulty=0.70)
        assert _liquidity_penalty(t) > 0.0

    def test_high_confidence_low_risk_no_uncertainty_penalty(self):
        t = make_candidate(confidence=0.80, risk_level=0.25)
        assert _uncertainty_penalty(t) == 0.0

    def test_low_confidence_high_risk_adds_penalty(self):
        t = make_candidate(confidence=0.40, risk_level=0.70)
        assert _uncertainty_penalty(t) > 0.0


class TestBaseSize:
    def test_high_conviction_larger_than_low(self):
        policy = RiskPolicy(max_single_position=50.0)
        t_high = make_candidate(conviction="HIGH", edge=0.10)
        t_low  = make_candidate(conviction="LOW",  edge=0.10)
        assert _compute_base_size(t_high, policy, 1000.0) > _compute_base_size(t_low, policy, 1000.0)

    def test_size_never_exceeds_cap(self):
        policy = RiskPolicy(max_single_position=50.0)
        t = make_candidate(conviction="EXTREME", edge=0.50)
        size = _compute_base_size(t, policy, 10_000.0)
        assert size <= 50.0

    def test_edge_scales_size_within_tier(self):
        policy = RiskPolicy(max_single_position=50.0)
        lo = _compute_base_size(make_candidate(conviction="MEDIUM", edge=0.03), policy, 1000.0)
        hi = _compute_base_size(make_candidate(conviction="MEDIUM", edge=0.10), policy, 1000.0)
        assert hi > lo


class TestCategorySaturation:
    def test_empty_category_no_saturation(self):
        t = make_candidate(category="crypto")
        state = make_state(cat_exposure={})
        policy = RiskPolicy()
        sat = _category_saturation(t, state.current_positions, state.exposure, policy)
        assert sat == 0.0

    def test_at_cap_returns_1(self):
        t = make_candidate(category="politics")
        policy = RiskPolicy(max_category_exposure=200.0)
        state = make_state(cat_exposure={"politics": 200.0})
        sat = _category_saturation(t, state.current_positions, state.exposure, policy)
        assert sat >= 1.0


# ---------------------------------------------------------------------------
# Time bucket
# ---------------------------------------------------------------------------

class TestTimeBucket:
    def test_hour_maps_to_24h(self):
        assert _time_bucket("6h") == "24h"

    def test_day_maps_to_1w(self):
        assert _time_bucket("2d") == "1w"

    def test_week_maps_to_1w(self):
        assert _time_bucket("2w") == "1w"

    def test_month_maps_to_1m(self):
        assert _time_bucket("30d") == "1m"

    def test_unknown_maps_to_3m_plus(self):
        assert _time_bucket("quarterly") == "3m+"


# ---------------------------------------------------------------------------
# Rebalancing suggestions
# ---------------------------------------------------------------------------

class TestSuggestReductions:
    def test_no_reductions_when_under_cap(self):
        positions = [make_position(category="politics", size=50.0)]
        exposure = PortfolioExposure(
            total_exposure=50.0,
            category_exposure={"politics": 50.0},
            correlated_exposure={},
            time_bucket_exposure={},
        )
        policy = RiskPolicy(max_category_exposure=200.0)
        reductions = _suggest_reductions(positions, exposure, policy, "NORMAL")
        cat_reductions = [r for r in reductions if "over cap" in r.reason]
        assert len(cat_reductions) == 0

    def test_category_over_cap_triggers_reduction(self):
        positions = [make_position(category="politics", size=250.0)]
        exposure = PortfolioExposure(
            total_exposure=250.0,
            category_exposure={"politics": 250.0},
            correlated_exposure={},
            time_bucket_exposure={},
        )
        policy = RiskPolicy(max_category_exposure=200.0)
        reductions = _suggest_reductions(positions, exposure, policy, "NORMAL")
        assert any("politics" in r.reason for r in reductions)

    def test_defensive_mode_closes_losing_low_conviction(self):
        positions = [
            make_position(market_id="p1", conviction="LOW", unrealized_pnl=-10.0, size=30.0),
            make_position(market_id="p2", conviction="HIGH", unrealized_pnl=5.0, size=40.0),
        ]
        exposure = PortfolioExposure(
            total_exposure=70.0,
            category_exposure={"politics": 70.0},
            correlated_exposure={},
            time_bucket_exposure={},
        )
        policy = RiskPolicy()
        reductions = _suggest_reductions(positions, exposure, policy, "DEFENSIVE")
        defensive = [r for r in reductions if "Defensive" in r.reason]
        assert any(r.market_id == "p1" for r in defensive)


# ---------------------------------------------------------------------------
# Portfolio risk level
# ---------------------------------------------------------------------------

class TestPortfolioRiskLevel:
    def test_normal_low_exposure_is_low(self):
        level = _portfolio_risk_level(100.0, 1000.0, "NORMAL")
        assert level == "LOW"

    def test_high_exposure_is_moderate(self):
        level = _portfolio_risk_level(500.0, 1000.0, "NORMAL")
        assert level == "MODERATE"

    def test_very_high_exposure_is_high(self):
        level = _portfolio_risk_level(750.0, 1000.0, "NORMAL")
        assert level == "HIGH"

    def test_defensive_state_is_high(self):
        level = _portfolio_risk_level(100.0, 1000.0, "DEFENSIVE")
        assert level == "HIGH"


# ---------------------------------------------------------------------------
# Full allocator — LOCKDOWN
# ---------------------------------------------------------------------------

class TestLockdown:
    def test_lockdown_approves_nothing(self):
        optimizer = PortfolioOptimizer()
        state = make_state(drawdown_state="LOCKDOWN", drawdown_pct=0.25)
        candidates = [make_candidate("m1"), make_candidate("m2")]
        report = optimizer.allocate(state, candidates)
        assert report.approved_trades == 0
        assert report.rejected_trades == 2

    def test_lockdown_capital_unchanged(self):
        optimizer = PortfolioOptimizer()
        state = make_state(available_capital=500.0, drawdown_state="LOCKDOWN")
        report = optimizer.allocate(state, [make_candidate()])
        assert report.capital_deployed == 0.0
        assert report.capital_remaining == 500.0

    def test_lockdown_risk_level_high(self):
        optimizer = PortfolioOptimizer()
        state = make_state(drawdown_state="LOCKDOWN")
        report = optimizer.allocate(state, [make_candidate()])
        assert report.portfolio_risk_level == "HIGH"

    def test_lockdown_suggests_rebalancing(self):
        optimizer = PortfolioOptimizer()
        positions = [make_position(conviction="LOW", unrealized_pnl=-20.0)]
        state = make_state(
            drawdown_state="LOCKDOWN",
            positions=positions,
            cat_exposure={"politics": 30.0},
        )
        report = optimizer.allocate(state, [])
        assert report.portfolio_adjustments.rebalance_needed


# ---------------------------------------------------------------------------
# Full allocator — hard gate rejections
# ---------------------------------------------------------------------------

class TestHardGateRejections:
    def setup_method(self):
        self.optimizer = PortfolioOptimizer()
        self.state = make_state()
        self.policy = RiskPolicy()

    def test_low_edge_rejected(self):
        t = make_candidate(edge=0.01)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        assert report.rejected_trades == 1
        assert "Edge" in report.rejected_trades_detail[0]["reason"]

    def test_low_confidence_rejected(self):
        t = make_candidate(confidence=0.20)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        assert report.rejected_trades == 1

    def test_low_liquidity_rejected(self):
        t = make_candidate(liquidity_score=0.10)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        assert report.rejected_trades == 1

    def test_high_execution_difficulty_rejected(self):
        t = make_candidate(execution_difficulty=0.90)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        assert report.rejected_trades == 1

    def test_category_at_cap_rejected(self):
        t = make_candidate(category="politics")
        policy = RiskPolicy(max_category_exposure=100.0)
        state = make_state(cat_exposure={"politics": 100.0})
        report = self.optimizer.allocate(state, [t], policy)
        assert report.rejected_trades == 1


# ---------------------------------------------------------------------------
# Full allocator — approved trades
# ---------------------------------------------------------------------------

class TestApprovedTrades:
    def setup_method(self):
        self.optimizer = PortfolioOptimizer()
        self.state = make_state(total_capital=1000.0, available_capital=800.0)
        self.policy = RiskPolicy(max_single_position=50.0, capital_reserve_pct=0.10)

    def test_good_candidate_gets_approved(self):
        t = make_candidate(edge=0.10, confidence=0.75, conviction="HIGH",
                           liquidity_score=0.85, execution_difficulty=0.15)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        assert report.approved_trades == 1
        assert report.trade_allocations[0].approved

    def test_approved_size_within_cap(self):
        t = make_candidate(conviction="EXTREME", edge=0.20)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        if report.approved_trades > 0:
            assert report.trade_allocations[0].size_absolute <= self.policy.max_single_position

    def test_capital_deployed_matches_allocations(self):
        candidates = [
            make_candidate("m1", edge=0.10, confidence=0.75, conviction="HIGH"),
            make_candidate("m2", edge=0.08, confidence=0.65, conviction="MEDIUM"),
        ]
        report = self.optimizer.allocate(self.state, candidates, self.policy)
        total = sum(a.size_absolute for a in report.trade_allocations if a.approved)
        assert abs(report.capital_deployed - total) < 0.01

    def test_pct_of_capital_consistent(self):
        t = make_candidate(conviction="HIGH", edge=0.12)
        report = self.optimizer.allocate(self.state, [t], self.policy)
        for alloc in report.trade_allocations:
            expected_pct = alloc.size_absolute / self.state.total_capital
            assert abs(alloc.size_pct_of_capital - expected_pct) < 0.001

    def test_candidates_ranked_by_score(self):
        """Best candidate should appear first."""
        high = make_candidate("m_high", edge=0.18, confidence=0.85, conviction="HIGH",
                              liquidity_score=0.90, execution_difficulty=0.10)
        low  = make_candidate("m_low",  edge=0.04, confidence=0.40, conviction="LOW",
                              liquidity_score=0.50, execution_difficulty=0.50)
        report = self.optimizer.allocate(self.state, [low, high], self.policy)
        if len(report.trade_allocations) >= 2:
            assert report.trade_allocations[0].allocation_score >= report.trade_allocations[1].allocation_score

    def test_capital_reserve_respected(self):
        """Never deploys more than available - reserve."""
        state = make_state(total_capital=1000.0, available_capital=800.0)
        policy = RiskPolicy(capital_reserve_pct=0.20, max_single_position=50.0)
        candidates = [make_candidate(f"m{i}", conviction="HIGH", edge=0.15) for i in range(20)]
        report = self.optimizer.allocate(state, candidates, policy)
        reserve = state.total_capital * policy.capital_reserve_pct
        deployable = state.available_capital - reserve
        assert report.capital_deployed <= deployable + 0.01  # small float tolerance


# ---------------------------------------------------------------------------
# Drawdown modes
# ---------------------------------------------------------------------------

class TestDrawdownModes:
    def setup_method(self):
        self.optimizer = PortfolioOptimizer()
        self.policy = RiskPolicy(max_single_position=50.0, capital_reserve_pct=0.10)

    def _size_for_state(self, ds: str) -> float:
        state = make_state(total_capital=1000.0, available_capital=800.0, drawdown_state=ds)
        t = make_candidate(conviction="HIGH", edge=0.12, confidence=0.75)
        report = self.optimizer.allocate(state, [t], self.policy)
        if report.approved_trades > 0:
            return report.trade_allocations[0].size_absolute
        return 0.0

    def test_caution_smaller_than_normal(self):
        normal  = self._size_for_state("NORMAL")
        caution = self._size_for_state("CAUTION")
        # Caution should result in smaller or equal size
        assert caution <= normal

    def test_defensive_smaller_than_caution(self):
        caution   = self._size_for_state("CAUTION")
        defensive = self._size_for_state("DEFENSIVE")
        assert defensive <= caution


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class TestJsonSerialization:
    def test_to_json_valid_structure(self):
        import json
        optimizer = PortfolioOptimizer()
        state = make_state()
        t = make_candidate(edge=0.10, confidence=0.70, conviction="MEDIUM")
        report = optimizer.allocate(state, [t])
        output = optimizer.to_json(report)
        serialised = json.dumps(output)
        parsed = json.loads(serialised)
        assert "allocation_summary" in parsed
        assert "trade_allocations" in parsed
        assert "rejected_trades" in parsed
        assert "portfolio_adjustments" in parsed
        assert "risk_warnings" in parsed

    def test_to_json_summary_fields(self):
        optimizer = PortfolioOptimizer()
        state = make_state()
        report = optimizer.allocate(state, [])
        output = optimizer.to_json(report)
        summary = output["allocation_summary"]
        assert "approved_trades" in summary
        assert "rejected_trades" in summary
        assert "capital_deployed" in summary
        assert "capital_remaining" in summary
        assert "portfolio_risk_level" in summary

    def test_to_json_allocation_fields(self):
        import json
        optimizer = PortfolioOptimizer()
        state = make_state()
        policy = RiskPolicy(max_single_position=50.0, capital_reserve_pct=0.10)
        t = make_candidate(edge=0.10, confidence=0.70, conviction="HIGH")
        report = optimizer.allocate(state, [t], policy)
        output = optimizer.to_json(report)
        for alloc in output["trade_allocations"]:
            assert "market_id" in alloc
            assert "action" in alloc
            assert "approved" in alloc
            assert "size" in alloc
            assert "allocation_score" in alloc
            assert "adjustments" in alloc
            assert "reasoning" in alloc

    def test_empty_report_serialises(self):
        import json
        optimizer = PortfolioOptimizer()
        state = make_state()
        report = optimizer.allocate(state, [])
        output = optimizer.to_json(report)
        assert json.dumps(output)  # no exception
        assert output["allocation_summary"]["approved_trades"] == 0


# ---------------------------------------------------------------------------
# No-trade / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_candidates_returns_empty_report(self):
        optimizer = PortfolioOptimizer()
        state = make_state()
        report = optimizer.allocate(state, [])
        assert report.approved_trades == 0
        assert report.rejected_trades == 0
        assert report.capital_deployed == 0.0

    def test_zero_available_capital_all_rejected(self):
        optimizer = PortfolioOptimizer()
        state = make_state(available_capital=0.0)
        policy = RiskPolicy(capital_reserve_pct=0.0)
        t = make_candidate(conviction="HIGH", edge=0.15, confidence=0.80)
        report = optimizer.allocate(state, [t], policy)
        assert report.approved_trades == 0

    def test_sizing_adjustments_sum_accessible(self):
        optimizer = PortfolioOptimizer()
        state = make_state()
        policy = RiskPolicy(max_single_position=50.0, capital_reserve_pct=0.10)
        t = make_candidate(edge=0.12, conviction="HIGH")
        report = optimizer.allocate(state, [t], policy)
        for alloc in report.trade_allocations:
            adj = alloc.adjustments
            total = adj.total
            assert isinstance(total, float)
            assert total >= 0.0

    def test_multiple_candidates_multiple_categories(self):
        optimizer = PortfolioOptimizer()
        state = make_state(total_capital=2000.0, available_capital=1800.0)
        policy = RiskPolicy(max_single_position=100.0, max_category_exposure=500.0,
                            capital_reserve_pct=0.10)
        candidates = [
            make_candidate("m1", category="politics",  correlation_group="us_elections", edge=0.10),
            make_candidate("m2", category="crypto",    correlation_group="btc_price",    edge=0.09),
            make_candidate("m3", category="finance",   correlation_group="fed_policy",   edge=0.08),
        ]
        report = optimizer.allocate(state, candidates, policy)
        # All should get through hard gates
        market_ids = {a.market_id for a in report.trade_allocations}
        assert len(market_ids) == len(report.trade_allocations)  # no duplicates
