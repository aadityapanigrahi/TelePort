"""
Task Runner - workspace management and task execution.

When a session_dir is provided (normal use), the AI works directly inside
that directory with its uv venv on PATH.  When no session_dir is given
(e.g. called from the MCP server without a session), a temporary isolated
workspace is created under workspace/ instead.
"""

import logging
import os
import re
import time
import threading
import uuid
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from ai_router import LiveOutput, run_with_fallback

log = logging.getLogger("teleport.task_runner")

WORKSPACE_BASE = Path(__file__).parent.parent / "workspace"

IMAGE_EXTS  = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
OUTPUT_EXTS = IMAGE_EXTS | {".pdf", ".svg", ".csv", ".txt", ".html", ".mp4"}

GITHUB_RE = re.compile(r'https?://github\.com/[\w\-\.]+/[\w\-\.]+(?:\.git)?')


# ---------------------------------------------------------------------------
# Workspace helpers (used when no session_dir is set)
# ---------------------------------------------------------------------------

def create_workspace(prefix: str = "task") -> Path:
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    ws  = WORKSPACE_BASE / f"{prefix}_{ts}_{uid}"
    (ws / "input").mkdir(parents=True)
    (ws / "work").mkdir(parents=True)
    return ws


def find_outputs(directory: Path, since: float = 0.0) -> list[Path]:
    """Return output files created/modified after `since` (epoch seconds)."""
    found = []
    for ext in OUTPUT_EXTS:
        for f in directory.rglob(f"*{ext}"):
            try:
                if f.stat().st_mtime > since:
                    found.append(f)
            except OSError:
                continue
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def extract_github_url(text: str) -> Optional[str]:
    m = GITHUB_RE.search(text)
    return m.group(0) if m else None


