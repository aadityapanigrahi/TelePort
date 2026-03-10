"""
TelePort MCP Server

Exposes TelePort's AI-execution capabilities as MCP tools so that
Claude Code, VS Code, Antigravity (Google's VS Code fork), or any other
MCP-compatible client can drive tasks on this machine.

Register in Claude Code (~/.claude/mcp.json):
  {
    "mcpServers": {
      "teleport": {
        "command": "python",
        "args": ["/Users/aadityapanigrahi/CodingProjects/TelePort/src/mcp_server.py"]
      }
    }
  }

Register in VS Code / Antigravity (settings.json):
  {
    "mcp": {
      "servers": {
        "teleport": {
          "command": "python",
          "args": ["/Users/aadityapanigrahi/CodingProjects/TelePort/src/mcp_server.py"]
        }
      }
    }
  }

Available tools
---------------
run_ai_task          – run any coding / analysis task with the AI fallback chain
process_file         – process a local file (PDF, CSV, image, …) with an instruction
clone_and_run        – clone a GitHub repo and run it
check_providers      – see which AI CLIs are available and which was last used
list_workspaces      – list recent task workspace directories
send_telegram        – send a Telegram message back to your phone
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure src/ siblings are importable
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import ai_router
import task_runner

load_dotenv(os.path.join(os.path.expanduser("~"), ".teleport.env"))
load_dotenv(os.path.join(os.getcwd(), ".env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_last_provider: Optional[str] = None

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

server = Server("teleport")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_ai_task",
            description=(
                "Run a coding, data-analysis, or scripting task on the Mac Mini "
                "using the AI CLI fallback chain (claude → codex → gemini). "
                "Returns text output and a list of generated files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "Natural-language task description.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["claude", "codex", "gemini"],
                        "description": "Force a specific AI provider (optional).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 300).",
                        "default": 300,
                    },
                },
                "required": ["instruction"],
            },
        ),
        Tool(
            name="process_file",
            description=(
                "Process a local file on the Mac Mini (PDF, CSV, image, code, …) "
                "using an AI instruction. The file is copied into a workspace and "
                "the AI runs there. Returns text output and generated files (plots, etc.)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file on the Mac Mini.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "What the AI should do with the file.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["claude", "codex", "gemini"],
                        "description": "Force a specific AI provider (optional).",
                    },
                },
                "required": ["file_path", "instruction"],
            },
        ),
        Tool(
            name="clone_and_run",
            description=(
                "Clone a GitHub repository onto the Mac Mini and run it via AI. "
                "Returns execution output and any generated files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "github_url": {
                        "type": "string",
                        "description": "Full GitHub repo URL (https://github.com/user/repo).",
                    },
                    "instruction": {
                        "type": "string",
                        "description": (
                            "What to do with the repo after cloning. "
                            "Defaults to: explore, understand, and run it."
                        ),
                        "default": "Explore the repository, understand what it does, and run it.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["claude", "codex", "gemini"],
                    },
                },
                "required": ["github_url"],
            },
        ),
        Tool(
            name="check_providers",
            description="Return the availability of each AI CLI binary and which was last used.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_workspaces",
            description="List recent task workspace directories on the Mac Mini.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of workspaces to return (default 10).",
                        "default": 10,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="send_telegram",
            description="Send a text message to your Telegram chat from the Mac Mini.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Text to send.",
                    }
                },
                "required": ["message"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _fmt_result(result: dict) -> str:
    """Format a task_runner result dict into a readable string."""
    elapsed = result.get("elapsed_seconds", "?")
    error_cat = result.get("error_category", "none")
    exit_code = result.get("exit_code", "?")

    lines = [
        f"Provider : {result['provider']}",
        f"Workspace: {result['workspace']}",
        f"Elapsed  : {elapsed}s",
        f"Status   : {error_cat} (exit {exit_code})",
    ]
    if result.get("github_url"):
        lines.append(f"GitHub   : {result['github_url']}")
        if result.get("clone_msg"):
            lines.append(f"Clone    : {result['clone_msg']}")

    # Show fallback chain if multiple providers were tried
    fallback_log = result.get("fallback_log", [])
    if len(fallback_log) > 1:
        lines.append("")
        lines.append("Fallback chain:")
        for entry in fallback_log:
            cat = entry.get('error_category', '?')
            t = entry.get('elapsed_seconds', 0)
            lines.append(f"  {entry['provider']}: {cat} ({t}s)")

    lines.append("")
    output = result["output"].strip()
    lines.append(output if output else "(no text output)")

    if result["files"]:
        lines.append("")
        lines.append("Generated files:")
        for f in result["files"]:
            lines.append(f"  {f}")

    return "\n".join(lines)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_provider

    loop = asyncio.get_event_loop()

    # ------------------------------------------------------------------
    if name == "run_ai_task":
        instruction = arguments["instruction"]
        provider    = arguments.get("provider") or _last_provider
        timeout     = int(arguments.get("timeout", 300))

        result = await loop.run_in_executor(
            None,
            lambda: task_runner.run_task(
                user_instruction=instruction,
                timeout=timeout,
                preferred_provider=provider,
            ),
        )
        _last_provider = result["provider"]
        return [TextContent(type="text", text=_fmt_result(result))]

    # ------------------------------------------------------------------
    elif name == "process_file":
        file_path   = Path(arguments["file_path"])
        instruction = arguments["instruction"]
        provider    = arguments.get("provider") or _last_provider

        if not file_path.exists():
            return [TextContent(type="text", text=f"File not found: {file_path}")]

        result = await loop.run_in_executor(
            None,
            lambda: task_runner.run_task(
                user_instruction=instruction,
                attached_files=[file_path],
                preferred_provider=provider,
            ),
        )
        _last_provider = result["provider"]
        return [TextContent(type="text", text=_fmt_result(result))]

    # ------------------------------------------------------------------
    elif name == "clone_and_run":
        github_url  = arguments["github_url"]
        instruction = arguments.get("instruction") or (
            "Explore the repository, understand what it does, and run it. "
            "Show all output."
        )
        provider = arguments.get("provider") or _last_provider

        # Inject the URL so task_runner picks it up automatically
        full_instruction = f"{instruction}\n\nGitHub repo: {github_url}"

        result = await loop.run_in_executor(
            None,
            lambda: task_runner.run_task(
                user_instruction=full_instruction,
                preferred_provider=provider,
            ),
        )
        _last_provider = result["provider"]
        return [TextContent(type="text", text=_fmt_result(result))]

    # ------------------------------------------------------------------
    elif name == "check_providers":
        detailed = ai_router.check_providers_detailed()
        lines = []
        for pname, info in detailed.items():
            if info["exists"]:
                version = info.get("version") or "unknown"
                lines.append(f"✓ {pname} — {version} ({info['path']})")
            else:
                lines.append(f"✗ {pname} — not found at {info['path']}")
        lines.append(f"\nLast used: {_last_provider or 'none yet'}")
        lines.append(f"Fallback order: claude → codex → gemini")
        return [TextContent(type="text", text="\n".join(lines))]

    # ------------------------------------------------------------------
    elif name == "list_workspaces":
        limit   = int(arguments.get("limit", 10))
        ws_base = task_runner.WORKSPACE_BASE

        if not ws_base.exists():
            return [TextContent(type="text", text="No workspaces yet.")]

        workspaces = sorted(
            [d for d in ws_base.iterdir() if d.is_dir() and not d.name.startswith("_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )[:limit]

        if not workspaces:
            return [TextContent(type="text", text="No workspaces yet.")]

        lines = []
        for ws in workspaces:
            mtime = datetime.fromtimestamp(ws.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            meta  = ws / "meta.txt"
            preview = ""
            if meta.exists():
                first_line = meta.read_text().splitlines()[1] if meta.read_text().splitlines() else ""
                preview = first_line.replace("Instruction:", "").strip()[:80]
            lines.append(f"{mtime}  {ws.name}  {preview}")

        return [TextContent(type="text", text="\n".join(lines))]

    # ------------------------------------------------------------------
    elif name == "send_telegram":
        message = arguments["message"]

        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return [TextContent(
                type="text",
                text="Telegram credentials not configured. Run `teleport config`.",
            )]

        try:
            from telegram import Bot
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            return [TextContent(type="text", text="Message sent.")]
        except Exception as exc:
            return [TextContent(type="text", text=f"Failed: {exc}")]

    # ------------------------------------------------------------------
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_run())
