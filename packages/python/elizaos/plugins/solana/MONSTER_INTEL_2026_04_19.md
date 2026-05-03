# Monster Token Deep Intel — 2026-04-19

Enrichment run over all **40 mints** in `monster_addresses.json`.
Data sources: Helius RPC (signatures, tx decode, DAS `getAsset`, DAS `searchAssets`),
pump.fun metadata JSON bodies (IPFS/Arweave blobs linked from DAS `json_uri`).

Artefacts written:
- `monster_creator_intel.json` — per-mint raw intel (creator, earliest txs, metadata body, early buyers)
- `monster_creator_whitelist.json` — consolidated whitelist (serial deployers, coord cluster, tweet anchors, communities)
- `monster_addresses.json` — 30 previously-missing `creator_wallet` fields backfilled

---

## 1. Serial monster deployers — the creator whitelist

Four wallets have shipped **more than one** monster from our 40-token corpus. These are the highest-signal entity tier we've identified.

| Creator wallet | Monsters deployed |
|---|---|
| `DbFS3c9wpKtJiqFYu83QmpqHDBJtjKY8hqTL9AGZfRir` | ASTEROID · NUERO · XRP — **3 monsters** |
| `CoPyPPWdm8SirumaAEe8S68nSJpHYhguN2L5i2YGxLB` | ELUN · LOL (vanity starts with `CoPy...`) |
| `AXmnRBrNtYYyyo82cLBBhnWJ7o1iqNLZbuEVpDB3V666` | Hamster · assface (vanity ends `...666`) |
| `DsCJ5siuJTPQtQa3A9N69azGZaWtUPzi9VPp2G9Jfpx9` | Freg · UNCEROID |

**Action:** monitor these 4 wallets for any new pump.fun token creation. If one of them calls `create` on the pump.fun program, alert and prepare a scout-entry position. That's a one-call getSignaturesForAddress poll we can run every 15s.

---

## 2. Coordination cluster — buyers who appear early in multiple monsters

We walked the **earliest 25 signatures** of each of the 40 mints and collected distinct fee-payers (creator excluded). 33 wallets appeared early in **2 or more** monsters. Top 10 shown.

| Wallet | # monsters | Tokens (sample) |
|---|---|---|
| `FireuLYd4yjJBhXQyBs3Mq6ZpNEjyHPNPG2eqhTP9RHV` | **9** | ASTEROID, BULL, BURNIE, PIXEL, Freg, Cthulhu, assface, XRP, DUMBMONEY |
| `A7FMMgue4aZmPLLoutVtbC7gJcyqkHybUieiaDg9aaVE` | 8 | ASTEROID, apple, Freg, Cthulhu, UNCEROID, assface, XRP, DUMBMONEY |
| `Luckywzbt7nYBhmEZLnzqAsQmRE8bMmgyduxsP5kktR` | 6 | ASTEROID, Chicky, fluide, Cthulhu, UNCEROID, DUMBMONEY |
| `CoPyPPWdm8SirumaAEe8S68nSJpHYhguN2L5i2YGxLB` | 6 | (also in deployers list — *buys what they deploy*) |
| `66666y3RmL6gNyttNKXNhF5awxAWbBdGSfpKuaTLrjRH` | 5 | ASTEROID, NUERO, Cthulhu, UNCEROID, DUMBMONEY |
| `8L2y55D11k63CAftvW7uMM2mBhtMxLoLnivG9uY2bt8j` | 5 | ZEN, ROCKET, Dunald, fluide, GAJAE |
| `JESUSL2s5BsffGNNn6wQtHART2iXVGjtGhKAwGw44bL` | 4 | BURNIE, Chicky, BIGLY, UNCEROID |
| `CatyeC3LgBxub7HcpW2n7cZZZ66CUKdcZ8DzHucHrSiP` | 4 | Dunald, fluide, GSD, $HACHI |
| `4r33xEKAD2cNMrC9NyJy8nb4XmruUKebZ6LZZm65PVUZ` | 4 | Freg, UNCEROID, assface, XRP |
| `8abFwsQkoMRAk5mD4eXFTBqVyTGsfUZ1hVsfZ3NDsDUW` | 3 | ASTEROID, Chicky, BIGLY |

Vanity prefixes `Fire...`, `Copy...`, `Lucky...`, `6666...`, `JESUS...`, `Caty...` — these are almost certainly **sniper-bot wallets or paid-platform vanity addresses** (Bonkbot, Trojan, BullX, etc.) rather than human KOLs. They don't give us alpha *directly* but a **rising count of these bots piling in within the first 5 minutes of a new token** is itself a conviction signal — institutional/automated flow chose this token.

**Action:** watch these 10 wallets. Two distinct mechanics:
- If **3 or more** of them buy the same fresh mint within ~10 minutes → high-probability monster forming → consider scout buy.
- Track them as separate `WATCHED_BOT_WALLETS` slot in `axiom_copy_trader.py` — different sizing / cooldown rules than human whales.

---

## 3. Where the news originates — hidden channels we're not watching

