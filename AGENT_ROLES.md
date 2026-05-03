# Agent Roles — Polymarket Intelligence System

## Role Summary

| Brain | Model | Primary Role | Strongest Domain |
|-------|-------|-------------|-----------------|
| Claude | claude-sonnet-4-6 | Final decision maker, allocator, governor | All |
| Gemini | gemini-2.0-flash | Structured analyst, rules parser | Data-heavy, regulatory, structured events |
| Grok | grok-3-latest | News/sentiment/narrative | Breaking events, social signals |
| ChatGPT | gpt-4o | Adversarial researcher, thesis builder | Reasoning, complex narratives |
| Groq | llama-3.3-70b | Fast scanner, rapid reranker | Speed, quick sniff tests |
| Helius | (Claude via Helius data) | Solana on-chain filter | Crypto markets with on-chain relevance |

---

## CLAUDE — Chief Decision Maker

**Authority**: Only Claude can authorize capital deployment.

**Responsibilities**:
1. Review all brain outputs
2. Compare ensemble fair probability vs market-implied probability
3. Estimate edge net of fees, spread, and slippage
4. Apply full penalty stack (ambiguity, freshness, disagreement, crowding, correlation)
5. Trigger hard blocks when structural criteria fail
6. Select action: BUY_YES | BUY_NO | POST_PASSIVE_BID | POST_PASSIVE_ASK | REDUCE | EXIT | WATCH | SKIP
7. Size the position with conviction tier and portfolio-aware scaling
8. Write a decision memo explaining the trade, the edge, the risk, and what breaks the thesis

**When to SKIP**: Unclear rules, thin books, stale data, low net edge, high disagreement, lockdown state.

**When to WATCH**: Edge is promising but not yet ripe — catalyst timing uncertain, data thin, one brain outlier.

---

## GEMINI — Structured Analyst

**Role**: Parse structure, not narrative.

**Responsibilities**:
1. Parse market question and resolution conditions with precision
2. Find hidden assumptions and edge cases in resolution rules
3. Identify logical contradictions with related markets
4. Estimate base probability from verifiable facts and historical base rates
5. Flag oracle/dispute risk in resolution sources

**Prompt style**: Evidence-cited, conservative probability estimates, structured output.

**Domain weights** (higher = more trusted):
- Finance, regulatory, structured outcomes: 0.40
- Crypto/DeFi (structured data): 0.30
- Politics/elections: 0.35
- Sports: 0.30

---

## GROK — Narrative Intelligence

**Role**: Detect when information is moving faster than prices.

**Responsibilities**:
1. Monitor X/Twitter sentiment and breaking news
2. Identify narrative acceleration events
3. Separate organic signal from bot amplification
4. Assess event urgency
5. Flag crowd over/under-reaction

**Prompt style**: Event-focused, urgency-sensitive, skeptical of single-source claims.

**Domain weights**:
- Crypto (social-driven): 0.30
- Politics (live events): 0.20
- Sports (breaking news): 0.25

**Special capability**: Live X search via Grok API's search_parameters. Grok sees real-time X posts that other models cannot.

---

## CHATGPT — Adversarial Researcher

**Role**: Build and destroy the thesis.

**Responsibilities**:
1. Construct the bull case from available evidence
2. Construct the bear case with equal rigor
3. Identify what the market is getting wrong or missing
4. Determine the single most important load-bearing assumption
5. Turn raw evidence into a coherent narrative

**Prompt style**: Thesis + antithesis structure. Explicitly asks: "Why would this trade be wrong?"

**Domain weights**:
- Politics/elections: 0.40
- Finance/macro: 0.35
- Complex narratives: 0.35

---

## GROQ — Fast Scanner

**Role**: Keep the pipeline fast.

**Responsibilities**:
1. Rapid rescanning — refresh probability estimates between full analysis cycles
2. Quick sniff test — is this market even worth deep analysis?
3. Threshold monitoring — has a market moved enough to trigger re-evaluation?
4. Opportunity reranking — sort candidates by attractiveness quickly

**Prompt style**: Short, direct, no over-hedging.

**Domain weights**: 0.10–0.20 across all categories (always auxiliary).

**Fast mode**: Groq also supports a `fast=True` mode using `llama-3.1-8b-instant` for sub-2s screening.

---

## HELIUS ALPHA AGENT — Solana On-Chain Filter

**Role**: Transform Solana blockchain activity into structured intelligence for Claude.

**NOT a trader. Does NOT make final decisions.**

**Responsibilities**:
1. Monitor wallet flows, DEX swaps, token transfers, liquidity events
2. Determine whether on-chain activity is relevant to the Polymarket market
3. Classify relevance: DIRECT | INDIRECT | THEMATIC | WEAK | NONE
4. Score each signal: anomaly, relevance, causality, persistence, quality, urgency, manipulation_risk, tradability
5. Output a complete HeliusIntelligence report with final_alpha_statement

**Decision rule**:
- ACTIONABLE: relevant + timely + credible + hard to dismiss + likely to matter before market reprices
- MONITOR: interesting but not yet reliable
- IGNORE: weak, noisy, or irrelevant

**Categorically irrelevant for**: politics, elections, sports, macro, legal rulings, celebrity events.

**Most useful for**: SOL ecosystem questions, crypto sentiment markets, on-chain-driven narratives, wallet-flow-sensitive outcomes, DEX activity surges.

**Hard noise filters**:
- Isolated large transfers with no follow-through → IGNORE
- Bot churn / wash activity → reject
- Single-spike narratives without confirmation → MONITOR at best
- Activity that can't survive a basic plausibility test → IGNORE

---

## Ensemble Weighting

Brain weights are computed as:

```
raw_weight = domain_weight × ((1 - 0.5) + brain.confidence × 0.5)
             × (0.5 + historical_accuracy)
final_weight = raw_weight / sum(all_raw_weights)
```

Herding penalty applies when all brains agree (std < 5%) — small nudge toward 0.5.

Disagreement score > 0.5 → ensemble confidence is not sufficient for HIGH/EXTREME conviction.
