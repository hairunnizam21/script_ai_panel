#!/usr/bin/env bash
# Suzu AI — VPS admin TUI
# Run as: suzu-admin   (after running install.sh)

set -u

INSTALL_DIR="${SUZU_INSTALL_DIR:-/var/www/suzu-ai-web}"
ENV_FILE="$INSTALL_DIR/.env"
DB_FILE="$INSTALL_DIR/suzu.db"
SERVICE_NAME="suzu-ai"

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_grn()   { printf "\033[32m%s\033[0m\n" "$*"; }
c_yel()   { printf "\033[33m%s\033[0m\n" "$*"; }
c_cyn()   { printf "\033[36m%s\033[0m\n" "$*"; }
c_bld()   { printf "\033[1m%s\033[0m\n" "$*"; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    c_red "Run as root."
    exit 1
  fi
}

require_install() {
  if [ ! -f "$ENV_FILE" ]; then
    c_red "Suzu AI not installed at $INSTALL_DIR. Run install.sh first."
    exit 1
  fi
}

# Read a value from .env (returns blank if missing)
env_get() {
  local key="$1"
  awk -F= -v k="$key" '$1==k { sub(/^[^=]*=/, "", $0); print $0 }' "$ENV_FILE" | tail -n 1
}

# Set a value in .env. Creates the line if missing.
env_set() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # Use awk so we don't have to worry about special chars in `sed`
    local tmp
    tmp="$(mktemp)"
    awk -F= -v k="$key" -v v="$value" 'BEGIN{set=0} {
      if ($1==k) { print k"="v; set=1 } else { print $0 }
    } END { if (!set) print k"="v }' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

restart_service() {
  c_cyn "Restarting $SERVICE_NAME via PM2…"
  pm2 restart "$SERVICE_NAME" >/dev/null && c_grn "  Restarted." || c_red "  PM2 restart failed (is it started?)"
}

sql() {
  sqlite3 "$DB_FILE" "$@"
}

press_enter() {
  printf "\nPress Enter to continue…"
  read -r _ || true
}

action_update_domain() {
  c_bld "=== Update domain ==="
  local current
  current="$(env_get SUZU_DOMAIN)"
  printf "Current domain: %s\n" "${current:-<none>}"
  read -rp "New domain (e.g. suzu-ai.online): " DOMAIN
  [ -z "$DOMAIN" ] && { c_yel "Cancelled."; return; }

  env_set SUZU_DOMAIN "$DOMAIN"

  local NGINX_CONF="/etc/nginx/sites-available/suzu-ai"
  if [ -f "$NGINX_CONF" ]; then
    sed -i -E "s/server_name [^;]+;/server_name $DOMAIN;/" "$NGINX_CONF"
    nginx -t && systemctl reload nginx && c_grn "Nginx reloaded for $DOMAIN"
  else
    c_yel "Nginx config not found at $NGINX_CONF — skipping reload"
  fi

  read -rp "Issue SSL via certbot for $DOMAIN now? [Y/n] " ans
  if [ "${ans:-Y}" != "n" ] && [ "${ans:-Y}" != "N" ]; then
    read -rp "Email for Let's Encrypt [admin@$DOMAIN]: " EMAIL
    EMAIL="${EMAIL:-admin@$DOMAIN}"
    certbot --nginx --non-interactive --agree-tos --email "$EMAIL" -d "$DOMAIN" || c_yel "certbot failed"
  fi
  press_enter
}

action_update_baseurl() {
  c_bld "=== Update AI base URL ==="
  printf "Current: %s\n" "$(env_get AI_API_BASE_URL)"
  read -rp "New base URL: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_API_BASE_URL "$V"
  restart_service
  press_enter
}

action_update_apikey() {
  c_bld "=== Update AI API key ==="
  printf "Current (masked): %s\n" "$(env_get AI_API_KEY | sed -E 's/(.{4}).*(.{4})/\1…\2/')"
  read -rp "New API key: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_API_KEY "$V"
  restart_service
  press_enter
}

action_update_model() {
  c_bld "=== Update default model ==="
  printf "Current: %s\n" "$(env_get AI_DEFAULT_MODEL)"
  read -rp "New model id: " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  env_set AI_DEFAULT_MODEL "$V"
  restart_service
  press_enter
}

action_default_limit() {
  c_bld "=== Default daily token limit (for ALL users) ==="
  local current
  current="$(sql "SELECT IFNULL(MIN(tokens_limit_daily),0) FROM users")"
  printf "Smallest current limit in DB: %s\n" "$current"
  read -rp "New default limit for all users (e.g. 2000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be an integer."; press_enter; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V;"
  c_grn "Updated $(sql "SELECT changes()") users."
  press_enter
}

