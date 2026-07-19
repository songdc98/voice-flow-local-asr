#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${VOICE_FLOW_APP_DIR:-${1:-$HOME/Applications/Voice Flow.app}}"
APP_NAME="$(basename "$APP_DIR" .app)"
EXECUTABLE_NAME="$APP_NAME"
BUNDLE_ID="${VOICE_FLOW_BUNDLE_ID:-local.voiceflow.launcher}"
SIGN_IDENTITY="${VOICE_FLOW_SIGN_IDENTITY:-}"

info() {
  printf '%s\n' "$1"
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required."
command -v clang >/dev/null 2>&1 || fail "clang was not found. Install Xcode Command Line Tools first."
[[ -f "$PROJECT_DIR/macos/voice_flow_app_launcher.m" ]] || fail "Missing macOS launcher source."

if [[ -z "$SIGN_IDENTITY" ]]; then
  SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/Developer ID Application:|Apple Development:/ {print $2; exit}')"
fi

mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  if "$PROJECT_DIR/.venv/bin/python" -c 'import PIL' >/dev/null 2>&1; then
    "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/macos/generate_voice_flow_icons.py"
  fi
fi

if [[ -d "$PROJECT_DIR/macos/assets/VoiceFlow.iconset" ]]; then
  /usr/bin/iconutil -c icns \
    -o "$PROJECT_DIR/macos/assets/VoiceFlow.icns" \
    "$PROJECT_DIR/macos/assets/VoiceFlow.iconset" >/dev/null 2>&1 || true
fi

clang -fobjc-arc \
  -framework Cocoa \
  -framework Carbon \
  -framework AVFoundation \
  -framework ApplicationServices \
  -o "$APP_DIR/Contents/MacOS/$EXECUTABLE_NAME" \
  "$PROJECT_DIR/macos/voice_flow_app_launcher.m"

cp "$PROJECT_DIR/macos/assets/VoiceFlow.icns" "$APP_DIR/Contents/Resources/VoiceFlow.icns"

/usr/bin/python3 - "$APP_DIR/Contents/Info.plist" "$PROJECT_DIR" "$APP_NAME" "$EXECUTABLE_NAME" "$BUNDLE_ID" <<'PY'
import plistlib
import sys

plist_path, project_dir, app_name, executable_name, bundle_id = sys.argv[1:6]
payload = {
    "CFBundleDevelopmentRegion": "en",
    "CFBundleDisplayName": app_name,
    "CFBundleExecutable": executable_name,
    "CFBundleIconFile": "VoiceFlow",
    "CFBundleIdentifier": bundle_id,
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleName": app_name,
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0.0",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "14.0",
    "NSHighResolutionCapable": True,
    "NSMicrophoneUsageDescription": "Voice Flow records your voice locally for speech-to-text dictation.",
    "NSSupportsAutomaticTermination": False,
    "NSSupportsSuddenTermination": False,
    "VoiceFlowProjectPath": project_dir,
}
with open(plist_path, "wb") as f:
    plistlib.dump(payload, f)
PY

if [[ -n "$SIGN_IDENTITY" ]]; then
  codesign --force --deep --options runtime \
    --entitlements "$PROJECT_DIR/macos/VoiceFlow.entitlements" \
    --sign "$SIGN_IDENTITY" \
    "$APP_DIR"
  info "Signed with: $SIGN_IDENTITY"
else
  codesign --force --deep --options runtime \
    --entitlements "$PROJECT_DIR/macos/VoiceFlow.entitlements" \
    --sign - \
    "$APP_DIR"
  info "Signed ad hoc."
fi

info "Built: $APP_DIR"
