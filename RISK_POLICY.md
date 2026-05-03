# Risk Policy — Polymarket Intelligence System

## Governing Principle

**Claude is the only component authorized to deploy capital.**
All subordinate models are advisory. Risk controls operate at two levels:
1. Claude's internal decision logic (penalty stack, hard blocks)
2. RiskEngine external enforcement (portfolio caps, circuit breakers)

A trade must pass BOTH to execute.

---

## Hard Blocks (Immediate SKIP)

These trigger before any edge computation:

| Block | Threshold | Reason |
|-------|-----------|--------|
| CLARITY_GATE | clarity_score < 0.60 | Rules too ambiguous to trade |
| AMBIGUITY_GATE | ambiguity_score > 0.35 | Resolution dispute risk too high |
| STALE_DATA | data_freshness < 0.50 | Order book or pricing data is stale |
| SPREAD_TOO_WIDE | spread > 0.12 | Market not tradable at reasonable cost |
| SLIPPAGE_TOO_HIGH | slippage > 5% | Execution cost exceeds acceptable limit |
| CIRCUIT_BREAKER | drawdown_state = LOCKDOWN | Daily loss or consecutive loss limit hit |
| THIN_BOOK | total visible depth < $50 | Cannot express trade safely |
| RESOLUTION_RISK | rules flags include serious danger | Explicit dispute/oracle red flag |

---

## Penalty Stack

Every trade has these deductions applied to gross edge:

| Penalty | Calculation | Notes |
|---------|-------------|-------|
| Fee | 2.0% taker / 0% maker | Polymarket standard |
| Spread | spread × 0.5 | Half-spread paid on entry |
| Slippage | slippage_estimate | From order book simulation |
| Ambiguity | ambiguity_score × 4% | Up to 4% for max ambiguity |
| Resolution risk | resolution_risk × 3% | Oracle/dispute risk deduction |
| Disagreement | disagreement_score × 4% | Penalise conflicting brain signals |
| Herding | ensemble.herding_penalty | Penalise fake consensus |
| Crowding | crowding_score × 2% | Crowded markets = less edge |
| Freshness | (1 - freshness) × 2% | Penalise stale data |

**Minimum net edge required**: 3% (configurable via `min_net_edge` in config)

---

## Position Limits

| Limit | Default | Config Key |
|-------|---------|-----------|
| Max per position | $50 USDC | max_position_usdc |
| Max per market | $100 USDC | max_market_exposure_usdc |
| Max per category | $250 USDC | max_category_exposure_usdc |
| Max total exposure | $500 USDC | max_total_exposure_usdc |
| Max concurrent positions | 5 | max_concurrent_positions |

---

## Conviction Sizing

Position sizes are expressed as a fraction of `max_position_usdc`:

| Conviction | Base Size | Reduced By |
|-----------|-----------|-----------|
| EXTREME | 100% | Disagreement, CAUTION state |
| HIGH | 75% | Disagreement, CAUTION state |
| MEDIUM | 50% | Disagreement, CAUTION state |
| LOW | 25% | Disagreement, CAUTION state |

Disagreement > 0.5 → additional 40% reduction.
CAUTION drawdown state → additional 40% reduction.
LOCKDOWN → no new positions at all.

---

## Circuit Breakers

| Trigger | Threshold | Effect |
|---------|-----------|--------|
| Daily loss | > max_daily_loss_usdc | LOCKDOWN |
| Consecutive losses | > max_consecutive_losses | LOCKDOWN |
| Total exposure | > max_total_exposure_usdc | LOCKDOWN |
| Correlated exposure | > max_correlated_exposure_usdc | LOCKDOWN |
| Halfway to daily loss | > 50% of daily limit | CAUTION (reduced sizing) |

CAUTION resets at UTC midnight if no new daily loss occurs.
LOCKDOWN must be manually cleared or triggers auto-reset logic (configurable).

---

## Helius Signal Safety Rules

Helius alpha is permitted to modestly adjust Claude's fair probability estimate:

| Signal Effect | Probability Adjustment |
|--------------|----------------------|
| STRONG_UP | +6% toward YES |
| MODEST_UP | +3% toward YES |
| NEUTRAL | No adjustment |
| MODEST_DOWN | -3% toward YES |
| STRONG_DOWN | -6% toward YES |

**Rules**:
- Only applies when market_relevance is DIRECT or INDIRECT
- THEMATIC, WEAK, NONE → zero adjustment
- Helius alone cannot trigger a trade — Claude must find independent edge
- Manipulation risk > 0.7 → Helius signal is ignored regardless of relevance

---

## No-Trade Conditions (expanded)

Beyond hard blocks, Claude should return SKIP when:

- The thesis relies on a single fragile source
- Model confidence is high but evidence quality is weak
- The apparent edge disappears after applying the penalty stack
- The market is near resolution and timing advantage is gone
- The position would exceed portfolio caps
- All brains agree but the herding penalty is high (fake consensus)
- Helius signal is being used for a categorically irrelevant market
- The trade cannot be expressed due to order book thinness

---

## Data Safety Guards

- "No trade on bad data": data_freshness < threshold triggers STALE_DATA block
- "No trade on ambiguous rules": ambiguity_score > threshold triggers AMBIGUITY_GATE block
- API failure during brain analysis → brain returns confidence=0 SKIP default → ensemble degrades gracefully
- Missing order book → immediate SKIP for that market cycle
- All persistence is local JSON — no external database dependency that can fail silently
