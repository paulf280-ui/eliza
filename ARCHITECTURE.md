# Polymarket Intelligence System — Architecture

## Overview

A production-grade multi-agent Polymarket trading and research system layered on top of the existing elizaOS agent runtime. Claude is the final capital allocator. All other models are advisory.

---

## Hierarchy of Authority

```
Claude (final decision maker, portfolio allocator, risk governor)
  ├── Gemini   (structured analyst, rules parser, base-rate modeler)
  ├── Grok     (news/sentiment/narrative monitor, breaking events)
  ├── ChatGPT  (adversarial researcher, thesis challenger)
  ├── Groq     (low-latency scanner, rapid reranker)
  └── Helius   (Solana on-chain intelligence filter — auxiliary, not primary)
```

---

## Data Flow

```
Market Discovery (Polymarket Gamma API)
    │
    ▼
Market Registry (MarketMonitorService)
    │  cache, status, price alerts, stream updates
    ▼
Rules Engine (RulesEngine)
    │  clarity_score, ambiguity_score, resolution_risk
    │  blocks trading if clarity < threshold
    ▼
Feature Engine (FeatureEngine)
    │  implied_prob, repricing_velocity, orderbook_imbalance,
    │  event_imminence, crowding, depth, freshness
    ▼
Brain Layer (parallel async tasks)
    ├── GeminiAgent     → BrainOutput
    ├── GrokAgent       → BrainOutput
    ├── ChatGPTAgent    → BrainOutput
    ├── GroqAgent       → BrainOutput
    └── HeliusAlphaAgent → HeliusIntelligence (only for Solana-relevant markets)
    │
    ▼
Ensemble Engine (EnsembleEngine)
    │  domain-weighted combination, disagreement detection,
    │  herding penalty, confidence weighting
    │
    ▼
Research Packet (ResearchPacket)
    │  full typed bundle: market + pricing + rules + features
    │  + all brain outputs + ensemble + helius + portfolio state
    │
    ▼
Claude Decision Engine (ClaudeDecisionEngine)
    │  hard blocks → fair probability → gross edge → penalty stack
    │  → net edge gate → action → conviction → sizing → memo
    │
    ▼
Risk Engine (RiskEngine)
    │  per-trade cap, per-category cap, total exposure cap,
    │  drawdown circuit breakers, final size computation
    │
    ▼
Trading Client (TradingClient)
    │  paper mode OR live CLOB order submission
    │
    ▼
Position Manager (PositionManagerService)
    │  tracks open positions, mark-to-mid PnL,
    │  TP/SL monitoring, JSON persistence
    └─→ JSON: data/positions.json, data/trade_history.json
```

---

## Module Map

```
packages/python/elizaos/plugins/polymarket/
├── __init__.py                     # Public API exports
├── constants.py                    # All API endpoints, addresses, thresholds
├── config.py                       # Hot-reloadable runtime config (JSON-persisted)
├── _typing.py                      # Shared type aliases
│
├── adapters/
│   ├── market_client.py            # Gamma REST API — market discovery
│   ├── orderbook_client.py         # CLOB REST API — order book pricing
│   ├── trading_client.py           # CLOB authenticated trading (paper + live)
│   └── normalizer.py               # ALL canonical typed schemas
│
├── core/
│   ├── rules_engine.py             # Parse resolution rules, score clarity
│   ├── feature_engine.py           # Derive quantitative features
│   └── ensemble.py                 # Combine brain outputs
│
├── brains/
│   ├── base.py                     # BrainAgent ABC + MarketContext + parse helpers
│   ├── helius_alpha_agent.py       # Solana on-chain intelligence filter
│   ├── gemini_agent.py             # Gemini 2.0 Flash — structured analyst
│   ├── grok_agent.py               # Grok — news/sentiment
│   ├── chatgpt_agent.py            # GPT-4o — adversarial researcher
│   └── groq_agent.py               # Groq Llama — fast scanner
│
├── decision/
│   └── claude_decision_engine.py   # Final allocator + risk governor
│
├── risk/
│   └── risk_engine.py              # Circuit breakers, caps, size computation
│
├── services/
│   ├── market_monitor.py           # Background market registry + price alerts
│   ├── position_manager.py         # Open/close positions, PnL, persistence
│   └── helius_service.py           # Helius API client (wallet flows, DEX swaps)
│
└── data/                           # Runtime JSON persistence
    ├── bot_config_polymarket.json
    ├── positions.json
    ├── trade_history.json
    └── decision_log.jsonl

packages/python/run_polymarket.py   # Main entry point + orchestration
```

---

## Key Design Decisions

### 1. Claude as final arbiter
No subordinate model can deploy capital. Every brain output is advisory.
Claude applies hard blocks, penalty stacks, and sizing — then writes a decision memo.

### 2. Helius as auxiliary only
Solana on-chain data is useful for crypto markets but irrelevant to politics, sports, etc.
The HeliusAlphaAgent explicitly returns NONE relevance for non-crypto markets and must
survive a multi-step plausibility test before recommending ACTIONABLE.

### 3. Rules first, edge second
The RulesEngine gates every market before any brain sees it.
A market with clarity_score < 0.6 is blocked regardless of apparent edge.
Resolution ambiguity is treated as a structural risk, not a minor consideration.

### 4. Conservative by default
- MIN_NET_EDGE = 3% after all penalties
- Paper mode is the default (PAPER_TRADING=true)
- SKIP and WATCH are valid and often superior outcomes
- No trade on stale data, thin books, or ambiguous rules

### 5. Hot config
All parameters live in config.py and are read on every loop iteration.
Changes take effect immediately without restart.
Jarvis (Claude) can update parameters via natural language commands.

### 6. Matching existing patterns
The Polymarket plugin mirrors the Solana plugin architecture:
- Factory function pattern (create_polymarket_plugin-compatible)
- JSON file persistence (matching Solana PM)
- live_config.py pattern for hot config
- Same logging structure
- Same aiohttp async client pattern

---

## API Keys Required

| Key | Purpose | Required? |
|-----|---------|-----------|
| ANTHROPIC_API_KEY | Claude decision engine + memos | Yes (core) |
| GEMINI_API_KEY | Gemini brain | Recommended |
| GROK_API_KEY | Grok brain + live X search | Recommended |
| OPENAI_API_KEY | ChatGPT adversarial brain | Recommended |
| GROQ_API_KEY | Fast scanner brain | Optional |
| HELIUS_API_KEY | Solana on-chain intelligence | Optional |
| POLY_API_KEY | Polymarket CLOB (authenticated) | Live mode only |
| POLY_PRIVATE_KEY | Order signing (Ethereum key) | Live mode only |

The system degrades gracefully when keys are absent:
- Missing brain = skipped in ensemble, weights redistributed
- Missing Helius = HeliusAlphaAgent returns IGNORE for all markets
- Missing POLY_API_KEY = read-only market data, no order submission

---

## Running

```bash
# Paper mode (safe default)
python packages/python/run_polymarket.py

# Live mode (requires POLY_API_KEY + POLY_PRIVATE_KEY)
PAPER_TRADING=false python packages/python/run_polymarket.py
```
