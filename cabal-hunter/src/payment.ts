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
import { PaymentRequest, PaymentVerification } from "./types.js"

const HELIUS_RPC  = process.env.HELIUS_RPC_URL ?? ""
const HELIUS_KEY  = process.env.HELIUS_API_KEY ?? ""
const RECIPIENT   = process.env.RECEIVING_WALLET ?? ""
const USDC_MINT   = process.env.USDC_MINT ?? "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
const PRICE_USDC  = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")
const PRICE_RAW   = Math.round(PRICE_USDC * 1_000_000)   // USDC has 6 decimals
const TX_MAX_AGE  = 120   // seconds — reject old payment proofs

// In-memory nonce store (nonce → expiry_ms). Prevents replay attacks.
// In production with multiple instances, use Redis. Single EC2 = Map is fine.
const _usedNonces = new Map<string, number>()

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
        signal: AbortSignal.timeout(10_000),
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
      signal: AbortSignal.timeout(10_000),
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
