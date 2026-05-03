# TRADERBOT — ALL TRADING DATA
**Compiled: 2026-04-18 | Bot running since: 2026-03-06**

---

## OVERVIEW STATISTICS

| Module | Trades | Wins | Win Rate | Total PnL |
|--------|--------|------|----------|-----------|
| Scanner (strategy C/D — all paper) | 20 closed | 6 | 30.0% | -0.075 SOL |
| Copy Trade (43-record sample set) | 43 | 14 | 32.6% | -0.124 SOL |
| Copy Trade Paper (full 119-record set) | 119 | 43 | 36.1% | +0.101 SOL |
| **COMBINED (copy trade paper, full)** | **119+43** | — | — | — |

> **Note on scanner data**: All 20 scanner trade history records have PAPER signatures (bug fixed 2026-04-18 — paper_trading_scout was being overridden by paper_trading flag). Live scanner trading began after fix deployment on 2026-04-18. One confirmed live scanner sell at -18.1% (索拉纳生活, trailing_stop_loss).

---

## SCANNER STRATEGY TRADE HISTORY (trade_history.json)
**32 total records: 12 buys, 20 sells**

### Closed Positions (20 sells)

| # | Mode | Token | DEX | PnL % | Token Age | Exit Reason |
|---|------|-------|-----|--------|-----------|-------------|
| 1 | PAPER | zeck murris (ZECK) | pumpswap | +20.5% | 133m | trailing_stop_loss |
| 2 | PAPER | DOCATI | pumpswap | -6.4% | 124m | trailing_stop_loss |
| 3 | PAPER | Pluto the Penguin | pumpswap | +11.0% | 166m | trailing_stop_loss |
| 4 | PAPER | Freg | pumpswap | -15.9% | 225m | trailing_stop_loss |
| 5 | PAPER | Dude 44 | pumpswap | -15.5% | 160m | trailing_stop_loss |
| 6 | PAPER | Personal Elec Nico I | pumpswap | +16.3% | 234m | trailing_stop_loss |
| 7 | PAPER | Sam Altcoin | pumpswap | -19.4% | 160m | trailing_stop_loss |
| 8 | PAPER | Gaytarded | pumpswap | -16.8% | 144m | trailing_stop_loss |
| 9 | PAPER | (no name) | pump_fun | -9.9% | — | early_stop_loss |
| 10 | PAPER | (no name) | pump_fun | -0.5% | — | stagnant_exit |
| 11 | PAPER | (no name) | pump_fun | +0.2% | — | stagnant_exit |
| 12 | PAPER | (no name) | pump_fun | -0.5% | — | trailing_stop_loss |
| 13 | PAPER | (no name) | pump_fun | -3.0% | — | trailing_stop_loss |
| 14 | PAPER | (no name) | pump_fun | -6.0% | — | early_stop_loss |
| 15 | PAPER | (no name) | pump_fun | +33.6% | — | tp_full_exit |
| 16 | LIVE | (no name) | pump_fun | -1.3% | — | stagnant_exit |
| 17 | PAPER | SpaceX Mascot | pumpswap | -27.8% | 141m | stop_loss |
| 18 | PAPER | MINI MU | pumpswap | -11.5% | 156m | manual_close |
| 19 | PAPER | Doctor Jesus Trump | pumpswap | +12.6% | 149m | manual_close |
| 20 | LIVE | 索拉纳生活 | pumpswap | -18.1% | 137m | trailing_stop_loss |

### Scanner Performance by DEX

| DEX | Trades | Wins | Win Rate | PnL (SOL) |
|-----|--------|------|----------|-----------|
| pumpswap | 12 | 4 | 33.3% | -0.071 |
| pump_fun | 8 | 2 | 25.0% | -0.004 |

### Open Positions
None at time of compilation (2026-04-18 10:xx UTC).

---

## COPY TRADE — LEARNING OUTCOMES (43-record analytical set)
**Source: learning_outcomes.json — seeded from paper trade history on bot startup**

### Summary
- Total trades: 43
- Winners: 14 (32.6% WR)
- Total PnL: -0.124 SOL
- SL losses: 13 | TP wins: 3 | Rugs: 2 | Other exits: 25

### Wallet Performance