action_set_user_limit() {
  c_bld "=== Set token limit for one user (donor) ==="
  read -rp "User id (uid) or email substring: " Q
  [ -z "$Q" ] && { c_yel "Cancelled."; return; }
  local matches
  matches="$(sql "SELECT id, IFNULL(display_name,''), IFNULL(email,''), tokens_used_today, tokens_limit_daily FROM users WHERE id LIKE '%$Q%' OR email LIKE '%$Q%' LIMIT 20" -separator " | ")"
  if [ -z "$matches" ]; then c_red "No users match."; press_enter; return; fi
  printf "Matches (id | name | email | used | limit):\n%s\n" "$matches"
  read -rp "Exact user id to update: " UID2
  [ -z "$UID2" ] && { c_yel "Cancelled."; return; }
  read -rp "New token limit (e.g. 10000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be an integer."; press_enter; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V WHERE id='$UID2';"
  c_grn "Updated user $UID2 to $V tokens/day."
  press_enter
}

action_list_users() {
  c_bld "=== Users ==="
  sql ".headers on" ".mode column" "SELECT id, IFNULL(email,'') AS email, IFNULL(display_name,'') AS name, tokens_used_today AS used, tokens_limit_daily AS limit, tokens_reset_at AS reset FROM users ORDER BY tokens_used_today DESC;"
  press_enter
}

action_reset_user_tokens() {
  c_bld "=== Reset today's token usage for a user ==="
  read -rp "User id: " UID2
  [ -z "$UID2" ] && { c_yel "Cancelled."; return; }
  sql "UPDATE users SET tokens_used_today=0 WHERE id='$UID2';"
  c_grn "Done."
  press_enter
}

action_restart() {
  restart_service
  press_enter
}

action_logs() {
  c_bld "=== Live logs (Ctrl-C to exit) ==="
  pm2 logs "$SERVICE_NAME" --lines 100 || true
}

action_update_repo() {
  c_bld "=== Update from git + rebuild ==="
  cd "$INSTALL_DIR" || return
  git fetch --all --tags
  read -rp "Branch to checkout [$(git rev-parse --abbrev-ref HEAD)]: " B
  B="${B:-$(git rev-parse --abbrev-ref HEAD)}"
  git checkout "$B"
  git pull --ff-only
  npm install --no-audit --no-fund
  ( cd client && npm install --no-audit --no-fund )
  ( cd client && npm run build )
  restart_service
  press_enter
}

action_view_env() {
  c_bld "=== Current .env (API key masked) ==="
  awk -F= '{
    if ($1=="AI_API_KEY") {
      v=$2
      if (length(v)>8) { print $1"="substr(v,1,4)"…"substr(v,length(v)-3) }
      else { print $1"=…" }
    } else { print $0 }
  }' "$ENV_FILE"
  press_enter
}

show_menu() {
  clear
  c_bld "╔════════════════════════════════════════╗"
  c_bld "║         Suzu AI — Admin Panel          ║"
  c_bld "╚════════════════════════════════════════╝"
  printf "  Domain:   %s\n" "$(env_get SUZU_DOMAIN)"
  printf "  Base URL: %s\n" "$(env_get AI_API_BASE_URL)"
  printf "  Model:    %s\n" "$(env_get AI_DEFAULT_MODEL)"
  echo
  echo "  1) Update domain (+ SSL)"
  echo "  2) Update AI base URL"
  echo "  3) Update AI API key"
  echo "  4) Update default model"
  echo "  5) Set default daily token limit (all users)"
  echo "  6) Set token limit for one user (donor)"
  echo "  7) List users"
  echo "  8) Reset today's token usage for a user"
  echo "  9) Restart service (pm2 restart)"
  echo " 10) View live logs"
  echo " 11) git pull + rebuild + restart"
  echo " 12) View current .env"
  echo "  0) Exit to shell"
  echo
  read -rp "Choose an option: " choice
}

main() {
  need_root
  require_install
  while true; do
    show_menu
    case "${choice:-}" in
      1) action_update_domain ;;
      2) action_update_baseurl ;;
      3) action_update_apikey ;;
      4) action_update_model ;;
      5) action_default_limit ;;
      6) action_set_user_limit ;;
      7) action_list_users ;;
      8) action_reset_user_tokens ;;
      9) action_restart ;;
      10) action_logs ;;
      11) action_update_repo ;;
      12) action_view_env ;;
      0|q|Q|exit) c_grn "Bye."; exit 0 ;;
      *) c_red "Invalid choice."; sleep 1 ;;
    esac
  done
}

main "$@"
