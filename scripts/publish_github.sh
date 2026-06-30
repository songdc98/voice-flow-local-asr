#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_NAME="${1:-voice-flow-local-asr}"
VISIBILITY="${VOICE_FLOW_GITHUB_VISIBILITY:-public}"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

cd "$PROJECT_DIR"

command -v gh >/dev/null 2>&1 || fail "GitHub CLI is required. Install it, then run: gh auth login"
gh auth status >/dev/null 2>&1 || fail "GitHub CLI is not authenticated. Run: gh auth login"

OWNER="${VOICE_FLOW_GITHUB_OWNER:-$(gh api user --jq .login)}"
[[ -n "$OWNER" ]] || fail "Could not determine GitHub owner."

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -b main
fi

if ! git rev-parse HEAD >/dev/null 2>&1; then
  git add .
  git commit -m "Release local voice flow app"
fi

if [[ -n "$(git status --porcelain)" ]]; then
  fail "Working tree is not clean. Commit or discard changes before publishing."
fi

if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  git remote remove origin >/dev/null 2>&1 || true
  git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin main
else
  case "$VISIBILITY" in
    public)
      gh repo create "$OWNER/$REPO_NAME" --public --source=. --remote=origin --push
      ;;
    private)
      gh repo create "$OWNER/$REPO_NAME" --private --source=. --remote=origin --push
      ;;
    *)
      fail "VOICE_FLOW_GITHUB_VISIBILITY must be public or private."
      ;;
  esac
fi

printf 'Published: https://github.com/%s/%s\n' "$OWNER" "$REPO_NAME"
