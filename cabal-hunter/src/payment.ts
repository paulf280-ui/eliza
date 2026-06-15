/**
 * payment.ts — Native USDC payment verification on Solana.
 *
 * No external payment packages required. We call Helius directly.
 *
 * Flow:
 *  1. Client requests analysis → server returns HTTP 402 with payment instructions
 *  2. Client sends 0.05 USDC to our receiving wallet with a nonce in the memo
 *  3. Client resubmits with X-Payment-Signature header
 *  4. We verify the transaction on-chain via Helius RPC
 *  5. Verified → run analysis and return results
 *
 * USDC SPL token on Solana mainnet: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
 * Decimals: 6  (so 0.05 USDC = 50000 raw units)
 */

import crypto from "crypto"
import { createRequire } from "module"
const require = createRequire(import.meta.url)
const Database = require("better-sqlite3")
import { PaymentRequest, PaymentVerification } from "./types.js"

const HELIUS_RPC  = process.env.HELIUS_RPC_URL ?? ""
const HELIUS_KEY  = process.env.HELIUS_API_KEY ?? ""
const RECIPIENT   = process.env.RECEIVING_WALLET ?? ""
const USDC_MINT   = process.env.USDC_MINT ?? "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
const PRICE_USDC  = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")
const PRICE_RAW   = Math.round(PRICE_USDC * 1_000_000)   // USDC has 6 decimals
const TX_MAX_AGE  = 120   // seconds — reject old payment proofs

// In-memory nonce store (nonce → expiry_ms). Prevents replay attacks.
const _usedNonces = new Map<string, number>()

// ── Free tier tracking (SQLite) ───────────────────────────────────────────────
// 100 free queries per IP per calendar month. No account required.
// After 100, the standard $0.05 USDC payment gate applies.
const FREE_QUERIES_PER_MONTH = 100

let _freeDb: ReturnType<typeof Database> | null = null

function getFreeDb(): ReturnType<typeof Database> {
  if (_freeDb) return _freeDb
  const dbPath = join(__dirname2, "..", "pnl.db")
  _freeDb = new Database(dbPath)
  _freeDb.exec(`
    CREATE TABLE IF NOT EXISTS free_usage (
      ip    TEXT NOT NULL,
      month TEXT NOT NULL,
      count INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (ip, month)
    );
    CREATE TABLE IF NOT EXISTS map_usage (
      ip    TEXT NOT NULL,
      month TEXT NOT NULL,
      count INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (ip, month)
    );
  `)
  return _freeDb
}

// ── Map (manual web tool) metering ────────────────────────────────────────────
// The bubble map is free. But 100 manual scans/month = a power user who clearly
// finds it useful — exactly the moment to nudge them to integrate the API into
// their bot (where real volume lives, and where billing kicks in). Soft nudge:
// we never block the map, we just surface the prompt once they cross the line.
export const MAP_SCAN_LIMIT = Number(process.env.MAP_FREE_SCANS ?? 100)

/** Increment this IP's monthly map-scan counter; return the new running count. */
export function recordMapScan(ip: string): number {
  try {
    const db    = getFreeDb()
    const month = new Date().toISOString().slice(0, 7)
    db.prepare(`
      INSERT INTO map_usage (ip, month, count) VALUES (?, ?, 1)
      ON CONFLICT(ip, month) DO UPDATE SET count = count + 1
    `).run(ip, month)
    const row = db.prepare("SELECT count FROM map_usage WHERE ip=? AND month=?")
      .get(ip, month) as { count: number } | undefined
    return row?.count ?? 0
  } catch { return 0 }
}

/** Returns how many free queries this IP has remaining this month (0 = none left). */
export function getFreeQueriesRemaining(ip: string): number {
  try {
    const db    = getFreeDb()
    const month = new Date().toISOString().slice(0, 7)   // "2026-06"
    const row   = db.prepare(
      "SELECT count FROM free_usage WHERE ip=? AND month=?"
    ).get(ip, month) as { count: number } | undefined
    const used = row?.count ?? 0
    return Math.max(0, FREE_QUERIES_PER_MONTH - used)
  } catch { return 0 }
}

