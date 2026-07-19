#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${VOICE_FLOW_APP_DIR:-$HOME/Applications/Voice Flow.app}"
PYTHON_VERSION="${VOICE_FLOW_PYTHON_VERSION:-3.12}"
ASSUME_YES="${VOICE_FLOW_ASSUME_YES:-1}"

info() {
  printf '%s\n' "$1"
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

confirm() {
  local prompt="$1"
  [[ "$ASSUME_YES" == "1" ]] && return 0
  printf '%s [y/N] ' "$prompt"
  read -r answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

ensure_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "Voice Flow currently supports macOS only."
  [[ "$(uname -m)" == "arm64" ]] || fail "The default MLX/Qwen3 pipeline requires Apple Silicon."
}

ensure_command_line_tools() {
  if ! xcode-select -p >/dev/null 2>&1; then
    xcode-select --install >/dev/null 2>&1 || true
    fail "Install Xcode Command Line Tools, then run this script again."
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  confirm "uv is required to create the local Python environment. Install uv now?" || fail "uv is required."
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installation finished, but uv is still not on PATH."
}

setup_python_env() {
  info "Creating local Python environment..."
  uv venv --python "$PYTHON_VERSION" "$PROJECT_DIR/.venv"
  info "Installing Python dependencies..."
  uv pip install --python "$PROJECT_DIR/.venv/bin/python" -r "$PROJECT_DIR/requirements.txt"
}

build_app() {
  info "Building Voice Flow.app..."
  VOICE_FLOW_APP_DIR="$APP_DIR" "$PROJECT_DIR/scripts/build_macos_app.sh"
}

print_next_steps() {
  cat <<EOF

Installed: $APP_DIR

Next steps:
1. Open Voice Flow.app.
2. Grant Microphone permission when macOS asks.
3. Open System Settings -> Privacy & Security -> Accessibility, then enable Voice Flow.app.
4. Click any text box, press Page Down to start recording, and press Page Down again to stop and paste.

EOF
}

ensure_macos
ensure_command_line_tools
ensure_uv
setup_python_env
build_app

if [[ "${VOICE_FLOW_SKIP_OPEN:-0}" != "1" ]]; then
  open "$APP_DIR"
fi

print_next_steps
