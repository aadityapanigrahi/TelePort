"""
AI Router - manages provider selection and fallback.

Priority order: claude-code → codex → gemini
Falls through to the next provider when a rate/usage limit is detected.

Binary paths can be overridden via environment variables:
    TELEPORT_CLAUDE_BIN, TELEPORT_CODEX_BIN, TELEPORT_GEMINI_BIN
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path

_ANSI_ESC = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\r")

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and carriage returns."""
    return _ANSI_ESC.sub("", text)
from typing import Optional

log = logging.getLogger("teleport.ai_router")

# ---------------------------------------------------------------------------
# Configurable binary paths (env-var overridable)
# ---------------------------------------------------------------------------

CLAUDE_BIN = os.environ.get("TELEPORT_CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
GEMINI_BIN = os.environ.get("TELEPORT_GEMINI_BIN", "/usr/local/bin/gemini")
CODEX_BIN  = os.environ.get("TELEPORT_CODEX_BIN", "/usr/local/bin/codex")

# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------

RATE_LIMIT_PATTERNS = [
    r"rate.?limit",
    r"quota.?exceed",
    r"usage.?limit",
    r"too.?many.?request",
    r"resource.?exhausted",
    r"\b429\b",
    r"overloaded",
    r"try.?again.?later",
    r"capacity",
    r"billing",
    r"plan.?limit",
    r"daily.?limit",
    r"monthly.?limit",
]

# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

PROVIDERS = [
    {
        "name": "claude",
        "cmd": [CLAUDE_BIN, "-p", None, "--dangerously-skip-permissions"],
        "prompt_index": 2,
    },
    {
        "name": "codex",
        "cmd": [CODEX_BIN, "exec", None, "--full-auto", "--skip-git-repo-check"],
        "prompt_index": 2,
    },
    {
        "name": "gemini",
        "cmd": [GEMINI_BIN, "-p", None, "--yolo"],
        "prompt_index": 2,
    },
]

# Error categories for structured reporting
ERR_NONE         = "none"
ERR_RATE_LIMITED = "rate_limited"
ERR_TIMEOUT      = "timeout"
ERR_NOT_FOUND    = "binary_not_found"
ERR_CRASH        = "crash"
ERR_UNKNOWN      = "unknown"


def is_rate_limited(output: str) -> bool:
    """Return True if the output suggests a rate or usage limit was hit."""
    text = output.lower()
    return any(re.search(pattern, text) for pattern in RATE_LIMIT_PATTERNS)


def _clean_env(env: dict) -> dict:
    """Strip env vars that cause AI CLIs to think they're running nested."""
    return {
        k: v for k, v in env.items()
        if not k.startswith("CLAUDE_CODE")
        and k not in ("CLAUDECODE", "__CFBundleIdentifier")
    }


# ---------------------------------------------------------------------------
# Single-provider execution (structured result)
# ---------------------------------------------------------------------------

def run_provider(
    provider: dict,
    prompt: str,
    cwd: str,
    timeout: int,
    env: dict = None,
) -> dict:
    """
    Run a single AI provider and return a structured result dict.

    Keys: output, exit_code, error_category, elapsed_seconds, provider
    """
    cmd = list(provider["cmd"])
    cmd[provider["prompt_index"]] = prompt
    run_env = _clean_env(env or os.environ.copy())
    name = provider["name"]

    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
        elapsed = round(time.monotonic() - t0, 1)
        stdout = _strip_ansi(result.stdout or "")
        stderr = _strip_ansi(result.stderr or "")

        # Codex (and other providers) dump their agent loop / progress to
        # stderr.  We do NOT want that flooding the user's Telegram chat.
        # Only fall back to stderr if stdout produced nothing useful.
        if stdout.strip():
            output = stdout
        elif stderr.strip():
            output = stderr
        else:
            output = ""

        if is_rate_limited(output):
            log.info("%s: rate-limited (%.1fs)", name, elapsed)
            return {
                "provider": name,
                "output": output,
                "exit_code": result.returncode,
                "error_category": ERR_RATE_LIMITED,
                "elapsed_seconds": elapsed,
            }

        category = ERR_NONE if result.returncode == 0 else ERR_CRASH
        if category == ERR_CRASH:
            log.warning("%s: non-zero exit %d (%.1fs)", name, result.returncode, elapsed)

        return {
            "provider": name,
            "output": output,
            "exit_code": result.returncode,
            "error_category": category,
            "elapsed_seconds": elapsed,
        }

    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t0, 1)
        log.warning("%s: timed out after %ds", name, timeout)
        return {
            "provider": name,
            "output": f"[{name}] Timed out after {timeout}s.",
            "exit_code": -1,
            "error_category": ERR_TIMEOUT,
            "elapsed_seconds": elapsed,
        }
    except FileNotFoundError:
        elapsed = round(time.monotonic() - t0, 1)
        log.warning("%s: binary not found at %s", name, provider["cmd"][0])
        return {
            "provider": name,
            "output": f"[{name}] Binary not found at {provider['cmd'][0]}.",
            "exit_code": -1,
            "error_category": ERR_NOT_FOUND,
            "elapsed_seconds": elapsed,
        }
    except Exception as e:
        elapsed = round(time.monotonic() - t0, 1)
        log.exception("%s: unexpected error", name)
        return {
            "provider": name,
            "output": f"[{name}] Unexpected error: {e}",
            "exit_code": -1,
            "error_category": ERR_UNKNOWN,
            "elapsed_seconds": elapsed,
        }


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