/** Increments the free usage counter for this IP. */
export function consumeFreeQuery(ip: string): void {
  try {
    const db    = getFreeDb()
    const month = new Date().toISOString().slice(0, 7)
    db.prepare(`
      INSERT INTO free_usage (ip, month, count) VALUES (?, ?, 1)
      ON CONFLICT(ip, month) DO UPDATE SET count = count + 1
    `).run(ip, month)
  } catch { /* non-fatal */ }
}

// ── Payment log (SQLite) ──────────────────────────────────────────────────────

import { join, dirname } from "path"
import { fileURLToPath } from "url"

const __filename2 = fileURLToPath(import.meta.url)
const __dirname2  = dirname(__filename2)

let _logDb: ReturnType<typeof Database> | null = null

function getLogDb(): ReturnType<typeof Database> {
  if (_logDb) return _logDb
  const dbPath = join(__dirname2, "..", "pnl.db")
  _logDb = new Database(dbPath)
  _logDb.exec(`
    CREATE TABLE IF NOT EXISTS payments (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      tx_signature  TEXT UNIQUE,
      mint_queried  TEXT,
      amount_usdc   REAL,
      timestamp     INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_ts ON payments(timestamp);
  `)
  return _logDb
}

export function logPayment(txSig: string, mint: string, amountUsdc: number): void {
  try {
    getLogDb().prepare(
      "INSERT OR IGNORE INTO payments (tx_signature, mint_queried, amount_usdc, timestamp) VALUES (?,?,?,?)"
    ).run(txSig, mint, amountUsdc, Math.floor(Date.now() / 1000))
  } catch { /* non-fatal */ }
}

export function getPnlStats(): Record<string, unknown> {
  try {
    const db  = getLogDb()
    const now = Math.floor(Date.now() / 1000)
    const get = (since: number) => db.prepare(
      "SELECT COUNT(*) as cnt, SUM(amount_usdc) as rev FROM payments WHERE timestamp > ?"
    ).get(since) as { cnt: number; rev: number | null }

    const today  = get(now - 86400)
    const week   = get(now - 7*86400)
    const month  = get(now - 30*86400)
    const total  = get(0)
    const top    = db.prepare(
      "SELECT mint_queried, COUNT(*) as cnt FROM payments GROUP BY mint_queried ORDER BY cnt DESC LIMIT 10"
    ).all() as Array<{ mint_queried: string; cnt: number }>

    // ── REAL token queries (de-botted) ────────────────────────────────────────
    // A genuine scan carries a real mint (≥32 chars). Endpoint-enumeration bots
    // hit /api/* with NO mint, so this filter drops them automatically. We also
    // exclude our own/blocked traffic. This is what people actually queried —
    // shown even before any of it converts to paid.
    const REAL = "mint IS NOT NULL AND length(mint)>=32 AND category IN ('map','api') " +
      "AND (ip_hash IS NULL OR ip_hash NOT IN (SELECT ip_hash FROM excluded_visitors))"
    const nowMs = Date.now()
    const realCnt = (sinceMs: number) => {
      try {
        return (db.prepare(
          `SELECT COUNT(*) c FROM visits WHERE ${REAL} AND ts > ?`
        ).get(sinceMs) as { c: number }).c
      } catch { return 0 }
    }
    let topQueried: Array<{ mint_queried: string; cnt: number; users: number }> = []
    try {
      topQueried = db.prepare(
        `SELECT mint AS mint_queried, COUNT(*) AS cnt, COUNT(DISTINCT ip_hash) AS users
         FROM visits WHERE ${REAL} GROUP BY mint ORDER BY cnt DESC LIMIT 12`
      ).all() as Array<{ mint_queried: string; cnt: number; users: number }>
    } catch { topQueried = [] }

    return {
      today:  { queries: today.cnt,  revenue_usdc: +(today.rev  || 0).toFixed(4) },
      week:   { queries: week.cnt,   revenue_usdc: +(week.rev   || 0).toFixed(4) },
      month:  { queries: month.cnt,  revenue_usdc: +(month.rev  || 0).toFixed(4) },
      total:  { queries: total.cnt,  revenue_usdc: +(total.rev  || 0).toFixed(4) },
      top_mints: top,
      // real usage (free + paid scans of actual tokens, bots excluded)
      real_queries: {
        today: realCnt(nowMs - 86400e3),
        week:  realCnt(nowMs - 7 * 86400e3),
        month: realCnt(nowMs - 30 * 86400e3),
        total: realCnt(0),
      },
      top_queried: topQueried,
    }
  } catch (e) {
    return { error: String(e) }
  }
}

