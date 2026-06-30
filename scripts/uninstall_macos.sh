#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${VOICE_FLOW_APP_DIR:-$HOME/Applications/Voice Flow.app}"

osascript -e 'tell application "Voice Flow" to quit' >/dev/null 2>&1 || true
pkill -f "$PROJECT_DIR/voice_flow.py" >/dev/null 2>&1 || true
pkill -f "$PROJECT_DIR/voice_flow_menu_app.py" >/dev/null 2>&1 || true
rm -rf "$APP_DIR"
rm -f "$PROJECT_DIR/voice_flow.pid" "$PROJECT_DIR/voice_flow.lock" "$PROJECT_DIR/voice_flow_status.json"
rm -f "$PROJECT_DIR/paste_request.json" "$PROJECT_DIR/paste_status.json" "$PROJECT_DIR/native_hotkeys.json"
printf 'Removed: %s\n' "$APP_DIR"