def run_with_fallback(
    prompt: str,
    cwd: str,
    timeout: int = 300,
    preferred: Optional[str] = None,
    env: dict = None,
) -> dict:
    """
    Try each provider in priority order, falling back on rate limits.

    Returns a structured dict with keys:
        provider, output, exit_code, error_category, elapsed_seconds,
        fallback_log (list of dicts for each provider attempted)
    """
    providers = list(PROVIDERS)

    if preferred:
        providers.sort(key=lambda p: 0 if p["name"] == preferred else 1)

    fallback_log = []

    for provider in providers:
        result = run_provider(provider, prompt, cwd, timeout, env=env)
        fallback_log.append(result)

        # Only fall through on rate limits or missing binaries
        if result["error_category"] not in (ERR_RATE_LIMITED, ERR_NOT_FOUND):
            result["fallback_log"] = fallback_log
            return result

    # All providers exhausted
    summary_lines = []
    for entry in fallback_log:
        summary_lines.append(
            f"  • {entry['provider']}: {entry['error_category']} ({entry['elapsed_seconds']}s)"
        )
    summary = "\n".join(summary_lines)

    return {
        "provider": "none",
        "output": (
            "All AI providers are currently rate-limited or unavailable.\n\n"
            f"{summary}\n\n"
            "💡 Suggestions:\n"
            "  • Wait a few minutes and try again\n"
            "  • Use `/use <provider>` to force a specific provider\n"
            "  • Check provider status with `/status`"
        ),
        "exit_code": -1,
        "error_category": ERR_RATE_LIMITED,
        "elapsed_seconds": sum(e["elapsed_seconds"] for e in fallback_log),
        "fallback_log": fallback_log,
    }


# ---------------------------------------------------------------------------
# Provider inspection
# ---------------------------------------------------------------------------

def check_binary(name: str) -> dict:
    """Check a specific provider binary. Returns {exists, path, version}."""
    provider = next((p for p in PROVIDERS if p["name"] == name), None)
    if not provider:
        return {"exists": False, "path": None, "version": None}

    bin_path = provider["cmd"][0]
    exists = Path(bin_path).exists()
    version = None

    if exists:
        try:
            result = subprocess.run(
                [bin_path, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            version = result.stdout.strip().split("\n")[0] or result.stderr.strip().split("\n")[0]
        except Exception:
            version = "unknown"

    return {"exists": exists, "path": bin_path, "version": version}


def check_providers() -> dict[str, bool]:
    """Quick check of which provider binaries exist on disk."""
    return {
        p["name"]: Path(p["cmd"][0]).exists()
        for p in PROVIDERS
    }


def check_providers_detailed() -> dict[str, dict]:
    """Detailed check of all providers with version info."""
    return {p["name"]: check_binary(p["name"]) for p in PROVIDERS}
