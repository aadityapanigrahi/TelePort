"""
TelePort Daemon - persistent Telegram bot for remote Mac Mini control.

Start: ./run_daemon.sh

Session flow
------------
1. Daemon starts → sends a directory picker to your Telegram chat.
2. You pick an existing directory, type a custom path, or ask it to create one.
3. A `uv` virtual environment is set up in that directory.
4. Every subsequent message runs as a task inside that directory/venv.

Yolo mode
---------
Add the word "yolo" anywhere in a message to allow the AI to install packages
with `uv add` and run arbitrary system commands.  Without it the AI is
instructed to work only with what's already available in the environment.

Commands
--------
/start           – re-run the directory picker (reset session)
/status          – show session dir + active AI provider
/use <provider>  – switch provider: claude, codex, gemini, or auto
/cancel          – cancel the currently running task
/logs            – view recent task log entries
/help            – list commands
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

sys.path.insert(0, str(Path(__file__).parent))
from ai_router import ERR_CRASH, ERR_NONE, ERR_RATE_LIMITED, ERR_TIMEOUT, ERR_NOT_FOUND, LiveOutput, check_providers, check_providers_detailed
from task_runner import IMAGE_EXTS, run_task

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.expanduser("~"), ".teleport.env"))
load_dotenv(os.path.join(os.getcwd(), ".env"))

console = Console()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("teleport.daemon")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Telegram file size limits
MAX_PHOTO_SIZE  = 10 * 1024 * 1024   # 10 MB
MAX_DOC_SIZE    = 50 * 1024 * 1024   # 50 MB

# Directories to offer in the picker
DEFAULT_DIR   = Path.home() / "CodingProjects"
SUGGEST_ROOTS = [
    DEFAULT_DIR,
    Path.home() / "Desktop",
    Path.home() / "Documents",
]
MAX_PICKER_DIRS = 6

_executor = ThreadPoolExecutor(max_workers=2)

# ---------------------------------------------------------------------------
# Per-chat session state
# ---------------------------------------------------------------------------

class Session:
    """Holds state for a single Telegram chat."""

    def __init__(self):
        self.state: str = "setup"   # "setup" | "await_path" | "await_new_name" | "ready"
        self.dir: Optional[Path] = None
        self.venv: Optional[Path] = None
        self.last_provider: Optional[str] = None
        self.preferred_provider: Optional[str] = None
        self.running_task: Optional[asyncio.Future] = None
        self.task_ack_msg_id: Optional[int] = None
        self.bot_message_ids: list[int] = []   # track our own messages for /clearchat
        # Live task state
        self.live_output: Optional[LiveOutput] = None
        self.task_start_time: Optional[float] = None
        self.task_timeout: int = 7 * 86400   # 7 days default — overridable with timeout:Nd
        self.task_instruction: str = ""
        # Temp storage for directory options shown in the picker
        self._dir_options: list[Path] = []

    @property
    def ready(self) -> bool:
        return self.state == "ready"


_sessions: dict[int, Session] = {}

def get_session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session()
    return _sessions[chat_id]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _authorised(update: Update) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


# ---------------------------------------------------------------------------
# Safe message sending (retry + Markdown fallback)
# ---------------------------------------------------------------------------

async def _safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode=ParseMode.MARKDOWN,
    reply_markup=None,
    retries: int = 2,
    track_chat_id: Optional[int] = None,   # if set, track this message for /clearchat
) -> Optional[int]:
    """Send a message with retry and automatic Markdown fallback."""
    for attempt in range(retries + 1):
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            if track_chat_id is not None:
                session = get_session(track_chat_id)
                session.bot_message_ids.append(msg.message_id)
            return msg.message_id
        except BadRequest as e:
            if "can't parse" in str(e).lower() and parse_mode == ParseMode.MARKDOWN:
                parse_mode = None
                continue
            log.warning("BadRequest sending message: %s", e)
            return None
        except (NetworkError, TimedOut) as e:
            if attempt < retries:
                log.warning("Network error (attempt %d/%d): %s", attempt + 1, retries, e)
                await asyncio.sleep(1)
            else:
                log.error("Failed to send message after %d retries: %s", retries, e)
                return None
        except Exception as e:
            log.error("Unexpected error sending message: %s", e)
            return None
    return None


async def _safe_edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode=ParseMode.MARKDOWN,
) -> None:
    """Edit a message with Markdown fallback."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
        )
    except BadRequest as e:
        if "can't parse" in str(e).lower() and parse_mode == ParseMode.MARKDOWN:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=None,
                )
            except Exception:
                pass
    except Exception as e:
        log.warning("Could not edit message %d: %s", message_id, e)


