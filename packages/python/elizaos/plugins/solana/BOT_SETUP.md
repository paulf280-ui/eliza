# TRADERBOT — COMPLETE SETUP DESCRIPTION
**Last updated: 2026-04-18 | AWS Frankfurt EC2 | Elastic IP: 63.180.211.88**

---

## INFRASTRUCTURE

### Server
- **Provider**: AWS EC2, Frankfurt (eu-central-1)
- **Elastic IP**: 63.180.211.88 (permanent — survives reboots)
- **SSH**: `ssh -i ~/.ssh/traderbot-frankfurt-key.pem ubuntu@63.180.211.88`
- **Process**: Managed by systemd (`traderbot.service`)
- **Start/Restart**: `sudo systemctl restart traderbot.service`
- **Logs**: `tail -f /home/ubuntu/eliza/packages/python/traderbot.out`
- **Dashboard**: `http://63.180.211.88:3001/`

### Dashboard
React + Vite frontend served by the Python bot on port 3001. WebSocket connection for real-time updates. Shows:
- Open positions (scanner + copy trade separately)
- Wallet balance + daily PnL
- Activity feed (all bot decisions, AI cascade outputs)
- Trade history table
- Risk / circuit breaker status
- Jarvis chat interface (AI brain conversation)

---

## ARCHITECTURE

The bot runs as a single Python process (`run_traderbot.py`) with these main loops running as async tasks:

```
run_traderbot.py
├── Dashboard HTTP server (port 3001, WebSocket)
├── copy_trade_loop()       — watches Frost/rambo/Walta wallets on Helius
├── raydium_scout_loop()    — Strategy C (PumpSwap) + Strategy D (Meteora)
├── position_manager        — monitors all open positions every 30s
│   ├── Helius real-time price polling
│   ├── AI cascade (Groq → Gemini → Claude → Opus)
│   └── SL / trailing stop / TP enforcement
├── Jarvis (axiom_jarvis.py) — conversational AI for config changes
└── learning_engine.py      — outcome recording + pattern analysis
```

---

## TRADING STRATEGIES

### Strategy C — PumpSwap Survivor Scanner
Scans DexScreener for PumpSwap-graduated tokens that have survived early sell pressure.

**Entry filters**:
- `pumpswap_min_age_secs: 7200` — token must be 2+ hours old (survivorship filter)
- `c_min_liq_usd: 20000` — minimum $20k liquidity
- `c_min_mc_usd: 50000` — minimum $50k market cap
- `c_min_buy_ratio: 50` — at least 50% buy pressure
- `c_max_vol_liq_ratio: 8.0` — cap out extreme pumps
- `c_min_momentum_score: 0.0` — momentum floor
- `c_min_score: 4` — minimum composite score

**Position sizing**: `strategy_c_buy_sol: 0.0` (falls back to `buy_sol: 0.1`)

**Exit logic**: AI cascade primary. TP ceiling at 10x (safety net only). Trailing stop 10%.

---

### Strategy D — Meteora DLMM Scanner
Scans Meteora DLMM pools for tokens showing active momentum.

**Entry filters**:
- `d_min_liq_usd: 20000` — minimum $20k liquidity
- `d_min_mc_usd: 50000` — minimum $50k market cap
- `d_min_buy_ratio: 50` — at least 50% buy pressure
- `d_max_vol_liq_ratio: 8.0`
- `d_min_momentum_score: 0.0`

**Position sizing**: `strategy_d_buy_sol: 0.6`

**Exit logic**: AI cascade primary. TP ceiling at 10x. Trailing stop 10%.

> Meteora historically outperforms PumpSwap (45.5% WR vs 32.9%) due to deeper pool depth and more sustained moves.

---

### Copy Trade — Wallet Following
Monitors on-chain transactions of watched wallets via Helius WebSocket. Copies buys immediately.

**Watched wallets (2026-04-18)**:
- **Frost** — 62.5% WR, best performer. Standard copy.
- **rambo** — 28.6% WR. Marginal but active.
- **Walta** — 14.3% WR. Carried by 1 large outlier win. Large buys (0.5+ SOL) = high conviction.

**Config**:
```
copy_trade_enabled: true
copy_trade_tp_pct: 15.0     (quant-locked from 91-trade simulation)
copy_trade_sl_pct: 10.0
copy_trade_buy_sol: 0.35
copy_trade_trail_pct: 15.0
copy_trade_consensus: 1     (one wallet signal = entry)
copy_trade_max_hold_mins: 60.0
copy_trade_daily_loss_halt_sol: 0.5
copy_trade_min_balance_halt_sol: 1.0
paper_trading: true         (copy trade is currently simulated)
```

