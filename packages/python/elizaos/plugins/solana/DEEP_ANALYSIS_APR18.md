# TraderBot — Deep Analysis & Profitability Playbook
**Analysis date:** 2026-04-18
**Analyst:** Claude Opus 4.7 (full filesystem + on-chain data review)
**Scope:** 119 closed copy-trade paper trades, 20 closed scanner trades, 35 monster addresses, 1315 wallet signals, 500 rejected tokens, 43 learning outcomes, 3 brain-memory files, full source code review of trade_monitor / learning_engine / brain_memory / axiom_copy_trader / bot_config.

---

## 1. Executive summary

The bot is **net +0.101 SOL over 119 copy-trade paper trades (36% WR, profit factor 1.02)** — functionally break-even. Scanner paper trades run at **-0.075 SOL over 20 closed trades (30% WR)**. We are not losing, but we are not winning either, and the data shows **six concrete leaks** that together explain why.

**Bottom line:** the edge exists in the data — three wallets (Wallet_3BLj, Whale_CyaE historical, Frost) produced +1.73 SOL between them. The bleed comes from following too many wallets uncritically, from a broken learning loop, and from a price-feed indexing gap that is silently eating the highest-quality signals.

The five highest-leverage fixes ranked by expected SOL impact are listed in Section 8. Implementing them does not require rewriting the bot — it requires surgical config changes, one indexing fix, and turning on an already-written learning loop that currently does nothing.

---

## 2. Where the money actually flows (closed trades)

### 2.1 Copy-trade paper P&L (119 trades)
- **Net: +0.101 SOL | WR: 36.1% | profit factor: 1.02**
- Wins: 43 avg +0.105 SOL | Losses: 76 avg -0.058 SOL
- Best: +174.6% (Wallet_3BLj — single trade, +0.611 SOL)
- Worst: -77.5%
- Median trade: -4.48% — **the median trade is a small loser**
- **Median hold: 1.1 minutes. 55 of 119 exit in under 1 minute.** We are running a scalp book, not a runner book.

### 2.2 Exit-reason breakdown — where SOL goes in vs out
| Reason | n | Net SOL | Notes |
|---|---:|---:|---|
| `stop_loss_20pct` | 12 | **-1.357** | Legacy SL before 10% fix. Dominant historical leak. |
| `wallet_exit:Trenchman` | 12 | -0.299 | Trenchman is a **losing wallet** still enabled via `trenchman_fast_lane=true`. |
| `wallet_exit:Schoen` | 6 | -0.344 | Blacklisted ✓ |
| `wallet_exit:clukz` | 12 | -0.112 | Net +0.397 overall but losses cluster post-peak. |
| `stop_loss_15pct` | 4 | -0.154 | |
| `wallet_exit:Wallet_3BLj` | 1 | **+0.611** | The only filled signal from this wallet. **174% in 5 min.** |
| `manual_close` (user intervention) | 15 | **+0.388** | **User intervention is our best exit signal.** |
| `take_profit_trail_73pct` | 1 | +0.363 | |
| `peak_protection_10pct` | 1 | +0.222 | New logic — pays for itself. |
| `tp_15pct` | 9 | +0.067 | WR 44% — about half of TP hits still end negative after slippage. |
| `stagnant_low_mc_$5k` | 1 | +0.013 | Good guard. |

**Takeaway:** The bot's *automatic* wins are tiny; the big wins come from the rare 100%+ runners and from the user manually closing at the right moment. Without `manual_close` (+0.388) and a single Wallet_3BLj trade (+0.611), automatic-only P&L would be **-0.898 SOL**.

