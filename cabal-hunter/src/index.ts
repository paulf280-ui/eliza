/**
 * index.ts — Cabal-Hunter unified entry point.
 * Starts the Express REST server and mounts the MCP server on the same port.
 */

import "dotenv/config"
import { createApp }     from "./server.js"
import { mountMcp }      from "./mcp-server.js"
import { validatePaymentConfig } from "./payment.js"

const PORT = parseInt(process.env.PORT ?? "8080", 10)

async function main() {
  console.log("╔══════════════════════════════════════════╗")
  console.log("║         CABAL-HUNTER MCP SERVER          ║")
  console.log("║   On-chain Solana token safety oracle     ║")
  console.log("╚══════════════════════════════════════════╝")
  console.log()

  // Validate critical config before starting
  try {
    validatePaymentConfig()
  } catch (err) {
    console.error("[startup] Configuration error:", err instanceof Error ? err.message : err)
    process.exit(1)
  }

  const app = createApp()

  // Mount MCP server alongside REST API on the same port
  await mountMcp(app)

  app.listen(PORT, "0.0.0.0", () => {
    console.log()
    console.log(`[server] Listening on port ${PORT}`)
    console.log(`[server] REST endpoint:  POST http://0.0.0.0:${PORT}/api/scan-cabal`)
    console.log(`[server] MCP endpoint:   POST http://0.0.0.0:${PORT}/mcp`)
    console.log(`[server] Info:           GET  http://0.0.0.0:${PORT}/api/info`)
    console.log(`[server] Health:         GET  http://0.0.0.0:${PORT}/health`)
    console.log()
    console.log("[server] Ready to accept paid queries ✓")
  })

  // Graceful shutdown
  process.on("SIGTERM", () => { console.log("[server] SIGTERM received, shutting down"); process.exit(0) })
  process.on("SIGINT",  () => { console.log("[server] SIGINT received, shutting down");  process.exit(0) })
}

main().catch(err => {
  console.error("[startup] Fatal error:", err)
  process.exit(1)
})
