#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Music Vault Startup Orchestrator
# Usage: ./start.sh [--port 8080] [--dev]
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Always resolve paths relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${MV_PORT:-8080}"
DEV_MODE=false
VENV_DIR=".venv"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
VIOLET='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'

print_banner() {
  echo -e "${VIOLET}${BOLD}"
  echo "  ╔════════════════════════════════╗"
  echo "  ║     V I B E V A U L T  🎵      ║"
  echo "  ║   Music Downloader & Library   ║"
  echo "  ╚════════════════════════════════╝"
  echo -e "${NC}"
}

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
info() { echo -e "${CYAN}[i]${NC} $*"; }

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --port) PORT="$2"; shift 2 ;;
    --dev)  DEV_MODE=true; shift ;;
    *) shift ;;
  esac
done

print_banner

# ── Check/kill existing process on port ───────────────────────────────────────
cleanup_port() {
  local pid
  pid=$(lsof -ti ":${PORT}" 2>/dev/null || true)
  if [[ -n "$pid" ]]; then
    warn "Port ${PORT} in use by PID ${pid} — killing…"
    kill -9 $pid 2>/dev/null || true
    sleep 1
    log "Port ${PORT} freed"
  fi
}

# ── Create directories ─────────────────────────────────────────────────────────
setup_dirs() {
  local dirs=(
    "downloads/singles"
    "downloads/playlists"
    "data/logs"
    "config"
    "frontend/static"
  )
  for d in "${dirs[@]}"; do
    mkdir -p "$d"
  done
  log "Directory structure ready"
}

# ── Python venv ────────────────────────────────────────────────────────────────
setup_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment…"
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  log "Virtual environment activated"
}

# ── Install dependencies ───────────────────────────────────────────────────────
install_deps() {
  info "Checking Python dependencies…"
  pip install -q --upgrade pip
  pip install -q -r requirements.txt

  # yt-dlp
  if ! command -v yt-dlp &>/dev/null; then
    warn "yt-dlp not found — installing…"
    pip install -q yt-dlp
    log "yt-dlp installed"
  else
    log "yt-dlp: $(yt-dlp --version)"
  fi

  # ffmpeg
  if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found — please install: sudo apt install ffmpeg / brew install ffmpeg"
  else
    log "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
  fi

  # streamrip
  if ! python3 -c "import streamrip" &>/dev/null 2>&1; then
    warn "streamrip not found — attempting install…"
    pip install -q streamrip || warn "streamrip install failed (optional — YouTube fallback will be used)"
  else
    log "streamrip: available"
  fi
}

# ── Default config ─────────────────────────────────────────────────────────────
setup_config() {
  if [[ ! -f "config/config.toml" ]]; then
    cat > config/config.toml << 'EOF'
[deezer]
arl = ""

[downloads]
quality = "MP3_320"
EOF
    info "Created default config/config.toml — set your Deezer ARL via the web UI"
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  cleanup_port
  setup_dirs
  setup_venv
  install_deps
  setup_config

  echo ""
  log "Starting Music Vault on port ${PORT}…"
  echo -e "${CYAN}  → Open: http://localhost:${PORT}${NC}"
  echo ""

  # Trap for cleanup
  trap 'echo -e "\n${YELLOW}Shutting down Music Vault…${NC}"; kill $SERVER_PID 2>/dev/null; exit 0' SIGINT SIGTERM

  # Support both flat layout (server.py at root) and nested (backend/server.py)
  if [[ -f "$SCRIPT_DIR/backend/server.py" ]]; then
    SERVER_SCRIPT="$SCRIPT_DIR/backend/server.py"
  else
    SERVER_SCRIPT="$SCRIPT_DIR/server.py"
  fi
  MV_PORT=$PORT python3 "$SERVER_SCRIPT" &
  SERVER_PID=$!

  # Wait and check health
  sleep 2
  if kill -0 $SERVER_PID 2>/dev/null; then
    log "Music Vault running (PID: ${SERVER_PID})"
  else
    err "Server failed to start. Check logs in $SCRIPT_DIR/data/logs/"
    exit 1
  fi

  wait $SERVER_PID
}

main