# ---------------------------------------------------------------------------
# Directory picker
# ---------------------------------------------------------------------------

def _candidate_dirs() -> list[Path]:
    """Return existing subdirectories from suggested roots."""
    dirs = []
    for root in SUGGEST_ROOTS:
        if root.exists():
            dirs.extend(d for d in sorted(root.iterdir()) if d.is_dir())
    return dirs[:MAX_PICKER_DIRS]


async def send_dir_picker(bot: Bot, chat_id: int) -> None:
    session = get_session(chat_id)
    session.state = "setup"

    candidates = _candidate_dirs()
    session._dir_options = candidates

    keyboard = []
    for i, d in enumerate(candidates):
        label = f"📂 {d.parent.name}/{d.name}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"dirpick:{i}")])

    keyboard.append([InlineKeyboardButton(f"📁 ~/CodingProjects (default)", callback_data="dirpick:default")])
    keyboard.append([InlineKeyboardButton("✏️  Type any path", callback_data="dirpick:type")])
    keyboard.append([InlineKeyboardButton("🆕 Create new directory", callback_data="dirpick:new")])

    await bot.send_message(
        chat_id=chat_id,
        text="👋 *TelePort ready!*\n\nWhere should I work this session?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------------------------------------------------------------------------
# uv venv setup
# ---------------------------------------------------------------------------

def _uv_bin() -> str:
    for candidate in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", "uv"]:
        if Path(candidate).exists() or candidate == "uv":
            try:
                subprocess.run([candidate, "--version"], capture_output=True, check=True)
                return candidate
            except Exception:
                continue
    return "uv"


def setup_venv(session_dir: Path) -> tuple[bool, str]:
    """Create a uv venv in session_dir if one doesn't exist. Returns (ok, message)."""
    venv_path = session_dir / ".venv"
    if venv_path.exists():
        return True, "existing venv reused"

    uv = _uv_bin()
    result = subprocess.run(
        [uv, "venv"],
        cwd=session_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, "new venv created"


async def _activate_dir(bot: Bot, chat_id: int, path: Path, query=None) -> None:
    """Set the session directory, create venv, mark ready."""
    session = get_session(chat_id)

    try:
        path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        err = f"❌ Cannot create directory `{path}`:\n{e}"
        if query:
            await query.edit_message_text(err)
        else:
            await _safe_send(bot, chat_id, err, parse_mode=None)
        return

    # Spinner
    msg_text = f"⏳ Setting up `{path}` …"
    if query:
        await query.edit_message_text(msg_text, parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=chat_id, text=msg_text, parse_mode=ParseMode.MARKDOWN)

    ok, venv_msg = await asyncio.get_event_loop().run_in_executor(
        None, lambda: setup_venv(path)
    )

    if not ok:
        err = f"❌ Could not create uv venv:\n```{venv_msg}```"
        if query:
            await query.edit_message_text(err, parse_mode=ParseMode.MARKDOWN)
        else:
            await bot.send_message(chat_id=chat_id, text=err, parse_mode=ParseMode.MARKDOWN)
        return

    session.dir   = path
    session.venv  = path / ".venv"
    session.state = "ready"

    confirm = (
        f"✅ *Session ready*\n\n"
        f"📁 `{path}`\n"
        f"🐍 uv venv — {venv_msg}\n\n"
        f"Send me a task! Add _yolo_ to allow system-wide commands."
    )
    if query:
        await query.edit_message_text(confirm, parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=chat_id, text=confirm, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    # Reset session and re-show picker
    _sessions[update.effective_chat.id] = Session()
    await send_dir_picker(update.get_bot(), update.effective_chat.id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await update.message.reply_text(
        "/start     – reset session & pick working directory\n"
        "/status    – show active directory + AI provider\n"
        "/use claude|codex|gemini|auto – switch provider\n"
        "/cancel    – cancel the currently running task\n"
        "/logs      – view recent task log entries\n"
        "/clearchat – delete TelePort's recent messages from this chat\n"
        "/help      – this message\n\n"
        "Prefix with `@claude`, `@codex`, or `@gemini` to override for one task.\n"
        "Append `timeout:12h`, `timeout:90m` or `timeout:2d` to override timeout.\n"
        "Append *yolo* to allow system-wide commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


VALID_PROVIDERS = {"claude", "codex", "gemini", "auto"}


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    session = get_session(update.effective_chat.id)
    args = (update.message.text or "").split()

    if len(args) < 2 or args[1].lower() not in VALID_PROVIDERS:
        await update.message.reply_text(
            "Usage: `/use claude`, `/use codex`, `/use gemini`, or `/use auto`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    choice = args[1].lower()
    session.preferred_provider = None if choice == "auto" else choice

    label = "auto (claude → codex → gemini)" if choice == "auto" else choice
    await update.message.reply_text(f"Switched to `{label}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    session = get_session(update.effective_chat.id)
    availability = check_providers()

    provider_lines = "\n".join(
        f"{'✓' if ok else '✗'} {name}" for name, ok in availability.items()
    )
    dir_line  = f"`{session.dir}`" if session.dir else "_not set_"
    pref_line = f"`{session.preferred_provider}`" if session.preferred_provider else "_auto_"
    last_line = f"`{session.last_provider}`" if session.last_provider else "_none yet_"

    # Live task progress
    if session.running_task and not session.running_task.done() and session.task_start_time:
        elapsed  = int(time.monotonic() - session.task_start_time)
        timeout  = session.task_timeout
        filled   = min(20, int(20 * elapsed / timeout))
        bar      = "█" * filled + "░" * (20 - filled)
        pct      = min(100, int(100 * elapsed / timeout))
        task_line = f"🔄 Running for *{_fmt_duration(elapsed)}*  [{bar}] {pct}%"
        # ETA from tqdm
        eta_s = _parse_tqdm_eta(session.live_output)
        if eta_s > 0:
            task_line += f"  ·  ETA ~*{_fmt_duration(eta_s)}*"
        if session.task_instruction:
            task_line += f"\n  📝 _{session.task_instruction[:80]}_"
        if session.live_output and len(session.live_output) > 0:
            tail = session.live_output.tail(3)
            task_line += f"\n  💬 Last output:\n```\n{tail[:300]}\n```"
    else:
        task_line = "💤 Idle"

    await update.message.reply_markdown(
        f"*Session directory:* {dir_line}\n"
        f"*Preferred provider:* {pref_line}\n"
        f"*Last provider:* {last_line}\n\n"
        f"*Task status:* {task_line}\n\n"
        f"*AI providers:*\n{provider_lines}\n"
        f"*Fallback order:* claude → codex → gemini"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the currently running task."""
    if not _authorised(update):
        return
    session = get_session(update.effective_chat.id)

    if session.running_task and not session.running_task.done():
        session.running_task.cancel()
        await update.message.reply_text("🛑 Task cancellation requested. It may take a moment to stop.")
    else:
        await update.message.reply_text("No task is currently running.")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent task log entries."""
    if not _authorised(update):
        return
    session = get_session(update.effective_chat.id)

    if not session.dir:
        await update.message.reply_text("No session directory set. Use /start first.")
        return

    log_file = session.dir / ".teleport_task.log"
    if not log_file.exists():
        await update.message.reply_text("No task logs yet in this session.")
        return

    try:
        content = log_file.read_text()
        # Show last 2000 chars
        tail = content[-2000:] if len(content) > 2000 else content
        if len(content) > 2000:
            tail = "… (truncated)\n" + tail
        await _safe_send(
            update.get_bot(),
            update.effective_chat.id,
            f"📋 *Recent logs:*\n```\n{tail}\n```",
        )
    except Exception as e:
        await update.message.reply_text(f"Could not read logs: {e}")


async def cmd_clearchat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    count: int = 50,
) -> None:
    """
    Delete the bot's own recent messages from this chat.
    Telegram only allows bots to delete their own messages, so we track
    every message TelePort sends and bulk-delete them here.
    Pass a number to limit: /clearchat 20
    """
    if not _authorised(update):
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    bot     = update.get_bot()

    # Also allow /clearchat <N> to limit how many
    args = (update.message.text or "").split()
    try:
        limit = int(args[1]) if len(args) > 1 else count
    except ValueError:
        limit = count

    ids_to_delete = session.bot_message_ids[-limit:]

    if not ids_to_delete:
        await update.message.reply_text("Nothing to clear — no tracked messages yet.")
        return

    deleted = 0
    failed  = 0
    for msg_id in ids_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)   # stay under rate limits

    # Clear tracked list
    session.bot_message_ids.clear()

    # Also delete the /clearchat command message itself
    try:
        await update.message.delete()
    except Exception:
        pass

    if failed == 0:
        confirm = await bot.send_message(chat_id=chat_id, text=f"🧹 Cleared {deleted} message(s).")
    else:
        confirm = await bot.send_message(chat_id=chat_id, text=f"🧹 Cleared {deleted} message(s) ({failed} couldn't be deleted — may be too old).")

    # Track the confirm message so it can be cleared next time
    session.bot_message_ids.append(confirm.message_id)


# ---------------------------------------------------------------------------
# Callback handler (inline keyboard)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    bot     = update.get_bot()
    data    = query.data

    if not data.startswith("dirpick:"):
        return

    choice = data[len("dirpick:"):]

    if choice == "default":
        await _activate_dir(bot, chat_id, DEFAULT_DIR, query=query)

    elif choice == "type":
        session.state = "await_path"
        await query.edit_message_text("✏️ Type the full path to work in (e.g. `/Users/you/myproject`):")

    elif choice == "new":
        session.state = "await_new_name"
        await query.edit_message_text("🆕 What should I name the new directory?\n_(Type just the name, I'll create it inside `~/CodingProjects/`)_", parse_mode=ParseMode.MARKDOWN)

    else:
        try:
            idx  = int(choice)
            path = session._dir_options[idx]
            await _activate_dir(bot, chat_id, path, query=query)
        except (ValueError, IndexError):
            await query.edit_message_text("⚠️ Couldn't find that option — use /start to try again.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    half = (limit - 40) // 2
    return text[:half] + "\n\n… [truncated] …\n\n" + text[-half:]


def _format_status_report(result: dict) -> str:
    """Build a rich status report from a task result dict."""
    provider = result["provider"]
    error_cat = result.get("error_category", ERR_NONE)
    elapsed = result.get("elapsed_seconds", 0)
    output = result["output"].strip()

    # --- Success ---
    if error_cat == ERR_NONE:
        header = f"✅ Done  ·  `{provider}`  ·  {elapsed}s"
        body = _truncate(output) if output else "_No text output._"
        return f"{header}\n\n{body}"

    # --- Error report ---
    if error_cat == ERR_RATE_LIMITED:
        icon = "⚠️"
        title = "Rate Limited"
    elif error_cat == ERR_TIMEOUT:
        icon = "⏱️"
        title = "Timed Out"
    elif error_cat == ERR_NOT_FOUND:
        icon = "🔍"
        title = "Provider Not Found"
    elif error_cat == ERR_CRASH:
        icon = "💥"
        title = "Provider Error"
    else:
        icon = "❌"
        title = "Error"

    lines = [f"{icon} *{title}*  ·  `{provider}`  ·  {elapsed}s"]

    # Show fallback chain if multiple providers were tried
    fallback_log = result.get("fallback_log", [])
    if len(fallback_log) > 1:
        lines.append("")
        lines.append("*Providers tried:*")
        for entry in fallback_log:
            cat = entry.get("error_category", "?")
            t = entry.get("elapsed_seconds", 0)
            status_icon = "✓" if cat == ERR_NONE else "✗"
            lines.append(f"  {status_icon} {entry['provider']}: {cat} ({t}s)")

    if output:
        lines.append("")
        lines.append(_truncate(output, limit=3000))

    # Suggestions for errors
    if error_cat in (ERR_RATE_LIMITED, ERR_TIMEOUT, ERR_NOT_FOUND):
        lines.append("")
        lines.append("💡 *What to try:*")
        if error_cat == ERR_RATE_LIMITED:
            lines.append("  • Wait a few minutes and retry")
            lines.append("  • Use `/use <provider>` to try a different one")
        elif error_cat == ERR_TIMEOUT:
            lines.append("  • Simplify or split the task")
            lines.append("  • Try a different provider with `/use <provider>`")
        elif error_cat == ERR_NOT_FOUND:
            lines.append("  • Check that the AI CLI is installed")
            lines.append("  • Use `/status` to see available providers")

    return "\n".join(lines)


async def _send_result(bot: Bot, chat_id: int, result: dict) -> None:
    session = get_session(chat_id)
    session.last_provider = result["provider"]

    report = _format_status_report(result)
    await _safe_send(bot, chat_id, report, track_chat_id=chat_id)

    # Send generated files
    for path in result["files"][:20]:
        try:
            file_size = path.stat().st_size
            ext = path.suffix.lower()

            if ext in IMAGE_EXTS:
                if file_size > MAX_PHOTO_SIZE:
                    # Too large for photo — send as document
                    with open(path, "rb") as fh:
                        await bot.send_document(chat_id=chat_id, document=fh, caption=f"📎 {path.name} (too large for photo)")
                else:
                    with open(path, "rb") as fh:
                        await bot.send_photo(chat_id=chat_id, photo=fh, caption=path.name)
            else:
                if file_size > MAX_DOC_SIZE:
                    await _safe_send(bot, chat_id, f"⚠️ File `{path.name}` is too large to send ({file_size // (1024*1024)}MB, limit 50MB).", parse_mode=ParseMode.MARKDOWN)
                else:
                    with open(path, "rb") as fh:
                        await bot.send_document(chat_id=chat_id, document=fh, caption=path.name)
        except Exception as exc:
            log.warning("Could not send %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Incoming file cleanup
# ---------------------------------------------------------------------------

def _cleanup_old_incoming(incoming_dir: Path, max_age_hours: int = 24) -> None:
    """Remove files older than max_age_hours from the incoming directory."""
    if not incoming_dir.exists():
        return
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    for f in incoming_dir.iterdir():
        try:
            if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                log.info("Cleaned up old incoming file: %s", f.name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Task dispatching
# ---------------------------------------------------------------------------

def _detect_provider_override(text: str) -> tuple[str, str | None]:
    """Check for @provider mention. Returns (cleaned_text, provider_or_None)."""
    import re
    for name in ("claude", "codex", "gemini"):
        pattern = rf"@{name}\b"
        if re.search(pattern, text, re.IGNORECASE):
            cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            return cleaned, name
    return text, None


def _tqdm_bar(elapsed: int, timeout: int, width: int = 20) -> str:
    """Return a tqdm-style text progress bar."""
    filled = min(width, int(width * elapsed / max(timeout, 1)))
    return "[" + "█" * filled + "░" * (width - filled) + f"] {elapsed}s/{timeout}s"


def _parse_timeout(text: str, default: int = 7 * 86400) -> tuple[str, int]:
    """
    Extract a timeout override from message text.
    Supported formats: timeout:300  timeout:10m  timeout:12h  timeout:2d
    Returns (cleaned_text, timeout_seconds).
    Default is 7 days so long-running jobs never get killed.
    """
    import re as _re
    m = _re.search(r"\btimeout:(\d+)([smhd]?)\b", text, _re.IGNORECASE)
    if m:
        val, unit = int(m.group(1)), m.group(2).lower()
        multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, '': 1}
        seconds = val * multipliers.get(unit, 1)
        cleaned = _re.sub(r"\btimeout:\d+[smhd]?\b", "", text, flags=_re.IGNORECASE).strip()
        return cleaned, seconds
    return text, default


_TQDM_ETA_RE = re.compile(
    r"""\[\s*
        (?:[\d:]+)           # elapsed  e.g. 01:23
        <
        (\d{1,2}:\d{2}(?::\d{2})?)   # remaining  e.g. 12:34:56 or 12:34
        [,\s>]
    """,
    re.VERBOSE,
)


def _parse_tqdm_eta(live_output) -> int:
    """
    Scan the last 20 lines of live_output for tqdm-style ETA strings
    (e.g. '[01:23<12:34:56, 2.3it/s]') and return the best ETA in seconds.
    Returns 0 if no ETA is found.
    """
    if not live_output:
        return 0
    tail = live_output.tail(20)
    best_eta = 0
    for m in _TQDM_ETA_RE.finditer(tail):
        parts = m.group(1).split(":")
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            eta_s = parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            eta_s = parts[0] * 60 + parts[1]
        else:
            continue
        if eta_s > best_eta:
            best_eta = eta_s
    return best_eta


def _next_heartbeat_interval(eta_seconds: int) -> int:
    """
    Choose the next heartbeat sleep duration based on tqdm ETA.
    The goal is ~3-5 check-ins regardless of total job length.
    """
    if eta_seconds <= 0:         return 30          # unknown — use short default
    if eta_seconds < 5 * 60:    return 30          # < 5 min  → every 30s
    if eta_seconds < 30 * 60:   return 5 * 60      # < 30 min → every 5 min
    if eta_seconds < 2 * 3600:  return 15 * 60     # < 2 h    → every 15 min
    if eta_seconds < 12 * 3600: return 30 * 60     # < 12 h   → every 30 min
    if eta_seconds < 48 * 3600: return 2 * 3600    # < 2 days → every 2 h
    return 6 * 3600                                # ≥ 2 days → every 6 h


def _fmt_duration(seconds: int) -> str:
    """Human-readable duration string."""
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m"


async def _heartbeat(
    bot,
    chat_id: int,
    ack_msg_id: int,
    session,
    task_future,
) -> None:
    """
    Runs concurrently with the task. Edits the ack message at adaptive intervals.

    Interval logic (driven by tqdm ETA from live output):
      ETA unknown / <5m  → 30s
      ETA 5-30m          → 5 min
      ETA 30m-2h         → 15 min
      ETA 2h-12h         → 30 min
      ETA 12h-2d         → 2 h
      ETA ≥ 2d           → 6 h

    This means a 2-day job will only ping you ~8 times total, not every 30s.
    The bot stays fully responsive to all commands throughout.
    """
    # Start with a short initial interval to show the task kicked off
    interval = 30

    while not task_future.done():
        await asyncio.sleep(interval)
        if task_future.done():
            break
        try:
            elapsed   = int(time.monotonic() - session.task_start_time)
            eta_s     = _parse_tqdm_eta(session.live_output)
            interval  = _next_heartbeat_interval(eta_s)
            provider  = session.preferred_provider or "auto"

            bar   = _tqdm_bar(elapsed, session.task_timeout)
            lines = [f"⏳ *Working…* {bar}"]

            if eta_s > 0:
                eta_str = _fmt_duration(eta_s)
                next_check = _fmt_duration(interval)
                lines.append(f"  ⏱ ETA: ~*{eta_str}*  (next update in {next_check})")

            lines.append(f"  Provider: `{provider}`  ·  Dir: `{session.dir}`")

            if session.task_instruction:
                lines.append(f"  📝 _{session.task_instruction[:80]}_")

            if session.live_output and len(session.live_output) > 0:
                tail = session.live_output.tail(4)
                lines.append(f"\n💬 *Recent output:*\n```\n{tail[:400]}\n```")

            lines.append("\n_/status for details · /cancel to stop_")
            await _safe_edit(bot, chat_id, ack_msg_id, "\n".join(lines))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.debug("Heartbeat edit failed: %s", e)


async def _dispatch(update: Update, instruction: str, attached: list[Path]) -> None:
    bot     = update.get_bot()
    chat_id = update.effective_chat.id
    session = get_session(chat_id)

    # Per-message @provider and timeout overrides
    instruction, override = _detect_provider_override(instruction)
    provider = override or session.preferred_provider
    instruction, timeout = _parse_timeout(instruction, default=session.task_timeout)

    yolo = "yolo" in instruction.lower()

    # Store live task metadata on session (used by heartbeat + /status)
    live_buf = LiveOutput()
    session.live_output      = live_buf
    session.task_start_time  = time.monotonic()
    session.task_timeout     = timeout
    session.task_instruction = instruction[:120]

    provider_label = provider or "auto"
    ack_text = (
        f"⏳ Working… {_tqdm_bar(0, timeout)}\n"
        f"  Provider: `{provider_label}`"
        + (" · 🚀 yolo" if yolo else "")
        + f"\n  Directory: `{session.dir}`"
        + "\n_/status for live progress · /cancel to stop_"
    )
    ack = await update.message.reply_text(ack_text, parse_mode=ParseMode.MARKDOWN)
    session.task_ack_msg_id = ack.message_id

    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    task_future = loop.run_in_executor(
        _executor,
        lambda: run_task(
            user_instruction=instruction,
            attached_files=attached,
            session_dir=session.dir,
            yolo=yolo,
            timeout=timeout,
            preferred_provider=provider,
            live_output=live_buf,
        ),
    )
    session.running_task = task_future

    # Heartbeat runs concurrently — the asyncio event loop is never blocked
    heartbeat = asyncio.ensure_future(
        _heartbeat(bot, chat_id, ack.message_id, session, task_future)
    )

    try:
        result = await task_future
    except asyncio.CancelledError:
        heartbeat.cancel()
        await _safe_edit(bot, chat_id, ack.message_id, "🛑 Task cancelled.")
        return
    except Exception as exc:
        heartbeat.cancel()
        log.exception("Task failed")
        error_report = (
            f"❌ *Task Failed*\n\n"
            f"*Error:* `{type(exc).__name__}`\n"
            f"```\n{str(exc)[:1500]}\n```\n\n"
            f"💡 *What to try:*\n"
            f"  • Simplify the instruction\n"
            f"  • Check `/status` for provider availability\n"
            f"  • Try a different provider with `/use <provider>`"
        )
        await _safe_edit(bot, chat_id, ack.message_id, error_report)
        return
    finally:
        heartbeat.cancel()
        session.running_task    = None
        session.task_ack_msg_id = None
        session.task_start_time = None
        session.live_output     = None

    try:
        await bot.delete_message(chat_id=chat_id, message_id=ack.message_id)
    except Exception:
        pass

    await _send_result(bot, chat_id, result)

# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

TEMP_DIR = Path(__file__).parent.parent / "workspace" / "_incoming"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Files queued while waiting for a text instruction
_pending_files: dict[int, list[Path]] = {}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    text    = (update.message.text or "").strip()

    # --- Setup states ---
    if session.state == "await_path":
        path = Path(text).expanduser()
        session.state = "setup"
        await _activate_dir(update.get_bot(), chat_id, path)
        return

    if session.state == "await_new_name":
        name = text.strip().replace(" ", "_")
        path = Path.home() / "CodingProjects" / name
        session.state = "setup"
        await _activate_dir(update.get_bot(), chat_id, path)
        return

    if not session.ready:
        await update.message.reply_text("Please pick a working directory first — use /start")
        return

    # --- Ready: dispatch task ---
    files = _pending_files.pop(chat_id, [])
    await _dispatch(update, text, files)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return

    chat_id  = update.effective_chat.id
    session  = get_session(chat_id)
    document = update.message.document
    caption  = (update.message.caption or "").strip()

    tfile = await document.get_file()
    dest  = TEMP_DIR / (document.file_name or f"doc_{update.message.id}")
    await tfile.download_to_drive(dest)

    if not session.ready:
        await update.message.reply_text("Please pick a working directory first — use /start")
        return

    if caption:
        await _dispatch(update, caption, [dest])
    else:
        _pending_files.setdefault(chat_id, []).append(dest)
        await update.message.reply_text(
            f"📎 Got `{dest.name}`. Now send me your instruction.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return

    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    caption = (update.message.caption or "").strip()

    photo_file = await update.message.photo[-1].get_file()
    dest       = TEMP_DIR / f"photo_{update.message.id}.jpg"
    await photo_file.download_to_drive(dest)

    if not session.ready:
        await update.message.reply_text("Please pick a working directory first — use /start")
        return

    instruction = caption or "Describe this image in detail."
    await _dispatch(update, instruction, [dest])


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to voice messages with a helpful note."""
    if not _authorised(update):
        return
    await update.message.reply_text(
        "🎤 Voice messages aren't supported yet. Please type your instruction instead."
    )


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to stickers, videos, etc. with a helpful note."""
    if not _authorised(update):
        return
    await update.message.reply_text(
        "📦 I can work with *text*, *photos*, and *documents* (PDF, CSV, code, etc.).\n"
        "Please send one of those!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Startup self-check
# ---------------------------------------------------------------------------

async def _startup_report(bot: Bot, chat_id: int) -> None:
    """Send a startup self-check showing provider availability."""
    providers = check_providers_detailed()
    lines = ["🚀 *TelePort Daemon Started*\n", "*AI Provider Status:*"]
    for name, info in providers.items():
        if info["exists"]:
            version = info.get("version") or "unknown version"
            lines.append(f"  ✅ {name} — {version}")
        else:
            lines.append(f"  ❌ {name} — not found at `{info['path']}`")

    lines.append(f"\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    await _safe_send(bot, int(chat_id), "\n".join(lines))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    """Send the startup report and directory picker as soon as the bot connects."""
    # Clean up old incoming files
    _cleanup_old_incoming(TEMP_DIR)

    if TELEGRAM_CHAT_ID:
        await _startup_report(application.bot, int(TELEGRAM_CHAT_ID))
        await send_dir_picker(application.bot, int(TELEGRAM_CHAT_ID))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        console.print("[bold red]Error:[/bold red] TELEGRAM_BOT_TOKEN not set. Run `teleport config`.")
        sys.exit(1)

    console.print("[bold green]TelePort daemon starting…[/bold green]")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("use",       cmd_use))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("logs",      cmd_logs))
    app.add_handler(CommandHandler("clearchat", cmd_clearchat))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice))
    app.add_handler(MessageHandler(filters.Sticker.ALL | filters.VIDEO | filters.ANIMATION, handle_unsupported))

    console.print("[bold blue]Listening…[/bold blue]  (Ctrl+C to stop)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

