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

import Database from "better-sqlite3"
import { CabalReport, Cluster, Holder } from "./types.js"

const DB_PATH = process.env.CABAL_CACHE_DB ?? "/home/ubuntu/eliza/packages/python/elizaos/plugins/solana/cabal_cache.db"
const BOT_URL = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
const SECRET  = process.env.CABAL_INTERNAL_SECRET ?? ""

// Open SQLite in read-only mode — Python bot owns writes
let _db: Database.Database | null = null

function getDb(): Database.Database | null {
  if (_db) return _db
  try {
    _db = new Database(DB_PATH, { readonly: true, fileMustExist: true })
    return _db
  } catch {
    return null   // DB not yet created — bot hasn't seen any tokens yet
  }
}

function buildVerdict(report: Partial<CabalReport>): string {
  const score = report.cabal_score ?? 0
  const clusters = report.coordinated_clusters ?? []
  if (score === 0 || clusters.length === 0) {
    return `CLEAN — No coordinated wallet clusters detected (${report.wallets_checked ?? 0} wallets traced).`
  }
  const top = clusters[0]
  if (report.risk === "HIGH") {
    return `AVOID — ${top.wallet_count} wallets funded by the same source (${top.master_short}) control ${top.combined_pct.toFixed(1)}% of supply. High probability of coordinated dump.`
  }
  return `CAUTION — Possible coordination: ${top.wallet_count} wallets from ${top.master_short} hold ${top.combined_pct.toFixed(1)}% combined. Monitor closely.`
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
    const score = Number(row.cabal_score ?? 0)
    const risk  = row.risk as "HIGH" | "MEDIUM" | "CLEAN"

    const report: CabalReport = {
      mint,
      token_name:           String(row.token_name ?? ""),
      risk,
      cabal_score:          score,
      is_controlled:        Boolean(row.is_controlled),
      verdict:              buildVerdict({ risk, cabal_score: score, coordinated_clusters: clusters, wallets_checked: Number(row.wallets_checked ?? 0) }),
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
    signal: AbortSignal.timeout(30_000),
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

  return {
    mint,
    token_name:           String(data.token_name ?? ""),
    risk,
    cabal_score:          score,
    is_controlled:        Boolean(data.is_controlled),
    verdict:              buildVerdict({ risk, cabal_score: score, coordinated_clusters: clusters, wallets_checked: Number(data.wallets_checked ?? 0) }),
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
