"""Jarvis Agent — Gemini 2.5 Flash with full shell/file access to the trading server.

Uses google.genai function calling + streaming. The async generator yields SSE-friendly
event dicts; the HTTP handler in dashboard_api.py serialises them to text/event-stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Generator

REPO_ROOT  = Path('/home/ubuntu/eliza')
_GEMINI_KEY = os.getenv('GOOGLE_GENERATIVE_AI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')
MODEL      = 'gemini-2.5-flash'
MAX_ROUNDS = 8   # max tool-use rounds per request

# ── Tool definitions (OpenAPI-ish schema, converted to Gemini at call time) ────

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Execute a shell command on the Frankfurt trading server. "
            "Working directory is /home/ubuntu/eliza. "
            "Use for: tailing logs (tail -n 50 traderbot.out), checking git, "
            "restarting the bot (sudo systemctl restart traderbot.service), "
            "checking systemctl status, editing .env, deploying files, etc."
        ),
        "properties": {
            "command": ("string", "Shell command to execute"),
            "timeout": ("number", "Timeout seconds (default 30, max 120)"),
        },
        "required": ["command"],
    },
    {
        "name": "read_file",
        "description": (
            "Read a file from the server. Path can be absolute or relative to "
            "/home/ubuntu/eliza. Use offset/limit for large files."
        ),
        "properties": {
            "path":   ("string", "File path"),
            "offset": ("number", "Start line (0-indexed, default 0)"),
            "limit":  ("number", "Max lines (default 300)"),
        },
        "required": ["path"],
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file on the server. Creates parent directories.",
        "properties": {
            "path":    ("string", "File path"),
            "content": ("string", "Full content to write"),
        },
        "required": ["path", "content"],
    },
]

# ── Tool execution (synchronous — runs in a thread) ──────────────────────────────

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

# ── Gemini schema conversion ─────────────────────────────────────────────────────

def _make_gemini_tools():
    from google.genai import types as gt
    _TYPE = {"string": gt.Type.STRING, "number": gt.Type.NUMBER, "boolean": gt.Type.BOOLEAN}
    decls = []
    for t in TOOLS:
        props = {
            k: gt.Schema(type=_TYPE.get(vtype, gt.Type.STRING), description=desc)
            for k, (vtype, desc) in t["properties"].items()
        }
        decls.append(gt.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=gt.Schema(
                type=gt.Type.OBJECT,
                properties=props,
                required=t["required"],
            ),
        ))
    return [gt.Tool(function_declarations=decls)]

# ── Synchronous streaming agent (runs in a thread) ──────────────────────────────

def _sync_agent(
    message: str,
    history: list[dict],
    status_context: str,
    event_queue: "asyncio.Queue[dict | None]",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Run the Gemini agentic loop in a background thread, posting events to the queue."""
    def emit(ev: dict) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, ev)

    def done() -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, None)

    if not _GEMINI_KEY:
        emit({"type": "error", "message": "GOOGLE_GENERATIVE_AI_API_KEY not set on server."})
        done(); return

    try:
        from google import genai as _gai
        from google.genai import types as gt
    except ImportError:
        emit({"type": "error", "message": "google-genai package not installed."})
        done(); return

    client = _gai.Client(api_key=_GEMINI_KEY)
    gemini_tools = _make_gemini_tools()

    system = f"""You are J.A.R.V.I.S. — autonomous trading assistant with full server access (AWS Frankfurt).

INFRASTRUCTURE
• Repo: /home/ubuntu/eliza
• Restart bot: sudo systemctl restart traderbot.service
• Logs: /home/ubuntu/eliza/traderbot.out (tail for recent activity)
• Python venv: /home/ubuntu/eliza/.venv_py/bin/python

LIVE BOT STATE
{status_context}

TOOLS: bash, read_file, write_file — full server access.
When doing tasks: do it, verify it worked, report concisely. Be direct."""

    # Build conversation history
    contents: list = []
    for h in history[-12:]:
        role = "user" if h.get("role") == "user" else "model"
        text = h.get("content", "")
        if text:
            contents.append(gt.Content(role=role, parts=[gt.Part.from_text(text=str(text))]))
    contents.append(gt.Content(role="user", parts=[gt.Part.from_text(text=message)]))

    cfg = gt.GenerateContentConfig(
        tools=gemini_tools,
        system_instruction=system,
        temperature=0.2,
        max_output_tokens=4096,
    )

    for _round in range(MAX_ROUNDS):
        # Collect the streamed response
        fn_calls: list[tuple[str, dict]] = []
        text_buf = ""

        try:
            stream = client.models.generate_content_stream(
                model=MODEL, contents=contents, config=cfg,
            )
            for chunk in stream:
                if not chunk.candidates:
                    continue
                for part in chunk.candidates[0].content.parts:
                    if getattr(part, "text", None):
                        emit({"type": "text", "content": part.text})
                        text_buf += part.text
                    fc = getattr(part, "function_call", None)
                    if fc:
                        args = dict(fc.args) if fc.args else {}
                        cmd_hint = args.get("command") or args.get("path", "")
                        emit({"type": "tool_call", "name": fc.name, "cmd": cmd_hint})
                        fn_calls.append((fc.name, args))
        except Exception as e:
            emit({"type": "error", "message": f"Gemini error: {e}"})
            break

        if not fn_calls:
            break   # no tool calls — we're done

        # Build the model turn to add to history
        model_parts: list = []
        if text_buf:
            model_parts.append(gt.Part.from_text(text=text_buf))
        for fname, fargs in fn_calls:
            model_parts.append(gt.Part.from_function_call(name=fname, args=fargs))
        contents.append(gt.Content(role="model", parts=model_parts))

        # Execute tools and build function response turn
        result_parts: list = []
        for fname, fargs in fn_calls:
            result = _run_tool(fname, fargs)
            emit({"type": "tool_result", "name": fname, "preview": result[:400]})
            result_parts.append(gt.Part.from_function_response(
                name=fname, response={"output": result},
            ))
        contents.append(gt.Content(role="user", parts=result_parts))

    done()

# ── Public async generator ───────────────────────────────────────────────────────

async def stream_jarvis(
    message: str,
    history: list[dict],
    status_context: str,
):
    """Yield SSE-friendly event dicts.

    Event shapes:
      {"type": "text",        "content": "..."}
      {"type": "tool_call",   "name": "bash",  "cmd": "..."}
      {"type": "tool_result", "name": "bash",  "preview": "..."}
      {"type": "error",       "message": "..."}
      {"type": "done"}
    """
    loop  = asyncio.get_event_loop()
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
            yield {"type": "error", "message": "Agent timed out."}
            break
        if event is None:
            break
        yield event

    yield {"type": "done"}
