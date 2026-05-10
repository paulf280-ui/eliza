"""Jarvis Agent — Gemini 1.5 Flash with full shell/file access to the trading server.

Uses the installed google-generativeai SDK (0.8.x).
The async generator yields SSE-friendly event dicts that the dashboard_api.py
handler serialises to text/event-stream.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

REPO_ROOT   = Path('/home/ubuntu/eliza')
_GEMINI_KEY = os.getenv('GOOGLE_GENERATIVE_AI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')
MODEL       = 'gemini-2.5-flash'
MAX_ROUNDS  = 8

# ── Tool execution (synchronous) ────────────────────────────────────────────────

def _bash(command: str, timeout: int = 30) -> str:
    timeout = min(max(int(timeout or 30), 5), 120)
    try:
        r = subprocess.run(
            command, shell=True, cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=timeout,
        )
        out = r.stdout
        if r.returncode not in (0, 1) and r.stderr:
            out += f"\n[stderr] {r.stderr.strip()}"
        if r.returncode not in (0, 1):
            out += f"\n[exit {r.returncode}]"
        out = out or "(no output)"
        if len(out) > 8000:
            out = out[:8000] + f"\n[truncated — {len(out)} chars total]"
        return out
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


def _read_file(path: str, offset: int = 0, limit: int = 300) -> str:
    try:
        p = Path(path) if Path(path).is_absolute() else REPO_ROOT / path
        if not p.exists():
            return f"[not found: {p}]"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        chunk = lines[offset: offset + limit]
        result = "\n".join(f"{offset + i + 1}\t{l}" for i, l in enumerate(chunk))
        if total > offset + limit:
            result += f"\n[lines {offset+1}–{offset+len(chunk)} of {total} shown]"
        return result or "(empty file)"
    except Exception as e:
        return f"[read error: {e}]"


def _write_file(path: str, content: str) -> str:
    try:
        p = Path(path) if Path(path).is_absolute() else REPO_ROOT / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[wrote {len(content)} chars → {p}]"
    except Exception as e:
        return f"[write error: {e}]"


def _run_tool(name: str, inputs: dict) -> str:
    if name == "bash":
        return _bash(inputs.get("command", ""), inputs.get("timeout", 30))
    if name == "read_file":
        return _read_file(inputs.get("path", ""), int(inputs.get("offset", 0)), int(inputs.get("limit", 300)))
    if name == "write_file":
        return _write_file(inputs.get("path", ""), inputs.get("content", ""))
    return f"[unknown tool: {name}]"

# ── Tool schema (OpenAPI dict — google-generativeai accepts this directly) ───────

_TOOL_DEFS = [
    {
        "name": "bash",
        "description": (
            "Execute a shell command on the Frankfurt trading server. "
            "Working directory is /home/ubuntu/eliza. "
            "Use for: tailing logs, checking git, restarting the bot "
            "(sudo systemctl restart traderbot.service), editing .env, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout seconds (default 30, max 120)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the server. Path absolute or relative to /home/ubuntu/eliza. "
            "Use offset/limit for large files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":   {"type": "string",  "description": "File path"},
                "offset": {"type": "number",  "description": "Start line (0-indexed, default 0)"},
                "limit":  {"type": "number",  "description": "Max lines to read (default 300)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file on the server. Creates parent directories.",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Full content to write"},
            },
            "required": ["path", "content"],
        },
    },
]

# ── Synchronous agent (runs in background thread) ────────────────────────────────

def _sync_agent(
    message: str,
    history: list[dict],
    status_context: str,
    event_queue: "asyncio.Queue[dict | None]",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Run Gemini agentic loop, posting events to the async queue."""

    def emit(ev: dict) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ev)

    def done() -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, None)

    if not _GEMINI_KEY:
        emit({"type": "error", "message": "GOOGLE_GENERATIVE_AI_API_KEY not set on server."})
        done()
        return

    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import google.generativeai as genai  # type: ignore
            from google.generativeai.types import FunctionDeclaration, Tool  # type: ignore
    except ImportError as e:
        emit({"type": "error", "message": f"Import error: {e}"})
        done()
        return

    genai.configure(api_key=_GEMINI_KEY)

    # Build tool declarations
    fn_decls = [FunctionDeclaration(**t) for t in _TOOL_DEFS]
    tool = Tool(function_declarations=fn_decls)

    system = f"""You are J.A.R.V.I.S. — autonomous trading assistant with full server access (AWS Frankfurt).

INFRASTRUCTURE
• Repo: /home/ubuntu/eliza
• Restart bot: sudo systemctl restart traderbot.service
• Logs: /home/ubuntu/eliza/traderbot.out
• Python venv: /home/ubuntu/eliza/.venv_py/bin/python

LIVE BOT STATE
{status_context}

TOOLS: bash, read_file, write_file — full server access.
When doing tasks: do it, verify it worked, report concisely. Be direct."""

    # Build Gemini history format
    gemini_history: list[dict] = []
    for h in history[-12:]:
        role = "user" if h.get("role") == "user" else "model"
        content = h.get("content", "")
        if content:
            gemini_history.append({"role": role, "parts": [content]})

    model = genai.GenerativeModel(
        model_name=MODEL,
        system_instruction=system,
        tools=[tool],
    )
    chat = model.start_chat(history=gemini_history)

    current_message: object = message  # first turn is the user's text; subsequent are function responses

    for _round in range(MAX_ROUNDS):
        try:
            response = chat.send_message(current_message, stream=True)
        except Exception as e:
            emit({"type": "error", "message": f"Gemini error: {e}"})
            break

        fn_calls: list = []
        text_buf = ""

        # Stream chunks
        try:
            for chunk in response:
                # Stream text
                try:
                    if chunk.text:
                        emit({"type": "text", "content": chunk.text})
                        text_buf += chunk.text
                except Exception:
                    pass
                # Collect function calls from this chunk
                try:
                    for part in chunk.parts:
                        fc = getattr(part, "function_call", None)
                        if fc and getattr(fc, "name", None):
                            args = dict(fc.args) if fc.args else {}
                            cmd_hint = args.get("command") or args.get("path", "")
                            emit({"type": "tool_call", "name": fc.name, "cmd": cmd_hint})
                            fn_calls.append(fc)
                except Exception:
                    pass
        except Exception as e:
            emit({"type": "error", "message": f"Stream error: {e}"})
            break

        if not fn_calls:
            break   # no tool calls — done

        # Execute tools and build function response parts
        import google.generativeai.protos as protos  # type: ignore
        response_parts: list = []
        for fc in fn_calls:
            args = dict(fc.args) if fc.args else {}
            result = _run_tool(fc.name, args)
            emit({"type": "tool_result", "name": fc.name, "preview": result[:400]})
            response_parts.append(protos.Part(
                function_response=protos.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                )
            ))

        # Next iteration sends the function results back as a Content message
        current_message = protos.Content(parts=response_parts)

    done()

# ── Public async generator ───────────────────────────────────────────────────────

async def stream_jarvis(
    message: str,
    history: list[dict],
    status_context: str,
):
    """Yield SSE-friendly event dicts for the dashboard HTTP handler."""
    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    t = threading.Thread(
        target=_sync_agent,
        args=(message, history, status_context, queue, loop),
        daemon=True,
    )
    t.start()

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=90.0)
        except asyncio.TimeoutError:
            yield {"type": "error", "message": "Agent timed out after 90s."}
            break
        if event is None:
            break
        yield event

    yield {"type": "done"}
