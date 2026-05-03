# Polymarket Refactor Plan

## What Was Built vs Reused

### Reused (adapted from Solana TraderBot)
| Component | Original | Adapted To |
|-----------|----------|-----------|
| `live_config.py` pattern | Solana strategy params | `config.py` — Polymarket params (edges, risk caps, brain enables) |
| `position_manager.py` pattern | Solana spot positions | `services/position_manager.py` — USDC prediction market positions |
| `dashboard_api.py` pattern | aiohttp routes | Compatible REST/WS structure |
| `jarvis_command_centre.py` pattern | Natural language commands | Reusable with new param aliases |
| `analysis_loop()` pattern | 2-hour strategy review | Compatible orchestration structure |
| `_jarvis_position_coach()` pattern | 5-second EXIT/HOLD loop | Adaptable for Polymarket position monitoring |
| `SolanaRpcClient` pattern | aiohttp reconnecting WS | All Polymarket API clients follow same pattern |
| Telegram alerts | Push + command polling | Drop-in reuse |
| `eliza_lessons.txt` | Append-only AI learning log | Compatible |
| `RiskEngine` circuit breakers | Daily loss, consecutive losses | Directly implemented |

### New Modules (Polymarket-specific)
| Module | Location | Purpose |
|--------|----------|---------|
| MarketScanner | `core/market_scanner.py` | LLM-free first-pass filter and tier classifier |
| RulesEngine | `core/rules_engine.py` | Parse resolution conditions, score clarity |
| FeatureEngine | `core/feature_engine.py` | Derive quantitative market features |
| EnsembleEngine | `core/ensemble.py` | Multi-brain output combiner with weighting |
| ClaudeDecisionEngine | `decision/claude_decision_engine.py` | Final allocator + hard blocks + penalty stack |
| MarketClient | `adapters/market_client.py` | Gamma REST API discovery |
| OrderBookClient | `adapters/orderbook_client.py` | CLOB REST API order books |
| TradingClient | `adapters/trading_client.py` | Authenticated CLOB orders (paper + live) |
| Normalizer | `adapters/normalizer.py` | All canonical typed schemas |
| GeminiAgent | `brains/gemini_agent.py` | Gemini 2.0 Flash structured analyst |
| GrokAgent | `brains/grok_agent.py` | Grok live news/sentiment brain |
| ChatGPTAgent | `brains/chatgpt_agent.py` | GPT-4o adversarial research brain |
| GroqAgent | `brains/groq_agent.py` | Groq fast scanner brain |
| HeliusAlphaAgent | `brains/helius_alpha_agent.py` | Solana on-chain intelligence filter |
| HeliusService | `services/helius_service.py` | Helius API data fetcher |
| MarketMonitorService | `services/market_monitor.py` | Background market registry + price alerts |

---

## Pipeline (v1 — Live)

```
1. MarketMonitor polls Gamma API every 60s
   → emits new_market events

2. MarketScanner.scan(markets)
   → hard-rejects: thin, wide spread, near-expiry, lottery, dead
   → scores: liquidity, tradability, catalyst, inefficiency, info flow, etc.
   → classifies: TIER1 (→ brains) | TIER2 (watchlist) | TIER3 | REJECTED

3. For each TIER1 market (max 10/cycle):
   a. Fetch order book (OrderBookClient)
   b. Derive pricing + slippage estimate
   c. RulesEngine.analyse() → clarity/ambiguity/resolution scores
   d. FeatureEngine.compute() → all quantitative features

4. Run brains concurrently (asyncio.gather):
   GeminiAgent, GrokAgent, ChatGPTAgent, GroqAgent → BrainOutput × 4
   HeliusAlphaAgent (conditional — only Solana-relevant markets)

5. EnsembleEngine.combine() → EnsembleResult
   - domain-weighted per category
   - confidence-adjusted
   - herding penalty
   - disagreement score

6. Assemble ResearchPacket (all data in one typed object)

7. ClaudeDecisionEngine.decide(packet)
   - hard blocks check
   - fair probability estimation
   - gross edge computation
   - full penalty stack
   - action selection
   - conviction + sizing
   - decision memo (Claude LLM if key available)

8. RiskEngine.check_position_allowed()
   - per-trade, per-market, per-category, total exposure caps
   - circuit breaker state check

9. TradingClient.place_limit_order() (paper or live)

10. PositionManagerService.open_position()
    → persisted to data/positions.json

11. Ongoing monitoring (every 30s):
    PositionManagerService._monitor_cycle()
    → price feed → TP/SL checks → unrealized PnL update
```

---

## What Remains to Build (Future Phases)

### Phase 2 — WebSocket Streams
- Implement `adapters/ws_client.py` for CLOB WebSocket
- Real-time order book updates (replace 60s polling with streaming)
- Price change events via WebSocket → trigger immediate re-analysis

### Phase 3 — Portfolio Module
- `portfolio/portfolio_manager.py` — full mark-to-mid + scenario stress testing
- Cross-market correlation detection
- Resolution-date clustering limits

### Phase 4 — Backtesting
- `backtesting_and_replay/` — replay stored market data
- Calibration scoring per brain
- Historical accuracy → feed back into ensemble weights

### Phase 5 — Jarvis Integration
- Wire `jarvis_command_centre.py` to Polymarket commands
- Parameter aliases for Polymarket config keys
- Natural language: "show positions", "set min edge to 5%", "pause crypto markets"

### Phase 6 — Dashboard
- Extend `dashboard_api.py` with Polymarket endpoints
- `/api/polymarket/status` — portfolio snapshot
- `/api/polymarket/decisions` — recent decision log
- `/api/polymarket/scanner` — current TIER1 candidates

### Phase 7 — Live Execution
- Complete Polygon/USDC wallet integration
- EIP-712 signing for CLOB authenticated orders
- Order fill monitoring and reconciliation

---

## Running in Paper Mode

```bash
# 1. Install dependencies (already in venv)
source .venv_py/bin/activate

# 2. Set API keys in .env
cp .env.example .env
# edit .env — at minimum: ANTHROPIC_API_KEY, one or more brain keys

# 3. Run in paper mode
python packages/python/run_polymarket.py

# Logs go to: polymarket.out + stdout
# Decisions logged to: packages/python/elizaos/plugins/polymarket/data/decision_log.jsonl
# Positions saved to: packages/python/elizaos/plugins/polymarket/data/positions.json
```

## Running Tests

```bash
python -m pytest packages/python/tests/test_polymarket.py -v
# 26 tests, ~0.4s
```
