import asyncio
import json
import os
import sys
import uuid

from elizaos import AgentRuntime, Character
from elizaos.bootstrap.plugin import create_bootstrap_plugin
from elizaos.bootstrap.types import CapabilityConfig
from elizaos.plugins.solana import create_solana_plugin


HTTP_PORT = int(os.environ.get("TRADERBOT_PORT", "3001"))


async def start_http_bridge(runtime: AgentRuntime) -> None:
    """Start a lightweight HTTP server so external clients (including the TS
    server) can POST messages to the Python agent runtime.

    Endpoints:
        POST /message   {"text": "...", "user_id": "...", "room_id": "..."}
                        -> {"text": "...", "thought": "...", "actions": [...]}
        GET  /health    -> {"status": "ok", "agent": "TraderBot"}
    """
    try:
        from aiohttp import web
    except ImportError:
        print(
            "[bridge] aiohttp not installed — HTTP bridge disabled. "
            "Install with: pip install aiohttp",
            file=sys.stderr,
        )
        return

    async def handle_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)

        text = body.get("text", "").strip()
        if not text:
            return web.json_response({"error": "text is required"}, status=400)

        user_id_str = body.get("user_id") or str(uuid.uuid4())
        room_id_str = body.get("room_id") or str(uuid.uuid4())

        from elizaos.types.primitives import as_uuid

        response = await runtime.send_message(
            text,
            user_id=as_uuid(user_id_str),
            room_id=as_uuid(room_id_str),
        )

        return web.json_response(
            {
                "text": response.content.text or "",
                "thought": getattr(response.content, "thought", None),
                "actions": getattr(response.content, "actions", None),
            }
        )

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "agent": runtime.character.name,
                "providers": [p.name for p in runtime.providers],
                "actions": [a.name for a in runtime.actions],
            }
        )

    app = web.Application()
    app.router.add_post("/message", handle_message)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
    await site.start()
    print(f"[bridge] HTTP bridge listening on http://0.0.0.0:{HTTP_PORT}")
    print(f"[bridge]   POST /message  {{\"text\": \"...\"}}")
    print(f"[bridge]   GET  /health")


async def interactive_repl(runtime: AgentRuntime) -> None:
    """Simple interactive console loop for chatting with the bot."""
    user_id = uuid.uuid4()
    room_id = uuid.uuid4()

    from elizaos.types.primitives import as_uuid

    uid = as_uuid(str(user_id))
    rid = as_uuid(str(room_id))

    print("\n--- TraderBot Interactive REPL ---")
    print("Type your message and press Enter. Ctrl+C or 'quit' to exit.\n")

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: input("You: ")
            )
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if user_input.strip().lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        if not user_input.strip():
            continue

        try:
            response = await runtime.send_message(user_input.strip(), uid, rid)
            print(f"TraderBot: {response.content.text or '(no response)'}\n")
        except Exception as exc:
            print(f"[error] {exc}\n", file=sys.stderr)


async def main():
    character = Character(
        name="TraderBot",
        username="traderbot",
        bio=(
            "You are a transparent Solana trading bot. "
            "You can report your wallet address, SOL balance, token prices, "
            "recent pump.fun launches, and execute token trades."
        ),
        system=(
            "You are TraderBot. When the user asks for your wallet address or SOL balance, "
            "use the solana_wallet provider data in your context to answer accurately. "
            "For token prices use GET_TOKEN_PRICE. For trading use BUY_TOKEN or SELL_TOKEN. "
            "For recent launches use the pump_fun_launches provider."
        ),
    )

    # Bootstrap plugin (disable extended to avoid duplicate solana_wallet provider)
    bootstrap = create_bootstrap_plugin(CapabilityConfig(enable_extended=False))
    solana = create_solana_plugin()

    agent_id = uuid.UUID("63adb390-3424-0d07-bf74-e7d049dfc2dc")
    runtime = AgentRuntime(character=character, agent_id=agent_id, plugins=[bootstrap, solana])
    await runtime.initialize()

    print(f"TraderBot runtime started agentId={runtime.agent_id}")
    print(f"Providers registered: {[p.name for p in runtime.providers]}")
    print(f"Actions registered:   {[a.name for a in runtime.actions]}")

    # Start HTTP bridge in background
    await start_http_bridge(runtime)

    # Run interactive REPL (blocks until user exits)
    await interactive_repl(runtime)


if __name__ == "__main__":
    asyncio.run(main())