| Wallet | Trades | Wins | Win Rate | PnL (SOL) | Status |
|--------|--------|------|----------|-----------|--------|
| **Frost** | 8 | 5 | **62.5%** | **+0.408** | ACTIVE — best performer |
| **Walta** | 7 | 1 | 14.3% | +0.076 | ACTIVE — 1 large win carries it |
| CookDoc | 1 | 0 | 0% | -0.008 | Removed |
| clukz | 1 | 0 | 0% | -0.011 | Removed |
| **rambo** | 7 | 2 | 28.6% | -0.040 | ACTIVE — marginal |
| Axiom_W1 | 3 | 0 | 0% | -0.220 | Removed/blacklisted |
| Schoen | 16 | 6 | 37.5% | -0.328 | Removed |

### Key Analytical Findings from Learning Outcomes
- **Frost is the anchor**: 62.5% WR, +0.408 SOL — every other wallet is net negative
- **Walta carried by 1 big trade**: Without that outlier, Walta is a losing wallet at 14% WR
- **Axiom_W1 confirmed loss factory**: 0% WR, -0.220 SOL — blacklisted
- **Schoen borderline**: 37.5% WR but -0.328 SOL net — small wins, large losses pattern

---

## COPY TRADE — FULL PAPER TRADE HISTORY (119-record set)
**Source: copy_trade_paper_trades.json**

### Summary
- Total records: 119
- Winners: 43 (36.1% WR)
- Losers: 71
- **Total PnL: +0.101 SOL** (profitable!)

### Wallet Performance (full 119-record set)

| Wallet | Trades | Win Rate | PnL (SOL) | Status |
|--------|--------|----------|-----------|--------|
| **Whale_CyaE** | 4 | 75.0% | **+0.708** | BLACKLISTED (Cented — 175 trades/day) |
| **Wallet_3BLj** | 1 | 100% | +0.611 | Historical reference only |
| **Frost** | 8 | 62.5% | **+0.408** | ACTIVE WATCHED |
| clukz | 20 | 45.0% | +0.397 | Removed from watching |
| **Walta** | 7 | 14.3% | +0.076 | ACTIVE WATCHED |
| CookDoc | 10 | 50.0% | +0.002 | Removed |
| **rambo** | 7 | 28.6% | -0.040 | ACTIVE WATCHED |
| Axiom_W1 | 7 | 28.6% | -0.068 | BLACKLISTED |
| Radiance | 1 | 0% | -0.111 | Removed |
| Schoen | 16 | 37.5% | -0.328 | Removed |
| Gang_0SOL | 4 | 25.0% | -0.369 | BLACKLISTED |
| Trenchman | 19 | 21.1% | -0.592 | Removed |
| **Bundler_EeX** | 15 | 26.7% | **-0.592** | **BLACKLISTED — confirmed front-running bundler bot** |

### Exit Category Breakdown (from copy_trade_paper_trades data)
- TP wins (tp_full_exit, moonbag_trail): confirmed 100% profitable — zero TP exits resulted in a loss
- SL exits: avg actual exit worse than trigger (slippage on fast moves)
- Rug exits: 2 confirmed rugs in analytical set

---

## MONSTER TRADES REFERENCE
**Source: monster_trades_reference.json — 8 confirmed monster tokens for tuning filters**

| Token | Multiplier | Peak Gain | Buy Ratio | Hold Time | DEX | Liq at Entry | Notes |
|-------|-----------|-----------|-----------|-----------|-----|--------------|-------|
| ZEN | 143x | +400%+ | 57% | 8h | Raydium | $61k | Best — scored 10 |
| BURNIE | 22x | +2,214% | 52.4% | 14h | PumpSwap | $71k | MISSED |
| PISS | 22x | +297% | 55% | 6h | Raydium | $38k | Scored only 3 |
| milkers | 14x | +1,380% | 57.9% | 20h | PumpSwap | $60k | MISSED |
| LOLA | 15x | +172% | 54% | 10h | Raydium | $52k | Patient exit |
| NoHat | 18x | +311% | 55% | 8h | Raydium | $45k | Low score, quiet |
| Chicky | 3x | +207% | 63.1% | 83h | PumpSwap | $74k | MISSED |
| TARO | 6.7x | +91% | 50.5% | 189h | Raydium | $29k | 7.9 day hold |
| **SpaceX Mascot** | est 3x | +75%+ | — | — | PumpSwap | — | Reached 75%+ in our paper trade |
| **ASTEROID** | 89x (Walta) | +18,769% | — | — | PumpSwap+Meteora | — | Dual-DEX launch DNA |

