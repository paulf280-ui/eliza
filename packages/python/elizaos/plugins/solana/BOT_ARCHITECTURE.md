# TraderBot — Solana Monster-Strategy Architecture

_Last reviewed 2026-04-24. Monster-only era. Copy-trade permanently retired._

## The goal, in one sentence

Find Solana memecoins during the brief post-graduation window where they look like they could become monster runners (10x+ within hours), enter before the real pump, ride to a managed exit, and refuse everything that looks like a rug setup.

## Design principles

1. **Deterministic rules over LLM opinions on the hot path.** Entry filters and exit rules are plain Python thresholds. LLMs advise; they don't gate money-moving decisions. Research consensus (FINSABER, LLMTime, TABULA-8B) is clear: LLMs underperform gradient-boosted rules on numerical tabular data at our sample size.
2. **Defense in depth.** Multiple independent layers reject the same bad token. If one filter drifts, others catch it.
3. **Log-only before enforce.** New filters ship with logging-only behavior so we measure false-positive rate against the bot's actual data before trusting them.
4. **Reversible, not destroyed.** Dead code is gated with a flag (`copy_trade_enabled=false`, `MONSTER_BE_TRAIL_ENABLED=False`), not deleted. Changing our mind is one line.
5. **One pause button actually pauses everything.** Dashboard Pause sets `trading_paused=true` and every strategy checks it.

---

## High-level flow

```mermaid
flowchart LR
    subgraph Sources["Data Sources"]
        HEL[Helius RPC / LaserStream]
        DEX[DexScreener API]
        PP[PumpPortal API]
    end

    subgraph Scouts["Scout Layer — find candidates"]
        CL[cluster_confirm]
        SE[serial_deployer]
        LC[lifecycle]
        BR[breakout_candle]
    end

    subgraph Guards["Guard Layer — reject bad candidates"]
        DET[Deterministic rule filters<br/>top-1/top-10/h1/age/liq/mc]
        HG[holder_guard<br/>hard-block + sweet-spot<br/>currently log-only]
    end

    subgraph Exec["Execution"]
        OMP[open_monster_position]
        PP_BUY[PumpPortal buy tx]
        JITO[Jito block submit]
    end

    subgraph Mon["Position Monitoring"]
        EVAL[evaluate_exit]
        FLOW[holder_guard.flow<br/>log-only]
        AI[AICascade brains<br/>Groq / Gemini / Opus]
    end

    subgraph ExitLayer["Exits"]
        TP1[TP1 +30% → sell 75%]
        FLOOR[Pre-TP1 floor -15%]
        FLAT[60min flat gate]
        MOONBAG[Post-TP1 state machine]
    end

    HEL --> Scouts
    DEX --> Scouts
    Scouts --> Guards
    Guards --> OMP
    OMP --> PP_BUY --> JITO
    OMP --> Mon
    DEX --> Mon
    HEL --> Mon
    Mon --> ExitLayer
    ExitLayer --> PP_BUY

    classDef enforced fill:#1e4d2b,stroke:#4caf50,color:#fff
    classDef logonly fill:#4d3d1e,stroke:#ff9800,color:#fff
    classDef dead fill:#4d1e1e,stroke:#f44336,color:#fff
    class DET,TP1,FLOOR,FLAT,MOONBAG,OMP,PP_BUY,JITO enforced
    class HG,FLOW logonly
```

Green = enforcing. Orange = log-only (gathering data before enforce). Red (not shown) = gated off (copy-trade, Frost Mirror, pre-TP1 BE trail).

---

## Entry side — how a signal becomes a trade

```mermaid
sequenceDiagram
    participant Scout as Monster Scout
    participant Dex as DexScreener
    participant RPC as Helius RPC
    participant Guard as holder_guard
    participant Pause as live_config
    participant Buy as open_monster_position
    participant Exec as PumpPortal

    Scout->>Dex: poll new pump-amm tokens
    Scout->>Scout: age ∈ [30min, 90min]?<br/>liq ≥ $50k, mc ≥ $50k?<br/>h1 ∈ [-10%, +100%]?<br/>buy_ratio ∈ [48%, 65%]?
    Scout->>RPC: top_wallet_distribution
    Scout->>Scout: top-1 ≤ 10%?<br/>top-10 ≤ 35%?
    Scout->>Buy: await open_monster_position(mint, ...)
    Buy->>Pause: trading_paused?
    alt paused
        Pause-->>Buy: true
        Buy-->>Scout: False (skip)
    else not paused
        Buy->>Buy: already holding mint?<br/>slot pool full?<br/>loser cooldown (24h) active?
        Buy->>Guard: evaluate_entry (log-only)
        Note over Guard: Logs hard-block reasons<br/>and sweet-spot failures<br/>to rejected_tokens.json
        Buy->>Exec: pump service buy<br/>(paper or live)
        Exec-->>Buy: tx sig
        Buy->>Buy: record open position
    end
```