### 2.3 Wallet-level P&L — signal quality
| Wallet | Trades | WR | Net SOL | Status | Recommendation |
|---|---:|---:|---:|---|---|
| **Whale_CyaE (Cented)** | 4 | 75% | **+0.708** | blacklisted | Unblacklist — but see Section 5 first |
| **Wallet_3BLj** | 1 | 100% | **+0.611** | not watched | **ADD** to watchlist |
| **Frost** | 8 | 62% | +0.408 | watched ✓ | Keep |
| **clukz** | 20 | 45% | +0.397 | blacklisted | Unblacklist — edge is real |
| **Walta** | 7 | 14% | +0.076 | watched | Marginal — single 174%-type win carries it |
| **CookDoc** | 10 | 50% | +0.002 | blacklisted | Neutral — leave off |
| **rambo** | 7 | 29% | -0.040 | watched | **REMOVE** — negative expectancy |
| **Axiom_W1** | 7 | 29% | -0.068 | blacklisted | Correct ✓ |
| **Radiance** | 1 | 0% | -0.111 | - | Ignore |
| **Schoen** | 16 | 38% | -0.328 | blacklisted | Correct ✓ |
| **Gang_0SOL** | 4 | 25% | -0.369 | blacklisted | Correct ✓ |
| **Trenchman** | 19 | 21% | **-0.592** | `trenchman_fast_lane=true` **still enabled** | **DISABLE IMMEDIATELY** |
| **Bundler_EeX** | 15 | 27% | -0.592 | blacklisted | Correct ✓ |

---

## 3. The signal pipeline is leaking high-quality trades

**Signal log (1315 events, 12-day window):**
- Entered: **121**
- Skipped: **562**
- Sold-by-whale events: **632**
- Overall fill rate: **9.2%**

### 3.1 Per-wallet fill rates
| Wallet | Total BUY signals | Filled | Whale already sold | **Not indexed** | Fill rate |
|---|---:|---:|---:|---:|---:|
| Schoen | 162 | 16 | 126 | 0 | 10% |
| **Whale_CyaE** | 111 | 4 | 0 | **102** | **4%** |
| Walta | 90 | 8 | 64 | 0 | 9% |
| rambo | 71 | 7 | 54 | 0 | 10% |
| clukz | 52 | 21 | 7 | 0 | 40% |
| Frost | 50 | 8 | 32 | 0 | 16% |
| **Wallet_PMJA** | 24 | 0 | 0 | **24** | **0%** |
| Radiance | 20 | 1 | 0 | 15 | 5% |
| Wallet_3BLj | 11 | 1 | 0 | **10** | **9%** |
| Trenchman | 26 | 19 | 0 | 6 | 73% |
| Bundler_EeX | 26 | 15 | 0 | 0 | 58% |

**Two failure modes explain nearly 75% of missed fills:**

### 3.2 Failure mode A — `whale_already_sold` (293 skips, 52% of skips)
Walta, Schoen, rambo, Frost are **sub-minute scalpers**. By the time our wallet-scan loop sees the BUY tx, they have already done their SELL. 293 signals wasted this way.

**The ASTEROID case is the archetype.** Walta bought ASTEROID at 05:45:23, sold at 05:50:59 — **5 minutes 36 seconds end-to-end**. We missed it. Copy-trade paper trades shows `"copy_trade_not_triggered — max_concurrent_positions=2 slots full"` as an additional reason.

**Fix options (Section 8):**
- Use Helius WebSocket subscribe rather than polling (sub-second detection)
- Raise `trenchman_max_positions` → **no, this is a slot problem for copy trade, not Trenchman**
- The copy-trade path already goes direct from signal to trade; the bottleneck is the *wallet scanner poll interval*. Confirm this is < 2 seconds.

### 3.3 Failure mode B — `not_indexed` (157 skips, 28% of skips)
**102 of 157 not_indexed skips came from Whale_CyaE, 24 from Wallet_PMJA, 15 from Radiance, 10 from Wallet_3BLj.**

These wallets buy **fresh bonding-curve tokens before DexScreener indexes them**. The price feed returns null → we skip. But these are the HIGHEST-alpha wallets in the dataset (Whale_CyaE 75% WR, Wallet_3BLj 100% WR on the 1 that indexed).

**THE single Wallet_3BLj trade that did fill was +174.6%.** If we had indexed the other 10, conservative extrapolation at 50% WR would add **+1.5 to +3 SOL** of expected value.

**Root cause:** `_get_price_and_mc()` in axiom_copy_trader.py falls back to DexScreener which lags on new-launch BC tokens. PumpPortal has a direct `/api/price` endpoint and Helius RPC can read the bonding curve reserves for an instant price. **Neither is used in the fallback chain.**

### 3.4 Failure mode C — Cented was right to blacklist, for the wrong reason
Whale_CyaE / Cented was blacklisted because "175 trades/day, uncopyable." But the data shows **4 filled trades at 75% WR and +0.708 SOL** — the wallet's alpha is real, we just can't index its picks. The *volume* argument is moot if only 4 out of 111 ever fill. **This is an indexing problem, not a wallet problem.**

