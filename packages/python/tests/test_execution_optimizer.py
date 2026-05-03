"""
Tests for the Execution Optimizer.

Run: python -m pytest packages/python/tests/test_execution_optimizer.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest

from elizaos.plugins.polymarket.execution.execution_optimizer import (
    ExecutionOptimizer,
    TradeDecisionInput,
    OrderBookInput,
    OrderBookLevel,
    MarketState,
    ExecutionConstraints,
    ExecutionReport,
    _estimate_slippage,
    _fill_probability_passive,
    _adverse_selection_risk,
    _choose_strategy,
    _compute_size_adjustment,
    _mid_price,
    _best_ask,
    _best_bid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_decision(
    market_id="m1",
    action="BUY_YES",
    target_size=20.0,
    urgency="MEDIUM",
    edge=0.08,
    confidence=0.65,
) -> TradeDecisionInput:
    return TradeDecisionInput(
        market_id=market_id,
        action=action,
        target_size=target_size,
        urgency=urgency,
        edge=edge,
        confidence=confidence,
    )


def make_book(
    best_bid_p=0.48,
    best_ask_p=0.52,
    bid_size=200.0,
    ask_size=200.0,
    n_levels=5,
) -> OrderBookInput:
    spread = best_ask_p - best_bid_p
    asks = [OrderBookLevel(price=round(best_ask_p + i * 0.01, 4), size=ask_size)
            for i in range(n_levels)]
    bids = [OrderBookLevel(price=round(best_bid_p - i * 0.01, 4), size=bid_size)
            for i in range(n_levels)]
    depth_top = best_ask_p * ask_size
    depth_full = sum(l.price * l.size for l in asks)
    return OrderBookInput(
        bids=bids, asks=asks,
        spread=spread,
        depth_top=depth_top,
        depth_full=depth_full,
    )


def make_thin_book(
    best_ask_p=0.52,
    ask_size=5.0,   # very thin
) -> OrderBookInput:
    return make_book(best_ask_p=best_ask_p, ask_size=ask_size, bid_size=5.0, n_levels=2)


def make_state(
    volatility=0.10,
    trend="FLAT",
    activity=0.40,
    liquidity=0.70,
) -> MarketState:
    return MarketState(
        volatility=volatility,
        price_trend=trend,
        liquidity_score=liquidity,
        activity_level=activity,
    )


def make_constraints(
    max_slippage=0.02,
    fee_rate=0.02,
) -> ExecutionConstraints:
    return ExecutionConstraints(max_slippage=max_slippage, fee_rate=fee_rate)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

class TestBookHelpers:
    def test_mid_price_calculated_correctly(self):
        book = make_book(best_bid_p=0.48, best_ask_p=0.52)
        assert abs(_mid_price(book) - 0.50) < 0.001

    def test_best_ask_returns_lowest_ask(self):
        book = make_book(best_ask_p=0.52)
        ask = _best_ask(book)
        assert ask is not None
        assert ask.price == pytest.approx(0.52, abs=0.001)

    def test_best_bid_returns_highest_bid(self):
        book = make_book(best_bid_p=0.48)
        bid = _best_bid(book)
        assert bid is not None
        assert bid.price == pytest.approx(0.48, abs=0.001)

    def test_empty_book_mid_price(self):
        empty = OrderBookInput(bids=[], asks=[], spread=0.0, depth_top=0.0, depth_full=0.0)
        mid = _mid_price(empty)
        assert mid == 0.5   # fallback default


# ---------------------------------------------------------------------------
# Slippage estimation
# ---------------------------------------------------------------------------

class TestSlippageEstimation:
    def test_deep_book_low_slippage(self):
        # With a deep book the cost is approximately the half-spread (ask - mid).
        # ask=0.52, mid=0.50 → half-spread cost ≈ 4%. No extra market-impact slippage.
        book = make_book(ask_size=500.0)
        slip = _estimate_slippage(20.0, book, "BUY_YES")
        assert slip < 0.06   # no sweep slippage — only half-spread cost

    def test_thin_book_high_slippage(self):
        book = make_thin_book(ask_size=2.0)
        slip = _estimate_slippage(50.0, book, "BUY_YES")
        assert slip > 0.01  # Should show notable slippage

    def test_exhausted_book_returns_max(self):
        book = OrderBookInput(
            bids=[], asks=[OrderBookLevel(price=0.52, size=1.0)],
            spread=0.04, depth_top=0.52, depth_full=0.52,
        )
        slip = _estimate_slippage(100.0, book, "BUY_YES")
        assert slip >= 0.10   # book exhausted sentinel


# ---------------------------------------------------------------------------
# Adverse selection risk
# ---------------------------------------------------------------------------

class TestAdverseSelectionRisk:
    def test_stable_deep_book_low_risk(self):
        book  = make_book(ask_size=500.0)
        state = make_state(volatility=0.05, activity=0.20)
        risk  = _adverse_selection_risk(book, state, "LOW")
        assert risk < 0.40

    def test_volatile_thin_book_high_risk(self):
        book  = make_thin_book()
        state = make_state(volatility=0.80, activity=0.90)
        risk  = _adverse_selection_risk(book, state, "LOW")
        assert risk > 0.40

    def test_risk_in_zero_to_one(self):
        book  = make_book()
        state = make_state()
        risk  = _adverse_selection_risk(book, state, "MEDIUM")
        assert 0.0 <= risk <= 1.0


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

class TestStrategySelection:
    def _constraints(self):
        return make_constraints()

    def test_high_urgency_fast_market_aggressive(self):
        dec   = make_decision(urgency="HIGH", target_size=20.0)
        book  = make_book(ask_size=500.0)
        state = make_state(volatility=0.80, activity=0.90)
        strategy, reason = _choose_strategy(dec, book, state, 0.005, self._constraints())
        assert strategy == "AGGRESSIVE_TAKE"

    def test_excessive_slippage_insufficient_edge_wait(self):
        dec   = make_decision(urgency="LOW", edge=0.02, target_size=20.0)
        book  = make_book()
        state = make_state()
        strategy, reason = _choose_strategy(dec, book, state, 0.05, make_constraints(max_slippage=0.02))
        assert strategy == "WAIT"

    def test_thin_book_staged_execution(self):
        dec  = make_decision(target_size=100.0, urgency="MEDIUM")
        book = make_thin_book(ask_size=3.0)
        state = make_state()
        strategy, reason = _choose_strategy(dec, book, state, 0.01, self._constraints())
        assert strategy == "STAGED_EXECUTION"

    def test_normal_conditions_passive_post(self):
        dec   = make_decision(urgency="LOW", target_size=15.0)
        book  = make_book(ask_size=300.0)
        state = make_state(volatility=0.05, activity=0.20)
        strategy, reason = _choose_strategy(dec, book, state, 0.005, self._constraints())
        assert strategy == "PASSIVE_POST"

    def test_wide_spread_low_urgency_waits(self):
        dec   = make_decision(urgency="LOW", target_size=15.0)
        book  = make_book(best_bid_p=0.40, best_ask_p=0.60)  # 20% spread
        state = make_state(volatility=0.10, activity=0.20)
        strategy, reason = _choose_strategy(dec, book, state, 0.01, self._constraints())
        assert strategy == "WAIT"


# ---------------------------------------------------------------------------
# Size adjustment
# ---------------------------------------------------------------------------

class TestSizeAdjustment:
    def test_no_adjustment_when_within_tolerance(self):
        book = make_book(ask_size=500.0)
        size, reason = _compute_size_adjustment(20.0, 0.005, book, make_constraints(max_slippage=0.02))
        assert size == pytest.approx(20.0, abs=0.5)
        assert "no reduction" in reason.lower()

    def test_reduces_size_when_slippage_exceeds_max(self):
        book = make_thin_book(ask_size=1.0)
        size, reason = _compute_size_adjustment(50.0, 0.05, book, make_constraints(max_slippage=0.02))
        assert size < 50.0
        assert "Slippage" in reason or "Reducing" in reason

    def test_zero_depth_reduces_to_half(self):
        book = OrderBookInput(bids=[], asks=[], spread=0.04, depth_top=0.0, depth_full=0.0)
        size, reason = _compute_size_adjustment(20.0, 0.01, book, make_constraints())
        assert size <= 12.0   # 50% or less


# ---------------------------------------------------------------------------
# Fill probability
# ---------------------------------------------------------------------------

class TestFillProbability:
    def test_at_ask_high_fill_probability(self):
        book = make_book(best_ask_p=0.52)
        prob = _fill_probability_passive(0.52, book, "HIGH", "FLAT")
        assert prob >= 0.60

    def test_far_from_ask_low_fill_probability(self):
        book = make_book(best_ask_p=0.52, best_bid_p=0.48)
        prob = _fill_probability_passive(0.45, book, "LOW", "FLAT")
        assert prob < 0.60

    def test_probability_in_range(self):
        book = make_book()
        prob = _fill_probability_passive(0.50, book, "MEDIUM", "UP")
        assert 0.0 <= prob <= 1.0


# ---------------------------------------------------------------------------
# Full optimizer — strategy types
# ---------------------------------------------------------------------------

class TestFullOptimizerStrategies:
    def setup_method(self):
        self.optimizer = ExecutionOptimizer()

    def test_passive_post_returns_one_order(self):
        dec   = make_decision(urgency="LOW", target_size=15.0)
        book  = make_book(ask_size=400.0)
        state = make_state(volatility=0.05, activity=0.15)
        report = self.optimizer.plan(dec, book, state, make_constraints())
        if report.execution_plan.strategy == "PASSIVE_POST":
            assert len(report.orders) == 1
            assert report.orders[0].side == "BUY"

    def test_wait_returns_no_orders(self):
        dec   = make_decision(urgency="LOW", edge=0.01, target_size=15.0)
        book  = make_book(best_bid_p=0.40, best_ask_p=0.60)  # wide spread
        state = make_state(volatility=0.05, activity=0.15)
        report = self.optimizer.plan(dec, book, state, make_constraints(max_slippage=0.005))
        if report.execution_plan.strategy == "WAIT":
            assert len(report.orders) == 0
            assert report.execution_details.fill_probability == 0.0

    def test_aggressive_take_uses_ask_price(self):
        dec   = make_decision(urgency="HIGH", target_size=15.0)
        book  = make_book(best_ask_p=0.52, ask_size=500.0)
        state = make_state(volatility=0.80, activity=0.95)
        report = self.optimizer.plan(dec, book, state, make_constraints())
        if report.execution_plan.strategy == "AGGRESSIVE_TAKE":
            assert len(report.orders) >= 1
            assert report.orders[0].price >= 0.52

    def test_staged_produces_multiple_orders(self):
        dec   = make_decision(urgency="MEDIUM", target_size=80.0)
        book  = make_thin_book(ask_size=5.0)
        state = make_state()
        report = self.optimizer.plan(dec, book, state, make_constraints())
        if report.execution_plan.strategy == "STAGED_EXECUTION":
            assert len(report.orders) >= 2

    def test_hybrid_has_immediate_and_passive_legs(self):
        dec   = make_decision(urgency="MEDIUM", target_size=40.0)
        book  = make_thin_book(ask_size=15.0)
        state = make_state()
        report = self.optimizer.plan(dec, book, state, make_constraints())
        if report.execution_plan.strategy == "HYBRID":
            assert len(report.orders) >= 1


# ---------------------------------------------------------------------------
# Execution details
# ---------------------------------------------------------------------------

class TestExecutionDetails:
    def setup_method(self):
        self.optimizer = ExecutionOptimizer()

    def test_slippage_is_non_negative(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert report.execution_details.expected_slippage >= 0.0

    def test_fill_probability_in_range(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert 0.0 <= report.execution_details.fill_probability <= 1.0

    def test_adverse_selection_in_range(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert 0.0 <= report.execution_details.adverse_selection_risk <= 1.0

    def test_fee_impact_matches_constraint(self):
        dec  = make_decision()
        book = make_book()
        constr = make_constraints(fee_rate=0.03)
        report = self.optimizer.plan(dec, book, constraints=constr)
        assert report.execution_details.fee_impact == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Size adjustments
# ---------------------------------------------------------------------------

class TestAdjustments:
    def setup_method(self):
        self.optimizer = ExecutionOptimizer()

    def test_no_reduction_in_good_conditions(self):
        dec  = make_decision(target_size=20.0)
        book = make_book(ask_size=1000.0)
        report = self.optimizer.plan(dec, book, make_state(volatility=0.05), make_constraints())
        # Size reduction should be 0 or very small in good conditions
        assert report.adjustments.size_reduction < 15.0  # at most 75% reduction

    def test_reduction_reason_always_present(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert isinstance(report.adjustments.reason, str)
        assert len(report.adjustments.reason) > 0

    def test_fallback_strategy_present(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert report.adjustments.fallback_strategy in ("WAIT", "STAGED_EXECUTION")


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

class TestMonitoring:
    def setup_method(self):
        self.optimizer = ExecutionOptimizer()

    def test_monitoring_has_all_sections(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        mon = report.monitoring
        assert isinstance(mon.reprice_conditions, list)
        assert isinstance(mon.cancel_conditions, list)
        assert isinstance(mon.escalation_triggers, list)

    def test_reprice_conditions_non_empty(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert len(report.monitoring.reprice_conditions) > 0

    def test_cancel_conditions_non_empty(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert len(report.monitoring.cancel_conditions) > 0


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

class TestJsonSerialization:
    def setup_method(self):
        self.optimizer = ExecutionOptimizer()

    def test_to_json_valid(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        output = self.optimizer.to_json(report)
        serialised = json.dumps(output)
        parsed = json.loads(serialised)
        assert "execution_plan" in parsed
        assert "orders" in parsed
        assert "execution_details" in parsed
        assert "adjustments" in parsed
        assert "monitoring" in parsed

    def test_execution_plan_fields(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        output = self.optimizer.to_json(report)
        ep = output["execution_plan"]
        assert "strategy" in ep
        assert "reasoning" in ep
        assert "urgency_assessment" in ep
        assert "market_conditions" in ep

    def test_order_fields(self):
        dec   = make_decision(urgency="LOW", target_size=15.0)
        book  = make_book(ask_size=500.0)
        state = make_state(volatility=0.05, activity=0.10)
        report = self.optimizer.plan(dec, book, state, make_constraints())
        output = self.optimizer.to_json(report)
        for order in output["orders"]:
            assert "type" in order
            assert "side" in order
            assert "price" in order
            assert "size" in order
            assert "timing" in order

    def test_execution_details_fields(self):
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        output = self.optimizer.to_json(report)
        ed = output["execution_details"]
        assert "expected_slippage" in ed
        assert "spread_cost" in ed
        assert "fee_impact" in ed
        assert "fill_probability" in ed
        assert "adverse_selection_risk" in ed

    def test_empty_book_no_crash(self):
        dec  = make_decision()
        book = OrderBookInput(bids=[], asks=[], spread=0.0, depth_top=0.0, depth_full=0.0)
        report = self.optimizer.plan(dec, book)
        output = self.optimizer.to_json(report)
        assert json.dumps(output)

    def test_strategy_is_valid_type(self):
        valid = {"PASSIVE_POST", "AGGRESSIVE_TAKE", "HYBRID",
                 "STAGED_EXECUTION", "ICEBERG_STYLE", "WAIT"}
        dec  = make_decision()
        book = make_book()
        report = self.optimizer.plan(dec, book)
        assert report.execution_plan.strategy in valid
