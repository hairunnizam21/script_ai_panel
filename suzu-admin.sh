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

osc52_copy() {
  # Best-effort clipboard copy via OSC52 escape (works in iTerm2, Windows Terminal,
  # kitty, mintty, recent xterm, tmux with set-clipboard on, etc).
  local data="$1" b64
  if command -v base64 >/dev/null 2>&1; then
    b64="$(printf '%s' "$data" | base64 | tr -d '\n')"
    printf '\033]52;c;%s\a' "$b64" >/dev/tty 2>/dev/null || true
  fi
}

_grant_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  printf "Duration formats: 24h, 7d, 30d, 3mo, 1y, or a bare number = days\n"
  read -rp "Premium duration [30d]: " DUR
  DUR="${DUR:-30d}"
  local secs
  if ! secs="$(parse_duration_to_seconds "$DUR")"; then c_red "Invalid duration."; return; fi
  read -rp "Premium daily token limit [20000000]: " LIM
  LIM="${LIM:-20000000}"
  if ! [[ "$LIM" =~ ^[0-9]+$ ]]; then c_red "Limit must be integer."; return; fi
  local expiry_iso
  expiry_iso="$(date -u -d "+$secs seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || python3 -c "import datetime; print((datetime.datetime.utcnow()+datetime.timedelta(seconds=$secs)).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
  sql "UPDATE users SET plan='premium', plan_expires_at='$expiry_iso', tokens_limit_daily=$LIM WHERE id='$UID2';"
  c_grn "Granted Premium to $UID2 until $expiry_iso (limit=$LIM/day)."
}

_extend_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  local plan cur
  plan="$(sql "SELECT IFNULL(plan,'free') FROM users WHERE id='$UID2';")"
  cur="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"
  if [ "$plan" != "premium" ] || [ -z "$cur" ]; then
    c_yel "User is not premium yet. Use Grant instead."; return
  fi
  printf "Current expiry: %s\n" "$cur"
  read -rp "Extra duration (e.g. 7d, 24h, 1mo): " DUR
  [ -z "$DUR" ] && { c_yel "Cancelled."; return; }
  local secs
  if ! secs="$(parse_duration_to_seconds "$DUR")"; then c_red "Invalid."; return; fi
  local new_iso
  new_iso="$(date -u -d "$cur + $secs seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
  if [ -z "$new_iso" ]; then c_red "Could not compute new expiry."; return; fi
  sql "UPDATE users SET plan_expires_at='$new_iso' WHERE id='$UID2';"
  c_grn "New expiry for $UID2: $new_iso"
}

_revoke_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  sql "UPDATE users SET plan='free', plan_expires_at=NULL, tokens_limit_daily=2000000 WHERE id='$UID2';"
  c_grn "Revoked. $UID2 is now Free (limit reset to 2,000,000/day)."
}

_set_limit_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  read -rp "New daily token limit (e.g. 10000000): " V
  [ -z "$V" ] && { c_yel "Cancelled."; return; }
  if ! [[ "$V" =~ ^[0-9]+$ ]]; then c_red "Must be integer."; return; fi
  sql "UPDATE users SET tokens_limit_daily=$V WHERE id='$UID2';"
  c_grn "Updated $UID2 limit to $V/day."
}

_reset_tokens_uid() {
  local UID2="$1"
  [ -z "$UID2" ] && return
  sql "UPDATE users SET tokens_used_today=0 WHERE id='$UID2';"
  c_grn "Reset $UID2 tokens_used_today=0."
}

action_list_users() {
  c_bld "=== Users ==="
  # Get rows: id|email|display_name|plan|expires|used|limit
  local IFS_BACKUP="$IFS"
  mapfile -t rows < <(sql "SELECT id || '|' || IFNULL(email,'') || '|' || IFNULL(display_name,'') || '|' || IFNULL(plan,'free') || '|' || IFNULL(plan_expires_at,'') || '|' || tokens_used_today || '|' || tokens_limit_daily FROM users ORDER BY (plan='premium') DESC, tokens_used_today DESC LIMIT 200;")
  if [ "${#rows[@]}" -eq 0 ]; then c_yel "No users yet."; press_enter; return; fi

  printf "  %-3s  %-9s  %-32s  %-20s  %-19s  %s\n" "#" "Plan" "Email" "Name" "Expires" "Tokens"
  printf "  %s\n" "------------------------------------------------------------------------------------------------------------"
  local i=0
  for row in "${rows[@]}"; do
    i=$((i+1))
    IFS='|' read -r rid remail rname rplan rexpires rused rlimit <<<"$row"
    local badge="Free"
    [ "$rplan" = "premium" ] && badge="★PREMIUM"
    printf "  %-3s  %-9s  %-32s  %-20s  %-19s  %s/%s\n" \
      "$i" "$badge" "${remail:0:32}" "${rname:0:20}" "${rexpires:0:19}" "$rused" "$rlimit"
  done
  IFS="$IFS_BACKUP"
  echo
  read -rp "Pick row # for actions (Enter to exit): " PICK
  if [ -z "$PICK" ]; then return; fi
  if ! [[ "$PICK" =~ ^[0-9]+$ ]] || [ "$PICK" -lt 1 ] || [ "$PICK" -gt "${#rows[@]}" ]; then
    c_red "Invalid #."; press_enter; return
  fi
  local picked="${rows[$((PICK-1))]}"
  IFS='|' read -r SEL_ID SEL_EMAIL SEL_NAME SEL_PLAN SEL_EXP SEL_USED SEL_LIMIT <<<"$picked"
  user_action_menu "$SEL_ID" "$SEL_EMAIL" "$SEL_NAME" "$SEL_PLAN" "$SEL_EXP"
}