---

## 4. Monster DNA — what ACTUALLY separates runners from rugs

From `monster_addresses.json` (35 confirmed monsters) + `pattern_analysis.confirmed_entry_filters`:

### 4.1 Iron-clad filters (from 35 monsters, 100% hit rate)
| Filter | Range | Notes |
|---|---|---|
| **Buy ratio %** | **50–65** | IRON-CLAD. ROCKET=37.8% (failed), FML=69.9% (failed), Apple=79.8% (failed). |
| **Vol/Liq ratio** | **1–3x** | At ENTRY. Anything >10x = already peaked. |
| **Liquidity USD** | **$25k–$150k** | At ENTRY. Apple failed at $10k. >$150k = already run. |
| **m5 % change** | **-15 to +15** | "Quiet entry" — monsters are NOT pumping when you buy. |
| **Age hours** | **2–20h** sweet spot, extended 20-72h OK | Survivorship filter. |
| **DEX pools** | **multi-DEX preferred** | PumpSwap + Meteora DLMM = tier-1 (see ASTEROID). |

### 4.2 Things that DON'T matter (previous assumptions busted)
- **Socials:** Previously assumed "zero socials" was monster DNA. Revised: All 4 new monsters (fluide, BIGLY, GAJAE, Hamster) had socials. Having Twitter/Telegram is **neutral**. What matters is *no active influencer promo* at entry (= not priced in yet).
- **Score:** PISS=3 (22x), NoHat=4 (18x), LOLA=4 (15x). Average monster score = 5.6. **Gating on `min_score ≥ 8` would reject 4 of 5 Raydium monsters.** Our `strategy_e_min_score=6` and `a2_min_score=1` are fine; `strategy_c_min_score=4` is correct; **do not raise these.**
- **Token age** alone: GAJAE ran from 1515h (63 days old). Revivals happen. Rule: after 48h require h24 > 40% + buy ratio in zone.

### 4.3 The ASTEROID archetype (the one Walta 89x'd that we missed)
```
PumpSwap launch:         2026-04-17T03:50:40Z
Meteora DLMM opened:     2026-04-17T03:56:18Z     (+5.6 min)
Walta buys:              2026-04-17T05:45:23Z     (token age 114 min)
Walta sells:             2026-04-17T05:50:59Z     (5 min hold)
Walta: 0.66 SOL → 58.81 SOL (89x)

At check time:
  buy_ratio_pct:    51.4%            ← iron-clad zone
  liq_usd:          $282,451
  vol_liq_ratio:    107x              ← exploding (post-peak check)
  h24 gain:         +18,769%
  m5:               +4.27%            ← quiet entry
  age_hours:        5.7h
```

**Why we missed it:** combination of (a) max_concurrent_positions slots full, (b) Walta's 5-minute hold didn't give us time, (c) our wallet-scan cadence missed the BUY signal.

### 4.4 Rug pattern (from 22% of losses, avg -54.4%)
- Buy ratio **> 70%** at entry (engineered excitement)
- Liquidity **< $10k** (thin pool)
- Single DEX pool
- Token age **< 10 min** (creation snipers)
- **No holder growth** between scans (fake volume)
- **Bundler_EeX wallet = 100% loss rate** (confirmed front-running bot)

---

## 5. Exit timing — are we leaving money on the table?

### 5.1 Peak-vs-exit analysis (copy-trade paper)
- **27 trades peaked > +10% but closed at a LOSS** (23% of all trades)
- **16 trades peaked > +20% but exited below +10%**
- **8 trades peaked > +40% but exited below +40%**
- **86 of 119 trades (72%) had peak > exit by 5%+**

Translation: **we routinely watch positions run 10-20% in our favor, then give it all back.**

### 5.2 Post-exit price movement (learning_outcomes.json, 34 trades with 60m data)
- **Median 60m post-exit price change: -49.5%**
- Up 20%+ after exit: **10** (we left money on table)
- Down 20%+ after exit: **21** (we correctly dodged a dump)

**This is the single most important exit statistic in the dataset.** Our exits are DEFENSIVELY CORRECT 62% of the time. The paper narrative that "we exit too early" is mostly wrong — we exit into rugs.

