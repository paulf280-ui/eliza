"""
Tests for the Polymarket Intelligence System.

Run: cd packages/python && python -m pytest tests/test_polymarket.py -v
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from elizaos.plugins.polymarket.adapters.normalizer import (
    Market, MarketPricing, OrderBook, OrderBookLevel, RulesAnalysis,
    MarketFeatures, BrainOutput, EnsembleResult, PortfolioState,
    HeliusIntelligence, HeliusSignal,
)
from elizaos.plugins.polymarket.core.rules_engine import RulesEngine
from elizaos.plugins.polymarket.core.feature_engine import FeatureEngine
from elizaos.plugins.polymarket.core.ensemble import EnsembleEngine
from elizaos.plugins.polymarket.risk.risk_engine import RiskEngine, RiskPolicy
from elizaos.plugins.polymarket.brains.base import _parse_brain_json, MarketContext


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

def make_market(
    question="Will BTC reach $100k by Dec 2025?",
    category="crypto",
    end_date=None,
    resolution_source="CoinGecko",
    resolution_description="Resolves YES if BTC price on CoinGecko exceeds $100,000.",
    description="",
) -> Market:
    return Market(
        id="test-market-1",
        event_id="evt-1",
        question=question,
        description=description,
        category=category,
        tags=["crypto", "btc"],
        end_date=end_date or (datetime.now(timezone.utc) + timedelta(days=30)),
        resolution_source=resolution_source,
        resolution_description=resolution_description,
        token_ids=["yes-token", "no-token"],
        neg_risk=False,
        active=True,
        closed=False,
        liquidity=5000.0,
        volume=1500.0,
    )


def make_pricing(yes=0.60, spread=0.04, volume=1500.0) -> MarketPricing:
    return MarketPricing(
        yes_price=yes, no_price=1.0 - yes,
        spread=spread,
        best_bid=yes - spread / 2,
        best_ask=yes + spread / 2,
        mid_price=yes,
        recent_price_change=0.02,
        volume_24h=volume,
        liquidity_score=0.75,
        slippage_estimate=0.01,
    )


def make_order_book(bid=0.58, ask=0.62) -> OrderBook:
    return OrderBook(
        token_id="yes-token",
        bids=[OrderBookLevel(bid, 100), OrderBookLevel(bid - 0.02, 200)],
        asks=[OrderBookLevel(ask, 80),  OrderBookLevel(ask + 0.02, 150)],
    )


def make_brain_output(name="test", p_yes=0.65, conf=0.70, action="BUY_YES") -> BrainOutput:
    return BrainOutput(
        brain_name=name,
        summary=f"{name} thinks YES",
        probability_yes=p_yes,
        confidence=conf,
        time_horizon="30d",
        key_evidence=["Strong on-chain accumulation", "Macro tailwinds"],
        risks=["Regulatory risk", "Market reversal"],
        resolution_concerns=[],
        suggested_action=action,
        why_now="Price is lagging news",
        invalidates_thesis=["BTC drops below $80k"],
    )


# -----------------------------------------------------------------------
# Rules Engine tests
# -----------------------------------------------------------------------

class TestRulesEngine:
    def setup_method(self):
        self.engine = RulesEngine()

    def test_clear_market_scores_high(self):
        m = make_market(
            resolution_source="CoinGecko official API",
            resolution_description="Resolves YES if BTC price exceeds $100,000 on CoinGecko on the end date.",
        )
        result = self.engine.analyse(m)
        assert result.clarity_score >= 0.5
        assert result.ambiguity_score < 0.4

    def test_ambiguous_market_scores_lower(self):
        m = make_market(
            resolution_source="community vote",
            resolution_description="Resolves YES if BTC substantially exceeds approximately $100k or similar threshold.",
        )
        result = self.engine.analyse(m)
        assert result.ambiguity_score > 0.15
        assert len(result.flags) > 0

    def test_weak_source_penalises_score(self):
        m = make_market(
            resolution_source="twitter",
            resolution_description="Resolves based on social media consensus.",
        )
        result = self.engine.analyse(m)
        assert result.source_reliability_score < 0.4
        assert any("source" in f.lower() for f in result.flags)

    def test_no_end_date_flags(self):
        m = make_market(end_date=None)
        m.end_date = None
        result = self.engine.analyse(m)
        assert any("end date" in f.lower() for f in result.flags)

    def test_past_end_date_flags(self):
        m = make_market(end_date=datetime.now(timezone.utc) - timedelta(days=1))
        result = self.engine.analyse(m)
        assert any("past" in f.lower() or "past" in result.summary.lower() for f in result.flags + [result.summary])


# -----------------------------------------------------------------------
# Feature Engine tests
# -----------------------------------------------------------------------

class TestFeatureEngine:
    def setup_method(self):
        self.engine = FeatureEngine()

    def test_basic_feature_computation(self):
        m       = make_market()
        pricing = make_pricing()
        ob      = make_order_book()
        rules   = RulesEngine().analyse(m)
        features = self.engine.compute(m, pricing, ob, rules)

        assert 0.0 <= features.implied_probability <= 1.0
        assert -1.0 <= features.orderbook_imbalance <= 1.0
        assert 0.0 <= features.depth_score <= 1.0
        assert 0.0 <= features.data_freshness_score <= 1.0
        assert features.implied_probability == pytest.approx(0.60, abs=0.05)

    def test_imminence_high_for_near_expiry(self):
        m = make_market(end_date=datetime.now(timezone.utc) + timedelta(hours=2))
        pricing = make_pricing()
        ob = make_order_book()
        rules = RulesEngine().analyse(m)
        features = self.engine.compute(m, pricing, ob, rules)
        assert features.event_imminence_score > 0.8

    def test_crowding_increases_with_volume(self):
        m = make_market()
        m.liquidity = 1000.0
        p_low  = make_pricing(volume=500.0)
        p_high = make_pricing(volume=50000.0)
        ob = make_order_book()
        rules = RulesEngine().analyse(m)
        f_low  = self.engine.compute(m, p_low,  ob, rules)
        f_high = self.engine.compute(m, p_high, ob, rules)
        assert f_high.crowding_score > f_low.crowding_score


# -----------------------------------------------------------------------
# Ensemble tests
# -----------------------------------------------------------------------

class TestEnsembleEngine:
    def setup_method(self):
        self.engine = EnsembleEngine()

    def test_basic_combination(self):
        brains = {
            "gemini":  make_brain_output("gemini",  p_yes=0.70, conf=0.75),
            "grok":    make_brain_output("grok",    p_yes=0.65, conf=0.60),
            "chatgpt": make_brain_output("chatgpt", p_yes=0.72, conf=0.80),
        }
        result = self.engine.combine(brains, "crypto")
        assert 0.60 < result.ensemble_probability_yes < 0.80
        assert 0.0 <= result.disagreement_score <= 1.0
        assert len(result.brain_weights) == 3

    def test_disagreement_detected(self):
        brains = {
            "gemini":  make_brain_output("gemini",  p_yes=0.20, conf=0.80),
            "chatgpt": make_brain_output("chatgpt", p_yes=0.80, conf=0.80),
        }
        result = self.engine.combine(brains, "politics")
        assert result.disagreement_score > 0.5

    def test_herding_penalty_applied(self):
        brains = {
            "gemini":  make_brain_output("gemini",  p_yes=0.70, conf=0.90),
            "grok":    make_brain_output("grok",    p_yes=0.71, conf=0.90),
            "chatgpt": make_brain_output("chatgpt", p_yes=0.70, conf=0.85),
            "groq":    make_brain_output("groq",    p_yes=0.70, conf=0.80),
        }
        result = self.engine.combine(brains, "crypto")
        assert result.herding_penalty > 0.0
        # Ensemble should be nudged toward 0.5 slightly
        assert result.ensemble_probability_yes <= 0.70

    def test_empty_brains_returns_neutral(self):
        result = self.engine.combine({}, "crypto")
        assert result.ensemble_probability_yes == 0.5
        assert result.ensemble_confidence == 0.0


# -----------------------------------------------------------------------
# Risk Engine tests
# -----------------------------------------------------------------------

class TestRiskEngine:
    def setup_method(self):
        self.risk = RiskEngine(RiskPolicy(
            max_position_usdc=50.0,
            max_market_exposure_usdc=100.0,
            max_category_exposure_usdc=200.0,
            max_total_exposure_usdc=300.0,
            max_daily_loss_usdc=100.0,
            max_consecutive_losses=5,
            bankroll_usdc=500.0,
        ))

    def make_portfolio(self, total=0.0, cat=None, drawdown="NORMAL") -> PortfolioState:
        return PortfolioState(
            total_exposure_usdc=total,
            category_exposure=cat or {},
            correlated_exposure_usdc=0.0,
            drawdown_state=drawdown,
            open_positions=[],
        )

    def test_circuit_breaker_not_triggered_initially(self):
        port = self.make_portfolio()
        triggers = self.risk.check_circuit_breakers(port)
        assert len(triggers) == 0

    def test_daily_loss_triggers_lockdown(self):
        self.risk.record_result(-101.0)
        port = self.make_portfolio()
        self.risk.update_drawdown_state(port)
        assert self.risk.state.drawdown_state == "LOCKDOWN"

    def test_consecutive_losses_trigger_lockdown(self):
        for _ in range(6):
            self.risk.record_result(-5.0)
        port = self.make_portfolio()
        self.risk.update_drawdown_state(port)
        assert self.risk.state.drawdown_state == "LOCKDOWN"

    def test_compute_allowed_size_respects_caps(self):
        port = self.make_portfolio(total=250.0, cat={"crypto": 180.0})
        size = self.risk.compute_allowed_size(1.0, port, "crypto")
        # Category room: 200 - 180 = 20
        # Total room: 300 - 250 = 50
        # Requested: 50 × 1.0 = 50 → capped by category room = 20
        assert size <= 20.0


# -----------------------------------------------------------------------
# Brain output parsing tests
# -----------------------------------------------------------------------

class TestBrainOutputParsing:
    def test_valid_output_parsed(self):
        raw = {
            "summary": "Strong bull case for YES",
            "probability_yes": 0.72,
            "confidence": 0.80,
            "time_horizon": "30d",
            "key_evidence": ["fact A", "fact B"],
            "risks": ["risk X"],
            "resolution_concerns": [],
            "suggested_action": "BUY_YES",
            "why_now": "Price is stale",
            "invalidates_thesis": ["BTC drops below $80k"],
        }
        out = _parse_brain_json(raw, "gemini")
        assert out.probability_yes == pytest.approx(0.72)
        assert out.suggested_action == "BUY_YES"
        assert out.brain_name == "gemini"

    def test_out_of_range_probability_clamped(self):
        raw = {"probability_yes": 1.5, "confidence": -0.1, "suggested_action": "BUY_YES"}
        out = _parse_brain_json(raw, "test")
        assert 0.0 <= out.probability_yes <= 1.0
        assert 0.0 <= out.confidence <= 1.0

    def test_invalid_action_defaults_to_skip(self):
        raw = {"probability_yes": 0.5, "confidence": 0.5, "suggested_action": "INVALID"}
        out = _parse_brain_json(raw, "test")
        assert out.suggested_action == "SKIP"


# -----------------------------------------------------------------------
# Order book tests
# -----------------------------------------------------------------------

class TestOrderBook:
    def test_mid_price(self):
        ob = make_order_book(bid=0.58, ask=0.62)
        assert ob.mid == pytest.approx(0.60, abs=0.01)

    def test_spread(self):
        ob = make_order_book(bid=0.58, ask=0.62)
        assert ob.spread == pytest.approx(0.04, abs=0.001)

    def test_imbalance_direction(self):
        # Equal depth → near zero imbalance
        ob = OrderBook(
            token_id="t",
            bids=[OrderBookLevel(0.58, 100)],
            asks=[OrderBookLevel(0.62, 100)],
        )
        assert abs(ob.imbalance()) < 0.1

    def test_depth_computation(self):
        ob = make_order_book()
        assert ob.bid_depth() > 0
        assert ob.ask_depth() > 0


# -----------------------------------------------------------------------
# Helius Agent fast-path tests
# -----------------------------------------------------------------------

class TestHeliusAlphaAgent:
    def test_irrelevant_category_fast_path(self):
        from elizaos.plugins.polymarket.brains.helius_alpha_agent import HeliusAlphaAgent

        agent = HeliusAlphaAgent(model_call=AsyncMock())
        m = make_market(question="Will Biden win the election?", category="politics")

        # Run the relevance check synchronously
        relevant, reason = agent._is_likely_relevant(m)
        assert relevant is False
        assert "irrelevant" in reason.lower() or "politics" in reason.lower()

    def test_crypto_market_is_relevant(self):
        from elizaos.plugins.polymarket.brains.helius_alpha_agent import HeliusAlphaAgent

        agent = HeliusAlphaAgent(model_call=AsyncMock())
        m = make_market(question="Will SOL reach $200 by March?", category="crypto")
        relevant, _ = agent._is_likely_relevant(m)
        assert relevant is True

    def test_no_data_returns_ignore(self):
        from elizaos.plugins.polymarket.brains.helius_alpha_agent import HeliusAlphaAgent
        import asyncio

        agent = HeliusAlphaAgent(model_call=AsyncMock())
        m = make_market(category="crypto")

        result = asyncio.get_event_loop().run_until_complete(
            agent.analyze(market=m, helius_data=None)
        )
        assert result.recommendation == "IGNORE"
        assert result.market_relevance in ("WEAK", "NONE")