user_action_menu() {
  local UID2="$1" EMAIL="$2" NAME="$3" PLAN="$4" EXP="$5"
  while true; do
    echo
    c_bld "--- Selected user ---"
    printf "  ID:      %s\n" "$UID2"
    printf "  Email:   %s\n" "$EMAIL"
    printf "  Name:    %s\n" "$NAME"
    printf "  Plan:    %s\n" "$PLAN"
    printf "  Expires: %s\n" "${EXP:-—}"
    echo
    echo "  1) Salin User ID (clipboard via OSC52 + print full)"
    echo "  2) Grant Premium (donor)"
    echo "  3) Extend Premium"
    echo "  4) Revoke Premium"
    echo "  5) Set token limit"
    echo "  6) Reset tokens hari ini"
    echo "  0) Back"
    read -rp "Action: " a
    case "$a" in
      1)
         osc52_copy "$UID2"
         echo
         c_cyn "User ID (highlight to copy):"
         printf "  %s\n" "$UID2"
         c_grn "(also sent to clipboard via OSC52 if your terminal supports it)"
         press_enter
         ;;
      2) _grant_uid "$UID2";    PLAN="premium"; EXP="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"; press_enter ;;
      3) _extend_uid "$UID2";   EXP="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$UID2';")"; press_enter ;;
      4) _revoke_uid "$UID2";   PLAN="free"; EXP="" ; press_enter ;;
      5) _set_limit_uid "$UID2";press_enter ;;
      6) _reset_tokens_uid "$UID2"; press_enter ;;
      0|"") return ;;
      *) c_red "Invalid."; sleep 1 ;;
    esac
  done
}

# Parse duration strings like '24h', '7d', '3mo', '90m', or bare number (interpreted as days)
parse_duration_to_seconds() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)$ ]]; then
    # bare number = days
    echo $(( ${BASH_REMATCH[1]} * 86400 ))
    return 0
  fi
  if [[ "$s" =~ ^([0-9]+)(s|m|h|d|w|mo|y)$ ]]; then
    local n="${BASH_REMATCH[1]}" unit="${BASH_REMATCH[2]}"
    case "$unit" in
      s)  echo $(( n )) ;;
      m)  echo $(( n * 60 )) ;;
      h)  echo $(( n * 3600 )) ;;
      d)  echo $(( n * 86400 )) ;;
      w)  echo $(( n * 604800 )) ;;
      mo) echo $(( n * 2592000 )) ;;
      y)  echo $(( n * 31536000 )) ;;
    esac
    return 0
  fi
  return 1
}

action_grant_premium() {
  c_bld "=== Donate / Grant Premium to a user ==="
  read -rp "User id (uid) or email substring (or leave empty to pick from list): " Q
  if [ -z "$Q" ]; then action_list_users; return; fi
  local matches
  matches="$(sql "SELECT id || ' | ' || IFNULL(email,'') || ' | ' || IFNULL(display_name,'') || ' | plan=' || IFNULL(plan,'free') FROM users WHERE id LIKE '%$Q%' OR email LIKE '%$Q%' LIMIT 20")"
  if [ -z "$matches" ]; then c_red "No users match."; press_enter; return; fi
  printf "Matches:\n%s\n" "$matches"
  read -rp "Exact user id to upgrade: " UID2
  _grant_uid "$UID2"
  press_enter
}

action_extend_premium() {
  c_bld "=== Extend Premium duration ==="
  read -rp "User id: " UID2
  _extend_uid "$UID2"
  press_enter
}

action_revoke_premium() {
  c_bld "=== Revoke Premium ==="
  read -rp "User id: " UID2
  _revoke_uid "$UID2"
  press_enter
}

action_reset_user_tokens() {
  c_bld "=== Reset today's token usage for a user ==="
  read -rp "User id: " UID2
  _reset_tokens_uid "$UID2"
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
  c_yel " ─── Donate / Premium ───"
  echo "  7) Grant Premium to user (donor)"
  echo "  8) Extend Premium duration"
  echo "  9) Revoke Premium"
  c_yel " ─── Users ───"
  echo " 10) List users (interactive: pick row → actions)"
  echo " 11) Reset today's token usage for a user"
  c_yel " ─── Service ───"
  echo " 12) Restart service (pm2 restart)"
  echo " 13) View live logs"
  echo " 14) git pull + rebuild + restart"
  echo " 15) View current .env"
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
      7) action_grant_premium ;;
      8) action_extend_premium ;;
      9) action_revoke_premium ;;
      10) action_list_users ;;
      11) action_reset_user_tokens ;;
      12) action_restart ;;
      13) action_logs ;;
      14) action_update_repo ;;
      15) action_view_env ;;
      0|q|Q|exit) c_grn "Bye."; exit 0 ;;
      *) c_red "Invalid choice."; sleep 1 ;;
    esac
  done
}

main "$@"