---

## Scouts — the four signal sources

All four scouts feed the same choke point (`open_monster_position`). Each has a different hypothesis about what a future monster looks like.

| Scout | File | Hypothesis | Key filters |
|---|---|---|---|
| **cluster_confirm** | [monster_signals.py](monster_signals.py) `cluster_confirm_scout_loop` | 3+ known-alpha cluster wallets coincidentally buying the same new mint within 10min = insider convergence | cluster_wallets.json watchlist, 60min peak-ratio ≥ 0.85, liq ≥ $30k, top-10 ≤ 35% |
| **serial_deployer** | `serial_deployer_sniper_loop` | Creator has shipped prior monsters — if they ship a new one, follow | monster_creator_whitelist.json, waits up to 1h for graduation, top-1 ≤ 15%, top-10 ≤ 35% |
| **lifecycle** | `lifecycle_scout_loop` | Post-graduation token in the age/liq/mc/buy-ratio sweet spot where monster runs begin | age 30–90min, liq $50k–$1M, mc $50k–$5M, liq/mc ratio 4–15%, h1 ∈ [-10%, +100%], m5 ≤ +15%, top-1 ≤ 10%, top-10 ≤ 35%, socials required |
| **breakout_candle** | `breakout_candle_scout_loop` | +20% m5 breakout candle on a thin pump-amm pair with healthy buy-ratio | age 30min–12h, m5 ≥ +20%, liq $60k–$300k, h1 vol ≥ $3k, buy_ratio ≥ 65%, top-1 ≤ 12%, top-10 ≤ 35%, rugcheck pass |

**Rejection outcome loop**: every reject goes to [rejection_tracker.py](rejection_tracker.py) → 2h later, DexScreener outcome is checked → if the rejected token pumped ≥50%, filter flagged as "over-rejecting"; if rugged ≥50%, filter flagged as "saving us." Jarvis uses this to suggest threshold tunes.

---

## Holder Guard — the new defense-in-depth layer

```mermaid
flowchart TB
    subgraph Snapshot["build_snapshot()"]
        RPC1[getTokenLargestAccounts]
        RPC2[getAccountInfo → authorities]
        RPC3[getAsset → dev wallet]
        RPC4[getTokenAccountsByOwner → dev balance]
        LP[LP burn check]
    end

    subgraph HardBlock["hard_block.py"]
        HB1[top-10 > 35%?]
        HB2[dev > 5%?]
        HB3[LP burned < 99%?]
        HB4[mint authority live?]
        HB5[freeze authority live?]
    end

    subgraph SweetSpot["sweet_spot.py"]
        SS1[top-10 ∈ 5–25%?]
        SS2[dev < 1%?]
        SS3[≥500 holders, growth ≥5%/min?]
        SS4[buys > sells?]
        SS5[authorities renounced?]
    end

    subgraph Flow["flow.py — open positions"]
        F1[top-10 +3% in 5min?]
        F2[holders -5% in 10min?]
        F3[dev wallet → CEX?]
        F4[HOLD OVERRIDE: growth +2%/min?]
    end

    Snapshot --> HardBlock
    HardBlock -->|pass| SweetSpot
    HardBlock -->|fail| BLOCK[block / watchlist]
    SweetSpot --> WATCH[watchlist if miss]
    SweetSpot -->|pass| ENTER[enter with confidence]

    OPEN[open position] --> Flow
    Flow --> EXIT[force exit]
    Flow --> HOLD[suppress price-based exits]

    classDef logonly fill:#4d3d1e,stroke:#ff9800,color:#fff
    class HardBlock,SweetSpot,Flow,HB1,HB2,HB3,HB4,HB5,SS1,SS2,SS3,SS4,SS5,F1,F2,F3,F4 logonly
```

**Mode**: `HOLDER_GUARD_ENFORCE=false` (log-only). Every would-be block writes to `rejected_tokens.json` with `reason=holder_guard_hard_block` and all the snapshot data. After 24–48h of live data, we compare blocked-and-pumped vs blocked-and-rugged to decide whether to flip enforcement.