**Exit logic**: Fixed TP at 15% + 10% SL + 15% trailing stop. NOT AI cascade (copy trade uses fixed exits — the wallet's own exits are the signal).

---

## AI BRAIN CASCADE

All scanner positions (C, D) are monitored by a 4-tier AI cascade. Each tier escalates if the previous tier can't decide.

### Tier 1 — Groq (every 30 seconds)
- **Model**: Llama 3.3 70B via Groq API (fast, free-tier)
- **Role**: Real-time momentum monitor. Fires on every tick.
- **Triggers SELL on**: Liq drain detected, holder count dropping, buy ratio collapsing, price stagnation
- **Triggers HOLD on**: Active momentum, rising holders, healthy vol/liq
- **Context**: Current price, PnL%, holder delta 5min, liq change 5min%, entry_verdict, learned patterns

### Tier 2 — Gemini (every 2 minutes)
- **Model**: Gemini 1.5 Flash via Google AI API
- **Role**: Trend analysis. Reviews 2-minute window.
- **Acts when**: Groq is uncertain or borderline signal
- **Context**: Extended price history, momentum indicators

### Tier 3 — Claude Opus 4.7 (every 5 minutes) — main depth brain
- **Model**: claude-opus-4-7 via Anthropic API
- **Role**: Quantitative meme-coin trader. Reads token makeup + accumulated trade history, makes data-driven HOLD/SELL/WATCH calls with full context.
- **Acts when**: Position age ≥ 3 min, every 5 min thereafter
- **Context**: Learned patterns, brain memory, full position context, bad_token_dna
- **Note**: ANTHROPIC_API_KEY at monthly limit until 2026-05-01 (Groq stepping in as fallback)

### Tier 4 — Claude Opus 4.7 (emergency escalation)
- **Model**: claude-opus-4-7 (same model as Tier 3, separate cadence/trigger)
- **Role**: Emergency exit decisions only (drawdown ≥ −12% or forced escalation)
- **Acts when**: Drawdown threshold hit or pending escalation flag set, min 2 min between fires

### Entry Scan (immediate on position open)
- **Model**: Groq (fast)
- **Output**: RUNNER / NORMAL / RUG_RISK verdict stored as `entry_verdict`
- **Used by**: All subsequent cascade tiers as context

### Brain Memory
Each AI brain maintains a persistent memory file:
- `groq_brain_memory.json` — Groq's accumulated context
- `gemini_brain_memory.json` — Gemini's accumulated context
- `claude_brain_memory.json` — Claude's accumulated context

All brains also receive:
- `winning_patterns.json` — 5 rules generated from outcome analysis
- `eliza_lessons.txt` — 11 on-chain verified lessons (embedded at context build time)

---

## GUARDRAILS

### Stop Loss
- **Copy trade SL**: 10% hard stop
- **Scanner early SL**: 12% (`early_stop_loss_pct`) — fires in first 5 minutes if token dumps hard
- **PumpSwap SL**: 20% (`stop_loss_pct`) — scanner positions use trailing stop before this
- **PumpSwap-specific SL**: 9% (`pumpswap_stop_loss_pct`) — tighter for pumpswap risk
- **SL execution**: Helius real-time price (NOT DexScreener — DexScreener lags 5–30s)

### Trailing Stop
- `trailing_stop_enabled: true`
- `trailing_stop_pct: 0.10` — 10% trailing from peak
- Activates once position is profitable; rides momentum while protecting gains

### Take Profit (Scanner)
- **TP ceiling**: 10x (900% gain) — safety net only; AI cascade drives actual exit
- **No fixed TP for scanner positions** — AI cascade is the primary exit mechanism

### Take Profit (Copy Trade)
- `copy_trade_tp_pct: 15.0` — fixed 15% TP (quant-locked — 91-trade simulation confirmed optimal)
- Moonbag trail: after TP1 hit, remaining position uses 33% trailing stop

### Daily Loss Circuit Breaker
- `max_daily_loss_pct: 0.30` — halt all trading if daily loss exceeds 30% of start balance
- `copy_trade_daily_loss_halt_sol: 0.5` — halt copy trading if daily loss exceeds 0.5 SOL
- `copy_trade_min_balance_halt_sol: 1.0` — halt if wallet balance drops below 1 SOL

### Position Limits
- `max_concurrent_positions: 10` — hard cap on open scanner positions
- `trenchman_max_positions: 2` — Trenchman strategy limited separately

### Stagnant Exit
Positions that show no meaningful price movement after a configurable window are closed to free capital.
- Trigger: minimal price change over 30 minutes with no AI cascade signal to hold

### Post-SL Cooldown
- `post_sl_cooldown_secs: 600` — 10 minute cooldown after a stop loss before re-entering the same token

### Ghost Position Detection
On first monitor tick after restart, `_verify_restored_positions()` checks the actual wallet for each restored position. If the wallet holds zero tokens for a non-paper position, it's written off as an orphaned ghost (avoids showing zombie positions on dashboard after crashes).

---

## ENTRY SCORING SYSTEM

Each candidate token is scored before entry. Score components include:
- Volume ($): +1 to +3 based on thresholds
- Liquidity ($): +1 to +2
- Token age: +1 for age in sweet spot
- Momentum (h1/h24 ratio): +1 for healthy momentum
- Buyer activity h1: +1 to +2
- Social presence: +2 if has socials
- Momentum score (high vol/hr): +3
- Rising price h1: +1
- Safety checks (freeze authority, mint authority, top holder concentration): +3 or disqualify
- Minimum score thresholds: `c_min_score: 4`, `strategy_e_min_score: 6`

---

## LEARNING ENGINE

`learning_engine.py` records outcomes for every closed position into `learning_outcomes.json`.

Each outcome captures:
- Token mint, name, wallet source
- Entry/exit prices, hold time
- PnL %, PnL SOL
- Peak PnL reached
- Exit reason and category (sl_loss, tp_win, rug, other)
- Whether exit was "early" (price rose 30 min after exit)
- Last AI monitor action and confidence
- was_winner, was_rug flags

On startup, `bootstrap_from_copy_trades()` seeds learning_outcomes from paper trade history so the AI has prior context immediately.

**Pattern Analysis**: Groq (or Claude when available) analyses learning_outcomes to generate `winning_patterns.json` — 5 actionable rules injected into ALL three brain prompts.

---

## BLACKLISTED WALLETS & TOKENS

### Wallet Blacklist
`copy_trade_blacklist` in axiom_copy_trader.py — wallets that are NEVER copied:
- Bundler_EeX — confirmed front-running bundler bot (15 buys in 35s, instant sell)
- Gang_0SOL — consistent loser
- Axiom_W1 — 0% WR in our data
- GdRSPexhxbQz5H2zFQrNN2BAZUqEjAULBigTPvQ6oDMP — confirmed bundler (April 10 forensics)
- Schoen, clukz, CookDoc, Trenchman — removed after data analysis

### Token Blacklist
`traded_mints.json` — permanent record of all tokens we've traded. Used as re-entry prevention for known rugs/losers. **NEVER DELETE THIS FILE.**

`cooldowns.json` — temporary cooldowns per token after SL exits.

---

## RUGCHECK INTEGRATION

All candidate tokens are checked via rugcheck.xyz API before entry:
- `rugcheck_max_score: 100000` — threshold for rejection
- Checks: freeze authority, mint authority, top holder concentration, LP locked status
- Any freeze authority = instant reject (can lock your tokens permanently)

---

## DATA FILES REFERENCE

| File | Purpose |
|------|---------|
| `bot_config.json` | Live config — Jarvis reads/writes this |
| `trade_history.json` | All scanner strategy buys and sells |
| `learning_outcomes.json` | Outcome records for AI learning (43 records) |
| `copy_trade_paper_trades.json` | Full copy trade paper simulation history (119 records) |
| `copy_trade_alerts.json` | All copy trade wallet signals received |
| `copy_trade_signal_log.json` | Detailed signal log with entry/exit decisions |
| `winning_patterns.json` | AI-generated rules from outcome analysis |
| `groq_brain_memory.json` | Groq AI persistent memory |
| `gemini_brain_memory.json` | Gemini AI persistent memory |
| `claude_brain_memory.json` | Claude AI persistent memory |
| `jarvis_conv_history.json` | Full Jarvis chat history |
| `monster_trades_reference.json` | 8 confirmed monster tokens for filter tuning |
| `monster_addresses.json` | Extended monster token address database |
| `eliza_lessons.txt` | 11 on-chain verified lessons — injected into all brains |
| `traded_mints.json` | Permanent blacklist of traded tokens |
| `cooldowns.json` | Temporary per-token cooldowns |
| `rejected_tokens.json` | Tokens scanned but rejected with reasons |
| `post_exit_tracker.json` | Tracks price after our exits (early exit detection) |

---

## JARVIS — CONVERSATIONAL CONFIG INTERFACE

Jarvis is an AI chat interface accessible via the dashboard. It can:
- Explain the current bot state and positions
- Change bot config values (writes to bot_config.json)
- Answer questions about strategy and performance
- Escalate to Groq for pattern analysis when Claude is unavailable

Jarvis receives the same lessons and patterns as the trading brains.

---

## CURRENT STATUS (2026-04-18)

```
Bot PID: 132509
Running since: 07:26:53 UTC 2026-04-18
Mode: Copy trade = PAPER | Scanner = LIVE (from today)
Watched wallets: rambo, Frost, Walta
Scanner strategies: C (PumpSwap) + D (Meteora) LIVE
Claude API: at monthly limit until 2026-05-01 (Groq fallback active)
```

### Scaling Plan
- **Phase 1** (now — ~2026-05-01): 0.35 SOL copy trade, 0.6 SOL Strategy D, scanner LIVE, 14-day data collection
- **Phase 2** (~2026-05-01): Evaluate 91-trade simulation results, scale to 1 SOL/trade if confirmed
- **Trigger for scale-up**: 14+ days of data, Strategy D showing positive expectancy, Claude API renewed

---

*File generated 2026-04-18. For raw data see ALL_TRADING_DATA.md in the same directory.*
