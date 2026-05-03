# Monster Strategy — Social Monitoring Scope

Purpose: give the brain-managed runners (post-TP1) a *narrative health* signal so
it can call event-driven exits on `brain_emergency` before price collapses. On
the entry side, tweet-anchor + community-link scoring boosts a candidate monster
from "maybe" to "prioritise".

No price-based SL after TP1 — these signals are the safety net.

---

## Tier 1 — Tweet-anchor health (cheap, deterministic)

**Applies to:** every monster whose pump.fun metadata contains an `x.com/<handle>/status/<id>` URL. That's 10/40 monsters in the backtest corpus; likely ~25% of future monsters.

**What to poll:**
- Tweet engagement (likes / retweets / replies / quotes) — Twitter/X API v2 `tweets/{id}?tweet.fields=public_metrics`
- Tweet exists (not deleted) — deletion = narrative blown
- Author's reply/quote behaviour — has @elonmusk quoted it since? has @JakTheDegen posted a follow-up?

**Polling cadence:**
- First 30 min after entry: every 60s
- 30 min – 6h: every 5 min
- 6h+: every 30 min

**Trigger conditions (fire `brain_emergency` exit):**
- Tweet deleted → exit 100% immediately
- Engagement velocity collapses (new likes/min drops >80% from peak 10-min window AND price -15% in same window)
- Author posts a contradicting tweet ("not my token", "scam", "be careful") — detect via follow-up tweets from same author mentioning token or disavowing

**Boost conditions (entry scoring):**
- Author >10k followers → +2 priority
- Author >100k followers → +4 priority
- Tweet has >1k likes at mint time → +2 priority

**Known high-value anchor authors (from the 40-corpus):**
- @elonmusk (3 monsters: ASTEROID, Quality, SpaceXcoin)
- @yoheinakajima (pippin, 1.8M followers)
- @JakTheDegen (Freg, +1513% same-day)
- @Sizeyon, @deviousbondius, @w1zar9, @degentintin, @hyoki57

**Cost:** Twitter API v2 basic tier = $100/mo, 10k tweet lookups/mo.
Realistic usage: ~20 active monster positions max × 500 lookups = 10k/mo. Fits.

---

## Tier 2 — X Community presence (medium effort)

**Applies to:** monsters whose metadata contains `x.com/i/communities/<id>`. 4/40 in corpus (ZEN, apple, Hamster, 49). Likely ~10% of future monsters.

**What to monitor:**
- Member count growth rate (scrape via web fetch; no public API)
- Post frequency inside community
- Whether the community link still resolves (not deleted)

**Polling cadence:** every 15 min — these move slow.

**Trigger conditions:**
- Community deleted/privated → `brain_emergency` exit 100%
- Member count decline >10% in 1h → exit 50% (organised dump warning)

**Boost conditions (entry):**
- Community exists and has >500 members at mint → +3 priority
- Community exists with >2000 members → +5 priority (persistent organised buyer pool)

**Cost:** free (scraping), but brittle — Twitter could break the pattern any month.

---

## Tier 3 — Discord launch channels (manual-assist)

**Applies to:** monsters that pinned a `discord.gg/<code>` invite. 1/40 in corpus (Chicky). Likely ~5% of future monsters.

**Approach:**
- When we detect a discord invite in metadata, log it to `monster_discord_watchlist.json` and alert the operator — we're not joining Discord servers automatically, that's a TOS landmine.
- Operator can manually join + feed notable messages back via a `/monster-discord-flag {mint} {reason}` slash command (not built yet — backlog item).

**No automated trigger** in v1. This tier stays opt-in human-in-the-loop.

---

## Tier 4 — TikTok virality (ignore for v1)

**Applies to:** monsters with TikTok links in metadata. 3/40 (Chicky, Hamster, 49).

**Decision: skip in v1.** Rationale:
- No cheap API (TikTok commercial API requires business verification + $)
- Scraping is fragile and IP-banned fast
- Volume is too small (3/40) to justify the effort

**Re-visit when:** we see TikTok-anchored monsters in ≥15% of live captures (not backtest). Signal suggests TikTok is becoming a primary narrative vector.

---

## Tier 5 — Holder/on-chain velocity (NOT social, but overlapping alert surface)

Already in scope for the trade_monitor cascade but re-stated here because it triggers the same `brain_emergency` exit:

- `holder_delta_5min`: if new holders stop arriving for 10min post-TP1 → start winding down
- `liq_chg_5min_pct`: if liquidity pulled >40% in 5min → immediate exit 100%
- `buy_ratio_5min`: if <30% sustained 10min post-TP1 → exit 50%
- Dev/top5 coordinated sell (>10% of supply dumped by top 5 in 5min) → exit 100%

These run in the brain cascade on 30s cadence. No new work — already specified in `strategy_e_monster.py`.

---

## Wiring plan

1. **`monster_social_monitor.py`** (new service) — Tier 1 + Tier 2 poller. Uses `twitter-api-v2` SDK (cost $100/mo) + httpx for community scrape. Reads `monster_positions.json` to know what to watch.

2. **Extend `monster_positions.json` schema** — add `social_anchors: {tweet_url, community_url, discord_invite}` captured at entry.

3. **Extend `strategy_e_monster.py`** — add `social_event_exit(pos, event)` hook that the monitor service calls into. Event types: `tweet_deleted`, `engagement_collapse`, `author_disavow`, `community_deleted`, `community_drain`. Each maps to a fraction to sell (100% for severe, 50% for warning).

4. **Entry priority scoring** — add `compute_monster_entry_score(candidate)` to the entry signals layer. Sum of boosts from Tier 1/2. Score ≥5 = auto-enter, score 3–4 = enter if slot available, <3 = enter only if serial-deployer sniper (Tier-0 on-chain signal).

5. **Secrets** — `TWITTER_BEARER_TOKEN` env var. Guard the monitor with a feature flag `MONSTER_SOCIAL_MONITOR_ENABLED` defaulting to `false` so we can stage the roll-out.

---

## Roll-out order

1. Wire on-chain signal sources to `strategy_e_monster.py` (cluster-confirm, serial-deployer, lifecycle) — no social dependency. Paper trade 2 weeks.
2. If paper trade shows ≥30% hit-rate on real captures → add Tier 1 (tweet anchor) — cheapest, biggest expected lift.
3. After 2 weeks with Tier 1 → add Tier 2 (X Community) if data supports it.
4. Revisit Tier 3/4 quarterly.

**Do not build Tier 2–4 before Tier 1 proves out.** YAGNI.
