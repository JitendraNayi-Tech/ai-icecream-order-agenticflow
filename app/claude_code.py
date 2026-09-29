"""LLM backend that runs agents through the local Claude Code CLI (`claude -p`).

Uses the Claude Code login on this machine (e.g. a Pro/Max subscription) instead of an API key.
Personal, local use only: every agent turn spawns one `claude` process.

Agent tools are not run by the CLI itself. Each run gets an MCP config that points back at
this app's /mcp/{customer_id}/{agent} endpoint (see app/mcp_bridge.py), so Claude Code
calls the same Python tool functions the API backend uses.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app import config, usage

AGENT_NAMES = ("intake", "recommender", "checkout", "tracker", "followup")

# Empty working dir so the CLI doesn't pick up this repo's CLAUDE.md or project settings.
_WORKDIR = Path(tempfile.gettempdir()) / "icecream_claude_code"


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeCodeTimeout(ClaudeCodeError):
    pass


def find_claude() -> str:
    if config.CLAUDE_BIN:
        return config.CLAUDE_BIN
    found = shutil.which("claude")
    if not found:
        raise ClaudeCodeError("Claude Code CLI not found. Install it or set CLAUDE_BIN in .env.")
    # npm on Windows installs a .cmd shim; call the real exe to avoid cmd.exe argument quoting.
    exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return str(exe) if exe.exists() else found


def _env() -> dict:
    env = dict(os.environ)
    # Force the CLI's own login, and don't let it think it's nested inside another session.
    for key in list(env):
        if key in ("ANTHROPIC_API_KEY", "CLAUDECODE") or key.startswith("CLAUDE_CODE_"):
            env.pop(key)
    return env


def _run(args: list[str], prompt: str) -> dict:
    _WORKDIR.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(
            [find_claude(), "-p", "--output-format", "json", "--no-session-persistence", *args],
            input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=_WORKDIR,
            env=_env(), timeout=config.CLAUDE_CODE_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ClaudeCodeTimeout(f"claude took longer than {config.CLAUDE_CODE_TIMEOUT:.0f}s")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ClaudeCodeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    if data.get("is_error"):
        raise ClaudeCodeError(f"claude error: {data.get('result') or data.get('subtype')}")
    usage.record_claude_code(data)  # tokens + API-equivalent cost for the cost panel
    return data


def render_transcript(messages: list[dict]) -> str:
    who = {"user": "Customer", "assistant": "Shop"}
    return "\n\n".join(f"{who[m['role']]}: {m['content']}" for m in messages)


async def run_agent(system: str, messages: list[dict], effort: str, *, agent: str,
                    customer_id: str, volatile: str = "") -> str:
    mcp = {"mcpServers": {"shop": {
        "type": "http", "url": f"{config.APP_URL}/mcp/{customer_id}/{agent}"}}}
    args = [
        "--model", config.CLAUDE_CODE_MODEL,
        "--effort", effort,
        "--system-prompt", system,
        "--tools", "",  # no built-in tools (Bash, file edits, ...) - only the shop's MCP tools
        "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
        "--allowedTools", "mcp__shop",
    ]
    # --system-prompt stays identical across turns and customers (the CLI caches it);
    # the per-turn details ride in the prompt, after the conversation.
    prompt = (f"Conversation so far:\n\n{render_transcript(messages)}\n\n"
              f"Current details:\n{volatile}\n\n"
              "Write the shop's next chat message to the customer (use your tools as needed). "
              "Output only the message text.")
    data = await asyncio.to_thread(_run, args, prompt)
    return (data.get("result") or "").strip() or "(no reply)"


async def route(system: str, transcript: str, state: str) -> str:
    args = ["--model", config.CLAUDE_CODE_ROUTER_MODEL, "--effort", "low", "--tools", "",
            "--strict-mcp-config", "--system-prompt",
            system + "\nAnswer with exactly one word: the agent name."]
    prompt = f"Session state:\n{state}\n\nConversation (latest last):\n{transcript}"
    data = await asyncio.to_thread(_run, args, prompt)
    answer = (data.get("result") or "").strip().lower()
    return next((name for name in AGENT_NAMES if name in answer), "intake")