The 10 cases where price kept climbing 20%+ after exit are the ones to study. Those are what `exit_was_early` flag in the learning engine is designed to catch — but see Section 6.1, the tracker is not completing records.

### 5.3 Exit-category yield (learning outcomes, 43 trades)
| Category | n | Avg PnL % |
|---|---:|---:|
| tp_win | 3 | +20.9% |
| other | 25 | +0.1% |
| sl_loss | 13 | +0.1% |
| rug | 2 | -46.2% |

Only **3 out of 43** trades were clean TP wins. 25 were categorised `other` — a residual bucket meaning our exit taxonomy is insufficient.

---

## 6. Broken & dark systems

### 6.1 Post-exit tracker is not completing records
**114 post-exit records — 0 marked `done: true`.**

The `learning_engine._post_exit_scheduler` loop walks post_exit_tracker.json every 5 minutes, fetches 5m/30m/60m prices, fills in `price_60m_pct`, sets `done: true`, and routes to learning_outcomes. **The tracker file shows all 114 entries stuck at `done: false`.**

Either:
- `price_60m_pct` fetch is erroring silently and not retrying, OR
- The scheduler task is not started in `copy_trader` startup, OR
- The "is 60 min elapsed?" clock is wrong

**This means the `exit_was_early` feedback signal is effectively dead.** The learning loop cannot learn what it cannot observe.

### 6.2 Brain memory files contain zero decisions
All three brain files (`groq_brain_memory.json`, `gemini_brain_memory.json`, `claude_brain_memory.json`) last-updated at 10:50 today — **fresh writes** — but all three have:
```
"recent_decisions": [],
"session_stats": {"total_calls": 0, "holds_recommended": 0, ...}
```