def clone_github(url: str, dest: Path) -> tuple[bool, str]:
    clone_url = url if url.endswith(".git") else url + ".git"
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", clone_url, str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, f"Cloned {url}"
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"Git clone timed out after 120s"
    except Exception as e:
        return False, f"Git clone failed: {e}"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    user_instruction: str,
    input_files: list[Path],
    has_repo: bool,
    yolo: bool,
    session_dir: Optional[Path],
) -> str:
    parts = [user_instruction.strip()]

    if input_files:
        file_list = "\n".join(f"  - {f.name}" for f in input_files)
        parts.append(
            f"\nThe following files have been placed in the current directory:\n{file_list}"
        )

    if has_repo:
        parts.append(
            "\nA GitHub repository has been cloned into the `repo/` subdirectory. "
            "Explore it, understand what it does, and follow the user's instructions. "
            "Run it if appropriate."
        )

    parts.append(
        "\nInstall any packages you need using `uv add <package>` — never use bare pip. "
        "The uv virtual environment is already active in this directory."
    )
    if not yolo:
        parts.append(
            "Only work within the current session directory. "
            "Do not run system-level commands or modify anything outside this folder. "
            "(The user can add 'yolo' to lift this restriction.)"
        )

    parts.append(
        "\nSave any plots or output files in the current working directory as PNG files "
        "so they can be sent back to the user."
    )
    parts.append(
        "\nIMPORTANT: For any loops or long-running operations, use tqdm progress bars "
        "(e.g. `from tqdm import tqdm`) so the user can track progress. "
        "Install it with `uv add tqdm` if not already installed."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# uv-aware environment
# ---------------------------------------------------------------------------

def _uv_bin() -> str:
    for candidate in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", "uv"]:
        p = Path(candidate)
        if p.exists():
            return str(p)
    return "uv"


def _build_env(session_dir: Optional[Path]) -> dict:
    """Return an os.environ copy with the session venv on PATH if available."""
    env = os.environ.copy()
    if session_dir:
        venv_bin = session_dir / ".venv" / "bin"
        if venv_bin.exists():
            env["PATH"]        = f"{venv_bin}:{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(session_dir / ".venv")
            env.pop("PYTHONHOME", None)
    uv_dir = Path(_uv_bin()).parent
    env["PATH"] = f"{uv_dir}:{env.get('PATH', '')}"
    return env


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_task(
    user_instruction: str,
    attached_files: list[Path] = None,
    session_dir: Optional[Path] = None,
    yolo: bool = False,
    timeout: int = 300,
    preferred_provider: Optional[str] = None,
    live_output: LiveOutput = None,   # shared buffer for live heartbeat streaming
    cancel_event: "threading.Event | None" = None,
) -> dict:
    """
    Execute a task.

    Args:
        user_instruction : Natural-language task description.
        attached_files   : Files downloaded from Telegram (PDFs, images, …).
        session_dir      : The user's chosen working directory for this session.
                           If None, a temporary workspace is created instead.
        yolo             : Allow the AI to install packages and run anything.
        timeout          : Seconds before giving up on the AI.
        preferred_provider: Try this provider first.

    Returns dict with keys:
        provider, output, files, workspace, github_url, clone_msg,
        elapsed_seconds, exit_code, error_category, fallback_log
    """
    attached_files = attached_files or []
    task_start = time.monotonic()

    # ---- Validate session directory ----
    if session_dir and not session_dir.exists():
        log.warning("Session directory does not exist: %s — recreating", session_dir)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as e:
            return {
                "provider": "none",
                "output": f"❌ Cannot access session directory `{session_dir}`:\n{e}",
                "files": [],
                "workspace": session_dir,
                "github_url": None,
                "clone_msg": "",
                "elapsed_seconds": 0,
                "exit_code": -1,
                "error_category": "directory_error",
                "fallback_log": [],
            }

    # ---- Determine working directory ----
    if session_dir:
        work_dir = session_dir
        in_dir   = session_dir
        snapshot_time = datetime.now().timestamp()
    else:
        ws       = create_workspace()
        work_dir = ws / "work"
        in_dir   = ws / "input"
        snapshot_time = 0.0

    # ---- Copy attached files into working dir ----
    staged = []
    for src in attached_files:
        try:
            dest = in_dir / src.name
            dest.write_bytes(src.read_bytes())
            staged.append(dest)
        except (PermissionError, OSError) as e:
            log.warning("Failed to copy attached file %s: %s", src, e)

    # ---- GitHub clone ----
    github_url  = extract_github_url(user_instruction)
    repo_cloned = False
    clone_msg   = ""
    if github_url:
        ok, clone_msg = clone_github(github_url, work_dir / "repo")
        repo_cloned   = ok

    # ---- Build prompt ----
    prompt = _build_prompt(user_instruction, staged, repo_cloned, yolo, session_dir)

    # ---- Log ----
    log_file = work_dir / ".teleport_task.log"
    try:
        with open(log_file, "a") as f:
            f.write(f"\n[{datetime.now().isoformat()}] instruction: {user_instruction}\n")
            f.write(f"yolo={yolo}  github={github_url}\n")
            f.write(f"prompt:\n{prompt}\n{'─'*60}\n")
    except (PermissionError, OSError) as e:
        log.warning("Could not write task log: %s", e)

    # ---- Run AI ----
    env = _build_env(session_dir)
    # Create a live output buffer if the caller didn't supply one
    if live_output is None:
        live_output = LiveOutput()
    ai_result = run_with_fallback(
        prompt,
        cwd=str(work_dir),
        timeout=timeout,
        preferred=preferred_provider,
        env=env,
        live_output=live_output,
        cancel_event=cancel_event,
    )

    total_elapsed = round(time.monotonic() - task_start, 1)

    # ---- Collect outputs (only files created after task started) ----
    output_files = [
        f for f in find_outputs(work_dir, since=snapshot_time)
        if f.name != ".teleport_task.log"
           and f.suffix.lower() != ".py"
           and "/.venv/" not in str(f)
    ]

    return {
        "provider"       : ai_result["provider"],
        "output"         : ai_result["output"],
        "files"          : output_files,
        "workspace"      : work_dir,
        "github_url"     : github_url,
        "clone_msg"      : clone_msg,
        "elapsed_seconds": total_elapsed,
        "exit_code"      : ai_result["exit_code"],
        "error_category" : ai_result["error_category"],
        "fallback_log"   : ai_result.get("fallback_log", []),
        "live_output"    : live_output,
    }

