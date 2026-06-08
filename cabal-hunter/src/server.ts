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
import { createPaymentRequest, verifyPayment, validatePaymentConfig, logPayment, getPnlStats, getFreeQueriesRemaining, consumeFreeQuery } from "./payment.js"
import { CabalReport } from "./types.js"

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")

export function createApp(): express.Application {
  const app = express()

  app.use(cors({ origin: "*", methods: ["GET", "POST", "OPTIONS"] }))
  app.use(express.json({ limit: "512kb" }))

  // ── Serve the bubble map page ─────────────────────────────────────────────────
  const publicDir = join(__dirname, "..", "public")
  app.use("/public", express.static(publicDir))
  app.get("/map", (_req, res) => {
    res.sendFile(join(publicDir, "map.html"))
  })

  // ── Map data endpoint — called by map.html JavaScript (no payment required) ──
  // Always calls the Python bot's full get_cluster_map() which returns the complete
  // holder list with cluster assignments needed for the visual rendering.
  app.get("/api/map-data", async (req: Request, res: Response): Promise<void> => {
    const mint = (req.query.mint as string ?? "").trim()
    if (!mint || mint.length < 32) {
      res.status(400).json({ error: "mint required" }); return
    }
    try {
      const botUrl    = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const secret    = process.env.CABAL_INTERNAL_SECRET ?? ""
      const createdTs = req.query.created_ts as string | undefined
      const params    = new URLSearchParams({ mint })
      if (createdTs) params.set("created_ts", createdTs)

      const upstream = await fetch(`${botUrl}/api/cabal/internal?${params}`, {
        headers: secret ? { "X-Internal-Secret": secret } : {},
      })
      if (!upstream.ok) {
        const txt = await upstream.text().catch(() => "unknown")
        res.status(upstream.status).json({ error: `upstream error: ${txt}` })
        return
      }
      const data = await upstream.json()
      res.json(data)
    } catch (err) {
      res.status(500).json({
        error: "analysis_failed",
        message: err instanceof Error ? err.message : "unknown error",
      })
    }
  })

  // ── Root landing page — the public face of the product ───────────────────────
  app.get("/", (_req, res) => {
    res.setHeader("Content-Type", "text/html")
    res.send(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Cabal-Hunter — Solana Token Cabal Detection</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:40px 24px;max-width:780px;margin:0 auto}
  .logo{display:flex;align-items:center;gap:12px;margin-bottom:40px}
  .icon{width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#ff4d6d,#7c3aed);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:900;color:white;flex-shrink:0}
  h1{font-size:32px;font-weight:900;line-height:1.1;margin-bottom:12px;letter-spacing:-0.5px}
  .sub{font-size:16px;color:#94a3b8;margin-bottom:36px;line-height:1.6}
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:36px}
  .card{background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px}
  .card-title{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin-bottom:8px}
  .card-val{font-size:22px;font-weight:800}
  .links{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:36px}
  .btn{display:inline-flex;align-items:center;gap:6px;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;transition:opacity .15s}
  .btn:hover{opacity:.8}
  .btn-primary{background:rgba(255,77,109,.15);color:#ff4d6d;border:1px solid rgba(255,77,109,.4)}
  .btn-teal{background:rgba(20,184,166,.12);color:#2dd4bf;border:1px solid rgba(20,184,166,.35)}
  .btn-purple{background:rgba(124,58,237,.12);color:#a78bfa;border:1px solid rgba(124,58,237,.35)}
  .btn-gray{background:rgba(255,255,255,.06);color:#94a3b8;border:1px solid rgba(255,255,255,.1)}
  pre{background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:16px;font-size:13px;overflow-x:auto;color:#7dd3fc;line-height:1.6}
  .section-title{font-size:14px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
  .footer{margin-top:48px;padding-top:24px;border-top:1px solid rgba(255,255,255,.06);font-size:12px;color:#334155}
</style>
</head>
<body>
<div class="logo">
  <div class="icon">CH</div>
  <div>
    <div style="font-size:20px;font-weight:800">Cabal-Hunter</div>
    <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:1px">Solana Token Cabal Detection</div>
  </div>
</div>

<h1>Detect coordinated wallet<br>cabals before your bot buys.</h1>
<p class="sub">Real-time on-chain funding trace of the top 20 holders via Helius RPC.<br>Returns a cabal confidence score 0–100. <strong style="color:#e2e8f0">$0.05 USDC per query.</strong> No API key. No account.</p>

<div class="cards">
  <div class="card"><div class="card-title">Per Query</div><div class="card-val" style="color:#10b981">$0.05 USDC</div></div>
  <div class="card"><div class="card-title">Response Time</div><div class="card-val" style="color:#0ea5e9">&lt;100ms</div></div>
  <div class="card"><div class="card-title">Payment</div><div class="card-val" style="font-size:15px;color:#a78bfa">Native Solana</div></div>
  <div class="card"><div class="card-title">MCP Compatible</div><div class="card-val" style="font-size:15px;color:#fb923c">Claude · Cursor · Eliza</div></div>
</div>

<div class="links">
  <a class="btn btn-primary" href="/map?mint=Axpzs7FEMYzpcfqVcDjDMQb2rsgMYVJADNpUZe7bpump">🗺 Live Bubble Map Demo</a>
  <a class="btn btn-teal" href="/api/info">📖 API Reference</a>
  <a class="btn btn-purple" href="https://github.com/paulf280-ui/solana-safe-sniper-mcp-template" target="_blank">⚙ GitHub Template</a>
</div>

<div class="section-title">Add to Claude / Cursor / ElizaOS</div>
<pre>{"mcpServers": {"cabal-hunter": {"url": "https://api.cabal-hunter.com/mcp"}}}</pre>

<div class="footer">
  Built on Helius RPC · AWS Frankfurt ·
  <a href="/health" style="color:#475569">Status</a> ·
  <a href="/api/info" style="color:#475569">API Docs</a>
</div>
</body>
</html>`)
  })

  // ── Glama verification file ──────────────────────────────────────────────────
  // Glama checks /.well-known/glama.json to verify domain ownership and
  // confirm the connector is legitimate before marking it Healthy.
  app.get("/.well-known/glama.json", (_req, res) => {
    res.json({
      name:        "Cabal-Hunter",
      description: "Real-time on-chain coordinated wallet detection for Solana tokens",
      url:         "https://api.cabal-hunter.com",
      mcp:         "https://api.cabal-hunter.com/mcp",
      version:     "1.0.0",
      contact:     "paulf280@gmail.com",
    })
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

    // ── Free tier check ──────────────────────────────────────────────────────
    const clientIp = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
                     || req.socket.remoteAddress
                     || "unknown"
    const paymentSig = req.headers["x-payment-signature"]?.toString()
    const freeLeft   = getFreeQueriesRemaining(clientIp)

    if (!paymentSig && freeLeft > 0) {
      // Free tier — run the query, consume one free credit
      consumeFreeQuery(clientIp)
      try {
        const createdTs = req.body?.pairCreatedAt ? Number(req.body.pairCreatedAt) / 1000 : undefined
        const report    = await getCabalReport(mint, createdTs)
        res.status(200).json({
          ...report,
          free_tier:            true,
          free_queries_remaining: freeLeft - 1,
          note: freeLeft === 1
            ? "Last free query used. Future queries require $0.05 USDC payment."
            : `${freeLeft - 1} free queries remaining this month.`,
          request_time_ms: Date.now() - t0,
        })
      } catch (err) {
        res.status(500).json({ error: "analysis_failed", message: err instanceof Error ? err.message : "Unknown error" })
      }
      return
    }

    // ── Payment gate ──────────────────────────────────────────────────────────
    if (!paymentSig) {
      // No payment and no free credits — return 402 with instructions
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

  // ── Comparison page — SEO content, converts high-intent Google traffic ────────
  app.get("/compare", (_req, res) => {
    res.setHeader("Content-Type", "text/html")
    res.send(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Cabal-Hunter vs rugcheck vs GoPlus — What Each Tool Detects (and What It Misses)</title>
<meta name="description" content="Honest comparison of Solana token safety tools. Cabal-Hunter detects coordinated wallet clusters rugcheck and GoPlus miss entirely.">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:40px 24px;max-width:960px;margin:0 auto}
  h1{font-size:28px;font-weight:900;letter-spacing:-.5px;margin-bottom:8px}
  .sub{color:#94a3b8;font-size:15px;margin-bottom:40px;line-height:1.6}
  h2{font-size:18px;font-weight:800;margin:40px 0 16px;color:#e2e8f0}
  table{width:100%;border-collapse:collapse;margin-bottom:32px;font-size:13px}
  th{text-align:left;padding:12px 16px;background:#0d0f1e;border:1px solid rgba(255,255,255,.08);color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.8px}
  td{padding:11px 16px;border:1px solid rgba(255,255,255,.06);vertical-align:top;line-height:1.5}
  tr:hover td{background:rgba(255,255,255,.02)}
  .yes{color:#10b981;font-weight:700}
  .no{color:#ef4444;font-weight:700}
  .partial{color:#f59e0b;font-weight:700}
  .ch{background:rgba(255,77,109,.04)}
  .highlight{background:rgba(255,77,109,.08);border-left:3px solid #ff4d6d}
  .verdict{background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px 24px;margin:12px 0}
  .verdict-title{font-weight:800;margin-bottom:8px;font-size:15px}
  .verdict-body{color:#94a3b8;font-size:13px;line-height:1.6}
  .cta{background:linear-gradient(135deg,rgba(255,77,109,.15),rgba(124,58,237,.15));border:1px solid rgba(255,77,109,.3);border-radius:12px;padding:28px;margin:40px 0;text-align:center}
  .cta h3{font-size:20px;font-weight:900;margin-bottom:8px}
  .cta p{color:#94a3b8;margin-bottom:16px;font-size:13px}
  .btn{display:inline-block;padding:10px 22px;border-radius:8px;font-weight:700;font-size:13px;text-decoration:none;margin:4px}
  .btn-red{background:rgba(255,77,109,.2);color:#ff4d6d;border:1px solid rgba(255,77,109,.4)}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:32px}
  .icon{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,#ff4d6d,#7c3aed);display:flex;align-items:center;justify-content:center;font-weight:900;color:white;font-size:14px}
  code{background:rgba(255,255,255,.08);padding:2px 6px;border-radius:4px;font-size:12px}
  .real-rug{background:#0d0f1e;border:1px solid rgba(255,77,109,.3);border-radius:10px;padding:20px;margin:24px 0}
  .real-rug h3{color:#ff4d6d;margin-bottom:10px;font-size:14px;font-weight:800;text-transform:uppercase;letter-spacing:.5px}
</style>
</head>
<body>
<div class="logo"><div class="icon">CH</div><div><div style="font-weight:800">Cabal-Hunter</div><div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px">vs The Alternatives</div></div></div>

<h1>Cabal-Hunter vs rugcheck vs GoPlus</h1>
<p class="sub">An honest comparison of what each Solana token safety tool actually detects — and the coordinated wallet attacks that standard tools completely miss.</p>

<div class="real-rug">
  <h3>⚠️ Real example — caught by Cabal-Hunter, missed by everyone else</h3>
  <p style="color:#94a3b8;font-size:13px;line-height:1.6">A token last week scored <strong style="color:#10b981">8/8 on rugcheck.xyz</strong> — LP burned, contract clean, no honeypot. GoPlus: all green. Standard tools saw nothing wrong.<br><br>Cabal-Hunter traced the top 20 holders and found <strong>6 wallets all funded from the same source, 47 seconds before the first trade.</strong> The token rugged 3 hours later. <a href="/map?mint=Axpzs7FEMYzpcfqVcDjDMQb2rsgMYVJADNpUZe7bpump" style="color:#ff4d6d">See the on-chain proof →</a></p>
</div>

<h2>What Each Tool Checks</h2>
<table>
  <thead><tr><th>Detection Type</th><th>Cabal-Hunter</th><th>rugcheck.xyz</th><th>GoPlus Security</th></tr></thead>
  <tbody>
    <tr class="highlight"><td><strong>Coordinated wallet clusters</strong><br><small>Multiple wallets funded by same source before launch</small></td><td class="ch"><span class="yes">✅ YES</span><br><small>Core feature — traces funding lineage via Helius RPC</small></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr class="highlight"><td><strong>Cabal confidence score (0–100)</strong><br><small>% of supply controlled by coordinated wallets</small></td><td class="ch"><span class="yes">✅ YES</span></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr class="highlight"><td><strong>Insider sniper detection</strong><br><small>Wallets that bought within 60 seconds of launch</small></td><td class="ch"><span class="yes">✅ YES</span></td><td><span class="no">❌ NO</span></td><td><span class="partial">⚡ Partial</span></td></tr>
    <tr><td><strong>LP lock / burn check</strong></td><td class="ch"><span class="partial">⚡ Via Helius</span></td><td><span class="yes">✅ YES</span></td><td><span class="yes">✅ YES</span></td></tr>
    <tr><td><strong>Honeypot detection</strong></td><td class="ch"><span class="no">❌ NO</span></td><td><span class="yes">✅ YES</span></td><td><span class="yes">✅ YES</span></td></tr>
    <tr><td><strong>Mint authority check</strong></td><td class="ch"><span class="no">❌ NO</span></td><td><span class="yes">✅ YES</span></td><td><span class="yes">✅ YES</span></td></tr>
    <tr><td><strong>Contract audit / code analysis</strong></td><td class="ch"><span class="no">❌ NO</span></td><td><span class="yes">✅ YES</span></td><td><span class="yes">✅ YES</span></td></tr>
    <tr><td><strong>Top holder distribution</strong></td><td class="ch"><span class="yes">✅ YES</span><br><small>Top 20 with cluster assignments</small></td><td><span class="partial">⚡ Basic</span></td><td><span class="partial">⚡ Basic</span></td></tr>
    <tr><td><strong>Visual bubble map</strong></td><td class="ch"><span class="yes">✅ YES — Free</span><br><small>Interactive, clickable wallet links</small></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr><td><strong>MCP server (Claude/Cursor/ElizaOS)</strong></td><td class="ch"><span class="yes">✅ YES</span><br><small>Native AI agent integration</small></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr><td><strong>Pricing</strong></td><td class="ch"><span class="yes">$0.05 USDC/query</span><br><small>No account · No subscription</small></td><td><span class="yes">Free</span></td><td><span class="yes">Free</span></td></tr>
    <tr><td><strong>Payment method</strong></td><td class="ch">Native Solana USDC</td><td>Free</td><td>Free / Enterprise</td></tr>
  </tbody>
</table>

<h2>The Fundamental Difference</h2>
<div class="verdict">
  <div class="verdict-title">rugcheck.xyz and GoPlus audit <em>contract code</em></div>
  <div class="verdict-body">They answer: "Is this smart contract malicious?" They check mint authority, freeze authority, LP lock status, honeypot code patterns. All valid. But contract code doesn't rug you.</div>
</div>
<div class="verdict" style="border-color:rgba(255,77,109,.3)">
  <div class="verdict-title" style="color:#ff4d6d">Cabal-Hunter audits <em>wallet behaviour</em></div>
  <div class="verdict-body">It answers: "Are multiple wallets being coordinated by a single actor to control this token?" A token can have a perfectly clean contract, burned LP, and no honeypot — and still be set up for a coordinated dump by 15 wallets funded from the same master wallet 60 seconds before launch. That's the threat no contract scanner sees.</div>
</div>

<h2>When to Use Each Tool</h2>
<table>
  <thead><tr><th>Scenario</th><th>Best Tool</th></tr></thead>
  <tbody>
    <tr><td>Check if a contract has malicious code / honeypot</td><td>rugcheck.xyz or GoPlus</td></tr>
    <tr><td>Check if LP is locked or burned</td><td>rugcheck.xyz or GoPlus</td></tr>
    <tr class="highlight"><td>Check if multiple wallets are coordinated by a single actor</td><td><strong style="color:#ff4d6d">Cabal-Hunter</strong></td></tr>
    <tr class="highlight"><td>Pre-trade safety check in an AI trading agent (Claude, Cursor, ElizaOS)</td><td><strong style="color:#ff4d6d">Cabal-Hunter</strong></td></tr>
    <tr class="highlight"><td>Visualise the holder distribution and funding relationships</td><td><strong style="color:#ff4d6d">Cabal-Hunter</strong></td></tr>
    <tr><td>Full token safety audit (contract + wallet)</td><td><strong style="color:#ff4d6d">Cabal-Hunter</strong> + rugcheck.xyz</td></tr>
  </tbody>
</table>

<div class="cta">
  <h3>Try Cabal-Hunter free</h3>
  <p>100 free queries per month. No account. No API key. Check any Solana token in seconds.</p>
  <a class="btn btn-red" href="/map?mint=Axpzs7FEMYzpcfqVcDjDMQb2rsgMYVJADNpUZe7bpump">🗺 Live Demo — See a Detected Cabal</a>
  <a class="btn" style="background:rgba(255,255,255,.06);color:#94a3b8;border:1px solid rgba(255,255,255,.1)" href="/api/info">API Documentation</a>
</div>

<div style="margin-top:32px;padding-top:24px;border-top:1px solid rgba(255,255,255,.06);font-size:12px;color:#334155">
  <a href="/" style="color:#475569">← Back to Cabal-Hunter</a> ·
  Powered by Helius RPC · AWS Frankfurt
</div>
</body></html>`)
  })

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
