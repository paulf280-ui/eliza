import { createRequire } from "module"
const require = createRequire(import.meta.url)
/**
 * analytics.ts — lightweight, privacy-respecting visit logging.
 *
 * api.cabal-hunter.com is DNS-only (grey cloud) so Cloudflare never sees the
 * traffic — its analytics stay blank. We own the Express server, so we count
 * visits here instead: landing views, map views (and which mint was searched),
 * and API calls. IPs are salted-hashed for unique-visitor counts; raw IPs are
 * never stored.
 */

const Database = require("better-sqlite3")
import crypto from "crypto"
import { fileURLToPath } from "url"
import { dirname, join } from "path"

const __dir = dirname(fileURLToPath(import.meta.url))
const SALT  = process.env.CABAL_INTERNAL_SECRET ?? "cabal-salt"

let _db: ReturnType<typeof Database> | null = null
function db(): ReturnType<typeof Database> | null {
  if (_db) return _db
  try {
    _db = new Database(join(__dir, "..", "pnl.db"))
    _db.exec(`CREATE TABLE IF NOT EXISTS visits (
      ts INTEGER, day TEXT, category TEXT, mint TEXT, ip_hash TEXT, referer TEXT
    )`)
    _db.exec(`CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day)`)
    return _db
  } catch {
    return null
  }
}

function hashIp(ip: string): string {
  return crypto.createHash("sha256").update(SALT + ip).digest("hex").slice(0, 16)
}

export function recordVisit(opts: {
  category: string; mint?: string; ip?: string; referer?: string
}): void {
  const d = db()
  if (!d) return
  try {
    const now = Date.now()
    d.prepare(
      "INSERT INTO visits (ts, day, category, mint, ip_hash, referer) VALUES (?,?,?,?,?,?)"
    ).run(
      now,
      new Date(now).toISOString().slice(0, 10),
      opts.category,
      opts.mint ?? null,
      opts.ip ? hashIp(opts.ip) : null,
      (opts.referer ?? "").slice(0, 200) || null
    )
  } catch { /* analytics must never break a request */ }
}

export function getAnalytics(): Record<string, unknown> {
  const d = db()
  if (!d) return { error: "no db" }
  const since30 = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10)
  const q = (sql: string, ...a: unknown[]) => d.prepare(sql).all(...a)
  const one = (sql: string, ...a: unknown[]) => (d.prepare(sql).get(...a) as Record<string, unknown>) ?? {}
  const today = new Date().toISOString().slice(0, 10)
  return {
    totals: {
      visits:          (one("SELECT COUNT(*) c FROM visits").c as number) ?? 0,
      unique_visitors: (one("SELECT COUNT(DISTINCT ip_hash) c FROM visits").c as number) ?? 0,
      map_views:       (one("SELECT COUNT(*) c FROM visits WHERE category='map'").c as number) ?? 0,
      landing_views:   (one("SELECT COUNT(*) c FROM visits WHERE category='landing'").c as number) ?? 0,
      api_calls:       (one("SELECT COUNT(*) c FROM visits WHERE category='api'").c as number) ?? 0,
    },
    today: {
      visits:          (one("SELECT COUNT(*) c FROM visits WHERE day=?", today).c as number) ?? 0,
      unique_visitors: (one("SELECT COUNT(DISTINCT ip_hash) c FROM visits WHERE day=?", today).c as number) ?? 0,
    },
    by_day:       q("SELECT day, COUNT(*) visits, COUNT(DISTINCT ip_hash) uniques FROM visits WHERE day>=? GROUP BY day ORDER BY day DESC", since30),
    by_category:  q("SELECT category, COUNT(*) n FROM visits GROUP BY category ORDER BY n DESC"),
    top_mints:    q("SELECT mint, COUNT(*) n, COUNT(DISTINCT ip_hash) u FROM visits WHERE mint IS NOT NULL GROUP BY mint ORDER BY n DESC LIMIT 20"),
    top_referers: q("SELECT referer, COUNT(*) n FROM visits WHERE referer IS NOT NULL AND referer!='' AND referer NOT LIKE '%cabal-hunter.com%' GROUP BY referer ORDER BY n DESC LIMIT 15"),
  }
}
