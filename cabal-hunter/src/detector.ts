import { createRequire } from "module"
const require = createRequire(import.meta.url)
/**
 * detector.ts — Cabal detection engine.
 *
 * Primary path: reads from the shared SQLite cache written by the Python bot.
 * This gives sub-100ms responses for tokens the bot has already analysed.
 *
 * Fallback path: calls the Python bot's internal /api/cabal/internal bridge,
 * which runs the full Helius RPC analysis and caches the result for next time.
 *
 * We intentionally keep business logic in Python (cluster_check.py) rather
 * than duplicating it here. TypeScript is only the MCP/payment layer.
 */

const Database = require("better-sqlite3")
import { CabalReport, Cluster, DeployerReport, Holder } from "./types.js"

const DB_PATH = process.env.CABAL_CACHE_DB ?? "/home/ubuntu/eliza/packages/python/elizaos/plugins/solana/cabal_cache.db"
const BOT_URL = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
const SECRET  = process.env.CABAL_INTERNAL_SECRET ?? ""

// Open SQLite in read-only mode — Python bot owns writes
let _db: ReturnType<typeof Database> | null = null

function getDb(): ReturnType<typeof Database> | null {
  if (_db) return _db
  try {
    _db = new Database(DB_PATH, { readonly: true, fileMustExist: true })
    return _db
  } catch {
    return null   // DB not yet created — bot hasn't seen any tokens yet
  }
}

/** Freshest interesting token for the landing-page demo link — prefers a
 *  recent token with a meaningful score so the demo never shows a dead page. */
export function getFreshDemoMint(): string | null {
  const db = getDb()
  if (!db) return null
  try {
    const row = db.prepare(
      "SELECT mint FROM cabal_cache WHERE expires_at>unixepoch() " +
      "ORDER BY (cabal_score>=40) DESC, computed_at DESC LIMIT 1"
    ).get() as { mint?: string } | undefined
    return row?.mint ?? null
  } catch {
    return null
  }
}

function buildVerdict(report: Partial<CabalReport>): string {
  const score = report.cabal_score ?? 0
  const clusters = report.coordinated_clusters ?? []
  const deployer = report.deployer

  let deployerNote = ""
  if (deployer?.verdict === "SERIAL_RUGGER") {
    deployerNote = ` DEPLOYER ALERT: this creator has launched ${deployer.tokens_launched} tokens, ${deployer.dead} of ${deployer.sampled} checked are dead (${deployer.dead_pct}%).`
  } else if (deployer?.verdict === "POOR_TRACK_RECORD") {
    deployerNote = ` Deployer track record is weak: ${deployer.dead}/${deployer.sampled} previous launches dead.`
  }

  if (score === 0 || clusters.length === 0) {
    return `CLEAN — No coordinated wallet clusters detected (${report.wallets_checked ?? 0} wallets traced).${deployerNote}`
  }
  const top = clusters[0]
  const how = top.type === "time_sync"
    ? `${top.wallet_count} wallets bought in the EXACT same block (bundled launch)`
    : `${top.wallet_count} wallets funded by the same source (${top.master_short})`
  if (report.risk === "HIGH") {
    return `AVOID — ${how}, controlling ${top.combined_pct.toFixed(1)}% of supply. High probability of coordinated dump.${deployerNote}`
  }
  return `CAUTION — Possible coordination: ${how}, ${top.combined_pct.toFixed(1)}% combined. Monitor closely.${deployerNote}`
}

