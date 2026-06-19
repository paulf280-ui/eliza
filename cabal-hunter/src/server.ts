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
import { getCabalReport, getFreshDemoMint } from "./detector.js"
import { recordVisit, getAnalytics, excludeIp } from "./analytics.js"
import { createPaymentRequest, verifyPayment, validatePaymentConfig, logPayment, getPnlStats, getFreeQueriesRemaining, consumeFreeQuery, recordMapScan, MAP_SCAN_LIMIT } from "./payment.js"
import { CabalReport } from "./types.js"

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")

export function createApp(): express.Application {
  const app = express()

  app.use(cors({ origin: "*", methods: ["GET", "POST", "OPTIONS"] }))
  app.use(express.json({ limit: "512kb" }))

  // ── Outbound-click beacon (which external tools / links visitors jump to) ────
  // navigator.sendBeacon() always POSTs, so accept both GET and POST.
  const clickHandler = (req: Request, res: Response) => {
    const to = (req.query.to as string ?? "").slice(0, 40).replace(/[^a-zA-Z0-9_-]/g, "")
    const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim() || req.socket.remoteAddress || ""
    if (to) recordVisit({ category: "click", mint: to, ip, referer: req.headers["referer"] as string })
    res.status(204).end()
  }
  app.get("/click", clickHandler)
  app.post("/click", clickHandler)

  // ── Visit analytics (Cloudflare can't see us — we're DNS-only) ───────────────
  app.use((req, _res, next) => {
    try {
      const p = req.path
      // Don't log admin, health, static, or internal-noise paths
      if (p.startsWith("/admin") || p === "/health" || p.startsWith("/public") ||
          p === "/favicon.ico" || p.startsWith("/.well-known") || p === "/click") return next()
      let category = "other"
      let mint: string | undefined
      if (p === "/" || p === "/demo" || p === "/compare") category = "landing"
      else if (p === "/map") { category = "map"; mint = (req.query.mint as string)?.slice(0, 44) }
      else if (p.startsWith("/api/")) {
        category = "api"
        mint = ((req.query.mint as string) || (req.query.mintAddress as string))?.slice(0, 44)
      }
      const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
                 || req.socket.remoteAddress || ""
      recordVisit({ category, mint, ip, referer: req.headers["referer"] as string })
    } catch { /* never break a request on analytics */ }
    next()
  })

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
      if (req.query.fresh === "1") params.set("fresh", "1")

      const upstream = await fetch(`${botUrl}/api/cabal/internal?${params}`, {
        headers: secret ? { "X-Internal-Secret": secret } : {},
      })
      if (!upstream.ok) {
        const txt = await upstream.text().catch(() => "unknown")
        res.status(upstream.status).json({ error: `upstream error: ${txt}` })
        return
      }
      const data = await upstream.json() as Record<string, unknown>
      // Meter this manual scan. Map stays free (soft nudge) — but once a user
      // crosses the monthly threshold they clearly rely on it, so prompt them to
      // integrate the API/MCP into their bot rather than pasting mints by hand.
      const clientIp = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
                       || req.socket.remoteAddress || ""
      const scans = recordMapScan(clientIp)
      data._scans_this_month = scans
      if (scans > MAP_SCAN_LIMIT) {
        data._integrate_prompt = {
          scans,
          limit: MAP_SCAN_LIMIT,
          message: `You've run ${scans} scans this month — you clearly rely on this. Wire it straight into your bot with the API/MCP so you stop pasting mints by hand.`,
          docs: "/api/info",
        }
      }
      res.json(data)
    } catch (err) {
      res.status(500).json({
        error: "analysis_failed",
        message: err instanceof Error ? err.message : "unknown error",
      })
    }
  })

  // ── CEX funding breakdown proxy (lazy — map loads this after the main scan) ──
  app.get("/api/cex-funding", async (req: Request, res: Response): Promise<void> => {
    const mint = (req.query.mint as string ?? "").trim()
    if (!mint || mint.length < 32) { res.status(400).json({ error: "mint required" }); return }
    try {
      const botUrl = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
      const upstream = await fetch(`${botUrl}/api/cex/internal?mint=${encodeURIComponent(mint)}`, {
        headers: secret ? { "X-Internal-Secret": secret } : {},
      })
      const data = await upstream.json()
      res.status(upstream.ok ? 200 : upstream.status).json(data)
    } catch (err) {
      res.status(500).json({ error: "cex_lookup_failed", message: err instanceof Error ? err.message : "unknown" })
    }
  })

  // ── Cohort PnL proxy (lazy — map loads after the main scan) ──────────────────
  app.get("/api/cohorts", async (req: Request, res: Response): Promise<void> => {
    const mint = (req.query.mint as string ?? "").trim()
    if (!mint || mint.length < 32) { res.status(400).json({ error: "mint required" }); return }
    try {
      const botUrl = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
      const upstream = await fetch(`${botUrl}/api/cohorts/internal?mint=${encodeURIComponent(mint)}`, {
        headers: secret ? { "X-Internal-Secret": secret } : {},
      })
      const data = await upstream.json()
      res.status(upstream.ok ? 200 : upstream.status).json(data)
    } catch (err) {
      res.status(500).json({ error: "cohort_lookup_failed", message: err instanceof Error ? err.message : "unknown" })
    }
  })

  // ── Combined trade analysis proxy (cohorts + wash + liquidity) ───────────────
  app.get("/api/trade-analysis", async (req: Request, res: Response): Promise<void> => {
    const mint = (req.query.mint as string ?? "").trim()
    if (!mint || mint.length < 32) { res.status(400).json({ error: "mint required" }); return }
    try {
      const botUrl = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
      const ct = req.query.created_ts ? `&created_ts=${req.query.created_ts}` : ""
      const upstream = await fetch(`${botUrl}/api/trade-analysis/internal?mint=${encodeURIComponent(mint)}${ct}`, {
        headers: secret ? { "X-Internal-Secret": secret } : {},
      })
      const data = await upstream.json()
      res.status(upstream.ok ? 200 : upstream.status).json(data)
    } catch (err) {
      res.status(500).json({ error: "analysis_failed", message: err instanceof Error ? err.message : "unknown" })
    }
  })

  // ── Dump-watch registration proxy (emergency webhooks) ───────────────────────
  const watchProxy = async (req: Request, res: Response): Promise<void> => {
    try {
      const botUrl = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
      const upstream = await fetch(`${botUrl}/api/watch/internal`, {
        method: req.method,
        headers: {
          "Content-Type": "application/json",
          ...(secret ? { "X-Internal-Secret": secret } : {}),
        },
        body: req.method === "GET" ? undefined : JSON.stringify(req.body ?? {}),
      })
      const data = await upstream.json()
      res.status(upstream.status).json(data)
    } catch (err) {
      res.status(500).json({ error: "watch_failed", message: err instanceof Error ? err.message : "unknown" })
    }
  }
  app.get("/api/watch", watchProxy)
  app.post("/api/watch", watchProxy)
  app.delete("/api/watch", watchProxy)

  // ── Root landing page — the public face of the product ───────────────────────
  app.get("/", (_req, res) => {
    res.setHeader("Content-Type", "text/html")
    res.send(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Cabal-Hunter — Solana Rug & Cabal Detector | Are You Exit Liquidity?</title>
<meta name="description" content="Free Solana token safety scanner. Before you buy, trace coordinated wallets, same-block Jito bundles, serial-rug deployers and live dumps — one Exit-Liquidity Risk verdict. MCP + REST API for AI trading agents (Claude, Cursor, ElizaOS).">
<meta name="keywords" content="solana cabal detector, solana rug checker, pump.fun bundle detection, exit liquidity, am i exit liquidity, coordinated wallet detection, solana token safety, rug check, sniper bundle detection, serial rugger, deployer history, solana MCP server, on-chain funding trace, coordinated dump detection">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">
<meta name="author" content="PF Capital">
<meta name="theme-color" content="#07080f">
<link rel="canonical" href="https://api.cabal-hunter.com/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Cabal-Hunter">
<meta property="og:title" content="Cabal-Hunter — Solana Rug & Cabal Detector">
<meta property="og:description" content="Know if you're the exit liquidity before you buy. Traces coordinated wallets, same-block bundles, serial-rug deployers and live dumps on any Solana token. Free tier + MCP/API for AI agents.">
<meta property="og:url" content="https://api.cabal-hunter.com/">
<meta property="og:image" content="https://api.cabal-hunter.com/og.svg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:site" content="@CabalhunterAPI">
<meta name="twitter:title" content="Cabal-Hunter — Solana Rug & Cabal Detector">
<meta name="twitter:description" content="Know if you're the exit liquidity before you buy. On-chain cabal, bundle, rug-deployer & dump detection for any Solana token. Free + MCP/API for AI agents.">
<meta name="twitter:image" content="https://api.cabal-hunter.com/og.svg">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%23ff4d6d'/><text x='50' y='70' font-size='54' font-weight='900' text-anchor='middle' fill='white' font-family='Arial'>CH</text></svg>">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"SoftwareApplication","name":"Cabal-Hunter","applicationCategory":"DeveloperApplication","operatingSystem":"Web, MCP, REST API","description":"On-chain Solana token safety scanner: traces coordinated wallet funding, same-block Jito bundles, serial-rug deployers and coordinated dumps into a single Exit-Liquidity Risk verdict before you buy.","url":"https://api.cabal-hunter.com/","offers":{"@type":"Offer","price":"0","priceCurrency":"USD","description":"100 free queries per month per IP, then 0.05 USDC per query"},"featureList":["Funding-source tracing","Same-block Jito bundle detection","Coordinated dump detection","Serial-rug deployer history","CEX-noise filter","Exit-Liquidity Risk verdict","MCP server for Claude, Cursor and ElizaOS"],"creator":{"@type":"Organization","name":"PF Capital","url":"https://api.cabal-hunter.com/"}}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is a Solana cabal?","acceptedAnswer":{"@type":"Answer","text":"A cabal is a group of wallets — often funded from the same source and buying in the same block — that quietly accumulate a large share of a token's supply before retail, then dump simultaneously into the buyers who pile in after launch."}},{"@type":"Question","name":"How do I check if a Solana token is a rug?","acceptedAnswer":{"@type":"Answer","text":"Scan the mint with Cabal-Hunter. It traces holder funding back to shared sources, detects same-block bundle buys, flags serial-rug deployers and live coordinated dumps, and returns an Exit-Liquidity Risk verdict of LOW, ELEVATED or HIGH."}},{"@type":"Question","name":"Is Cabal-Hunter free?","acceptedAnswer":{"@type":"Answer","text":"Yes — 100 free queries per month per IP, with no signup or API key. Beyond that it is 0.05 USDC per query, paid natively on Solana."}},{"@type":"Question","name":"Can AI trading agents use Cabal-Hunter?","acceptedAnswer":{"@type":"Answer","text":"Yes. Cabal-Hunter exposes an MCP server at api.cabal-hunter.com/mcp so Claude, Cursor and ElizaOS agents can call check_cabal_risk automatically before a swap, plus a REST API for any language."}}]}</script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:40px 24px;max-width:780px;margin:0 auto}
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px;flex-wrap:wrap}
  .logo{display:flex;align-items:center;gap:12px}
  .socials{display:flex;gap:10px;flex-shrink:0}
  .social{display:inline-flex;align-items:center;gap:7px;padding:10px 16px;border-radius:10px;font-size:13.5px;font-weight:800;text-decoration:none;white-space:nowrap;transition:transform .15s,box-shadow .15s}
  .social:hover{transform:translateY(-2px)}
  .social-x{background:#fff;color:#000;box-shadow:0 0 0 1px rgba(255,255,255,.15)}
  .social-x:hover{box-shadow:0 8px 22px rgba(255,255,255,.28)}
  .social-tg{background:linear-gradient(135deg,#2AABEE,#229ED9);color:#fff;box-shadow:0 0 20px rgba(34,158,217,.5)}
  .social-tg:hover{box-shadow:0 8px 26px rgba(34,158,217,.65)}
  .social .pill{font-size:9.5px;font-weight:800;background:rgba(0,0,0,.22);padding:2px 7px;border-radius:20px;text-transform:uppercase;letter-spacing:.4px}
  .tg-nudge{font-size:12.5px;color:#94a3b8;margin-bottom:34px;display:flex;align-items:center;gap:7px}
  .tg-nudge a{color:#2dd4bf;font-weight:700;text-decoration:none}
  .tg-nudge a:hover{text-decoration:underline}
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
<div class="topbar">
  <div class="logo">
    <div class="icon">CH</div>
    <div>
      <div style="font-size:20px;font-weight:800">Cabal-Hunter</div>
      <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:1px">Solana Token Cabal Detection</div>
    </div>
  </div>
  <div class="socials">
    <a class="social social-x" href="https://x.com/CabalhunterAPI" target="_blank" rel="noopener">𝕏 Follow</a>
    <a class="social social-tg" href="https://t.me/CabalHunterAlerts" target="_blank" rel="noopener">✈ Telegram <span class="pill">Free alerts</span></a>
  </div>
</div>
<div class="tg-nudge">📡 <span>Live cabal &amp; rug alerts the moment we catch them — <a href="https://t.me/CabalHunterAlerts" target="_blank" rel="noopener">join the free Telegram →</a> no signup, no cost.</span></div>

<h1>Know if you're the exit liquidity —<br>before you buy.</h1>
<p class="sub">On Solana, <strong style="color:#e2e8f0">over half of pump.fun launches are sniped in the creation block by wallets the deployer funded</strong> — they buy at the bottom and dump on you. Cabal-Hunter traces the funding, catches the same-block bundles, flags the serial-rug devs, and gives you one verdict: <strong style="color:#f87171">are the insiders positioned to dump on you?</strong><br>One call, one 0–100 score, every flag linked to its on-chain proof. <strong style="color:#10b981">100 free queries/month</strong> — then $0.05 USDC per query. No API key. No account.</p>

<p style="font-size:13px;color:#64748b;margin:-20px 0 36px;line-height:1.6"><strong style="color:#94a3b8">The complete on-chain X-ray for any Solana token.</strong> In one scan: trace holder funding, catch same-block bundles and coordinated dumps, pull the deployer's track record, map which exchanges funded the holders, and see which cohorts are actually in profit — every red flag linked to its on-chain proof. Works on any mint, graduated or still on the curve.</p>

<div class="cards" style="grid-template-columns:1fr 1fr 1fr">
  <div class="card" style="border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.06)"><div class="card-title" style="color:#f87171">🩸 Exit-Liquidity Risk</div><div class="card-val" style="font-size:13px;color:#e2e8f0">The one question that matters: <strong>are YOU the buyer the insiders sell to?</strong> One verdict from the bundle, concentration, shared-funder and dump signals — designed-to-dump launches, flagged before you ape.</div></div>
  <div class="card"><div class="card-title">🔍 Funding Trace</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Top holders walked back to shared funding wallets</div></div>
  <div class="card"><div class="card-title">⚡ Bundle Detection</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Wallets that bought in the exact same block — Jito bundles can't hide</div></div>
  <div class="card"><div class="card-title">🚨 Coordinated Dump</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Multiple holders selling in the EXACT same block — a cabal exiting in real time</div></div>
  <div class="card"><div class="card-title">⛔ Deployer History</div><div class="card-val" style="font-size:13px;color:#e2e8f0">"Launched 14 tokens — 13 dead." Wallets rotate, deployers don't</div></div>
  <div class="card"><div class="card-title">🏦 CEX Funding Map</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Which exchanges funded the holders, % of supply each — read the distribution (Binance vs MEXC vs Revolut…)</div></div>
  <div class="card"><div class="card-title">👥 Cohort PnL</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Snipers vs Insiders: what they bought, how much they've dumped, and the SOL they've already banked</div></div>
  <div class="card"><div class="card-title">🔁 Wash-Trade Filter</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Catches fake volume — wallets round-tripping tokens to fake momentum and farm trending lists</div></div>
  <div class="card"><div class="card-title">💧 Exit Liquidity</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Price impact of your sell before you buy — will the pool absorb your take-profit, or slip 15%?</div></div>
  <div class="card"><div class="card-title">🚨 Dump Webhooks</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Your bot subscribes to a mint — we push the instant a dump or rug starts so it can auto-exit. No polling.</div></div>
  <div class="card"><div class="card-title">⛓ On-Chain Receipts</div><div class="card-val" style="font-size:13px;color:#e2e8f0">Every red flag links to the actual Solscan tx — verify, don't trust the score</div></div>
  <div class="card"><div class="card-title">⚡ Built for Bots</div><div class="card-val" style="font-size:13px;color:#e2e8f0">&lt;100ms pre-indexed · JSON · MCP for Claude / Cursor / Eliza</div></div>
</div>

<div class="section-title">What you see in one scan</div>
<p style="font-size:13px;color:#94a3b8;line-height:1.7;margin-bottom:36px">
  A <strong style="color:#e2e8f0">0–100 cabal score</strong> with a plain-English verdict, a live bubble map of holder clusters,
  the deployer's track record, a <strong style="color:#e2e8f0">freshness stamp + one-click recheck</strong> so you never act on stale data,
  and a <strong style="color:#e2e8f0">detection summary</strong> showing all five checks that ran — even the ones that came back clean.
  Every cluster and red flag links straight to the on-chain proof.
</p>

<div class="links">
  <a class="btn btn-primary" href="/demo">🗺 Live Bubble Map Demo</a>
  <a class="btn btn-teal" href="/api/info">📖 API Reference</a>
  <a class="btn btn-purple" href="https://github.com/paulf280-ui/solana-safe-sniper-mcp-template" target="_blank">⚙ GitHub Template</a>
  <a class="btn btn-gray" href="/compare">⚖ vs rugcheck / GoPlus</a>
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

  // ── Live demo — always points at a recently-scanned token, never a dead page
  app.get("/demo", (_req, res) => {
    const mint = getFreshDemoMint()
    res.redirect(302, mint ? `/map?mint=${encodeURIComponent(mint)}` : "/")
  })

  // ── Glama verification file ──────────────────────────────────────────────────
  // Glama checks /.well-known/glama.json to verify domain ownership and
  // confirm the connector is legitimate before marking it Healthy.
  app.get("/.well-known/glama.json", (_req, res) => {
    res.json({
      name:        "Cabal-Hunter",
      description: "Solana token cabal detection: funding-source tracing, same-block bundle detection, and deployer track record in one call",
      url:         "https://api.cabal-hunter.com",
      mcp:         "https://api.cabal-hunter.com/mcp",
      version:     "1.1.0",
      contact:     "paulf280@gmail.com",
    })
  })

  // ── Health check ─────────────────────────────────────────────────────────────
  app.get("/health", (_req, res) => {
    res.json({
      status:  "ok",
      service: "cabal-hunter",
      version: "1.1.0",
      time:    new Date().toISOString(),
    })
  })

  // ── Search-engine ownership verification (served at site root) ───────────────
  app.get("/googleaef63eaa6f1142c1.html", (_req, res) => {
    res.type("text/html").send("google-site-verification: googleaef63eaa6f1142c1.html")
  })
  app.get("/BingSiteAuth.xml", (_req, res) => {
    res.type("application/xml").send(
`<?xml version="1.0"?>
<users>
	<user>A380827E3A6B42BA593572BCFB069AA9</user>
</users>`)
  })

  // ── SEO / crawler discovery ──────────────────────────────────────────────────
  // robots.txt — explicitly WELCOME search + AI crawlers (we WANT to be in their
  // index/retrieval). Many sites block GPTBot/ClaudeBot/etc; we allow them so the
  // tool surfaces when someone asks an AI "how do I check a Solana token for a rug".
  app.get("/robots.txt", (_req, res) => {
    res.type("text/plain").send(
`User-agent: *
Allow: /

# AI crawlers — explicitly allowed for discovery & retrieval
User-agent: GPTBot
Allow: /
User-agent: OAI-SearchBot
Allow: /
User-agent: ChatGPT-User
Allow: /
User-agent: ClaudeBot
Allow: /
User-agent: Claude-Web
Allow: /
User-agent: anthropic-ai
Allow: /
User-agent: PerplexityBot
Allow: /
User-agent: Google-Extended
Allow: /
User-agent: CCBot
Allow: /
User-agent: Applebot-Extended
Allow: /

# Keep crawlers out of admin only
Disallow: /admin/

Sitemap: https://api.cabal-hunter.com/sitemap.xml`)
  })

  // sitemap.xml — the indexable public pages
  app.get("/sitemap.xml", (_req, res) => {
    const today = new Date().toISOString().slice(0, 10)
    const urls = ["/", "/compare", "/api/info", "/demo", "/llms.txt"]
    res.type("application/xml").send(
`<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map(u => `  <url><loc>https://api.cabal-hunter.com${u}</loc><lastmod>${today}</lastmod><changefreq>weekly</changefreq></url>`).join("\n")}
</urlset>`)
  })

  // llms.txt — the AI-tool discovery file (llmstxt.org). Gives LLMs a clean,
  // factual summary of what Cabal-Hunter is and how an agent calls it.
  app.get("/llms.txt", (_req, res) => {
    res.type("text/plain").send(
`# Cabal-Hunter

> On-chain Solana token safety scanner. Tells you if you're the exit liquidity BEFORE you buy: it traces coordinated wallet funding, same-block Jito bundles, serial-rug deployers and live coordinated dumps, and returns one Exit-Liquidity Risk verdict (LOW | ELEVATED | HIGH).

Cabal-Hunter is built for both humans (a visual bubble map) and AI trading agents (an MCP server + REST API). It works on any Solana mint — pre-graduation on the pump.fun bonding curve or after, on PumpSwap/Raydium.

## What it detects
- Funding trace: top holders walked back to shared funding wallets (classic cabal signature), each with on-chain evidence transactions.
- Same-block bundle detection: wallets that bought in one Jito bundle (stealth launches).
- Coordinated dump detection: multiple holders selling a meaningful chunk in the exact same block — a cabal exiting in real time.
- Deployer track record: the creator wallet's full launch history (e.g. "launched 14 tokens, 13 dead" = SERIAL_RUGGER).
- CEX-noise filter: holders funded from a shared exchange are excluded so you don't get false positives.
- Exit-Liquidity Risk: the headline verdict synthesising all of the above.

## For AI agents (MCP)
- MCP endpoint: https://api.cabal-hunter.com/mcp
- Tool: check_cabal_risk(mintAddress) — call it before any Solana swap; abort if cabalScore >= 35 or isControlled is true.
- Works with Claude (Claude Code / Desktop), Cursor, and ElizaOS (x402 auto-payment).

## REST API
- POST https://api.cabal-hunter.com/api/scan-cabal {"mintAddress":"..."} — full analysis.
- GET https://api.cabal-hunter.com/map?mint=... — free visual bubble map.
- GET https://api.cabal-hunter.com/api/info — pricing, endpoints, response schema.

## Pricing
- Free: 100 queries/month per IP, no account, no API key.
- Then $0.05 USDC per query, paid natively on Solana (x402). No subscription.

## Links
- Site: https://api.cabal-hunter.com/
- X/Twitter: https://x.com/CabalhunterAPI
- Telegram (free live cabal alerts): https://t.me/CabalHunterAlerts
- GitHub template: https://github.com/paulf280-ui/solana-safe-sniper-mcp-template`)
  })

  // og.svg — branded social/share image (1200x630)
  app.get("/og.svg", (_req, res) => {
    res.type("image/svg+xml").set("Cache-Control", "public,max-age=86400").send(
`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
<rect width="1200" height="630" fill="#07080f"/>
<rect x="60" y="60" width="84" height="84" rx="18" fill="url(#g)"/>
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff4d6d"/><stop offset="1" stop-color="#7c3aed"/></linearGradient></defs>
<text x="168" y="118" font-family="Arial,sans-serif" font-size="42" font-weight="900" fill="#e2e8f0">Cabal-Hunter</text>
<text x="60" y="300" font-family="Arial,sans-serif" font-size="76" font-weight="900" fill="#e2e8f0">Are you the</text>
<text x="60" y="392" font-family="Arial,sans-serif" font-size="76" font-weight="900" fill="#f87171">exit liquidity?</text>
<text x="60" y="476" font-family="Arial,sans-serif" font-size="32" fill="#94a3b8">On-chain Solana cabal, bundle, rug-deployer &amp; dump detection</text>
<text x="60" y="520" font-family="Arial,sans-serif" font-size="32" fill="#94a3b8">— before you buy. Free + MCP/API for AI agents.</text>
<text x="60" y="585" font-family="Arial,sans-serif" font-size="26" font-weight="700" fill="#2dd4bf">api.cabal-hunter.com</text>
</svg>`)
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

    // Browser redirect — humans get the visual map, bots get JSON
    const acceptsHtml = (req.headers.accept ?? "").includes("text/html")
    if (acceptsHtml && req.method === "GET") {
      res.redirect(302, `/map?mint=${encodeURIComponent(mint)}`)
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
  <p style="color:#94a3b8;font-size:13px;line-height:1.6">A token last week scored <strong style="color:#10b981">8/8 on rugcheck.xyz</strong> — LP burned, contract clean, no honeypot. GoPlus: all green. Standard tools saw nothing wrong.<br><br>Cabal-Hunter traced the top 20 holders and found <strong>6 wallets all funded from the same source, 47 seconds before the first trade.</strong> The token rugged 3 hours later. <a href="/demo" style="color:#ff4d6d">See the on-chain proof →</a></p>
</div>

<h2>What Each Tool Checks</h2>
<table>
  <thead><tr><th>Detection Type</th><th>Cabal-Hunter</th><th>rugcheck.xyz</th><th>GoPlus Security</th></tr></thead>
  <tbody>
    <tr class="highlight"><td><strong>Coordinated wallet clusters</strong><br><small>Multiple wallets funded by same source before launch</small></td><td class="ch"><span class="yes">✅ YES</span><br><small>Core feature — traces funding lineage via Helius RPC</small></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr class="highlight"><td><strong>Cabal confidence score (0–100)</strong><br><small>% of supply controlled by coordinated wallets</small></td><td class="ch"><span class="yes">✅ YES</span></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr class="highlight"><td><strong>Insider sniper detection</strong><br><small>Wallets that bought within 60 seconds of launch</small></td><td class="ch"><span class="yes">✅ YES</span></td><td><span class="no">❌ NO</span></td><td><span class="partial">⚡ Partial</span></td></tr>
    <tr class="highlight"><td><strong>Same-block bundle detection</strong><br><small>Holders that bought in the EXACT same block — Jito-bundled stealth launches that evade funding traces</small></td><td class="ch"><span class="yes">✅ YES</span><br><small>time_sync flag + cluster breakdown</small></td><td><span class="no">❌ NO</span></td><td><span class="no">❌ NO</span></td></tr>
    <tr class="highlight"><td><strong>Deployer track record</strong><br><small>"This dev launched 14 tokens — 13 are dead." Creator resolved on-chain, works after graduation</small></td><td class="ch"><span class="yes">✅ YES</span><br><small>SERIAL_RUGGER / POOR_TRACK_RECORD verdicts</small></td><td><span class="partial">⚡ Partial</span><br><small>creator shown, no history analysis</small></td><td><span class="no">❌ NO</span></td></tr>
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
  <a class="btn btn-red" href="/demo">🗺 Live Demo — See a Detected Cabal</a>
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

  // ── Dogfooding proof: our own bot's trades vs the cabal signals at entry ─────
  app.get("/admin/proof", async (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    const key = req.query.key as string ?? ""
    if (secret && key !== secret) { res.status(401).send("<h1>Unauthorized</h1>"); return }
    let d: any = {}
    try {
      const botUrl = process.env.BOT_INTERNAL_URL ?? "http://127.0.0.1:3001"
      const r = await fetch(`${botUrl}/api/proof/internal`, { headers: secret ? { "X-Internal-Secret": secret } : {} })
      d = await r.json()
    } catch { d = { error: "bot unreachable" } }
    const col = (v: number) => v > 0 ? "#10b981" : v < 0 ? "#ff4d6d" : "#94a3b8"
    const bucketRows = (arr: any[], k: string) => (arr || []).map(b =>
      `<tr><td style="padding:8px">${b[k]}</td><td style="padding:8px">${b.trades}</td><td style="padding:8px;color:${col(b.avg_pnl_pct)};font-weight:700">${b.avg_pnl_pct > 0 ? "+" : ""}${b.avg_pnl_pct}%</td><td style="padding:8px">${b.win_rate}%</td></tr>`).join("")
    const recentRows = (d.recent || []).map((t: any) =>
      `<tr><td style="padding:8px"><a href="/map?mint=${t.mint}" target="_blank" style="color:#a78bfa;text-decoration:none">${t.token}</a></td><td style="padding:8px;color:${col(t.pnl_pct)};font-weight:700">${t.pnl_pct > 0 ? "+" : ""}${t.pnl_pct}%</td><td style="padding:8px">${t.cabal_score ?? "–"}</td><td style="padding:8px">${t.wash_score ?? "–"}</td><td style="padding:8px;font-size:11px">${t.deployer_verdict ?? "–"}</td><td style="padding:8px">${t.exit_impact_10sol_pct ?? "–"}%</td></tr>`).join("")
    res.setHeader("Content-Type", "text/html")
    res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Cabal-Hunter — Live Proof</title>
<style>body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:28px;max-width:920px;margin:0 auto}h1{font-size:22px}.sub{color:#64748b;font-size:12px;margin-bottom:24px}h2{font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:26px 0 8px}table{width:100%;border-collapse:collapse;font-size:13px;background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:10px;overflow:hidden}th{text-align:left;padding:8px;color:#64748b;font-size:11px;text-transform:uppercase;border-bottom:1px solid rgba(255,255,255,.08)}tr td{border-bottom:1px solid rgba(255,255,255,.04)}</style></head>
<body><h1>Cabal-Hunter runs our own bot 🤖</h1>
<div class="sub">Every entry, the bot records the cabal-hunter signals, then we measure the realized P&L. ${d.trades_with_signals ?? 0} trades with signals · ${d.total_closed ?? 0} total closed · ${d.blocks_total ?? 0} bad tokens blocked.</div>
${(() => { const ix = d.deployer_index || {}; return ix.deployers_indexed ? `<div style="display:flex;gap:14px;margin-bottom:20px;flex-wrap:wrap">
  <div style="background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:14px 18px"><div style="font-size:11px;color:#64748b">DEPLOYERS INDEXED</div><div style="font-size:24px;font-weight:800">${ix.deployers_indexed}</div></div>
  <div style="background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:14px 18px"><div style="font-size:11px;color:#64748b">SERIAL RUGGERS</div><div style="font-size:24px;font-weight:800;color:#ff4d6d">${ix.serial_ruggers ?? 0}</div></div>
  <div style="background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:14px 18px"><div style="font-size:11px;color:#64748b">10+ LAUNCH DEVS</div><div style="font-size:24px;font-weight:800;color:#f59e0b">${ix.prolific_10plus ?? 0}</div></div>
</div>` : "" })()}
${(d.trades_with_signals ?? 0) === 0 ? `<p style="color:#94a3b8">Dataset is building — signals are captured from the next trade onward. Check back after a few trades.</p>` : `
<h2>Avg P&L by cabal score</h2><table><tr><th>Cabal score</th><th>Trades</th><th>Avg P&L</th><th>Win rate</th></tr>${bucketRows(d.by_cabal_score, "label")}</table>
<h2>Avg P&L by wash-trading score</h2><table><tr><th>Volume</th><th>Trades</th><th>Avg P&L</th><th>Win rate</th></tr>${bucketRows(d.by_wash_score, "label")}</table>
<h2>Avg P&L by deployer verdict</h2><table><tr><th>Deployer</th><th>Trades</th><th>Avg P&L</th><th>Win rate</th></tr>${bucketRows(d.by_deployer, "verdict")}</table>
${(() => { const f = d.fast_vs_normal || {}; return (f.fast_trades || f.normal_trades) ? `<h2>Early-entry test (cabal-clean fast path)</h2><table><tr><th>Path</th><th>Trades</th><th>Avg P&L</th></tr><tr><td style="padding:8px">🟢 Cabal-clean (earlier entry)</td><td style="padding:8px">${f.fast_trades||0}</td><td style="padding:8px;color:${col(f.fast_avg_pnl||0)};font-weight:700">${f.fast_avg_pnl!=null?(f.fast_avg_pnl>0?"+":"")+f.fast_avg_pnl+"%":"–"}</td></tr><tr><td style="padding:8px">Normal (strict gates)</td><td style="padding:8px">${f.normal_trades||0}</td><td style="padding:8px;color:${col(f.normal_avg_pnl||0)};font-weight:700">${f.normal_avg_pnl!=null?(f.normal_avg_pnl>0?"+":"")+f.normal_avg_pnl+"%":"–"}</td></tr></table>` : "" })()}
<h2>Recent trades (entered & traded)</h2><table><tr><th>Token</th><th>P&L</th><th>Path</th><th>Cabal</th><th>Wash</th><th>Deployer</th><th>Exit impact (10 SOL)</th></tr>${(d.recent||[]).map((t:any)=>`<tr><td style="padding:8px"><a href="/map?mint=${t.mint}" target="_blank" style="color:#a78bfa;text-decoration:none">${t.token}</a></td><td style="padding:8px;color:${col(t.pnl_pct)};font-weight:700">${t.pnl_pct>0?"+":""}${t.pnl_pct}%</td><td style="padding:8px">${t.cabal_clean_fast?"🟢 early":"strict"}</td><td style="padding:8px">${t.cabal_score??"–"}</td><td style="padding:8px">${t.wash_score??"–"}</td><td style="padding:8px;font-size:11px">${t.deployer_verdict??"–"}</td><td style="padding:8px">${t.exit_impact_10sol_pct??"–"}%</td></tr>`).join("")}</table>`}
${(d.blocks || []).length ? `<h2>Bad tokens we blocked (saves)${d.blocks_checked ? ` — ${d.blocks_rugged}/${d.blocks_checked} went to zero` : ""}</h2><table><tr><th>Token</th><th>Reason</th><th>Deployer record</th><th>Outcome</th></tr>${(d.blocks||[]).map((b:any)=>`<tr><td style="padding:8px"><a href="/map?mint=${b.mint}" target="_blank" style="color:#a78bfa;text-decoration:none">${b.token}</a></td><td style="padding:8px;color:#ff4d6d">${b.reason}</td><td style="padding:8px">${b.deployer_verdict ?? ""} ${b.deployer_dead!=null?`(${b.deployer_dead}/${b.deployer_sampled} dead)`:""}</td><td style="padding:8px;color:${b.outcome==='rugged'?'#ff4d6d':b.outcome==='survived'?'#10b981':'#64748b'}">${b.outcome ?? "pending 24h"}</td></tr>`).join("")}</table>` : ""}
</body></html>`)
  })

  // ── Site analytics (JSON + dashboard) ────────────────────────────────────────
  app.get("/admin/analytics", (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    if (secret && req.query.key !== secret && req.headers["x-internal-secret"] !== secret) {
      res.status(401).json({ error: "unauthorized" }); return
    }
    res.json(getAnalytics())
  })

  // Visit FROM the device you want excluded — purges its history + blocks future
  app.get("/admin/exclude-me", (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    if (secret && req.query.key !== secret) { res.status(401).send("<h1>Unauthorized</h1>"); return }
    const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim() || req.socket.remoteAddress || ""
    const r = excludeIp(ip)
    res.setHeader("Content-Type", "text/html")
    res.send(`<body style="background:#07080f;color:#e2e8f0;font-family:system-ui;padding:40px;text-align:center">
      <h1 style="color:#10b981">✓ This device is now excluded</h1>
      <p style="color:#94a3b8">Removed <b>${r.removed}</b> of your past visits. Future visits from this network won't be counted.</p>
      <p style="color:#475569;font-size:12px">On a different network later? Just open this link again from there.</p>
    </body>`)
  })

  app.get("/admin/traffic", (req, res) => {
    const secret = process.env.CABAL_INTERNAL_SECRET ?? ""
    const key = req.query.key as string ?? ""
    if (secret && key !== secret) { res.status(401).send("<h1>Unauthorized</h1>"); return }
    const a = getAnalytics() as any
    const t = a.totals ?? {}, td = a.today ?? {}
    const card = (label: string, val: unknown, col: string) =>
      `<div style="background:#0d0f1e;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:18px"><div style="font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:1px">${label}</div><div style="font-size:30px;font-weight:800;color:${col}">${val ?? 0}</div></div>`
    const rows = (arr: any[], cols: string[], keys: string[]) =>
      `<table style="width:100%;border-collapse:collapse;font-size:13px"><tr>${cols.map(c=>`<th style="text-align:left;padding:8px;color:#64748b;border-bottom:1px solid rgba(255,255,255,.08)">${c}</th>`).join("")}</tr>${(arr||[]).map(r=>`<tr>${keys.map(k=>`<td style="padding:8px;border-bottom:1px solid rgba(255,255,255,.04);font-family:${k==='mint'||k==='referer'?'monospace':'inherit'};font-size:12px">${r[k]??''}</td>`).join("")}</tr>`).join("")}</table>`
    res.setHeader("Content-Type","text/html")
    res.send(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Cabal-Hunter Traffic</title>
<style>body{background:#07080f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:28px;max-width:1000px;margin:0 auto}h1{font-size:22px;margin-bottom:4px}.sub{color:#64748b;font-size:12px;margin-bottom:24px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}h2{font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:28px 0 10px}</style></head>
<body><h1>Cabal-Hunter — Traffic</h1><div class="sub">Server-side · counts every visit (Cloudflare can't see us — DNS-only)</div>
<div class="grid">${card("Total Visits",t.visits,"#e2e8f0")}${card("Unique Visitors",t.unique_visitors,"#10b981")}${card("Map Views",t.map_views,"#7c3aed")}${card("API Calls",t.api_calls,"#0ea5e9")}</div>
<div class="grid">${card("Today Visits",td.visits,"#e2e8f0")}${card("Today Unique",td.unique_visitors,"#10b981")}${card("Landing Views",t.landing_views,"#f59e0b")}${card("Tokens Scanned",t.tokens_scanned,"#ec4899")}</div>
<div style="font-size:11px;color:#475569;margin-bottom:20px">Note: "total visits" includes automated scanner noise. The real human signal is <b>Landing + Map Views</b> and <b>Unique Visitors</b>.</div>
<h2>Top countries (real visitors)</h2>${rows(a.top_countries,["Country","Visitors","Hits"],["country","visitors","hits"])}
<h2>Outbound clicks (where they go next)</h2>${rows(a.outbound_clicks,["Target","Clicks"],["target","n"])}
<h2>Most-searched tokens</h2>${rows(a.top_mints,["Mint","Searches","Unique"],["mint","n","u"])}
<h2>Where visitors come from (referrers)</h2>${rows(a.top_referers,["Referrer","Hits"],["referer","n"])}
<h2>Last 30 days</h2>${rows(a.by_day,["Day","Visits","Unique"],["day","visits","uniques"])}
</body></html>`)
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
      description:     "On-chain coordinated wallet detection for Solana meme tokens — tells you if you'd be the exit liquidity before you buy",
      detection_layers: {
        exit_liquidity_risk: "Headline verdict (LOW | ELEVATED | HIGH) synthesising the signals that mean insiders are positioned to dump on a buyer — bundled launch, single-wallet concentration, shared-funder cluster, coordinated dump, serial-rug deployer. The one number a trader needs.",
        funding_trace:  "Top holders walked back to shared funding wallets (clusters[].type='funding'). Each cluster carries evidence_txs[] — the actual funding transactions.",
        bundle_detect:  "Holders that bought in the exact same block — Jito bundle signature (time_sync:true, clusters[].type='time_sync')",
        coordinated_exit: "Multiple holders that DUMPED (≥25% of their bag each) in the exact same block — a cabal exiting in real time (coordinated_exit:true, clusters[].type='coordinated_exit', sold_pct = % of supply dumped)",
        deployer:       "Token creator resolved on-chain + full launch history (deployer.verdict: FIRST_LAUNCH | NORMAL | POOR_TRACK_RECORD | SERIAL_RUGGER)",
        cex_filter:     "Holders funded from a shared exchange / high-volume wallet are excluded from the score and surfaced in filtered_clusters[] — no false positives from CEX withdrawals",
      },
      response_fields: {
        cabal_score:        "0–100. ≥65 HIGH, ≥35 CAUTION, else LOW SIGNAL",
        risk:               "HIGH | MEDIUM | CLEAN",
        is_controlled:      "true when score ≥ 35",
        verdict:            "plain-English summary string",
        time_sync:          "true if a same-block (bundled) buy group was found",
        coordinated_exit:   "true if a same-block coordinated dump was found",
        clusters:           "[] scored coordination groups; each has wallet_count, combined_pct, master_full, type ('funding'|'time_sync'|'coordinated_exit'), sold_pct (exits), evidence_txs[]",
        filtered_clusters:  "[] CEX/infra groups excluded from the score (funder_label, wallet_count, combined_pct)",
        deployer:           "{ creator, tokens_launched, dead, sampled, dead_pct, verdict }",
        holders:            "[] top holders; cluster members carry funding_tx + buy_slot (on-chain receipts)",
        computed_at:        "unix seconds — use with the freshness/recheck flow",
        source:             "pre_indexed (<100ms) | real_time",
      },
      free_tier:       "100 queries/month per IP — no key, no account",
      price_per_query: `${PRICE_USDC} USDC`,
      payment_method:  "Solana SPL USDC transfer + X-Payment-Signature header",
      mcp_endpoint:    "/mcp",
      rest_endpoint:   "/api/scan-cabal",
      map_endpoint:    "/map?mint=<MINT> — free visual bubble map",
      cex_funding:     "/api/cex-funding?mint=<MINT> — per-exchange funding breakdown (which CEXes funded the holders, % of supply each). Labels from Helius identity (12.5k+ verified) — only confirmed exchanges named.",
      cohorts:         "/api/cohorts?mint=<MINT> — Team/Snipers/Insiders cohort breakdown: initial buy %, % of bag still held, and realized SOL profit per cohort (reconstructed from the pool's full swap history).",
      trade_analysis:  "/api/trade-analysis?mint=<MINT> — combined cohorts + wash-trading score + exit-liquidity price-impact in one call (pool swaps fetched once). wash: {wash_score, wash_volume_pct, offenders}; liquidity: {liquidity_usd, lp_burned, sells:[{sol, impact_pct}]}.",
      dump_webhook:    "POST /api/watch {mint, webhook_url} — register a live dump watch; we POST your webhook the instant a coordinated dump / liquidity drain starts (payload: {event, mint, reason, coordinated, action}). GET to list, DELETE to remove. The push model for bot auto-exit.",
      recheck:         "/api/map-data?mint=<MINT>&fresh=1 — force a live re-trace, bypass cache",
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
<div class="section-title">Top Queried Tokens <span style="color:#475569;font-weight:400;text-transform:none;letter-spacing:0">· real scans, bots excluded</span></div>
<table><thead><tr><th>#</th><th>Mint Address</th><th>Scans</th><th>Unique Users</th></tr></thead>
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
        <div class="card-sub">\${d.real_queries?.today||0} real scans · \${d.today?.queries||0} paid</div></div>
      <div class="card"><div class="card-label">This Week</div>
        <div class="card-val blue">\${(d.week?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.real_queries?.week||0} real scans · \${d.week?.queries||0} paid</div></div>
      <div class="card"><div class="card-label">This Month</div>
        <div class="card-val amber">\${(d.month?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.real_queries?.month||0} real scans · \${d.month?.queries||0} paid</div></div>
      <div class="card"><div class="card-label">All Time</div>
        <div class="card-val purple">\${(d.total?.revenue_usdc||0).toFixed(4)} <span style="font-size:16px;font-weight:500">USDC</span></div>
        <div class="card-sub">\${d.real_queries?.total||0} real scans · \${d.total?.queries||0} paid</div></div>
    \`;
    const tops=d.top_queried||[];
    document.getElementById('top-table').innerHTML=tops.length?tops.map((t,i)=>\`
      <tr><td style="color:#64748b">\${i+1}</td>
      <td class="mono">\${t.mint_queried}</td>
      <td style="font-weight:700">\${t.cnt}</td>
      <td class="blue">\${t.users}</td></tr>\`).join('')
      :'<tr><td colspan="4" style="color:#64748b;text-align:center;padding:20px">No real scans yet — share the tool to start</td></tr>';
    document.getElementById('refresh-label').textContent='Last updated: '+new Date().toLocaleTimeString()+' · Auto-refreshes every 60s';
  }catch(e){console.error(e)}
}
load(); setInterval(load,60000);
</script>
</body></html>`
}
