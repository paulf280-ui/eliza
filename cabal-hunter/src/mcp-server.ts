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

const PRICE_USDC = parseFloat(process.env.PRICE_PER_QUERY_USDC ?? "0.05")

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
      "",
      "Traces funding relationships between the top 20 holders using Helius RPC to",
      "detect coordinated wallets (cabals), insider snipers at launch, and bubble-map",
      "clusters. Returns a cabal confidence score 0–100 and a structured risk report.",
      "",
      "COST: $0.05 USDC per query (paid on Solana mainnet).",
      "PAYMENT: Include X-Payment-Signature header with a valid USDC transaction",
      "signature, or call GET /api/info for payment instructions.",
      "",
      "Typical response time: <100ms for pre-indexed tokens, 2-5s for real-time analysis.",
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
  const mcpServer = createMcpServer()

  // Streamable HTTP transport — required for remote MCP servers
  app.post("/mcp", async (req: Request, res: Response) => {
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => crypto.randomUUID(),
    })
    await mcpServer.connect(transport)
    await transport.handleRequest(req, res, req.body)
  })

  app.get("/mcp", async (req: Request, res: Response) => {
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => crypto.randomUUID(),
    })
    await mcpServer.connect(transport)
    await transport.handleRequest(req, res)
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
