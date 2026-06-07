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
import { fileURLToPath } from "url"
import { dirname, join } from "path"

const __filename = fileURLToPath(import.meta.url)
const __dirname  = dirname(__filename)
import { getCabalReport } from "./detector.js"
import { createPaymentRequest, verifyPayment, validatePaymentConfig, logPayment, getPnlStats } from "./payment.js"
import { CabalReport } from "./types.js"

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")

export function createApp(): express.Application {
  const app = express()

  app.use(cors({ origin: "*", methods: ["GET", "POST", "OPTIONS"] }))
  app.use(express.json({ limit: "512kb" }))

  // ── Serve the bubble map page ─────────────────────────────────────────────────
  // Free to VIEW — great marketing, shows what the tool returns
  // GET /map?mint=<address>
  const publicDir = join(__dirname, "..", "public")
  app.use("/public", express.static(publicDir))
  app.get("/map", (_req, res) => {
    res.sendFile(join(publicDir, "map.html"))
  })

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

      // Log the payment for P&L tracking
      if (verification.tx_signature) {
        logPayment(verification.tx_signature, mint, verification.amount_usdc ?? PRICE_USDC)
      }

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

  // ── P&L stats endpoint (protected by internal secret) ────────────────────────
  app.get("/admin/pnl", (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    if (secret && req.headers["x-internal-secret"] !== secret) {
      // Also allow direct browser access from the same machine via query param
      if (req.query.key !== secret) {
        res.status(401).json({ error: "unauthorized" }); return
      }
    }
    res.json(getPnlStats())
  })

  // ── P&L dashboard page ────────────────────────────────────────────────────────
  app.get("/admin/dashboard", (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    const key    = req.query.key as string ?? ""
    if (secret && key !== secret) {
      res.status(401).send("<h1>Unauthorized</h1>"); return
    }
    res.send(buildPnlPage(key))
  })

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
  // (buildPnlPage defined below the app factory)
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    console.error("[server] unhandled error:", err)
    res.status(500).json({ error: "internal_error", message: err.message })
  })

  return app
}

// ── P&L Dashboard HTML ────────────────────────────────────────────────────────
function buildPnlPage(key: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Cabal-Hunter — P&L Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:24px}
  h1{font-size:20px;font-weight:800;margin-bottom:4px}
  .sub{color:#64748b;font-size:12px;margin-bottom:28px;letter-spacing:.5px}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}
  .card{background:#0d0f1e;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:18px 20px}
  .card-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  .card-val{font-size:28px;font-weight:800}
  .card-sub{font-size:12px;color:#64748b;margin-top:6px}
  .green{color:#10b981}.amber{color:#f59e0b}.blue{color:#0ea5e9}.purple{color:#7c3aed}
  table{width:100%;border-collapse:collapse;background:#0d0f1e;border-radius:12px;overflow:hidden;border:1px solid rgba(255,255,255,.07)}
  th{text-align:left;padding:12px 16px;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid rgba(255,255,255,.07)}
  td{padding:11px 16px;font-size:13px;border-bottom:1px solid rgba(255,255,255,.04)}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.02)}
  .mono{font-family:monospace;color:#94a3b8}
  .section-title{font-size:13px;font-weight:700;margin:24px 0 12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}
  .refresh{font-size:11px;color:#334155;margin-top:20px;text-align:center}
  .header{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .brand-icon{width:28px;height:28px;border-radius:7px;background:linear-gradient(135deg,#ff4d6d,#7c3aed);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:900;color:white}
</style>
</head>
<body>
<div class="header">
  <div class="brand-icon">CH</div>
  <h1>Cabal-Hunter P&amp;L</h1>
</div>
<div class="sub">Revenue dashboard · Cabal Risk Tool wallet · Auto-refreshes every 60s</div>

<div class="grid" id="grid"><div class="card"><div class="card-label">Loading…</div></div></div>
<div class="section-title">Top Queried Tokens</div>
<table><thead><tr><th>#</th><th>Mint Address</th><th>Queries</th><th>Revenue</th></tr></thead>
<tbody id="top-table"><tr><td colspan="4" style="color:#64748b;text-align:center;padding:24px">Loading…</td></tr></tbody>
</table>
<div class="refresh" id="refresh-label">—</div>

<script>
async function load(){
  try{
    const r=await fetch('/admin/pnl?key=${key}');
    const d=await r.json();
    if(d.error){document.getElementById('grid').innerHTML='<div class="card"><div class="card-label">Error</div><div class="card-val" style="color:#ff4d6d;font-size:14px">'+d.error+'</div></div>';return;}
    document.getElementById('grid').innerHTML=\`
      <div class="card"><div class="card-label">Today</div>
        <div class="card-val green">\${(d.today?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.today?.queries||0} queries</div></div>
      <div class="card"><div class="card-label">This Week</div>
        <div class="card-val blue">\${(d.week?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.week?.queries||0} queries</div></div>
      <div class="card"><div class="card-label">This Month</div>
        <div class="card-val amber">\${(d.month?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.month?.queries||0} queries</div></div>
      <div class="card"><div class="card-label">All Time</div>
        <div class="card-val purple">\${(d.total?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.total?.queries||0} total queries</div></div>
    \`;
    const tops=d.top_mints||[];
    document.getElementById('top-table').innerHTML=tops.length?tops.map((t,i)=>\`
      <tr><td style="color:#64748b">\${i+1}</td>
      <td class="mono">\${t.mint_queried}</td>
      <td style="font-weight:700">\${t.cnt}</td>
      <td class="green">\${(t.cnt*0.05).toFixed(4)} USDC</td></tr>\`).join('')
      :'<tr><td colspan="4" style="color:#64748b;text-align:center;padding:20px">No queries yet — share the API to start earning</td></tr>';
    document.getElementById('refresh-label').textContent='Last updated: '+new Date().toLocaleTimeString()+' · Auto-refreshes every 60s';
  }catch(e){console.error(e)}
}
load(); setInterval(load,60000);
</script>
</body></html>`
}
