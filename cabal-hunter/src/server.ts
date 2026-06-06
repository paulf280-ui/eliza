/**
 * server.ts — Express REST API with native USDC 402 payment middleware.
 *
 * Endpoints:
 *   GET  /health          — uptime check (no auth)
 *   POST /api/scan-cabal  — paid endpoint, $0.05 USDC per query
 *   GET  /api/scan-cabal  — same, for GET-friendly clients
 *
 * Payment flow:
 *   1. Client sends request without X-Payment-Signature header
 *   2. Server returns HTTP 402 with JSON payment instructions
 *   3. Client sends USDC, gets transaction signature
 *   4. Client re-submits with X-Payment-Signature: <sig>
 *   5. Server verifies on-chain, runs analysis, returns 200 + report
 */

import cors from "cors"
import express, { NextFunction, Request, Response } from "express"
import crypto from "crypto"
import { getCabalReport } from "./detector.js"
import { createPaymentRequest, verifyPayment, validatePaymentConfig } from "./payment.js"
import { CabalReport } from "./types.js"

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")

export function createApp(): express.Application {
  const app = express()

  app.use(cors({ origin: "*", methods: ["GET", "POST", "OPTIONS"] }))
  app.use(express.json({ limit: "512kb" }))

  // ── Health check ─────────────────────────────────────────────────────────────
  app.get("/health", (_req, res) => {
    res.json({
      status:  "ok",
      service: "cabal-hunter",
      version: "1.0.0",
      time:    new Date().toISOString(),
    })
  })

  // ── Main analysis endpoint ────────────────────────────────────────────────────
  const handleScan = async (req: Request, res: Response): Promise<void> => {
    const t0 = Date.now()

    // Extract mint from body (POST) or query (GET)
    const mintRaw = (req.body?.mintAddress ?? req.query.mintAddress ?? "").toString().trim()
    const mint    = mintRaw.replace(/[^1-9A-HJ-NP-Za-km-z]/g, "")

    if (!mint || mint.length < 32 || mint.length > 44) {
      res.status(400).json({
        error: "invalid_mint",
        message: "mintAddress must be a valid Solana public key (32–44 base58 chars)",
      })
      return
    }

    // ── Payment gate ──────────────────────────────────────────────────────────
    const paymentSig = req.headers["x-payment-signature"]?.toString()

    if (!paymentSig) {
      // No payment — return 402 with instructions
      const queryId = crypto.randomUUID()
      const payment = createPaymentRequest(queryId)
      res.status(402).json({
        error:          "payment_required",
        message:        `This endpoint requires ${PRICE_USDC} USDC per query on Solana mainnet.`,
        query_id:       queryId,
        payment:        payment,
        how_to_pay: [
          `1. Send ${PRICE_USDC} USDC to ${payment.recipient}`,
          `2. Include this memo: ${payment.memo_required}`,
          `3. Re-submit this request with header: X-Payment-Signature: <your_tx_signature>`,
        ],
        docs: "https://github.com/your-repo/cabal-hunter",
      })
      return
    }

    // ── Verify payment ────────────────────────────────────────────────────────
    const verification = await verifyPayment(paymentSig)
    if (!verification.valid) {
      res.status(402).json({
        error:   "payment_invalid",
        message: verification.error ?? "Payment verification failed",
        retry:   "Ensure the USDC transaction is confirmed on Solana mainnet and re-submit",
      })
      return
    }

    // ── Run analysis ──────────────────────────────────────────────────────────
    try {
      const createdTs = req.body?.pairCreatedAt
        ? Number(req.body.pairCreatedAt) / 1000
        : undefined

      const report = await getCabalReport(mint, createdTs)

      res.status(200).json({
        ...report,
        payment_received_usdc: verification.amount_usdc,
        payment_tx: verification.tx_signature,
        request_time_ms: Date.now() - t0,
      })
    } catch (err) {
      console.error("[server] analysis error:", err)
      res.status(500).json({
        error:   "analysis_failed",
        message: err instanceof Error ? err.message : "Internal server error",
        note:    "Your payment was received. Contact support for a refund if this persists.",
      })
    }
  }

  app.post("/api/scan-cabal", handleScan)
  app.get("/api/scan-cabal",  handleScan)

  // ── Info endpoint (no auth) ───────────────────────────────────────────────────
  app.get("/api/info", (_req, res) => {
    res.json({
      service:         "Cabal-Hunter",
      description:     "On-chain coordinated wallet detection for Solana meme tokens",
      price_per_query: `${PRICE_USDC} USDC`,
      payment_method:  "Solana SPL USDC transfer + X-Payment-Signature header",
      mcp_endpoint:    "/mcp",
      rest_endpoint:   "/api/scan-cabal",
      example_request: {
        method: "POST",
        url:    "/api/scan-cabal",
        headers: { "X-Payment-Signature": "<solana_tx_sig>" },
        body:   { mintAddress: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" },
      },
    })
  })

  // ── Global error handler ──────────────────────────────────────────────────────
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    console.error("[server] unhandled error:", err)
    res.status(500).json({ error: "internal_error", message: err.message })
  })

  return app
}
