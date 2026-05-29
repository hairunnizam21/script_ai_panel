#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  Suzu AI — Terminal Panel installer (server-only edition)          ║
# ║  Turns a fresh Ubuntu/Debian box into an AI chat + admin terminal. ║
# ╚══════════════════════════════════════════════════════════════════╝
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/hairunnizam21/script_ai_panel/setup/install.sh | sudo bash
# or locally:
#   sudo bash install.sh
set -euo pipefail

REPO_RAW="${SUZU_PANEL_RAW:-https://raw.githubusercontent.com/hairunnizam21/script_ai_panel}"
BRANCH="${SUZU_PANEL_BRANCH:-setup}"
SUZU_HOME="${SUZU_HOME:-/etc/suzu-ai}"
LIB_PATH="${SUZU_LIB_PATH:-/usr/local/lib/suzu-admin.sh}"
LINK_PATH="${SUZU_ADMIN_LINK:-/usr/local/bin/suzu-admin}"
ENV_FILE="$SUZU_HOME/suzu.env"

c() { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
bold() { c "1" "$1"; }
grn()  { c "32" "$1"; }
cyn()  { c "36" "$1"; }
yel()  { c "33" "$1"; }
red()  { c "31" "$1"; }

if [ "$(id -u)" -ne 0 ]; then
  red "Jalankan sebagai root: sudo bash install.sh"
  exit 1
fi

bold "==> Suzu AI Terminal Panel — installer"
cyn  "    Config dir : $SUZU_HOME"
cyn  "    Command    : suzu-admin"

# ── Dependencies ──────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  bold "==> Memasang dependensi (curl, jq, sqlite3, zip, unzip)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl jq sqlite3 zip unzip >/dev/null
else
  yel "apt-get tidak ditemukan — pastikan curl, jq, sqlite3, zip, unzip sudah terpasang."
fi

# ── Obtain the admin script (local copy if present, else download) ─────
mkdir -p "$SUZU_HOME" "$(dirname "$LIB_PATH")"
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/suzu-admin.sh" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SRC_DIR" ]; then
  bold "==> Memasang suzu-admin.sh (dari folder lokal)"
  install -m 0755 "$SRC_DIR/suzu-admin.sh" "$LIB_PATH"
else
  bold "==> Mengunduh suzu-admin.sh ($BRANCH)"
  curl -fsSL "$REPO_RAW/$BRANCH/suzu-admin.sh" -o "$LIB_PATH"
  chmod 0755 "$LIB_PATH"
fi
ln -sf "$LIB_PATH" "$LINK_PATH"

# ── Initial config ────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ] || [ "${SUZU_FORCE_REINIT:-0}" = "1" ]; then
  bold "==> Konfigurasi awal"
  read -rp "AI base URL [https://core.fiqstr.com/v1]: " AI_API_BASE_URL
  AI_API_BASE_URL="${AI_API_BASE_URL:-https://core.fiqstr.com/v1}"
  read -rp "AI API key: " AI_API_KEY
  read -rp "Model default [fiqstr/claude-sonnet-4.6-thinking-agentic]: " AI_DEFAULT_MODEL
  AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-fiqstr/claude-sonnet-4.6-thinking-agentic}"

  umask 077
  cat > "$ENV_FILE" <<EOF
AI_API_BASE_URL=$AI_API_BASE_URL
AI_API_KEY=$AI_API_KEY
AI_DEFAULT_MODEL=$AI_DEFAULT_MODEL
SUZU_DOMAIN=
SUZU_MODELS=
EOF
  chmod 600 "$ENV_FILE"
  grn "    Config tersimpan: $ENV_FILE"
else
  yel "Config sudah ada ($ENV_FILE) — dipertahankan. Ubah lewat 'suzu-admin'."
fi

# ── Login banner ──────────────────────────────────────────────────────
cat > /etc/profile.d/suzu-admin-banner.sh <<'EOH'
if [ -t 1 ] && [ -z "${SUZU_NO_ADMIN_BANNER:-}" ]; then
  printf "\n\033[36m=== Suzu AI Terminal ===\033[0m\n"
  printf "Ketik \033[1msuzu-admin\033[0m untuk membuka panel (chat AI + kelola server).\n\n"
fi
EOH
chmod +x /etc/profile.d/suzu-admin-banner.sh

echo
grn "==> Selesai!"
cyn "    Jalankan:  suzu-admin"
cyn "    Lalu pilih menu 1) Chat AI untuk mulai ngobrol dengan AI."