/** Read from shared SQLite cache — fastest path (<1ms) */
function readFromCache(mint: string): CabalReport | null {
  const db = getDb()
  if (!db) return null

  try {
    const row = db.prepare(
      "SELECT * FROM cabal_cache WHERE mint=? AND expires_at>unixepoch()"
    ).get(mint) as Record<string, unknown> | undefined

    if (!row) return null

    const clusters: Cluster[] = JSON.parse((row.clusters_json as string) || "[]")
    const holders: Holder[]   = JSON.parse((row.holders_json as string)  || "[]")
    const deployer: DeployerReport | null = row.deployer_json
      ? JSON.parse(row.deployer_json as string) : null
    const timeSync = Boolean(row.time_sync) || clusters.some(c => c.type === "time_sync")

    // Recompute the blended score from the row's own data — identical formula
    // to Python's blend_deployer_into_score (idempotent, no fixed floors)
    const totalPct = holders.length ? holders.reduce((s, h) => s + (h.pct || 0), 0) : 100
    const coordPct = clusters.reduce((s, c) => s + (c.combined_pct || 0), 0)
    const base = totalPct > 0 ? Math.min((coordPct / totalPct) * 100, 100) : 0
    const deadPct = Number(deployer?.dead_pct ?? 0)
    const sampled = Number(deployer?.sampled ?? 0)
    const depComponent = (Math.max(0, deadPct - 40) / 60) * Math.min(1, sampled / 10) * 75
    const score = Math.round(Math.min(100, base + depComponent) * 10) / 10
    const risk: "HIGH" | "MEDIUM" | "CLEAN" =
      score >= 65 ? "HIGH" : score >= 35 ? "MEDIUM" : "CLEAN"

    const report: CabalReport = {
      mint,
      token_name:           String(row.token_name ?? ""),
      risk,
      cabal_score:          score,
      is_controlled:        score >= 35,
      time_sync:            timeSync,
      deployer,
      verdict:              buildVerdict({ risk, cabal_score: score, coordinated_clusters: clusters, deployer, wallets_checked: Number(row.wallets_checked ?? 0) }),
      coordinated_clusters: clusters,
      holders,
      wallets_checked:      Number(row.wallets_checked ?? 0),
      analysis_time_ms:     0,
      source:               "pre_indexed",
      cached_age_seconds:   Math.floor(Date.now() / 1000 - Number(row.computed_at ?? 0)),
      pair_created_ts:      Number(row.pair_created_ts ?? 0),
      computed_at:          Number(row.computed_at ?? 0),
    }
    return report
  } catch {
    return null
  }
}

/** Call the Python bot's internal API for real-time analysis */
async function fetchFromBot(mint: string, createdTs?: number): Promise<CabalReport> {
  const params = new URLSearchParams({ mint })
  if (createdTs) params.set("created_ts", String(createdTs))

  const url = `${BOT_URL}/api/cabal/internal?${params}`
  const res = await fetch(url, {
    headers: SECRET ? { "X-Internal-Secret": SECRET } : {},
    signal: undefined,
  })

  if (!res.ok) {
    const err = await res.text().catch(() => "unknown error")
    throw new Error(`Bot internal API returned ${res.status}: ${err}`)
  }

  const data = await res.json() as Record<string, unknown>
  if (data.error) throw new Error(String(data.error))

  const clusters: Cluster[] = (data.clusters as Cluster[]) ?? []
  const holders: Holder[]   = (data.holders  as Holder[])  ?? []
  const score = Number(data.cabal_score ?? 0)
  const risk  = (data.risk as "HIGH" | "MEDIUM" | "CLEAN") ?? "CLEAN"
  const deployer = (data.deployer as DeployerReport | null) ?? null
  const timeSync = Boolean(data.time_sync)

  return {
    mint,
    token_name:           String(data.token_name ?? ""),
    risk,
    cabal_score:          score,
    is_controlled:        Boolean(data.is_controlled),
    time_sync:            timeSync,
    deployer,
    verdict:              buildVerdict({ risk, cabal_score: score, coordinated_clusters: clusters, deployer, wallets_checked: Number(data.wallets_checked ?? 0) }),
    coordinated_clusters: clusters,
    holders,
    wallets_checked:      Number(data.wallets_checked ?? 0),
    analysis_time_ms:     0,
    source:               (data.source as "pre_indexed" | "real_time") ?? "real_time",
    pair_created_ts:      Number(data.pair_created_ts ?? 0),
    computed_at:          Number(data.computed_at ?? 0),
  }
}

/**
 * Main entry point — returns a full CabalReport for a given mint.
 * Tries cache first, falls back to real-time bot analysis.
 */
export async function getCabalReport(
  mint: string,
  createdTs?: number
): Promise<CabalReport> {
  const t0 = Date.now()

  // 1. Try pre-indexed cache (sub-millisecond)
  const cached = readFromCache(mint)
  if (cached) {
    cached.analysis_time_ms = Date.now() - t0
    return cached
  }

  // 2. Real-time analysis via Python bot
  const report = await fetchFromBot(mint, createdTs)
  report.analysis_time_ms = Date.now() - t0
  return report
}
