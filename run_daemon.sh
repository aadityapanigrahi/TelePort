#!/usr/bin/env bash
# Run the TelePort AI daemon.
# Usage:
#   ./run_daemon.sh              # auto-fallback: claude → gemini → codex
#   ./run_daemon.sh --provider gemini   # force a specific provider

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate virtualenv if present
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# Make sure PATH includes known AI CLI locations
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

cd "$SCRIPT_DIR"
exec python src/daemon.py "$@"