function purgeExpiredNonces() {
  const now = Date.now()
  for (const [nonce, exp] of _usedNonces.entries()) {
    if (now > exp) _usedNonces.delete(nonce)
  }
}

/** Generate a payment request that the client must fulfil */
export function createPaymentRequest(queryId: string): PaymentRequest {
  const nonce     = `ch-${queryId}-${Date.now()}`
  const expiresAt = Math.floor(Date.now() / 1000) + 300   // 5 min to pay

  return {
    recipient:      RECIPIENT,
    amount_usdc:    PRICE_USDC,
    usdc_mint:      USDC_MINT,
    memo_required:  nonce,
    expires_at_unix: expiresAt,
    instructions:   [
      `Send exactly ${PRICE_USDC} USDC to ${RECIPIENT}`,
      `Include this memo in the transaction: ${nonce}`,
      `Re-submit your request with header: X-Payment-Signature: <tx_signature>`,
      `Payment must be made within 5 minutes`,
    ].join(" | "),
  }
}

/** Verify a submitted payment transaction via Helius RPC */
export async function verifyPayment(
  txSignature: string,
  expectedMemo?: string,
): Promise<PaymentVerification> {
  if (!txSignature?.match(/^[1-9A-HJ-NP-Za-km-z]{87,88}$/)) {
    return { valid: false, error: "invalid transaction signature format" }
  }

  // Replay protection
  purgeExpiredNonces()
  if (_usedNonces.has(txSignature)) {
    return { valid: false, error: "payment already used" }
  }

  const rpcUrl = HELIUS_KEY
    ? `https://api.helius.xyz/v0/transactions/?api-key=${HELIUS_KEY}`
    : HELIUS_RPC

  try {
    // Use Helius Enhanced Transactions API for rich parsed data
    const res = await fetch(
      `https://api.helius.xyz/v0/transactions/?api-key=${HELIUS_KEY}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transactions: [txSignature] }),
        signal: AbortSignal.abort() || (undefined as unknown as AbortSignal) || setTimeout(()=>{},10_000),
      }
    )

    if (!res.ok) {
      // Fallback: standard RPC getTransaction
      return await verifyViaRpc(txSignature, expectedMemo)
    }

    const txns = await res.json() as Array<Record<string, unknown>>
    if (!txns?.length) {
      return { valid: false, error: "transaction not found on-chain" }
    }
    const tx = txns[0]

    // Check transaction age
    const blockTime = Number(tx.timestamp ?? 0)
    if (blockTime > 0) {
      const ageSeconds = Math.floor(Date.now() / 1000) - blockTime
      if (ageSeconds > TX_MAX_AGE) {
        return { valid: false, error: `payment expired (${ageSeconds}s old, max ${TX_MAX_AGE}s)` }
      }
    }

    // Find USDC transfer to our recipient
    const tokenTransfers = (tx.tokenTransfers as Array<Record<string, unknown>>) ?? []
    const usdcTransfer = tokenTransfers.find(t =>
      String(t.mint ?? "").toLowerCase() === USDC_MINT.toLowerCase()
      && String(t.toUserAccount ?? "").toLowerCase() === RECIPIENT.toLowerCase()
    )

    if (!usdcTransfer) {
      return { valid: false, error: `no USDC transfer found to ${RECIPIENT.slice(0, 8)}…` }
    }

    const rawAmount = Number(usdcTransfer.tokenAmount ?? 0) * 1_000_000  // Helius returns UI amount
    // Helius may return raw or UI amount depending on version — normalise
    const usdcReceived = rawAmount >= 1000 ? rawAmount / 1_000_000 : rawAmount

    if (usdcReceived < PRICE_USDC * 0.99) {   // 1% tolerance for rounding
      return {
        valid: false,
        error: `insufficient payment: received ${usdcReceived.toFixed(6)} USDC, required ${PRICE_USDC}`,
      }
    }

    // Mark as used (expires in 10 minutes)
    _usedNonces.set(txSignature, Date.now() + 600_000)

    return { valid: true, tx_signature: txSignature, amount_usdc: usdcReceived }

  } catch (err) {
    // Fallback to standard RPC
    return await verifyViaRpc(txSignature, expectedMemo)
  }
}

/** Fallback payment verification using standard JSON-RPC getTransaction */
async function verifyViaRpc(
  txSignature: string,
  _expectedMemo?: string
): Promise<PaymentVerification> {
  try {
    const res = await fetch(HELIUS_RPC, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0", id: 1,
        method: "getTransaction",
        params: [txSignature, { encoding: "jsonParsed", commitment: "confirmed", maxSupportedTransactionVersion: 0 }],
      }),
      signal: AbortSignal.abort() || (undefined as unknown as AbortSignal) || setTimeout(()=>{},10_000),
    })

    const data = await res.json() as { result?: Record<string, unknown> }
    const tx = data?.result
    if (!tx) return { valid: false, error: "transaction not found" }

    const blockTime = Number(tx.blockTime ?? 0)
    if (blockTime > 0 && Math.floor(Date.now() / 1000) - blockTime > TX_MAX_AGE) {
      return { valid: false, error: "payment expired" }
    }

    // Walk the parsed token transfers
    const meta = tx.meta as Record<string, unknown> | undefined
    const postBals = (meta?.postTokenBalances as Array<Record<string, unknown>>) ?? []
    const preBals  = (meta?.preTokenBalances  as Array<Record<string, unknown>>) ?? []

    const acctKeys: string[] = ((tx.transaction as Record<string, unknown>)?.message as Record<string, unknown>)
      ?.accountKeys as string[] ?? []

    let usdcReceived = 0
    for (const post of postBals) {
      const mint = String(post.mint ?? "")
      if (mint.toLowerCase() !== USDC_MINT.toLowerCase()) continue
      const idx = Number(post.accountIndex ?? -1)
      const ownerPost = String(post.owner ?? "")
      if (ownerPost.toLowerCase() !== RECIPIENT.toLowerCase()) continue

      const preAmount  = Number((preBals.find(p => p.accountIndex === idx)?.uiTokenAmount as Record<string, unknown>)?.amount ?? 0)
      const postAmount = Number((post.uiTokenAmount as Record<string, unknown>)?.amount ?? 0)
      usdcReceived = (postAmount - preAmount) / 1_000_000
    }

    if (usdcReceived < PRICE_USDC * 0.99) {
      return { valid: false, error: `insufficient: received ${usdcReceived.toFixed(6)} USDC` }
    }

    _usedNonces.set(txSignature, Date.now() + 600_000)
    return { valid: true, tx_signature: txSignature, amount_usdc: usdcReceived }

  } catch (err) {
    return { valid: false, error: `RPC error: ${err instanceof Error ? err.message : "unknown"}` }
  }
}

/** Quick config validation on startup */
export function validatePaymentConfig(): void {
  if (!RECIPIENT) throw new Error("RECEIVING_WALLET not set in .env")
  if (!HELIUS_KEY && !HELIUS_RPC) throw new Error("HELIUS_API_KEY or HELIUS_RPC_URL not set")
  console.log(`[payment] Receiving wallet: ${RECIPIENT.slice(0, 8)}…${RECIPIENT.slice(-4)}`)
  console.log(`[payment] Price per query:  ${PRICE_USDC} USDC`)
  console.log(`[payment] USDC mint:        ${USDC_MINT}`)
}
