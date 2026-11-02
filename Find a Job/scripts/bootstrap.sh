#!/usr/bin/env bash
# JobFinder bootstrap for macOS / Linux. Called by "Start JobFinder.command", "start.sh", etc.
# Installs uv for this user (no sudo), lets uv install Python 3.12 + locked deps, then starts the app.
set -euo pipefail

ACTION="${1:-start}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" == "Darwin" ]]; then
  APP_HOME="$HOME/Library/Application Support/JobFinder"
  OS_TRIPLE="apple-darwin"
  # Files unzipped from the internet are quarantined; clear it for our own folder so scripts can run.
  xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
else
  APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/JobFinder"
  OS_TRIPLE="unknown-linux-gnu"
fi
BIN_DIR="$APP_HOME/bin"
VENV="$APP_HOME/venv"
LOG="$APP_HOME/bootstrap.log"
mkdir -p "$BIN_DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; echo "$*" >&2; }
fail() {
  log "ERROR: $*"
  echo ""; echo "Something went wrong. Details are in: $LOG"
  read -r -p "Press Enter to close" _ || true
  exit 1
}

find_uv() {
  for c in "$(command -v uv 2>/dev/null || true)" "$BIN_DIR/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [[ -n "$c" && -x "$c" ]] && { echo "$c"; return 0; }
  done
  return 1
}

download() {
  curl -fsSL --retry 2 --connect-timeout 15 -o "$2" "$1" 2>/dev/null && return 0
  curl -4 -fsSL --retry 2 --connect-timeout 15 -o "$2" "$1" 2>/dev/null && return 0
  return 1
}

install_uv() {
  log "Installing uv (one-time, about 15 MB)..."
  local arch; arch="$(uname -m)"
  [[ "$arch" == "arm64" ]] && arch="aarch64"
  local name="uv-${arch}-${OS_TRIPLE}"
  local tmp; tmp="$(mktemp -d)"
  if download "https://github.com/astral-sh/uv/releases/latest/download/${name}.tar.gz" "$tmp/uv.tgz"; then
    tar -xzf "$tmp/uv.tgz" -C "$tmp" && cp "$tmp/$name/uv" "$tmp/$name/uvx" "$BIN_DIR/" && chmod +x "$BIN_DIR/uv" "$BIN_DIR/uvx"
  fi
  rm -rf "$tmp"
  [[ -x "$BIN_DIR/uv" ]] && { echo "$BIN_DIR/uv"; return 0; }
  log "Direct download failed; trying the official installer..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH=1 sh >> "$LOG" 2>&1 || true
  [[ -x "$BIN_DIR/uv" ]] && { echo "$BIN_DIR/uv"; return 0; }
  return 1
}

sync_env() {
  log "Preparing Python and libraries (first run takes 1-3 minutes)..."
  ( cd "$ROOT" && "$UV" sync --frozen --no-dev --python 3.12 >> "$LOG" 2>&1 ) \
    || ( log "Locked install failed, retrying with resolution..." && cd "$ROOT" && "$UV" sync --no-dev --python 3.12 >> "$LOG" 2>&1 ) \
    || fail "Dependency install failed."
}

export UV_SYSTEM_CERTS=1
export UV_NATIVE_TLS=1  # older uv versions
export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_PYTHON_PREFERENCE=managed

UV="$(find_uv || install_uv || true)"
[[ -n "$UV" ]] || fail "Could not install uv. Check the internet connection and try again."
log "uv: $UV"
PY="$VENV/bin/python"
STAMP="$APP_HOME/synced.txt"
hash_lock() { (shasum -a 256 "$ROOT/uv.lock" 2>/dev/null || sha256sum "$ROOT/uv.lock") | cut -d' ' -f1; }
LOCK_HASH="$(hash_lock)|$ROOT"

if [[ "$ACTION" == "update" ]]; then
  if [[ -d "$ROOT/.git" ]] && command -v git >/dev/null; then
    log "Fetching latest version..."; (cd "$ROOT" && git pull --ff-only) || log "git pull failed"
    LOCK_HASH="$(hash_lock)|$ROOT"
  else
    log "Not a git checkout: download the latest zip and replace this folder, then run Update again."
  fi
  sync_env; echo "$LOCK_HASH" > "$STAMP"
  "$PY" -m jobfinder doctor || true
  log "Update complete."; exit 0
fi

if [[ ! -x "$PY" || ! -f "$STAMP" || "$(cat "$STAMP")" != "$LOCK_HASH" ]]; then
  sync_env; echo "$LOCK_HASH" > "$STAMP"
fi

case "$ACTION" in
  start)  "$PY" -m jobfinder start || fail "JobFinder stopped with an error." ;;
  doctor) "$PY" -m jobfinder doctor ;;
  agents)
    echo ""; echo "Connect JobFinder to an AI agent app (MCP):"
    clients=(codex claude-desktop claude-code vscode opencode cursor)
    for i in "${!clients[@]}"; do echo "  $((i+1))) ${clients[$i]}"; done
    read -r -p "Number (or several, e.g. 1 4): " pick || pick=""
    read -r -p "Will the app use a small or local model (Ollama, LM Studio, 7B-14B)? y/N " small || small=""
    lite=""
    case "$small" in [yYsS]*) lite="--lite"; echo "Using guided mode (one question at a time)." ;; esac
    for n in ${pick//,/ }; do
      if [[ "$n" =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#clients[@]} )); then
        "$PY" -m jobfinder mcp-install "${clients[$((n-1))]}" $lite
      else
        echo "Ignoring '$n' (choose 1-${#clients[@]})"
      fi
    done ;;
  *) fail "unknown action $ACTION" ;;
esac