**What's hand-seeded vs empty**:
- Top-10 concentration, dev %, LP burn, authority renunciation — all computed live via Helius RPC, no subscription needed beyond the existing Helius plan
- CEX registry — ~10 seed addresses for major exchanges in [holder_guard/cex_addresses.json](holder_guard/cex_addresses.json). Expand as needed.
- Snipers / bundlers / audit score / fresh-wallet % — fields exist on the snapshot but populate as `None`. Deferred until we subscribe to Bubblemaps / Nansen / audit feeds.

The scheduled 48h agent pulls `GET /api/holder-guard/report?hours=48` and emails you the FLIP / HOLD / INCONCLUSIVE verdict.

---

## Exit side — how a position closes

```mermaid
flowchart TD
    POLL[monitor_positions_loop<br/>every 15s]
    POLL --> PRICE[pull DexScreener<br/>price, liq, buys, sells]
    PRICE --> DETEXIT[evaluate_exit]

    DETEXIT -->|pnl ≥ +30% & not tp1_fired| TP1[tp1_100pct<br/>sell 75%]
    DETEXIT -->|pnl ≤ -15% & not tp1_fired| FLOOR[pre_tp1_floor<br/>full exit]
    DETEXIT -->|60min in ±5% zone| FLAT[flat_gate<br/>full exit]
    DETEXIT -->|tp1_fired, post-TP1 events| MOON[moonbag state machine<br/>liq pull / buy-ratio fade / dormant cap]

    PRICE --> FLOWCHK[holder_guard.flow<br/>evaluate_exit]
    FLOWCHK -->|log-only| FLOGGED[log to console<br/>no action]

    PRICE --> AIC[AICascade.evaluate]
    AIC -->|Groq 30s| GROQSELL[Groq SELL ≥0.75?]
    AIC -->|Gemini 2min| GEMSELL[Gemini SELL ≥0.70?]
    AIC -->|Opus 3min| OPUSSELL[Opus SELL ≥0.65?]
    AIC -->|drawdown ≤-12% OR escalation| OPUSE[Opus emergency]

    TP1 --> APPLY[_apply_exit]
    FLOOR --> APPLY
    FLAT --> APPLY
    MOON --> APPLY
    GROQSELL --> APPLY
    GEMSELL --> APPLY
    OPUSSELL --> APPLY
    OPUSE --> APPLY

    APPLY --> PP_SELL[PumpPortal sell]
    APPLY --> RECORD[record close + pnl]

    classDef dead fill:#4d1e1e,stroke:#f44336,color:#fff
    classDef logonly fill:#4d3d1e,stroke:#ff9800,color:#fff
    class FLOWCHK,FLOGGED logonly
```

**Pre-TP1 protection layers (current state)**:

| Layer | Status | Threshold | Why |
|---|---|---|---|
| Hard floor | ✅ active | -15% | Catastrophic loss cap, unconditional |
| Flat gate | ✅ active | 60min in ±5% | Opportunity cost — dead money exit |
| **BE trail** | ❌ **disabled 2026-04-24** | (was peak +20% / pnl ≤+10%) | Was ejecting monster runners on natural pulse-and-breath pullbacks (AINI, SAM) |
| Brain cascade | ✅ active | Groq/Gemini/Opus tiers | Advisory exit on sharp breakdown reads |

