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
    // Additive migration — country code (privacy-safe: derived from IP, raw IP discarded)
    try { _db.exec(`ALTER TABLE visits ADD COLUMN country TEXT`) } catch { /* exists */ }
    // Excluded visitors (our own traffic) — keeps the dashboard organic-only
    _db.exec(`CREATE TABLE IF NOT EXISTS excluded_visitors (ip_hash TEXT PRIMARY KEY)`)
    for (const r of _db.prepare(`SELECT ip_hash FROM excluded_visitors`).all() as { ip_hash: string }[]) {
      _excluded.add(r.ip_hash)
    }
    return _db
  } catch {
    return null
  }
}

function hashIp(ip: string): string {
  return crypto.createHash("sha256").update(SALT + ip).digest("hex").slice(0, 16)
}

// In-memory set of excluded visitor hashes (our own traffic) — loaded at init
const _excluded = new Set<string>()

/** Exclude the given IP from all analytics: purge its existing rows, block
 *  future ones. Returns how many historical rows were removed. */
export function excludeIp(ip: string): { ip_hash: string; removed: number } {
  const h = hashIp(ip)
  _excluded.add(h)
  const d = db()
  let removed = 0
  if (d) {
    try {
      d.prepare(`INSERT OR IGNORE INTO excluded_visitors (ip_hash) VALUES (?)`).run(h)
      const r = d.prepare(`DELETE FROM visits WHERE ip_hash=?`).run(h)
      removed = r.changes ?? 0
    } catch { /* noop */ }
  }
  return { ip_hash: h, removed }
}

// ── Privacy-safe country geo ──────────────────────────────────────────────────
// Resolve the visitor's COUNTRY (not city/IP) once per IP, cache it, store only
// the 2-letter code. Raw IPs are never stored. Uses the free ip-api.com endpoint.
const _countryCache = new Map<string, string>()
const _countryPending = new Set<string>()

function isPublicIp(ip: string): boolean {
  return !!ip && !ip.startsWith("10.") && !ip.startsWith("192.168.") &&
    !ip.startsWith("127.") && !ip.startsWith("172.1") && ip !== "::1" && !ip.startsWith("::ffff:127")
}

/** Backfill the resolved country onto rows already written for this visitor.
 *  Most visitors hit once, so the row is inserted with a null country before
 *  the async lookup returns — without this, ~77% of geo data is lost. We key
 *  by ip_hash (raw IP is never stored), so privacy is preserved. */
function backfillCountry(ip: string, code: string): void {
  const d = db()
  if (!d) return
  try {
    d.prepare("UPDATE visits SET country=? WHERE ip_hash=? AND (country IS NULL OR country='')")
      .run(code, hashIp(ip))
  } catch { /* analytics must never break a request */ }
}

function countryFor(ip: string): string | null {
  if (!isPublicIp(ip)) return "LO"
  const hit = _countryCache.get(ip)
  if (hit) return hit
  if (!_countryPending.has(ip)) {
    _countryPending.add(ip)
    // fire-and-forget; resolves a beat after the row is inserted, then we
    // backfill the country onto that row (and any earlier null rows for this IP)
    fetch(`http://ip-api.com/json/${ip}?fields=status,countryCode`)
      .then(r => r.json())
      .then((d: any) => {
        if (d?.status === "success" && d.countryCode) {
          _countryCache.set(ip, d.countryCode)
          backfillCountry(ip, d.countryCode)
        }
      })
      .catch(() => {})
      .finally(() => _countryPending.delete(ip))
  }
  return null
}

export function recordVisit(opts: {
  category: string; mint?: string; ip?: string; referer?: string
}): void {
  const d = db()
  if (!d) return
  // Skip our own / excluded traffic so the dashboard stays organic-only
  if (opts.ip && _excluded.has(hashIp(opts.ip))) return
  try {
    const now = Date.now()
    d.prepare(
      "INSERT INTO visits (ts, day, category, mint, ip_hash, referer, country) VALUES (?,?,?,?,?,?,?)"
    ).run(
      now,
      new Date(now).toISOString().slice(0, 10),
      opts.category,
      opts.mint ?? null,
      opts.ip ? hashIp(opts.ip) : null,
      (opts.referer ?? "").slice(0, 200) || null,
      opts.ip ? countryFor(opts.ip) : null
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
      // REAL distinct tokens scanned (de-botted: a real mint carries ≥32 chars).
      // The dashboard card used top_mints.length, which is LIMIT 20 — so it was
      // permanently stuck at "20" once ≥20 tokens had been scanned.
      tokens_scanned:  (one("SELECT COUNT(DISTINCT mint) c FROM visits WHERE mint IS NOT NULL AND length(mint)>=32 AND category IN ('map','api')").c as number) ?? 0,
    },
    today: {
      visits:          (one("SELECT COUNT(*) c FROM visits WHERE day=?", today).c as number) ?? 0,
      unique_visitors: (one("SELECT COUNT(DISTINCT ip_hash) c FROM visits WHERE day=?", today).c as number) ?? 0,
    },
    by_day:       q("SELECT day, COUNT(*) visits, COUNT(DISTINCT ip_hash) uniques FROM visits WHERE day>=? GROUP BY day ORDER BY day DESC", since30),
    by_category:  q("SELECT category, COUNT(*) n FROM visits GROUP BY category ORDER BY n DESC"),
    // Countries of REAL visitors (landing/map only — exclude scanner noise & null)
    top_countries: q("SELECT country, COUNT(DISTINCT ip_hash) visitors, COUNT(*) hits FROM visits WHERE country IS NOT NULL AND country!='LO' AND category IN ('landing','map') GROUP BY country ORDER BY visitors DESC LIMIT 20"),
    top_mints:    q("SELECT mint, COUNT(*) n, COUNT(DISTINCT ip_hash) u FROM visits WHERE mint IS NOT NULL AND mint!='' AND category!='click' GROUP BY mint ORDER BY n DESC LIMIT 20"),
    outbound_clicks: q("SELECT mint AS target, COUNT(*) n FROM visits WHERE category='click' GROUP BY mint ORDER BY n DESC LIMIT 12"),
    top_referers: q("SELECT referer, COUNT(*) n FROM visits WHERE referer IS NOT NULL AND referer!='' AND referer NOT LIKE '%cabal-hunter.com%' GROUP BY referer ORDER BY n DESC LIMIT 15"),
  }
}
