#!/usr/bin/env bash
# Suzu AI — one-shot VPS installer
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/hairunnizam21/suzu-ai-web/main/scripts/install.sh | bash
# or (interactive):
#   bash scripts/install.sh

set -euo pipefail

REPO_URL="${SUZU_REPO_URL:-https://github.com/hairunnizam21/suzu-ai-web.git}"
INSTALL_DIR="${SUZU_INSTALL_DIR:-/var/www/suzu-ai-web}"
ADMIN_LINK="${SUZU_ADMIN_LINK:-/usr/local/bin/suzu-admin}"
NODE_MAJOR="${SUZU_NODE_MAJOR:-20}"
BRANCH="${SUZU_BRANCH:-main}"

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_grn()   { printf "\033[32m%s\033[0m\n" "$*"; }
c_yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
c_cyn()   { printf "\033[36m%s\033[0m\n" "$*"; }
c_bld()   { printf "\033[1m%s\033[0m\n" "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  c_red "Please run as root (sudo bash install.sh)"
  exit 1
fi

c_bld "==> Suzu AI installer"
c_cyn "Install dir: $INSTALL_DIR"
c_cyn "Repo:        $REPO_URL (branch $BRANCH)"

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

c_bld "==> Updating APT"
DEBIAN_FRONTEND=noninteractive apt-get update -qq

c_bld "==> Installing system packages"
apt_install ca-certificates curl gnupg git nginx certbot python3-certbot-nginx \
  openjdk-17-jre-headless apktool zipalign apksigner unzip sqlite3 build-essential \
  whiptail

if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/v//;s/\..*//')" -lt "$NODE_MAJOR" ]; then
  c_bld "==> Installing Node.js $NODE_MAJOR"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt_install nodejs
fi

if ! command -v pm2 >/dev/null 2>&1; then
  c_bld "==> Installing PM2"
  npm install -g pm2 >/dev/null
fi

c_bld "==> Cloning repo to $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  c_yel "Existing checkout detected — pulling latest from $BRANCH"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

c_bld "==> Generating debug keystore (for signing recompiled APKs)"
mkdir -p server/keystores data/apk_workspaces
if [ ! -f server/keystores/debug.keystore ]; then
  keytool -genkey -v -keystore server/keystores/debug.keystore -storepass android \
    -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null
  c_grn "  Created server/keystores/debug.keystore"
else
  c_yel "  Existing keystore reused"
fi

if [ ! -f .env ] || [ "${SUZU_FORCE_REINIT_ENV:-0}" = "1" ]; then
  c_bld "==> Initial configuration"
  read -rp "Public domain (e.g. suzu-ai.online; leave empty for IP-only):  " DOMAIN
  read -rp "AI base URL [https://core.fiqstr.com/v1]: " AI_API_BASE_URL
  AI_API_BASE_URL="${AI_API_BASE_URL:-https://core.fiqstr.com/v1}"
  read -rp "AI API key: " AI_API_KEY
  read -rp "Default model [fiqstr/claude-sonnet-4.6-thinking-agentic]: " AI_DEFAULT_MODEL
  AI_DEFAULT_MODEL="${AI_DEFAULT_MODEL:-fiqstr/claude-sonnet-4.6-thinking-agentic}"
  read -rp "Firebase project ID (suzu-ai-39dc5): " FIREBASE_PROJECT_ID
  FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-suzu-ai-39dc5}"

  # Generate a random admin token for the REST admin API + APK panel.
  SUZU_ADMIN_TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48)"

  cat > .env <<EOF
NODE_ENV=production
PORT=3001
AI_API_KEY=$AI_API_KEY
AI_API_BASE_URL=$AI_API_BASE_URL
AI_DEFAULT_MODEL=$AI_DEFAULT_MODEL
FIREBASE_PROJECT_ID=$FIREBASE_PROJECT_ID
SUZU_DOMAIN=$DOMAIN
SUZU_ADMIN_TOKEN=$SUZU_ADMIN_TOKEN
EOF
  chmod 600 .env
  c_grn "  Wrote .env"
  c_cyn "  Generated admin token (for the APK admin panel):"
  printf "    %s\n" "$SUZU_ADMIN_TOKEN"
else
  c_yel "Existing .env preserved — use 'suzu-admin' to edit."
  DOMAIN="$(grep -E '^SUZU_DOMAIN=' .env | sed 's/SUZU_DOMAIN=//')"
  # Backfill admin token if missing on existing installs
  if ! grep -qE '^SUZU_ADMIN_TOKEN=' .env; then
    SUZU_ADMIN_TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '=/+' | head -c 48)"
    printf "SUZU_ADMIN_TOKEN=%s\n" "$SUZU_ADMIN_TOKEN" >> .env
    c_cyn "  Backfilled SUZU_ADMIN_TOKEN: $SUZU_ADMIN_TOKEN"
  fi
fi

c_bld "==> Installing npm dependencies"
npm install --no-audit --no-fund
( cd client && npm install --no-audit --no-fund )

c_bld "==> Building client"
( cd client && npm run build )

c_bld "==> Configuring Nginx"
NGINX_CONF="/etc/nginx/sites-available/suzu-ai"
SERVER_NAME="${DOMAIN:-_}"
cat > "$NGINX_CONF" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;
    client_max_body_size 250M;

    location / {
        proxy_pass http://127.0.0.1:3001;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }
}
NGINX
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/suzu-ai
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [ -n "${DOMAIN:-}" ]; then
  c_bld "==> Issuing/renewing SSL with certbot for $DOMAIN"
  certbot --nginx --non-interactive --agree-tos \
    --email "${SUZU_LETSENCRYPT_EMAIL:-admin@$DOMAIN}" \
    -d "$DOMAIN" || c_yel "  certbot failed — you can rerun via 'suzu-admin'"
fi

c_bld "==> Starting Suzu AI under PM2"
pm2 delete suzu-ai >/dev/null 2>&1 || true
pm2 start npm --name suzu-ai --cwd "$INSTALL_DIR" -- start
pm2 save
pm2 startup systemd -u root --hp /root >/dev/null || true

c_bld "==> Installing 'suzu-admin' command"
ln -sf "$INSTALL_DIR/scripts/suzu-admin.sh" "$ADMIN_LINK"
chmod +x "$INSTALL_DIR/scripts/suzu-admin.sh"

# Optional bashrc hook so admin menu auto-launches on SSH login
BASHRC_HOOK_FILE="/etc/profile.d/suzu-admin-banner.sh"
cat > "$BASHRC_HOOK_FILE" <<'EOH'
# Suzu AI admin banner
if [ -t 1 ] && [ -z "${SUZU_NO_ADMIN_BANNER:-}" ] && [ "$(id -u)" -eq 0 ]; then
  printf "\n\033[36m=== Suzu AI VPS ===\033[0m\n"
  printf "Type \033[1msuzu-admin\033[0m to open the admin menu.\n\n"
fi
EOH
chmod +x "$BASHRC_HOOK_FILE"

c_grn "==> Done. Visit https://${DOMAIN:-<server-ip>}/"
c_grn "    Manage with: suzu-admin"