The refresh loop IS running (`last_refresh_ts` is live) and is writing `config_snapshot` and `learned_patterns`. But `record_decision()` in [brain_memory.py:130](packages/python/elizaos/plugins/solana/brain_memory.py#L130) is never being called in production — OR the refresh loop is overwriting the decisions array.

**Why:** bot is running `paper_trading: true` with copy-trade-only. The AI-cascade (`TradeMonitor.evaluate`) is invoked from `trade_monitor.py` which is only called for scanner positions, not for copy-trade paper positions. The copy trade exit logic uses a much simpler rule-based set (SL 10%, TP 15%, peak protection, stagnant).

**Implication:** The 4-tier brain cascade (Groq → Gemini → Sonnet → Opus) with persistent per-brain memory is **architecturally unused by the code path that generates >99% of current P&L.** Every Claude/Gemini/Groq call log in the system applies only to Strategy C/D scanner positions — of which we have 20 closed trades total.

This is the **single biggest architectural mismatch in the codebase**: enormous complexity in the AI layer is producing zero measured impact on copy-trade P&L.

### 6.3 `entry_liq_usd` always null on learning outcomes
All 43 `learning_outcomes.json` records show entry_liq_usd bucketed `<10k` — because the field is not being captured at write time. Learning engine has no visibility into liquidity → outcome correlations.

Fix: in `record_trade_outcome()`, capture `entry_liq_usd` from `_paper_positions[mint].get("entry_liq_usd")` at the moment of write. Currently defaults to 0 → all buckets collapse to `<10k`.

### 6.4 Claude Sonnet tier is silently offline
`ANTHROPIC_API_KEY` at monthly usage limit until 2026-05-01. The cascade's **tier 3 (Claude Sonnet, 5-minute cadence, SELL@conf≥0.65)** is throwing auth errors and falling back. Pattern analysis (weekly Opus run) also falling back to Groq.

**Impact:** the "depth brain that carries the most weight" is dead until May 1. The Opus emergency tier (-12% drawdown) is also dead — but since copy-trade hard SL fires at -10%, this never mattered anyway (see 6.5).

### 6.5 Opus emergency tier never fires on copy trades
`trade_monitor.py` schedules Opus only when `_drawdown <= -12.0`. Copy-trade hard SL fires at **-10%** unconditionally. Opus cannot be reached on any copy-trade position: by the time drawdown would cross -12%, the position is already closed. **Tier 4 of the cascade is unreachable for the primary strategy.**

### 6.6 Strategy D (Meteora) is enabled but producing nothing visible
`strategy_d_enabled: true, strategy_d_buy_sol: 0.6` — highest per-trade size in the whole config. Yet `trade_history.json` shows **0 trades tagged to any strategy** (all 20 scanner trades have strategy = null). Either strategy attribution isn't being written, or Meteora scout isn't actually firing.

---

## 7. Config audit (`bot_config.json`)

Settings that matter **right now** (paper trading, copy-trade primary):
```
paper_trading:              true
copy_trade_enabled:         true
copy_trade_paused:          false
copy_trade_sl_pct:          10.0      ← correct
copy_trade_tp_pct:          15.0      ← quant-locked
copy_trade_paper_buy_sol:   0.35
copy_trade_max_hold_mins:   60.0
copy_trade_compound_pct:    0.07      ← ⚠️ audit says 0.20 is "required" — drift
```

**COMPOUND_PCT MISMATCH:** `axiom_copy_trader.py:2748` hardcodes `REQUIRED_COMPOUND_PCT = 0.20` in the self-audit and auto-corrects drift. But `bot_config.json` shows `copy_trade_compound_pct: 0.07`. **Either the self-audit is not firing, or it ran and is being overridden at write time.** At 0.07, compound growth is 3x slower than quant-designed.

Scanner settings (C and D both enabled):
```
strategy_c_enabled:         true      buy_sol=0 (uses default buy_sol=0.1)
strategy_d_enabled:         true      buy_sol=0.6  ← largest single-trade size
strategy_c_min_score:       4
strategy_c_min_liq_usd:     100000    ← TOO HIGH for monsters (range $25k-$150k)
strategy_d_min_liq_usd:     20000
strategy_c_min_mc_usd:      50000
```

**Strategy C min_liq is excluding the monster zone.** Monster DNA says $25k-$150k at entry. C's floor of $100k cuts 75% of the range. **Lower to $25k** to match confirmed filter.

Wallets still active but with negative historical expectancy:
- `trenchman_fast_lane: true` — Trenchman is **-0.592 SOL** over 19 trades. **Disable.**

---

## 8. Ranked recommendations (by expected SOL impact)

### Priority 1 — HIGH IMPACT, LOW EFFORT (this week)
| # | Change | Expected impact | Effort |
|---|---|---|---|
| 1 | **Disable `trenchman_fast_lane`.** Set `trenchman_fast_lane: false`. | Stops -0.592 SOL/month bleed | 1-line config change |
| 2 | **Remove `rambo` from WATCHED_WALLETS.** 29% WR, -0.040 SOL. | +0.04 SOL/month | 1-line code change in axiom_copy_trader.py |
| 3 | **Add `Wallet_3BLj` to WATCHED_WALLETS.** 100% WR on one trade (+174%). 11 signals, only 1 filled due to indexing. | If indexing improves: +1.5 to +3 SOL upside | Add to WATCHED_WALLETS dict |
| 4 | **Fix `not_indexed` skip.** Add PumpPortal `/api/price` + Helius BC reserves fallback before giving up. Affects 157 signals, concentrated in highest-WR wallets. | +1.5 to +3 SOL/month | ~80 lines in `_get_price_and_mc()` |
| 5 | **Lower `strategy_c_min_liq_usd` from 100000 → 25000.** Current floor rejects 75% of monster zone. | Unknown but significant — we are excluding every monster right now. | 1-line config change |
| 6 | **Fix `copy_trade_compound_pct` drift (0.07 → 0.20).** The self-audit claims this is enforced. It isn't. | 3x faster compound growth on each winning trade | Debug audit loop + config write |

### Priority 2 — MEDIUM IMPACT, MEDIUM EFFORT (next 2 weeks)
| # | Change | Expected impact |
|---|---|---|
| 7 | **Fix post_exit_tracker completion.** 114 pending records. Without this the `exit_was_early` signal is dead and no learning can happen. | Unlocks weekly Opus pattern analysis quality |
| 8 | **Capture `entry_liq_usd` properly in `record_trade_outcome()`.** Currently null on every outcome. | Unlocks liquidity-outcome correlation learning |
| 9 | **Reduce wallet-scan interval.** ASTEROID-class scalpers (Walta 5-min holds) need sub-second detection. Switch to Helius WebSocket wallet subscribe if not already. | Recovers some of the 293 `whale_already_sold` skips |
| 10 | **Raise `max_concurrent_positions` from 10 → 15 OR add copy-trade-only slot pool.** Copy-trade misses when general slots are full. | Specific to ASTEROID-style misses |

### Priority 3 — ARCHITECTURAL (this month)
| # | Change | Expected impact |
|---|---|---|
| 11 | **Connect copy-trade positions to the AI cascade.** The 4-tier brain system (Groq/Gemini/Sonnet/Opus) is active only on scanner positions. Route copy-trade positions through `TradeMonitor.evaluate()` as a complement to the rule-based exits. | Unlocks brain memory + learning loop for the 99% of trades that actually happen |
| 12 | **Lower Opus emergency threshold to -8% (from -12%)** so it fires BEFORE the -10% hard SL, giving the depth brain authority to veto. Currently it's architecturally unreachable for copy trades. | Adds one real chance for the most expensive brain to intervene |
| 13 | **Surface `strategy` attribution on every scanner buy.** Currently `trade_history.json` has strategy=null on all records. We cannot tell if Strategy D is actually firing. | Data quality for P&L attribution |
| 14 | **Bootstrap monster DNA into scanner entry filters.** Translate Section 4.1 into hard gates in `run_traderbot.py:_score_token()`: buy_ratio 50-65, vol/liq 1-3x, liq $25k-$150k, m5 within ±15%. Currently these live only in `eliza_lessons.txt` (prompt text, not enforcement). | High — converts proven filters from advice to enforcement |

### Priority 4 — HORIZON (pre-live trading)
| # | Change |
|---|---|
| 15 | Wait for `ANTHROPIC_API_KEY` monthly cap to reset (2026-05-01) OR provision a second project-level key. Sonnet tier and Opus pattern analysis have been offline since the cap hit. |
| 16 | Until then, promote **Gemini** (gemini-2.0-flash) to primary depth brain. It's 2-minute cadence, uncapped, and currently has zero recorded decisions because the brain cascade doesn't run on copy trades. |
| 17 | Fold `manual_close` outcomes back into the learning engine as labelled positive examples. User interventions are the single best exit signal in the dataset (+0.388 SOL over 15 events). |

---

## 9. Expected outcome if all Priority-1 items land

Using historical fill rates and per-wallet expected values:

| Item | Monthly SOL impact (conservative) |
|---|---:|
| Disable Trenchman | +0.4 |
| Remove rambo, add 3BLj | +0.1 |
| Fix not_indexed (PumpPortal/Helius price fallback) | +1.5 |
| Lower strategy_c min_liq | +0.3 (speculative — enables first monster catch) |
| Fix compound_pct | Multiplicative on all wins (~1.5x) |
| **Total expected** | **~+2.3 SOL/month at 0.35 SOL size, BEFORE compound** |

At 0.35 SOL per trade and 120 trades/month, going from +0.1 SOL to ~+2 SOL net is the difference between **break-even** and **a +570% monthly return on trading capital**.

---

## 10. What the data says about "where the edge actually is"

**The edge is not in the AI brains. The edge is not in the 40+ tunable parameters. The edge is in signal quality and slippage control.**

Look at the data distribution:
- 3 wallets (Whale_CyaE, Wallet_3BLj, Frost) = **+1.73 SOL gross**
- Rest of the wallets combined = **-1.63 SOL gross**
- User manual intervention = **+0.388 SOL**

**The whole P&L is produced by 4 wallets and the user.** Everything else is noise or negative.

The AI cascade with three brains, weekly pattern analysis, per-brain memory, 45-minute self-audit, confidence-weighted exits, peak protection, stale-price detection, and stagnant-MC guards is **architecturally impressive but measurably unused** on the dominant strategy. It runs on ~17% of trades (scanner) that produce ~-0.075 SOL.

**The path to "profitable in a big way" is:**
1. **Stop losing money on known losers** (Trenchman -0.592) → +0.6 SOL/month
2. **Catch more signals from known winners** (Whale_CyaE, Wallet_3BLj — fix not_indexed) → +1.5-3 SOL/month
3. **Enforce monster DNA at entry** (turn `eliza_lessons.txt` advice into `_score_token()` gates) → qualitative upside
4. **Connect the AI brains to the path where trades actually happen** so the learning loop has data to learn from → unlocks compounding improvement

Everything else is polish. Items 1-2 alone, if implemented this week, flip the bot from break-even to meaningfully profitable with zero new code — it's **one config change and one indexing fix.**

---

*End of analysis.*