### Monster DNA — Confirmed Entry Rules
1. **Buy ratio 50–65%** — not euphoric (>70% = exit liquidity already being set)
2. **Token age 60–180 min at entry** — past rug window, before most of the move
3. **Vol/liq ratio 1–15x at entry** — moderate momentum; >20x = already peaked
4. **Liquidity $20k–$300k** — real pool depth, not micro-cap
5. **Multiple DEX pools** = high conviction (PumpSwap + Meteora = strongest signal)
6. **Quiet m5 at entry** (< ±15%) — these are NOT already running when you enter
7. **Trailing stop only** — fixed TP at 30–60% kills a potential 10x–100x run

---

## BLACKLISTED WALLETS
Wallets confirmed as bots or consistently losing — never copy:

| Wallet | Reason | Evidence |
|--------|--------|---------|
| Bundler_EeX | Confirmed front-running bundler | 15 buys same token in 35 seconds, immediate full sell, peak PnL=0% on ALL 15 losses |
| Gang_0SOL | 25% WR, -0.369 SOL | Consistent loser |
| Axiom_W1 | 0% WR in our data | Loss factory |
| Schoen | -0.328 SOL net | Small wins, large losses |
| Whale_CyaE | 175 trades/day — Cented | Uncopyable speed |
| GdRSPexhxbQz5H2zFQrNN2BAZUqEjAULBigTPvQ6oDMP | Confirmed bundler bot | 15 buys in 35s on one token (2026-04-10), 8 days inactive before/after |
| clukz | Marginal at scale | Removed |
| CookDoc | Marginal | Removed |

---

## CURRENT BOT CONFIGURATION (2026-04-18)

```
paper_trading: true         (copy trade stays simulated)
paper_trading_scout: false  (scanner strategies LIVE from 2026-04-18)
strategy_b_enabled: false
strategy_c_enabled: true    (PumpSwap survivor scanner)
strategy_d_enabled: true    (Meteora DLMM scanner)
strategy_c_buy_sol: 0.0     (uses default buy_sol)
strategy_d_buy_sol: 0.6
copy_trade_enabled: true
copy_trade_tp_pct: 15.0     (quant-locked — 91-trade simulation confirmed)
copy_trade_sl_pct: 10.0
copy_trade_buy_sol: 0.35
trailing_stop_enabled: true
trailing_stop_pct: 0.10
c_min_liq_usd: 20000
d_min_liq_usd: 20000
max_concurrent_positions: 10
WATCHED_WALLETS: rambo, Frost, Walta
```

---

## KEY LESSONS LEARNED (from eliza_lessons.txt)

1. **Dual-DEX launch = tier-1 signal** — PumpSwap + Meteora DLMM within 10 minutes = institutional conviction
2. **60–150 minute sweet spot** — past rug window, before most of the move is gone
3. **Meteora pool age ≠ token age** — use TOKEN's total age, not the Meteora pool creation time
4. **Vol/liq 2–15x at entry** — the monster launchpad; below 1x = dead, above 20x = peaked
5. **Walta pattern: large buy, rapid sell ladder** — copy immediately when Walta buys 0.5+ SOL
6. **Rug fingerprints**: buy ratio >70%, liq <$10k, age <10min, single DEX, no holder growth
7. **TP=40% has 100% hit rate** — but AI cascade beats fixed TP for scanner positions
8. **Helius real-time price for SL** — DexScreener lags 5–30s; use Helius for execution
9. **Meteora WR 45.5% vs PumpSwap 32.9%** — deeper pools, more sustained moves
10. **Macro events trigger meme seasons** — lower thresholds when major political/economic news hits
11. **Scanner strategies = survivor scouts** — age floor IS the edge, not a limitation

---

*File generated 2026-04-18. Raw JSON sources: trade_history.json, learning_outcomes.json, copy_trade_paper_trades.json, monster_trades_reference.json*