**Post-TP1 (the moonbag rides)**: 25% of original position stays open. Exits event-driven:
- Liq drops > 40% in 5min → exit
- Buy-ratio < 30% sustained 10min → exit
- 8h dormant cap (don't hold forgotten bags)
- Brain SELL signals

---

## AI brain cascade — what the brains actually do

```mermaid
graph LR
    subgraph T0["t=0 one-shot entry scan"]
        ESG[Groq entry_scan]
        ESM[Gemini entry_scan]
    end

    subgraph HotPath["Per-position tick, every 15s"]
        CTX[_build_context<br/>price, atr, liq, buys/sells, holders, patterns]
    end

    subgraph Tiers["AICascade tiers"]
        T1[Tier 1: Groq llama-3.3-70b<br/>every 30s<br/>threshold SELL ≥0.75]
        T2[Tier 2: Gemini 2.5 flash-lite<br/>every 2min, age ≥60s<br/>threshold SELL ≥0.70]
        T3[Tier 3: Claude Opus 4.7<br/>every 3min, age ≥60s<br/>threshold SELL ≥0.65]
        T4[Tier 4: Claude Opus emergency<br/>drawdown ≤-12% or escalation]
    end

    subgraph Memory["brain_memory.json"]
        BM[Per-brain decision log<br/>Recent verdicts injected<br/>into next prompt as context]
    end

    subgraph Patterns["winning_patterns.json"]
        WP[5 learned rules<br/>generated from past outcomes<br/>injected into all tiers]
    end

    ESG -->|runner/normal/rug_risk| OPEN[mark position]
    ESM -->|runner/normal/rug_risk| OPEN
    CTX --> T1
    CTX --> T2
    CTX --> T3
    CTX --> T4
    T1 --> BM
    T2 --> BM
    T3 --> BM
    T4 --> BM
    BM --> T1
    BM --> T2
    BM --> T3
    WP --> T1
    WP --> T2
    WP --> T3
```

**Why tiered cadence**: Groq is fast + cheap, first line of defense on price breakdowns. Gemini adds independent second opinion. Opus 4.7 runs deep analysis including pattern memory from past trades. Emergency tier fires on catastrophic drawdown or when Groq+Gemini both escalate.

**Brains DON'T decide entries.** Entry is 100% deterministic: scout signal + filter gates + holder-guard (log-only). Brains only grade open positions.

**Honest note**: the brains have been demonstrably not the bottleneck for recent losses (AINI exited on deterministic BE trail; TRADE entered on deterministic lifecycle filter). The architecture above describes what they do; whether they're adding edge is a separate question being measured through rejection_tracker outcomes.

---

## What's NOT active (retired / gated)

```mermaid
flowchart LR
    subgraph Dead["Permanently off — gated, not deleted"]
        COPY[copy-trade strategy<br/>Frost, clukz, Walta<br/>copy_trade_enabled=false]
        FM[Frost Mirror<br/>read-only copy-of-one<br/>gates on same flag]
        BE[Pre-TP1 BE trail<br/>MONSTER_BE_TRAIL_ENABLED=False]
    end

    subgraph Deferred["Deferred — scoped, not built"]
        LGBM[LightGBM classifier<br/>needs 500+ labeled monsters<br/>paused until profitable]
        HAIKU[Bedrock Haiku 4.5 migration<br/>$450–800/mo<br/>paused until profitable]
        SNIPER[Sniper/bundler detection<br/>needs Bubblemaps $]
        AUDIT[Audit score ingestion<br/>no free API source]
    end

    classDef dead fill:#4d1e1e,stroke:#f44336,color:#fff
    classDef deferred fill:#3d3d4d,stroke:#9e9e9e,color:#fff
    class COPY,FM,BE dead
    class LGBM,HAIKU,SNIPER,AUDIT deferred
```

---

## Dashboard / Jarvis / control plane

```mermaid
flowchart TB
    subgraph Browser["Dashboard (63.180.211.88:3001)"]
        JC[Jarvis chat]
        PP_BTN[Pause button]
        PANELS[Position + P&L panels]
    end

    subgraph API["dashboard_api.py (aiohttp)"]
        ST[/api/status]
        POS[/api/positions]
        HIST[/api/history]
        CHAT[/api/chat]
        PAUSE[/api/copy-trade/pause]
        HGR["/api/holder-guard/report"]
    end

    subgraph Jarvis["jarvis_command_centre.py"]
        ROUTER[command router]
        CONTEXT[context assembler]
        LLM[Claude Haiku / Sonnet / Opus]
    end

    subgraph State["Runtime state"]
        LC[live_config: JSON]
        MP[monster_positions.json]
        TH[trade_history.json]
        RT[rejected_tokens.json]
        BM[brain_memory.json]
    end

    Browser --> API
    CHAT --> ROUTER
    ROUTER --> CONTEXT
    CONTEXT --> LLM
    LLM --> CHAT
    PAUSE --> LC
    LC -->|trading_paused| Scouts
    LC -->|copy_trade_paused| CopyTrade
    HGR --> RT
    HGR --> MP
    API --> State
    CONTEXT --> State
```

**Pause semantics (fixed 2026-04-24)**: the button sets both `trading_paused` AND `copy_trade_paused`. Monster, copy-trade, and any future strategy all check `trading_paused`. Pre-fix, only copy-trade honored it — monster kept trading while dashboard said paused.

---

## Infrastructure

| Layer | Service | Notes |
|---|---|---|
| **Compute** | AWS EC2 Frankfurt | Elastic IP `63.180.211.88`. systemd unit `traderbot.service` auto-restarts. Frankfurt for Solana validator density + future Jito Block Engine locality. |
| **RPC** | Helius Business (`mainnet.helius-rpc.com`) | Paid. LaserStream gRPC for pump.fun txns in real-time. Multiple RPC calls per signal (getTokenLargestAccounts, getAccountInfo, getAsset). |
| **Market data** | DexScreener (free) | Per-token polling for price / liq / mc / volume / buys / sells. |
| **Execution** | PumpPortal (`pumpportal.fun/api/trade-local`) | Free. `priorityFee` doubles as Jito tip. Routes pump-curve, pump-amm, raydium. |
| **Brains** | Groq / Gemini / Anthropic APIs | Pay-per-call. Anthropic auto-refills when <$5 balance. Groq X.AI social discovery uses a separate account (currently out of credit). |
| **Dashboard** | aiohttp + React SPA | Single port 3001. Public HTTP (no TLS). |
| **Alerts** | Telegram bot | Entry / exit / circuit-breaker notifications. |
| **Persistence** | Flat JSON files under `solana/` | `trade_history`, `monster_positions`, `brain_memory`, `rejected_tokens`, `live_config`, `cooldowns`, etc. Hot-reloaded where relevant. No database. |

---

## Current enforcement snapshot

| Protection | File | Enforcing? | Threshold |
|---|---|---|---|
| Pause (all strategies) | [strategy_e_monster.py:146](strategy_e_monster.py#L146), [live_config.py:164](live_config.py#L164) | ✅ | `trading_paused` |
| Lifecycle h1 downside floor | [monster_signals.py:745](monster_signals.py#L745) | ✅ | `h1_change ≥ -10%` |
| Top-10 cap, all scouts | [monster_signals.py:102](monster_signals.py#L102) | ✅ | `top_10_pct ≤ 35%` |
| Top-1 caps, per-scout | various | ✅ | 10 / 12 / 15 |
| Pre-TP1 hard floor | [strategy_e_monster.py:37](strategy_e_monster.py#L37) | ✅ | `-15%` |
| Flat gate | [strategy_e_monster.py:43](strategy_e_monster.py#L43) | ✅ | 60min in ±5% |
| Pre-TP1 BE trail | [strategy_e_monster.py:43](strategy_e_monster.py#L43) | ❌ **disabled** | flag off since 2026-04-24 |
| TP1 | [strategy_e_monster.py:35](strategy_e_monster.py#L35) | ✅ | +30% → sell 75% |
| Post-TP1 moonbag state machine | [strategy_e_monster.py:394](strategy_e_monster.py#L394) | ✅ | event-driven |
| AI cascade exit advisories | [trade_monitor.py:511](trade_monitor.py#L511) | ✅ | per-tier confidence thresholds |
| holder_guard hard-block | [holder_guard/hard_block.py](holder_guard/hard_block.py) | ⚠️ **log-only** | flip via `HOLDER_GUARD_ENFORCE=true` |
| holder_guard sweet-spot | [holder_guard/sweet_spot.py](holder_guard/sweet_spot.py) | ⚠️ **log-only** | same flag |
| holder_guard flow exits | [holder_guard/flow.py](holder_guard/flow.py) | ⚠️ **log-only** | same flag |
| CEX auto-exit | [holder_guard/cex_registry.py](holder_guard/cex_registry.py) | ⚠️ **log-only** | same flag |
| Copy-trade strategy | [axiom_copy_trader.py](axiom_copy_trader.py) | ❌ **gated off** | `copy_trade_enabled=false` |
| Frost Mirror | [axiom_copy_trader.py:597](axiom_copy_trader.py#L597) | ❌ **gated off** | same flag |

---

## The near-term roadmap (no new spend)

1. **48h log-only window** — let holder_guard observe live signals. Scheduled agent (`trig_01TuBq9ydbdX4frgXB3YAmFi`) emails the verdict Sunday 12:30 Dublin.
2. **Flip enforcement IF** false-positive rate is below 15% of holder_guard hard-blocks subsequently pumping ≥50%.
3. **Tune thresholds IF** the log-only window shows over-rejection. Only the thresholds — not the architecture.
4. **Expand CEX registry** from 10 → 50 seed addresses via Solscan labels. Zero code change, just [cex_addresses.json](holder_guard/cex_addresses.json) edits.

## Deferred until the bot is profitable

- **LightGBM classifier** on 50 holder/flow features ([PDF research](/dev/null) — it's the right path when the dataset supports it).
- **Bedrock Haiku 4.5 EU CRIS migration** — ~10× cheaper than Opus hot-path with prompt caching. Estimated $450–800/mo at your current volume.
- **Bubblemaps / Nansen** — unlocks sniper / bundler / smart-money-overlap filters that are currently `None` on the snapshot.

---

## If you're reading this cold

The bot's purpose is simple. The implementation is layered because we've learned (painfully) that no single filter catches every failure mode, and no single model decides trades well. The combination — deterministic scout filters → deterministic guards → paid LLM advisory → deterministic exits — is the architecture. The rest is tuning.
