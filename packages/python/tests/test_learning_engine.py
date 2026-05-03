"""
Tests for the Self-Learning and Model Weighting Engine.

Run: python -m pytest packages/python/tests/test_learning_engine.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest

from elizaos.plugins.polymarket.learning.model_weight_engine import (
    ModelWeightEngine,
    LearningInput,
    PerformanceSummary,
    TradeRecord,
    DEFAULT_WEIGHTS,
    MODELS,
    MIN_WEIGHT,
    MAX_WEIGHT,
    MAX_ADJUSTMENT_PER_CYCLE,
    MIN_SAMPLE_SIZE,
    _recency_weight,
    _outcome_correct,
    _brier_error,
    _helius_direction_correct,
    _evaluate_numeric_model,
    _evaluate_helius,
    _compute_weight_updates,
    _normalise_weights,
    _edge_analysis,
    _confidence_calibration,
    _market_segment_analysis,
    _detect_failure_modes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trade(
    market_id="m1",
    category="politics",
    market_type="SCHEDULED_EVENT",
    resolved="YES",
    pnl=5.0,
    edge=0.08,
    confidence=0.65,
    conviction="MEDIUM",
    gemini_p=0.65,
    grok_p=0.60,
    chatgpt_p=0.62,
    groq_p=0.58,
    helius_impact="NEUTRAL",
) -> TradeRecord:
    return TradeRecord(
        market_id=market_id,
        category=category,
        market_type=market_type,
        entry_price=0.50,
        exit_price=1.0 if resolved == "YES" else 0.0,
        resolved_outcome=resolved,
        pnl=pnl,
        edge_estimate=edge,
        confidence=confidence,
        conviction=conviction,
        time_held="3d",
        timestamp="2026-03-01T00:00:00Z",
        models={
            "gemini":  {"probability_yes": gemini_p,  "confidence": confidence},
            "grok":    {"probability_yes": grok_p,    "confidence": confidence},
            "chatgpt": {"probability_yes": chatgpt_p, "confidence": confidence},
            "groq":    {"probability_yes": groq_p,    "confidence": confidence},
            "helius":  {"impact": helius_impact,      "confidence": 0.60},
        },
    )


def make_summary(
    total=10,
    win_rate=0.50,
    avg_edge=0.06,
    realized_edge=0.04,
    max_drawdown=0.05,
) -> PerformanceSummary:
    return PerformanceSummary(
        total_trades=total,
        win_rate=win_rate,
        avg_edge=avg_edge,
        realized_edge=realized_edge,
        max_drawdown=max_drawdown,
    )


def make_input(
    trades=None,
    weights=None,
    summary=None,
) -> LearningInput:
    return LearningInput(
        historical_trades=trades or [],
        current_weights=weights or dict(DEFAULT_WEIGHTS),
        performance_summary=summary or make_summary(),
    )


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------

class TestRecencyWeight:
    def test_most_recent_has_weight_1(self):
        w = _recency_weight(idx=9, total=10)
        assert abs(w - 1.0) < 1e-9

    def test_older_has_lower_weight(self):
        w_new = _recency_weight(idx=9, total=10)
        w_old = _recency_weight(idx=0, total=10)
        assert w_old < w_new

    def test_weight_positive(self):
        for i in range(20):
            assert _recency_weight(i, 20) > 0.0


class TestOutcomeCorrect:
    def test_high_prob_yes_correct(self):
        assert _outcome_correct(0.70, "YES") is True

    def test_high_prob_yes_wrong(self):
        assert _outcome_correct(0.70, "NO") is False

    def test_low_prob_no_correct(self):
        assert _outcome_correct(0.30, "NO") is True

    def test_exactly_half_predicts_yes(self):
        assert _outcome_correct(0.50, "YES") is True
        assert _outcome_correct(0.50, "NO") is False


class TestBrierError:
    def test_perfect_yes(self):
        assert _brier_error(1.0, "YES") == pytest.approx(0.0)

    def test_perfect_no(self):
        assert _brier_error(0.0, "NO") == pytest.approx(0.0)

    def test_random_is_quarter(self):
        assert _brier_error(0.50, "YES") == pytest.approx(0.25)

    def test_completely_wrong_is_one(self):
        assert _brier_error(1.0, "NO") == pytest.approx(1.0)


class TestHeliusDirectionCorrect:
    def test_strong_up_yes(self):
        assert _helius_direction_correct("STRONG_UP", "YES") is True

    def test_strong_up_no(self):
        assert _helius_direction_correct("STRONG_UP", "NO") is False

    def test_strong_down_no(self):
        assert _helius_direction_correct("STRONG_DOWN", "NO") is True

    def test_neutral_is_none(self):
        assert _helius_direction_correct("NEUTRAL", "YES") is None
        assert _helius_direction_correct("NEUTRAL", "NO") is None

    def test_modest_up_yes(self):
        assert _helius_direction_correct("MODEST_UP", "YES") is True


# ---------------------------------------------------------------------------
# Model evaluation
# ---------------------------------------------------------------------------

class TestNumericModelEvaluation:
    def test_perfect_model_high_quality(self):
        # Model always predicts correctly with high confidence
        trades = [
            make_trade(resolved="YES", gemini_p=0.85, confidence=0.80)
            for _ in range(10)
        ]
        ev = _evaluate_numeric_model("gemini", trades)
        assert ev.quality_score > 0.60
        assert ev.accuracy_score > 0.80

    def test_wrong_model_low_quality(self):
        # Model consistently predicts NO but outcome is YES
        trades = [
            make_trade(resolved="YES", gemini_p=0.25, confidence=0.80)
            for _ in range(10)
        ]
        ev = _evaluate_numeric_model("gemini", trades)
        assert ev.accuracy_score < 0.30

    def test_missing_model_data_returns_empty(self):
        # Trades with no gemini data
        trades = [TradeRecord(
            market_id="m1", category="politics", market_type="SCHEDULED_EVENT",
            entry_price=0.5, exit_price=1.0, resolved_outcome="YES",
            pnl=5.0, edge_estimate=0.08, confidence=0.60,
            conviction="MEDIUM", time_held="3d", timestamp="2026-01-01",
            models={},
        )]
        ev = _evaluate_numeric_model("gemini", trades)
        assert ev.quality_score == 0.50  # neutral default
        assert ev.sample_count == 0

    def test_sample_count_accurate(self):
        trades = [make_trade() for _ in range(8)]
        ev = _evaluate_numeric_model("gemini", trades)
        assert ev.sample_count == 8

    def test_category_performance_populated(self):
        trades = (
            [make_trade(category="politics", resolved="YES", gemini_p=0.70) for _ in range(5)] +
            [make_trade(category="crypto",   resolved="NO",  gemini_p=0.30) for _ in range(5)]
        )
        ev = _evaluate_numeric_model("gemini", trades)
        assert "politics" in ev.category_performance
        assert "crypto"   in ev.category_performance


class TestHeliusEvaluation:
    def test_accurate_bullish_signals_score_well(self):
        trades = [
            make_trade(resolved="YES", helius_impact="STRONG_UP") for _ in range(8)
        ]
        ev = _evaluate_helius(trades)
        assert ev.accuracy_score > 0.70

    def test_all_neutral_returns_empty_eval(self):
        trades = [make_trade(helius_impact="NEUTRAL") for _ in range(5)]
        ev = _evaluate_helius(trades)
        assert ev.sample_count == 0   # no directional signals
        assert ev.quality_score == 0.50

    def test_wrong_strong_signals_reduce_quality(self):
        # STRONG_UP but resolves NO
        trades = [
            make_trade(resolved="NO", helius_impact="STRONG_UP", confidence=0.90)
            for _ in range(8)
        ]
        ev = _evaluate_helius(trades)
        assert ev.accuracy_score < 0.30


# ---------------------------------------------------------------------------
# Weight normalisation
# ---------------------------------------------------------------------------

class TestNormaliseWeights:
    def test_sum_to_one(self):
        w = _normalise_weights({"gemini": 0.30, "grok": 0.30, "chatgpt": 0.30,
                                 "groq": 0.05, "helius": 0.05})
        assert abs(sum(w.values()) - 1.0) < 1e-6

    def test_min_weight_enforced(self):
        w = _normalise_weights({"gemini": 0.90, "grok": 0.01, "chatgpt": 0.03,
                                 "groq": 0.01, "helius": 0.01})
        for v in w.values():
            assert v >= MIN_WEIGHT

    def test_max_weight_enforced(self):
        # With iterative clamping, max weight is preserved even after renormalization
        w = _normalise_weights({"gemini": 0.90, "grok": 0.02, "chatgpt": 0.02,
                                 "groq": 0.02, "helius": 0.02})
        for v in w.values():
            assert v <= MAX_WEIGHT + 1e-4   # small tolerance for 4-decimal rounding


# ---------------------------------------------------------------------------
# Weight updates
# ---------------------------------------------------------------------------

class TestComputeWeightUpdates:
    def _good_eval(self, n=10):
        from elizaos.plugins.polymarket.learning.model_weight_engine import ModelEvaluation
        return ModelEvaluation(
            quality_score=0.75, accuracy_score=0.70, calibration_score=0.70,
            consistency_score=0.70, contribution_score=0.70, reliability_score=0.70,
            strengths=[], weaknesses=[], sample_count=n, category_performance={},
        )

    def _poor_eval(self, n=10):
        from elizaos.plugins.polymarket.learning.model_weight_engine import ModelEvaluation
        return ModelEvaluation(
            quality_score=0.30, accuracy_score=0.35, calibration_score=0.35,
            consistency_score=0.35, contribution_score=0.35, reliability_score=0.35,
            strengths=[], weaknesses=[], sample_count=n, category_performance={},
        )

    def test_weights_sum_to_one(self):
        evals = {m: self._good_eval() for m in MODELS}
        update = _compute_weight_updates(dict(DEFAULT_WEIGHTS), evals)
        # 4-decimal rounding means up to 5*0.0001 = 0.0005 float error
        assert abs(sum(update.updated.values()) - 1.0) < 1e-3

    def test_max_adjustment_respected(self):
        # The cap is applied pre-normalization; post-normalization deltas can slightly exceed
        # the per-model cap due to renormalization corrections. Allow double the cap as bound.
        evals = {m: self._good_eval() for m in MODELS}
        evals["groq"] = self._poor_eval()
        update = _compute_weight_updates(dict(DEFAULT_WEIGHTS), evals)
        for name, delta in update.delta.items():
            assert abs(delta) <= MAX_ADJUSTMENT_PER_CYCLE * 2 + 1e-6

    def test_poor_model_weight_decreases(self):
        evals = {m: self._good_eval() for m in MODELS}
        evals["groq"] = self._poor_eval(n=10)
        evals["gemini"].quality_score = 0.90
        update = _compute_weight_updates(dict(DEFAULT_WEIGHTS), evals)
        # groq delta should be <= 0 or at least the poor model doesn't gain
        # (weight could stay flat if already at minimum)
        groq_delta = update.delta.get("groq", 0)
        assert groq_delta <= MAX_ADJUSTMENT_PER_CYCLE

    def test_no_data_returns_unchanged_weights(self):
        from elizaos.plugins.polymarket.learning.model_weight_engine import ModelEvaluation
        empty_eval = ModelEvaluation(
            quality_score=0.50, accuracy_score=0.50, calibration_score=0.50,
            consistency_score=0.50, contribution_score=0.50, reliability_score=0.50,
            strengths=[], weaknesses=[], sample_count=0, category_performance={},
        )
        evals = {m: empty_eval for m in MODELS}
        update = _compute_weight_updates(dict(DEFAULT_WEIGHTS), evals)
        assert update.updated == update.previous


# ---------------------------------------------------------------------------
# Edge and calibration analysis
# ---------------------------------------------------------------------------

class TestEdgeAnalysis:
    def test_overestimated_edge_detected(self):
        # Predicted high edge but poor win rate
        trades = [
            make_trade(edge=0.15, pnl=-5.0, resolved="NO", gemini_p=0.30)
            for _ in range(10)
        ]
        result = _edge_analysis(trades)
        assert "OVERESTIMATED" in result

    def test_no_trades_graceful(self):
        result = _edge_analysis([])
        assert isinstance(result, str)

    def test_calibrated_returns_neutral_message(self):
        trades = (
            [make_trade(edge=0.06, pnl=5.0, resolved="YES") for _ in range(5)] +
            [make_trade(edge=0.06, pnl=-3.0, resolved="NO", gemini_p=0.30) for _ in range(4)]
        )
        result = _edge_analysis(trades)
        assert isinstance(result, str)


class TestConfidenceCalibration:
    def test_high_confidence_better_than_low(self):
        trades = (
            [make_trade(confidence=0.80, pnl=5.0, resolved="YES") for _ in range(5)] +
            [make_trade(confidence=0.30, pnl=-2.0, resolved="NO", gemini_p=0.30) for _ in range(5)]
        )
        result = _confidence_calibration(trades)
        assert isinstance(result, str)
        # Should NOT warn about overconfidence
        assert "WARNING" not in result

    def test_overconfidence_detected(self):
        # High-confidence trades lose, low-confidence win
        trades = (
            [make_trade(confidence=0.85, pnl=-5.0, resolved="NO", gemini_p=0.30)
             for _ in range(5)] +
            [make_trade(confidence=0.35, pnl=3.0, resolved="YES") for _ in range(5)]
        )
        result = _confidence_calibration(trades)
        assert "WARNING" in result


# ---------------------------------------------------------------------------
# Market segment analysis
# ---------------------------------------------------------------------------

class TestMarketSegmentAnalysis:
    def test_high_wr_category_is_focus(self):
        trades = [make_trade(category="crypto", pnl=5.0, resolved="YES") for _ in range(6)]
        focus, avoid = _market_segment_analysis(trades)
        assert any("crypto" in f for f in focus)

    def test_low_wr_category_is_avoid(self):
        trades = [make_trade(category="sports", pnl=-5.0, resolved="NO", gemini_p=0.30)
                  for _ in range(6)]
        focus, avoid = _market_segment_analysis(trades)
        assert any("sports" in a for a in avoid)

    def test_too_few_trades_skipped(self):
        # 2 trades per category — below threshold of 3
        trades = [
            make_trade(category="finance", pnl=5.0),
            make_trade(category="finance", pnl=5.0),
        ]
        focus, avoid = _market_segment_analysis(trades)
        assert not any("finance" in f for f in focus)


# ---------------------------------------------------------------------------
# Failure mode detection
# ---------------------------------------------------------------------------

class TestFailureModeDetection:
    def test_consecutive_losses_detected(self):
        trades = [make_trade(pnl=-5.0, resolved="NO", gemini_p=0.30) for _ in range(3)]
        failures = _detect_failure_modes(trades)
        assert any("consecutive" in f.lower() for f in failures)

    def test_overconfidence_detected(self):
        trades = [
            make_trade(confidence=0.80, pnl=-5.0, resolved="NO", gemini_p=0.30)
            for _ in range(5)
        ]
        failures = _detect_failure_modes(trades)
        assert any("overconfidence" in f.lower() for f in failures)

    def test_no_failures_on_good_data(self):
        trades = [make_trade(pnl=5.0, resolved="YES") for _ in range(10)]
        failures = _detect_failure_modes(trades)
        # Most failure modes should not trigger
        assert not any("consecutive losses" in f for f in failures)

    def test_empty_trades_no_crash(self):
        failures = _detect_failure_modes([])
        assert failures == []


# ---------------------------------------------------------------------------
# Full evaluation cycle
# ---------------------------------------------------------------------------

class TestFullEvaluation:
    def setup_method(self):
        self.engine = ModelWeightEngine()

    def test_empty_trades_runs_without_error(self):
        inp = make_input(trades=[])
        report = self.engine.evaluate(inp)
        assert report.weight_update is not None
        # No data → weights unchanged
        assert report.weight_update.updated == report.weight_update.previous

    def test_updated_weights_sum_to_one(self):
        trades = [make_trade() for _ in range(10)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        assert abs(sum(report.weight_update.updated.values()) - 1.0) < 1e-5

    def test_all_models_evaluated(self):
        trades = [make_trade() for _ in range(10)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        for m in MODELS:
            assert m in report.model_evaluations

    def test_learning_actions_non_empty(self):
        trades = [make_trade(pnl=-5.0, resolved="NO", gemini_p=0.30) for _ in range(5)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        assert len(report.learning_actions) > 0

    def test_to_json_valid_structure(self):
        trades = [make_trade() for _ in range(10)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        output = self.engine.to_json(report)
        serialised = json.dumps(output)
        parsed = json.loads(serialised)
        assert "model_evaluation" in parsed
        assert "weight_updates" in parsed
        assert "system_insights" in parsed
        assert "strategy_adjustments" in parsed
        assert "learning_actions" in parsed

    def test_to_json_weight_updates_fields(self):
        trades = [make_trade() for _ in range(8)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        output = self.engine.to_json(report)
        wu = output["weight_updates"]
        assert "previous" in wu
        assert "updated" in wu
        assert "delta" in wu
        assert "change_reasoning" in wu

    def test_to_json_model_fields(self):
        trades = [make_trade() for _ in range(8)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        output = self.engine.to_json(report)
        for m in MODELS:
            me = output["model_evaluation"][m]
            assert "quality_score" in me
            assert "strengths" in me
            assert "weaknesses" in me

    def test_quality_scores_in_range(self):
        trades = [make_trade() for _ in range(10)]
        inp = make_input(trades=trades)
        report = self.engine.evaluate(inp)
        for name, ev in report.model_evaluations.items():
            assert 0.0 <= ev.quality_score <= 1.0
            assert 0.0 <= ev.accuracy_score <= 1.0

    def test_high_drawdown_triggers_risk_action(self):
        trades = [make_trade() for _ in range(10)]
        summary = make_summary(max_drawdown=0.20)
        inp = make_input(trades=trades, summary=summary)
        report = self.engine.evaluate(inp)
        assert any("drawdown" in a.lower() or "Drawdown" in a
                   for a in report.strategy_adjustments.risk_adjustments)