Not every monster has socials at mint time. Here's what we found in the pump.fun metadata JSON bodies (the fields the *creator* pinned before anyone edited DexScreener):

| Channel | Count | Notes |
|---|---|---|
| Twitter handle / tweet | 22 / 40 | Most common |
| Website | 14 / 40 | Often just a landing page or .top/.host |
| Telegram | 8 / 40 | Sometimes mis-labeled as X Community |
| **X Community link** | **4** confirmed | ZEN, apple, Hamster, 49 — persistent organised group |
| **Discord launch** | 1 confirmed | Chicky: *"Launched on discord.gg/uxento"* in its own description |
| **TikTok video/hashtag** | 3 | Chicky, Hamster, 49 all linked TikTok search or creator URLs |

### The specific-tweet anchor pattern — biggest finding

10 of 40 monsters pinned a **specific `/status/` tweet** (not a handle — a single post) as their narrative anchor. Top KOLs:

| KOL handle | Monsters anchored |
|---|---|
| @elonmusk | **3** (ASTEROID, Quality, SpaceXcoin) + OPK referenced in description |
| @Sizeyon | Chicky |
| @yoheinakajima | pippin (BabyAGI creator, 1.8M followers — biggest monster in set) |
| @JakTheDegen | Freg (+1513% same-day) |
| @deviousbondius | Pluto |
| @w1zar9 | assface |
| @degentintin | XRP |
| @hyoki57 | GREKT |

**The winning combo:** a tweet URL in pump.fun metadata + the tweet author has >10k followers + the token ticker reflects the tweet narrative. This is deterministic to scan for — the URL regex is `x\.com/[handle]/status/\d+`.

### What we can't see from on-chain alone

- **Discord-only coordination** (Chicky `discord.gg/uxento`) — we'd have to join specific servers and scrape. Manual for now.
- **X Communities** (ZEN, apple, Hamster, 49, UNCEROID, assface) — these are invite-based X groups. We can detect the *link* in metadata, but we can't see inside without membership.
- **TikTok virality** (Chicky, Hamster, 49) — narrative starts on TikTok before Twitter. No cheap API — would need TikTok trend scraping.

---

## 4. Graduation timing — when they leave pump.fun

Already captured for 6 of 40. From the DexScreener `pair_created_at_ms` data:

- **PumpSwap (graduation)** happens when bonding curve fills — typically 10 min to several hours from mint.
- **Meteora DLMM secondary pool** opens within **40–180 min of PumpSwap** for 5 of 6 new monsters (unc asteroid 102m, assface 119m, HPOP8 40m, GREKT none yet, DUMBMONEY day-4 revival).

The moment the **bonding curve graduates AND a Meteora pool opens shortly after** is the clean entry window. Before that = pump.fun bonding curve chaos (rug risk, front-running). After = already ran.

---

## 5. Updated winning formula — the layered signal stack

Rank signals by how reliably they precede a monster (strongest first):

1. **Creator wallet is in serial-deployer whitelist** (4 known) → near-automatic scout entry on any new mint they call `create` for.
2. **≥ 3 coord-cluster wallets buy within 10 min of mint** → bots piling = alpha forming.
3. **Graduation to PumpSwap + Meteora pool opens within 180 min** → MMs are stepping in; join with trailing stop.
4. **Metadata contains `/status/` tweet URL with KOL > 10k followers** → score the token higher.
5. **Metadata contains X-Community link (`x.com/i/communities/...`)** → bonus: persistent buyer pool.
6. **Top1 holder < 10%, Top5 < 20%** → structural stability check (must pass).
7. **Liq $25k–$200k, buy ratio 48–65%, vol/liq 1–5x, m5 within ±15%** → entry-window quality gate.

Exit: trailing stop 15% from peak + hard SL 12% + time stop 120 min @ pnl < +10%. Monsters need runway, not razor-sharp TPs.

---

## 6. Before we backtest — ready to decide

We now have three distinct entry paths we could run concurrently:

| Strategy | Trigger | Sizing | Edge |
|---|---|---|---|
| Copy-trade (current) | Frost / Walta / clukz buys | 0.25 SOL | slow but proven, 3 wallets only |
| **Serial-deployer sniper** | Creator whitelist wallet mints a new pump.fun token | 0.2 SOL | earliest possible; 4 wallets; needs rug guard |
| **Cluster-confirm scout** | ≥3 coord-cluster wallets buy same mint within 10m | 0.3 SOL | late vs #1 but with bot conviction built in |
| **Lifecycle scout (Strategy E from earlier)** | PumpSwap + fresh Meteora pool + holder/socials filter | 0.25 SOL | simplest to run, catches the unc-asteroid shape |

Running all three in parallel with separate slot pools (max 2 per strategy) gives us 3 × 2 = 6 concurrent positions. Total exposure cap 1.5 SOL.

Next step on your word: run the backtest against the 40-monster corpus to see **which of these signals actually fires at the entry window** — i.e., for each monster we already have, would the strategy have flagged it at 1–4h old? I'll score hit-rate per strategy.
