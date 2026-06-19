import { createRequire } from "module"
const require = createRequire(import.meta.url)
/**
 * mcp-server.ts — Model Context Protocol server for Cabal-Hunter.
 *
 * Exposes a single tool: check_cabal_risk
 *
 * Transport: Streamable HTTP (works over internet, required for remote MCP)
 * Discovery: List this server at mcp.so and Anthropic's MCP registry to get
 *            passive discovery from developers building autonomous trading agents.
 *
 * Payment: Same USDC-on-Solana gate as the REST server.
 *          Include X-Payment-Signature in the MCP HTTP request headers.
 *
 * Usage in Claude Desktop / Claude Code / Cursor:
 *   Add to mcp config:
 *   {
 *     "cabal-hunter": {
 *       "url": "https://your-domain.com/mcp",
 *       "headers": { "X-Payment-Signature": "<signed-tx>" }
 *     }
 *   }
 */

const { McpServer } = require("@modelcontextprotocol/sdk/server/mcp.js")
const { StreamableHTTPServerTransport } = require("@modelcontextprotocol/sdk/server/streamableHttp.js")
import { Request, Response } from "express"
import { z }             from "zod"
import { getCabalReport } from "./detector.js"
import { createPaymentRequest, verifyPayment } from "./payment.js"
import crypto from "crypto"

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.02")

/** Create and configure the MCP server instance */
export function createMcpServer() {
  const server = new McpServer({
    name:    process.env.MCP_SERVER_NAME    ?? "cabal-hunter",
    version: process.env.MCP_SERVER_VERSION ?? "1.0.0",
  })

  // ── Tool: check_cabal_risk ──────────────────────────────────────────────────
  server.tool(
    "check_cabal_risk",
    [
      "Real-time on-chain coordinated wallet detection for any Solana token mint.",
      "Three detection layers in one call:",
      "",
      "1. FUNDING TRACE — walks the top holders' history back to launch and finds",
      "   wallets funded by the same source (classic cabal signature).",
      "2. SAME-BLOCK BUNDLES — flags holders whose token accounts were created in",
      "   the exact same block (Jito-bundled multi-wallet launches that route",
      "   funding through intermediaries to evade funding traces). `time_sync: true`.",
      "2b. COORDINATED DUMP — flags ≥2 holders that SOLD a meaningful chunk (≥25%",
      "   of their bag) in the exact same block: a cabal exiting in real time.",
      "   `coordinated_exit: true`, clusters[].type='coordinated_exit', sold_pct.",
      "3. DEPLOYER TRACK RECORD — resolves the token creator on-chain (works after",
      "   graduation), pulls their full launch history, and reports how many of",
      "   their previous tokens are dead. Verdicts: FIRST_LAUNCH / NORMAL /",
      "   POOR_TRACK_RECORD / SERIAL_RUGGER.",
      "",
      "Returns a cabal confidence score 0–100, cluster breakdown, holder map, and a",
      "plain-English verdict, e.g. \"AVOID — 5 wallets bought in the EXACT same",
      "block, controlling 23% of supply. DEPLOYER ALERT: 13 of 13 previous launches dead.\"",
      "",
      "COST: $0.02 USDC per query (paid on Solana mainnet).",
      "PAYMENT: Include X-Payment-Signature header with a valid USDC transaction",
      "signature, or call GET /api/info for payment instructions.",
      "",
      "Typical response time: <100ms for pre-indexed tokens, 1-5s for real-time analysis.",
    ].join("\n"),
    {
      mintAddress: z
        .string()
        .min(32)
        .max(44)
        .describe("The Solana mint address of the token to audit (base58, 32–44 chars)"),
      pairCreatedAt: z
        .number()
        .optional()
        .describe("Optional: DexScreener pairCreatedAt timestamp in milliseconds. Speeds up analysis when provided."),
    },
    async ({ mintAddress, pairCreatedAt }: { mintAddress: string; pairCreatedAt?: number }, extra: Record<string, unknown>) => {
      const t0 = Date.now()

      // Extract payment signature from MCP request metadata or context
      // MCP clients can pass custom headers via the transport layer
      const paymentSig = (extra as Record<string, unknown>)?.paymentSignature as string | undefined

      // ── Payment gate ─────────────────────────────────────────────────────────
      if (!paymentSig) {
        const queryId = crypto.randomUUID()
        const payment = createPaymentRequest(queryId)
        return {
          content: [{
            type: "text" as const,
            text: JSON.stringify({
              error:          "payment_required",
              message:        `This tool costs ${PRICE_USDC} USDC per call.`,
              query_id:       queryId,
              payment_to:     payment.recipient,
              amount_usdc:    payment.amount_usdc,
              memo_required:  payment.memo_required,
              instructions:   payment.instructions,
            }, null, 2),
          }],
          isError: true,
        }
      }

      const verification = await verifyPayment(paymentSig)
      if (!verification.valid) {
        return {
          content: [{
            type: "text" as const,
            text: JSON.stringify({
              error:   "payment_invalid",
              message: verification.error,
              retry:   "Ensure USDC transaction is confirmed and include X-Payment-Signature header",
            }, null, 2),
          }],
          isError: true,
        }
      }

      // ── Run analysis ─────────────────────────────────────────────────────────
      try {
        const createdTs = pairCreatedAt ? pairCreatedAt / 1000 : undefined
        const report    = await getCabalReport(mintAddress, createdTs)

        // Format as structured text that Claude reads well
        const summary = [
          `# Cabal-Hunter Report: ${report.token_name || mintAddress.slice(0, 8)}`,
          ``,
          `**Mint:** ${mintAddress}`,
          `**Risk Level:** ${report.risk}`,
          `**Cabal Score:** ${report.cabal_score}/100`,
          `**Controlled:** ${report.is_controlled ? "YES" : "No"}`,
          ``,
          `## Verdict`,
          report.verdict,
          ``,
          `## Coordinated Clusters`,
          report.coordinated_clusters.length === 0
            ? "None detected."
            : report.coordinated_clusters.map(c =>
                `- **${c.wallet_count} wallets** funded by ${c.master_short}: ` +
                `${c.combined_pct.toFixed(1)}% of supply combined [${c.risk}]`
              ).join("\n"),
          ``,
          `## Analysis Metadata`,
          `- Wallets traced: ${report.wallets_checked}`,
          `- Analysis time: ${Date.now() - t0}ms`,
          `- Source: ${report.source}`,
          report.cached_age_seconds !== undefined
            ? `- Cache age: ${report.cached_age_seconds}s`
            : "",
          `- Payment: ${verification.amount_usdc?.toFixed(6)} USDC verified`,
        ].filter(Boolean).join("\n")

        return {
          content: [
            { type: "text" as const, text: summary },
            { type: "text" as const, text: "\n\n**Raw JSON:**\n```json\n" + JSON.stringify(report, null, 2) + "\n```" },
          ],
        }
      } catch (err) {
        return {
          content: [{
            type: "text" as const,
            text: JSON.stringify({
              error:   "analysis_failed",
              message: err instanceof Error ? err.message : "Unknown error",
              note:    "Payment received. Contact support if this persists.",
            }, null, 2),
          }],
          isError: true,
        }
      }
    }
  )

  return server
}

/** Mount the MCP server onto an Express app at /mcp */
export async function mountMcp(app: import("express").Application): Promise<void> {
  // Create a NEW McpServer + transport per request — the SDK does not support
  // reusing a single server instance across multiple HTTP connections.
  // "Already connected to a transport" is thrown if you reuse the same instance.
  app.post("/mcp", async (req: Request, res: Response) => {
    // Health checkers (glama) send plain POST without MCP Accept headers.
    const accept = req.headers.accept ?? ""
    if (!accept.includes("text/event-stream") && !accept.includes("application/json")) {
      res.json({
        service:   "cabal-hunter",
        version:   process.env.MCP_SERVER_VERSION ?? "1.0.0",
        transport: "streamable-http",
        endpoint:  "/mcp",
        tool:      "check_cabal_risk",
        status:    "ok",
      })
      return
    }
    try {
      const server    = createMcpServer()
      // stateless: false allows sequential requests (initialize + tools/list) in separate POSTs
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,  // stateless mode — no session tracking needed
      })
      await server.connect(transport)
      await transport.handleRequest(req, res, req.body)
    } catch (err) {
      console.error("[mcp] POST handler error:", err)
      if (!res.headersSent) res.status(500).json({ error: "mcp_error", message: String(err) })
    }
  })

  app.get("/mcp", async (req: Request, res: Response) => {
    // Health checkers (glama, uptime monitors) send plain GET without MCP Accept headers.
    // Return server info so they get a clean 200 rather than 406 Not Acceptable.
    const accept = req.headers.accept ?? ""
    if (!accept.includes("text/event-stream")) {
      res.json({
        service:   "cabal-hunter",
        version:   process.env.MCP_SERVER_VERSION ?? "1.0.0",
        transport: "streamable-http",
        endpoint:  "/mcp",
        tool:      "check_cabal_risk",
        status:    "ok",
      })
      return
    }
    try {
      const server    = createMcpServer()
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => crypto.randomUUID(),
      })
      await server.connect(transport)
      await transport.handleRequest(req, res)
    } catch (err) {
      console.error("[mcp] GET handler error:", err)
      if (!res.headersSent) res.status(500).json({ error: "mcp_error", message: String(err) })
    }
  })

  // MCP discovery manifest (used by Claude Desktop and mcp.so)
  app.get("/.well-known/mcp.json", (_req, res) => {
    res.json({
      name:        process.env.MCP_SERVER_NAME ?? "cabal-hunter",
      version:     process.env.MCP_SERVER_VERSION ?? "1.0.0",
      description: "On-chain Solana token cabal & bubble-map detection API",
      server_url:  `https://${process.env.DOMAIN ?? "your-domain.com"}/mcp`,
      transport:   "http",
      tools: [{
        name:        "check_cabal_risk",
        description: "Detect coordinated wallets and insider clusters in any Solana token",
        price:       `${PRICE_USDC} USDC per call`,
        payment:     "Solana SPL USDC via X-Payment-Signature header",
      }],
    })
  })

  console.log("[mcp] MCP server mounted at /mcp")
}
