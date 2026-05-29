#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  Suzu AI — Terminal Panel                                          ║
# ║  Chat with AI + manage config, users, backups — all in the shell. ║
# ║  Self-contained: needs only curl + jq (+ sqlite3 for users).      ║
# ╚══════════════════════════════════════════════════════════════════╝
set -u

# ──────────────────────────────────────────────────────────────────────
# Paths / config discovery
# ──────────────────────────────────────────────────────────────────────
# Where the config (.env) lives. Auto-detect a legacy suzu-ai-web install,
# otherwise fall back to a dedicated standalone location.
APP_DIR="${SUZU_INSTALL_DIR:-/var/www/suzu-ai-web}"
STANDALONE_DIR="${SUZU_HOME:-/etc/suzu-ai}"

if [ -n "${SUZU_ENV_FILE:-}" ]; then
  ENV_FILE="$SUZU_ENV_FILE"
elif [ -f "$APP_DIR/.env" ]; then
  ENV_FILE="$APP_DIR/.env"
else
  ENV_FILE="$STANDALONE_DIR/suzu.env"
fi

# Database (used only for the Users/Premium features). Optional.
if [ -n "${SUZU_DB_FILE:-}" ]; then
  DB_FILE="$SUZU_DB_FILE"
elif [ -f "$APP_DIR/suzu.db" ]; then
  DB_FILE="$APP_DIR/suzu.db"
else
  DB_FILE="$STANDALONE_DIR/suzu.db"
fi

SERVICE_NAME="${SUZU_SERVICE_NAME:-suzu-ai}"
CHAT_DIR="${SUZU_CHAT_DIR:-$STANDALONE_DIR/chats}"
BACKUP_DIR="${SUZU_BACKUP_DIR:-/var/backups}"

# ──────────────────────────────────────────────────────────────────────
# Colors (truecolor with graceful fallback; respects NO_COLOR)
# ──────────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  E=$'\033'
  RESET="${E}[0m"; BOLD="${E}[1m"; DIM="${E}[2m"
  # Kali-ish dragon palette
  CY="${E}[38;2;0;200;255m"      # cyan
  BL="${E}[38;2;80;140;255m"     # blue
  GR="${E}[38;2;90;255;140m"     # green
  RD="${E}[38;2;255;95;95m"      # red
  YE="${E}[38;2;255;210;90m"     # yellow
  MG="${E}[38;2;205;120;255m"    # magenta
  GY="${E}[38;2;130;140;150m"    # gray
  WT="${E}[38;2;235;240;245m"    # near-white
else
  RESET=""; BOLD=""; DIM=""
  CY=""; BL=""; GR=""; RD=""; YE=""; MG=""; GY=""; WT=""
fi

# ──────────────────────────────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────────────────────────────
ok()    { printf "  ${GR}✔${RESET} %s\n" "$*"; }
err()   { printf "  ${RD}✖${RESET} %s\n" "$*"; }
warn()  { printf "  ${YE}!${RESET} %s\n" "$*"; }
info()  { printf "  ${CY}i${RESET} %s\n" "$*"; }
hr()    { printf "${GY}  ─────────────────────────────────────────────────────────────${RESET}\n"; }

# A boxed section header
section() {
  printf "\n${BL}  ╭─${RESET} ${BOLD}${WT}%s${RESET}\n" "$1"
  printf "${BL}  ╰─────────────────────────────────────────────────────────────${RESET}\n"
}

pause() { printf "\n${DIM}  Tekan Enter untuk lanjut…${RESET}"; read -r _ || true; }

# Kali-style two-line prompt. Usage: ask "Label" VARNAME [default]
ask() {
  local label="$1" __var="$2" def="${3:-}" ans
  if [ -n "$def" ]; then
    printf "${GR}  ┌──(${CY}suzu${MG}㉿${CY}ai${GR})-[${WT}%s${GR}]${RESET}\n" "$label"
    printf "${GR}  └─${CY}\$${RESET} ${DIM}[%s]${RESET} " "$def"
  else
    printf "${GR}  ┌──(${CY}suzu${MG}㉿${CY}ai${GR})-[${WT}%s${GR}]${RESET}\n" "$label"
    printf "${GR}  └─${CY}\$${RESET} "
  fi
  IFS= read -r ans || true
  [ -z "$ans" ] && ans="$def"
  printf -v "$__var" '%s' "$ans"
}

# Silent prompt (for secrets)
ask_secret() {
  local label="$1" __var="$2" ans
  printf "${GR}  ┌──(${CY}suzu${MG}㉿${CY}ai${GR})-[${WT}%s${GR}]${RESET}\n" "$label"
  printf "${GR}  └─${CY}\$${RESET} "
  IFS= read -rs ans || true
  echo
  printf -v "$__var" '%s' "$ans"
}

confirm() {
  local prompt="$1" ans
  printf "${YE}  %s${RESET} ${DIM}[y/N]${RESET} " "$prompt"
  IFS= read -r ans || true
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

mask() {
  local v="$1"
  if [ -z "$v" ]; then printf "${DIM}(belum diset)${RESET}"; return; fi
  if [ "${#v}" -le 8 ]; then printf "%s" "••••"; return; fi
  printf "%s…%s" "${v:0:4}" "${v: -4}"
}

die() { err "$*"; exit 1; }

# ──────────────────────────────────────────────────────────────────────
# Dependencies
# ──────────────────────────────────────────────────────────────────────
has() { command -v "$1" >/dev/null 2>&1; }

ensure_dep() {
  # ensure_dep <command> <apt-package>
  local cmd="$1" pkg="$2"
  has "$cmd" && return 0
  if [ "$(id -u)" -eq 0 ] && has apt-get; then
    warn "Memasang '$pkg'…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" >/dev/null 2>&1
  fi
  has "$cmd"
}

require_runtime() {
  ensure_dep curl curl || die "curl wajib ada. Pasang dulu: apt-get install -y curl"
  ensure_dep jq jq     || die "jq wajib ada untuk chat. Pasang dulu: apt-get install -y jq"
  mkdir -p "$(dirname "$ENV_FILE")" "$CHAT_DIR" 2>/dev/null || true
  if [ ! -f "$ENV_FILE" ]; then
    # First run on a fresh box — create a minimal config.
    umask 077
    cat > "$ENV_FILE" <<EOF
AI_API_BASE_URL=https://core.fiqstr.com/v1
AI_API_KEY=
AI_DEFAULT_MODEL=fiqstr/claude-sonnet-4.6-thinking-agentic
SUZU_DOMAIN=
EOF
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    warn "Config baru dibuat di $ENV_FILE — set API key dulu lewat menu Config."
  fi
}

# ──────────────────────────────────────────────────────────────────────
# .env get/set  (config is read live every time → updates apply instantly)
# ──────────────────────────────────────────────────────────────────────
env_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || { printf ""; return; }
  # last matching line wins; strip surrounding quotes
  awk -F= -v k="$key" '
    $1==k { sub(/^[^=]*=/, "", $0); val=$0 }
    END { gsub(/^"|"$/, "", val); print val }
  ' "$ENV_FILE"
}

env_set() {
  local key="$1" value="$2" tmp
  mkdir -p "$(dirname "$ENV_FILE")" 2>/dev/null || true
  touch "$ENV_FILE"
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { set=0 }
    {
      if ($0 ~ "^"k"=") { print k"="v; set=1 }
      else { print $0 }
    }
    END { if (!set) print k"="v }
  ' "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

# sqlite helper (only if DB + sqlite3 exist)
db_ready() { [ -f "$DB_FILE" ] && has sqlite3; }
sql() { sqlite3 "$DB_FILE" "$@"; }

# Optional: restart the legacy web service if the user still runs it
maybe_restart_web() {
  has pm2 || return 0
  pm2 describe "$SERVICE_NAME" >/dev/null 2>&1 || return 0
  if confirm "Service web '$SERVICE_NAME' terdeteksi. Restart agar website ikut terupdate?"; then
    if pm2 restart "$SERVICE_NAME" --update-env >/dev/null 2>&1; then
      ok "Service web di-restart."
    else
      warn "Gagal restart service web (abaikan jika website memang sudah dimatikan)."
    fi
  fi
}

# ──────────────────────────────────────────────────────────────────────
# Banner + status panel
# ──────────────────────────────────────────────────────────────────────
banner() {
  clear
  printf "\n"
  printf "${CY}   ███████╗██╗   ██╗███████╗██╗   ██╗${MG}     █████╗ ██╗${RESET}\n"
  printf "${CY}   ██╔════╝██║   ██║╚══███╔╝██║   ██║${MG}    ██╔══██╗██║${RESET}\n"
  printf "${CY}   ███████╗██║   ██║  ███╔╝ ██║   ██║${MG}    ███████║██║${RESET}\n"
  printf "${BL}   ╚════██║██║   ██║ ███╔╝  ██║   ██║${MG}    ██╔══██║██║${RESET}\n"
  printf "${BL}   ███████║╚██████╔╝███████╗╚██████╔╝${MG}    ██║  ██║██║${RESET}\n"
  printf "${BL}   ╚══════╝ ╚═════╝ ╚══════╝ ╚═════╝ ${MG}    ╚═╝  ╚═╝╚═╝${RESET}\n"
  printf "${DIM}        Terminal AI Panel  •  v2.0  •  server-only edition${RESET}\n"
}

status_panel() {
  local base model key dom svc users
  base="$(env_get AI_API_BASE_URL)"
  model="$(env_get AI_DEFAULT_MODEL)"
  key="$(env_get AI_API_KEY)"
  dom="$(env_get SUZU_DOMAIN)"

  local keystate
  if [ -n "$key" ]; then keystate="${GR}● aktif${RESET} ($(mask "$key"))"; else keystate="${RD}● kosong${RESET}"; fi

  svc="${DIM}—${RESET}"
  if has pm2 && pm2 pid "$SERVICE_NAME" >/dev/null 2>&1; then
    if [ -n "$(pm2 pid "$SERVICE_NAME" 2>/dev/null)" ]; then svc="${GR}online${RESET}"; else svc="${RD}offline${RESET}"; fi
  fi

  users="${DIM}n/a${RESET}"
  if db_ready; then users="$(sql 'SELECT COUNT(*) FROM users' 2>/dev/null || echo '?')"; fi

  printf "\n${BL}  ╭─ status ──────────────────────────────────────────────────╮${RESET}\n"
  printf "  ${GY}API Key :${RESET} %b\n" "$keystate"
  printf "  ${GY}Base URL:${RESET} ${WT}%s${RESET}\n" "${base:-—}"
  printf "  ${GY}Model   :${RESET} ${WT}%s${RESET}\n" "${model:-—}"
  printf "  ${GY}Domain  :${RESET} ${WT}%s${RESET}   ${GY}Web svc:${RESET} %b   ${GY}Users:${RESET} ${WT}%s${RESET}\n" "${dom:-—}" "$svc" "$users"
  printf "  ${GY}Config  :${RESET} ${DIM}%s${RESET}\n" "$ENV_FILE"
  printf "${BL}  ╰───────────────────────────────────────────────────────────╯${RESET}\n"
}

# ──────────────────────────────────────────────────────────────────────
# AI CHAT (the star feature) — streaming, ala Claude, in the terminal
# ──────────────────────────────────────────────────────────────────────
CHAT_SYSTEM_PROMPT="${SUZU_SYSTEM_PROMPT:-You are Suzu AI, a concise and helpful assistant. Reply in the same language the user writes in. Be direct. Use markdown sparingly.}"

# Stream filter: removes <thinking>...</thinking> across chunk boundaries and
# prints the visible answer live. Reads raw concatenated content on stdin.
strip_thinking_stream() {
  local in_think=0 buf="" chunk
  # read char-by-char so we can stream live
  while IFS= read -r -d '' -n 1 chunk 2>/dev/null || [ -n "$chunk" ]; do
    buf+="$chunk"
    chunk=""
    while :; do
      if [ "$in_think" -eq 0 ]; then
        case "$buf" in
          *"<thinking>"*) printf '%s' "${buf%%<thinking>*}"; buf="${buf#*<thinking>}"; in_think=1 ;;
          *)
            # hold back last 10 chars in case a "<thinking>" tag is split
            if [ "${#buf}" -gt 10 ]; then
              printf '%s' "${buf:0:${#buf}-10}"; buf="${buf: -10}"
            fi
            break ;;
        esac
      else
        case "$buf" in
          *"</thinking>"*) buf="${buf#*</thinking>}"; in_think=0 ;;
          *) if [ "${#buf}" -gt 11 ]; then buf="${buf: -11}"; fi; break ;;
        esac
      fi
    done
  done
  [ "$in_think" -eq 0 ] && printf '%s' "$buf"
}

chat_loop() {
  local base key model
  base="$(env_get AI_API_BASE_URL)"; key="$(env_get AI_API_KEY)"; model="$(env_get AI_DEFAULT_MODEL)"
  if [ -z "$key" ]; then err "API key belum diset. Buka menu Config dulu."; pause; return; fi
  [ -z "$model" ] && model="fiqstr/claude-sonnet-4.6-thinking-agentic"

  local hist; hist="$(mktemp "${CHAT_DIR}/chat.XXXXXX.json")"
  printf '[{"role":"system","content":%s}]' "$(printf '%s' "$CHAT_SYSTEM_PROMPT" | jq -Rs .)" > "$hist"

  clear
  printf "\n${CY}  ┌─ Suzu AI Chat ───────────────────────────────────────────┐${RESET}\n"
  printf "  ${GY}model:${RESET} ${WT}%s${RESET}\n" "$model"
  printf "  ${DIM}Perintah: ${RESET}${YE}/model${RESET} ganti model · ${YE}/new${RESET} reset · ${YE}/exit${RESET} keluar\n"
  printf "${CY}  └──────────────────────────────────────────────────────────┘${RESET}\n\n"

  local input
  while :; do
    printf "${GR}  ┌──(${CY}you${GR})${RESET}\n${GR}  └─${CY}❯${RESET} "
    IFS= read -r input || break
    [ -z "$input" ] && continue
    case "$input" in
      /exit|/quit|/q) break ;;
      /new)
        printf '[{"role":"system","content":%s}]' "$(printf '%s' "$CHAT_SYSTEM_PROMPT" | jq -Rs .)" > "$hist"
        info "Percakapan direset."; echo; continue ;;
      /model)
        chat_pick_model model; echo; continue ;;
      /help)
        info "/model ganti model · /new reset · /exit keluar"; echo; continue ;;
    esac

    # append user message
    local tmp; tmp="$(mktemp)"
    jq --arg c "$input" '. += [{"role":"user","content":$c}]' "$hist" > "$tmp" && mv "$tmp" "$hist"

    # build request body
    local body; body="$(jq -n --arg m "$model" --slurpfile msgs "$hist" \
      '{model:$m, messages:$msgs[0], stream:true, stream_options:{include_usage:true}}')"

    printf "\n${MG}  ┌──(${WT}Suzu AI${MG})${RESET}\n  "
    local raw ans
    raw="$(mktemp)"
    # stream + render, capturing the cleaned answer
    ans="$(curl -sS -N --no-buffer "$base/chat/completions" \
        -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
        --data-binary "$body" 2>>"$raw" \
      | tee -a "$raw" \
      | grep --line-buffered '^data: ' \
      | sed -u 's/^data: //' \
      | grep --line-buffered -v '^\[DONE\]$' \
      | jq -rj --unbuffered '.choices[0].delta.content // empty' 2>/dev/null \
      | strip_thinking_stream \
      | tee /dev/tty)"
    printf "\n"

    if [ -z "$ans" ]; then
      # surface API error if present
      local emsg
      emsg="$(grep '^data: ' "$raw" 2>/dev/null | sed 's/^data: //' | jq -rs 'map(.error.message? // empty) | map(select(.!=null)) | .[0] // empty' 2>/dev/null)"
      [ -z "$emsg" ] && emsg="$(jq -r '.error.message? // empty' "$raw" 2>/dev/null)"
      if printf '%s' "$emsg" | grep -qiE 'rate.?limit|too many'; then
        err "Kena rate limit dari server AI. Coba ganti model (/model) atau ganti API key di menu Config."
      elif [ -n "$emsg" ]; then
        err "AI error: $emsg"
      else
        err "Tidak ada balasan (cek API key / base URL / koneksi)."
      fi
    else
      tmp="$(mktemp)"
      jq --arg c "$ans" '. += [{"role":"assistant","content":$c}]' "$hist" > "$tmp" && mv "$tmp" "$hist"
    fi
    rm -f "$raw"
    echo
  done
  rm -f "$hist"
}

# Favorite models are stored comma-separated in SUZU_MODELS.
favs_load() {
  # populates global array FAVS
  FAVS=()
  local raw; raw="$(env_get SUZU_MODELS)"
  [ -z "$raw" ] && return
  local IFS=','; local m
  for m in $raw; do m="${m#"${m%%[![:space:]]*}"}"; m="${m%"${m##*[![:space:]]}"}"; [ -n "$m" ] && FAVS+=("$m"); done
}

favs_save() {
  # joins FAVS into SUZU_MODELS
  local out=""; local m
  for m in "${FAVS[@]}"; do [ -z "$out" ] && out="$m" || out="$out,$m"; done
  env_set SUZU_MODELS "$out"
}

# Fetch model id list from the provider API.
fetch_models() {
  local base key; base="$(env_get AI_API_BASE_URL)"; key="$(env_get AI_API_KEY)"
  curl -sS "$base/models" -H "Authorization: Bearer $key" 2>/dev/null \
    | jq -r '.data[]?.id // .models[]?.id // empty' 2>/dev/null | sort -u
}

# Pick a model into a variable. Shows favorites first, then fetch/manual.
chat_pick_model() {
  local __var="$1" choice; declare -a FAVS=(); favs_load
  echo
  if [ "${#FAVS[@]}" -gt 0 ]; then
    printf "  ${YE}★ Favorit:${RESET}\n"
    local i=0; for m in "${FAVS[@]}"; do i=$((i+1)); printf "  ${YE}%2d)${RESET} %s\n" "$i" "$m"; done
    printf "  ${GY}─ atau ─${RESET}\n"
  fi
  printf "  ${CY}f${RESET}) Ambil daftar lengkap dari server   ${CY}m${RESET}) Ketik manual\n"
  ask "pilih model" choice ""
  case "$choice" in
    "") return ;;
    f|F) _pick_from_api "$__var"; return ;;
    m|M) local v; ask "model id" v "$(env_get AI_DEFAULT_MODEL)"; [ -n "$v" ] && { printf -v "$__var" '%s' "$v"; ok "Model: $v"; }; return ;;
  esac
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#FAVS[@]}" ]; then
    printf -v "$__var" '%s' "${FAVS[$((choice-1))]}"; ok "Model: ${FAVS[$((choice-1))]}"
  else
    printf -v "$__var" '%s' "$choice"; ok "Model: $choice"
  fi
}

_pick_from_api() {
  local __var="$1" choice; local -a arr=()
  info "Mengambil daftar model dari server…"
  mapfile -t arr < <(fetch_models)
  if [ "${#arr[@]}" -eq 0 ]; then
    warn "Tidak bisa ambil daftar (cek API key/base URL). Ketik manual."
    ask "model id" choice "$(env_get AI_DEFAULT_MODEL)"; [ -n "$choice" ] && printf -v "$__var" '%s' "$choice"; return
  fi
  local i=0; for m in "${arr[@]}"; do i=$((i+1)); printf "  ${YE}%3d)${RESET} %s\n" "$i" "$m"; done
  ask "pilih nomor (atau ketik model id)" choice ""
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#arr[@]}" ]; then
    printf -v "$__var" '%s' "${arr[$((choice-1))]}"; ok "Model: ${arr[$((choice-1))]}"
  elif [ -n "$choice" ]; then
    printf -v "$__var" '%s' "$choice"; ok "Model: $choice"
  fi
}

# Manage favorite models (add from API / add manual / remove / set default).
favorites_menu() {
  while :; do
    banner; section "Model Favorit"
    local -a FAVS=(); favs_load
    if [ "${#FAVS[@]}" -eq 0 ]; then
      warn "Belum ada model favorit."
    else
      local i=0; printf "  ${GY}Model default:${RESET} ${WT}%s${RESET}\n\n" "$(env_get AI_DEFAULT_MODEL)"
      for m in "${FAVS[@]}"; do i=$((i+1))
        local star=" "; [ "$m" = "$(env_get AI_DEFAULT_MODEL)" ] && star="${GR}●${RESET}"
        printf "  %b ${WT}%2d)${RESET} %s\n" "$star" "$i" "$m"
      done
    fi
    printf "\n  ${YE}a${RESET}) Tambah dari daftar server   ${YE}t${RESET}) Tambah manual\n"
    printf "  ${YE}d${RESET}) Hapus favorit   ${YE}s${RESET}) Jadikan default   ${YE}0${RESET}) Kembali\n\n"
    local c; ask "favorit" c ""
    case "$c" in
      a|A)
        info "Mengambil daftar model…"; local -a arr=(); mapfile -t arr < <(fetch_models)
        if [ "${#arr[@]}" -eq 0 ]; then warn "Gagal ambil daftar."; pause; continue; fi
        local i=0; for m in "${arr[@]}"; do i=$((i+1)); printf "  ${YE}%3d)${RESET} %s\n" "$i" "$m"; done
        local n; ask "nomor model untuk ditambahkan" n ""
        if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#arr[@]}" ]; then
          FAVS+=("${arr[$((n-1))]}"); favs_save; ok "Ditambahkan: ${arr[$((n-1))]}"
        else err "Nomor tidak valid."; fi; pause ;;
      t|T)
        local v; ask "model id" v ""; [ -n "$v" ] && { FAVS+=("$v"); favs_save; ok "Ditambahkan: $v"; }; pause ;;
      d|D)
        [ "${#FAVS[@]}" -eq 0 ] && { warn "Kosong."; pause; continue; }
        local n; ask "nomor favorit untuk dihapus" n ""
        if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#FAVS[@]}" ]; then
          local removed="${FAVS[$((n-1))]}"; unset 'FAVS[$((n-1))]'; FAVS=("${FAVS[@]}"); favs_save; ok "Dihapus: $removed"
        else err "Nomor tidak valid."; fi; pause ;;
      s|S)
        [ "${#FAVS[@]}" -eq 0 ] && { warn "Kosong."; pause; continue; }
        local n; ask "nomor favorit untuk jadi default" n ""
        if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#FAVS[@]}" ]; then
          env_set AI_DEFAULT_MODEL "${FAVS[$((n-1))]}"; ok "Default → ${FAVS[$((n-1))]}"; maybe_restart_web
        else err "Nomor tidak valid."; fi; pause ;;
      0|"") return ;;
      *) err "Tidak valid."; sleep 1 ;;
    esac
  done
}

# ──────────────────────────────────────────────────────────────────────
# CONFIG actions
# ──────────────────────────────────────────────────────────────────────
test_ai() {
  local base key model body out code
  base="$(env_get AI_API_BASE_URL)"; key="$(env_get AI_API_KEY)"; model="$(env_get AI_DEFAULT_MODEL)"
  [ -z "$key" ] && { err "API key kosong."; return 1; }
  info "Tes ke $base (model: $model)…"
  body="$(jq -n --arg m "$model" '{model:$m, messages:[{role:"user",content:"ping"}], max_tokens:5}')"
  out="$(curl -sS -w '\n%{http_code}' "$base/chat/completions" \
    -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
    --data-binary "$body" 2>/dev/null)"
  code="$(printf '%s' "$out" | tail -n1)"
  if [ "$code" = "200" ]; then
    ok "AI OK (HTTP 200) — config aktif & berfungsi."
    return 0
  else
    err "AI gagal (HTTP ${code:-?}). Pesan: $(printf '%s' "$out" | sed '$d' | jq -r '.error.message? // empty' 2>/dev/null)"
    return 1
  fi
}

config_menu() {
  while :; do
    banner
    section "Konfigurasi Server"
    printf "  ${GY}Base URL:${RESET} ${WT}%s${RESET}\n" "$(env_get AI_API_BASE_URL)"
    printf "  ${GY}API Key :${RESET} ${WT}%s${RESET}\n" "$(mask "$(env_get AI_API_KEY)")"
    printf "  ${GY}Model   :${RESET} ${WT}%s${RESET}\n" "$(env_get AI_DEFAULT_MODEL)"
    printf "  ${GY}Domain  :${RESET} ${WT}%s${RESET}\n\n" "$(env_get SUZU_DOMAIN)"
    printf "  ${YE}1${RESET}) Update API Key\n"
    printf "  ${YE}2${RESET}) Update Base URL\n"
    printf "  ${YE}3${RESET}) Pilih Model default ${DIM}(favorit / daftar server)${RESET}\n"
    printf "  ${YE}4${RESET}) Kelola Model Favorit\n"
    printf "  ${YE}5${RESET}) Update Domain\n"
    printf "  ${YE}6${RESET}) Tes koneksi AI sekarang\n"
    printf "  ${YE}0${RESET}) Kembali\n\n"
    local c; ask "config" c ""
    case "$c" in
      1) local v; ask_secret "API Key baru" v; [ -n "$v" ] && { env_set AI_API_KEY "$v"; ok "API key diperbarui."; test_ai; maybe_restart_web; } ; pause ;;
      2) local v; ask "Base URL baru" v "$(env_get AI_API_BASE_URL)"; [ -n "$v" ] && { env_set AI_API_BASE_URL "$v"; ok "Base URL diperbarui."; maybe_restart_web; }; pause ;;
      3) local v=""; chat_pick_model v; [ -n "$v" ] && { env_set AI_DEFAULT_MODEL "$v"; ok "Model default → $v"; maybe_restart_web; }; pause ;;
      4) favorites_menu ;;
      5) local v; ask "Domain baru" v "$(env_get SUZU_DOMAIN)"; env_set SUZU_DOMAIN "$v"; ok "Domain diperbarui."; pause ;;
      6) test_ai; pause ;;
      0|"") return ;;
      *) err "Pilihan tidak valid."; sleep 1 ;;
    esac
  done
}

# ──────────────────────────────────────────────────────────────────────
# USERS & PREMIUM (requires sqlite3 + DB)
# ──────────────────────────────────────────────────────────────────────
parse_duration_to_seconds() {
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)$ ]]; then echo $(( ${BASH_REMATCH[1]} * 86400 )); return 0; fi
  if [[ "$s" =~ ^([0-9]+)(s|m|h|d|w|mo|y)$ ]]; then
    local n="${BASH_REMATCH[1]}" u="${BASH_REMATCH[2]}"
    case "$u" in
      s) echo $(( n )) ;; m) echo $(( n*60 )) ;; h) echo $(( n*3600 )) ;;
      d) echo $(( n*86400 )) ;; w) echo $(( n*604800 )) ;;
      mo) echo $(( n*2592000 )) ;; y) echo $(( n*31536000 )) ;;
    esac
    return 0
  fi
  return 1
}

users_menu() {
  if ! db_ready; then
    banner; section "Users & Premium"
    warn "Database tidak ditemukan ($DB_FILE) atau sqlite3 belum terpasang."
    info "Fitur ini butuh data user dari website. Kalau hanya pakai chat terminal, abaikan saja."
    pause; return
  fi
  while :; do
    banner; section "Users & Premium"
    local rows i=0
    mapfile -t rows < <(sql "SELECT id || '|' || IFNULL(email,'') || '|' || IFNULL(display_name,'') || '|' || IFNULL(plan,'free') || '|' || IFNULL(plan_expires_at,'') || '|' || IFNULL(tokens_used_today,0) || '|' || IFNULL(tokens_limit_daily,0) FROM users ORDER BY (plan='premium') DESC, tokens_used_today DESC LIMIT 200;" 2>/dev/null)
    if [ "${#rows[@]}" -eq 0 ]; then warn "Belum ada user."; pause; return; fi
    printf "  ${GY}%-3s %-9s %-28s %-18s %s${RESET}\n" "#" "Plan" "Email" "Name" "Tokens"
    hr
    for row in "${rows[@]}"; do
      i=$((i+1))
      IFS='|' read -r _ remail rname rplan _ rused rlim <<<"$row"
      local badge="${GY}free${RESET}"; [ "$rplan" = "premium" ] && badge="${YE}★premium${RESET}"
      printf "  ${WT}%-3s${RESET} %b %-28s %-18s ${DIM}%s/%s${RESET}\n" "$i" "$badge" "${remail:0:28}" "${rname:0:18}" "$rused" "$rlim"
    done
    echo
    local pick; ask "pilih # user (Enter=kembali)" pick ""
    [ -z "$pick" ] && return
    if ! [[ "$pick" =~ ^[0-9]+$ ]] || [ "$pick" -lt 1 ] || [ "$pick" -gt "${#rows[@]}" ]; then err "Nomor tidak valid."; sleep 1; continue; fi
    IFS='|' read -r SEL_ID SEL_EMAIL SEL_NAME _ _ _ _ <<<"${rows[$((pick-1))]}"
    user_actions "$SEL_ID" "$SEL_EMAIL" "$SEL_NAME"
  done
}

user_actions() {
  local uid="$1" email="$2" name="$3"
  while :; do
    banner; section "User terpilih"
    printf "  ${GY}ID   :${RESET} ${WT}%s${RESET}\n  ${GY}Email:${RESET} ${WT}%s${RESET}\n  ${GY}Name :${RESET} ${WT}%s${RESET}\n  ${GY}Plan :${RESET} ${WT}%s${RESET}\n\n" \
      "$uid" "$email" "$name" "$(sql "SELECT IFNULL(plan,'free')||' '||IFNULL(plan_expires_at,'') FROM users WHERE id='$uid';")"
    printf "  ${YE}1${RESET}) Salin User ID (cetak penuh)\n"
    printf "  ${YE}2${RESET}) Grant Premium\n"
    printf "  ${YE}3${RESET}) Extend Premium\n"
    printf "  ${YE}4${RESET}) Revoke Premium\n"
    printf "  ${YE}5${RESET}) Set limit token harian\n"
    printf "  ${YE}6${RESET}) Reset penggunaan token hari ini\n"
    printf "  ${YE}0${RESET}) Kembali\n\n"
    local a; ask "user-action" a ""
    case "$a" in
      1) printf "\n  ${CY}User ID:${RESET}\n  ${WT}%s${RESET}\n" "$uid"; pause ;;
      2) local dur lim secs exp; ask "Durasi (24h/7d/30d/3mo/1y)" dur "30d"; secs="$(parse_duration_to_seconds "$dur")" || { err "Durasi salah."; pause; continue; }
         ask "Limit token/hari" lim "20000000"; [[ "$lim" =~ ^[0-9]+$ ]] || { err "Harus angka."; pause; continue; }
         exp="$(date -u -d "+$secs seconds" '+%Y-%m-%dT%H:%M:%SZ')"
         sql "UPDATE users SET plan='premium', plan_expires_at='$exp', tokens_limit_daily=$lim WHERE id='$uid';"
         ok "Premium aktif sampai $exp (limit $lim/hari)."; pause ;;
      3) local cur dur secs new; cur="$(sql "SELECT IFNULL(plan_expires_at,'') FROM users WHERE id='$uid';")"
         [ -z "$cur" ] && { warn "User belum premium. Pakai Grant."; pause; continue; }
         ask "Tambah durasi (7d/1mo/…)" dur ""; secs="$(parse_duration_to_seconds "$dur")" || { err "Durasi salah."; pause; continue; }
         new="$(date -u -d "$cur + $secs seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
         [ -z "$new" ] && { err "Gagal hitung tanggal."; pause; continue; }
         sql "UPDATE users SET plan_expires_at='$new' WHERE id='$uid';"; ok "Expiry baru: $new"; pause ;;
      4) sql "UPDATE users SET plan='free', plan_expires_at=NULL, tokens_limit_daily=2000000 WHERE id='$uid';"; ok "Premium dicabut → free."; pause ;;
      5) local v; ask "Limit token/hari baru" v ""; [[ "$v" =~ ^[0-9]+$ ]] || { err "Harus angka."; pause; continue; }
         sql "UPDATE users SET tokens_limit_daily=$v WHERE id='$uid';"; ok "Limit → $v/hari."; pause ;;
      6) sql "UPDATE users SET tokens_used_today=0 WHERE id='$uid';"; ok "Token hari ini direset."; pause ;;
      0|"") return ;;
      *) err "Tidak valid."; sleep 1 ;;
    esac
  done
}

# ──────────────────────────────────────────────────────────────────────
# BACKUP / RESTORE
# ──────────────────────────────────────────────────────────────────────
backup_menu() {
  while :; do
    banner; section "Backup & Restore"
    printf "  ${YE}1${RESET}) Export backup (config + database → zip)\n"
    printf "  ${YE}2${RESET}) Import backup (restore dari zip)\n"
    printf "  ${YE}0${RESET}) Kembali\n\n"
    local c; ask "backup" c ""
    case "$c" in
      1) do_backup; pause ;;
      2) do_restore; pause ;;
      0|"") return ;;
      *) err "Tidak valid."; sleep 1 ;;
    esac
  done
}

do_backup() {
  ensure_dep zip zip || { err "zip tidak tersedia."; return; }
  mkdir -p "$BACKUP_DIR"
  local ts work out; ts="$(date +%Y%m%d-%H%M%S)"; work="$(mktemp -d)"; out="$BACKUP_DIR/suzu-backup-$ts.zip"
  cp "$ENV_FILE" "$work/suzu.env" 2>/dev/null || true
  if db_ready; then
    sqlite3 "$DB_FILE" ".backup '$work/suzu.db'" 2>/dev/null || cp "$DB_FILE" "$work/suzu.db" 2>/dev/null || true
  fi
  ( cd "$work" && zip -q -9 "$out" ./* 2>/dev/null )
  rm -rf "$work"
  if [ -f "$out" ]; then ok "Backup tersimpan: $out"; info "Salin file ini ke tempat aman / server baru untuk restore."; else err "Backup gagal."; fi
}

do_restore() {
  ensure_dep unzip unzip || { err "unzip tidak tersedia."; return; }
  local zp; ask "Path file backup .zip" zp ""
  zp="${zp/#\~/$HOME}"
  [ -f "$zp" ] || { err "File tidak ditemukan: $zp"; return; }
  local work; work="$(mktemp -d)"
  unzip -q "$zp" -d "$work" 2>/dev/null || { err "Zip rusak."; rm -rf "$work"; return; }
  warn "Ini akan MENIMPA config" 
  confirm "Lanjut restore?" || { rm -rf "$work"; info "Dibatalkan."; return; }
  [ -f "$work/suzu.env" ] && { cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%s)" 2>/dev/null; cp "$work/suzu.env" "$ENV_FILE"; chmod 600 "$ENV_FILE"; ok "Config dipulihkan."; }
  if [ -f "$work/suzu.db" ]; then
    mkdir -p "$(dirname "$DB_FILE")"
    [ -f "$DB_FILE" ] && cp "$DB_FILE" "$DB_FILE.bak-$(date +%s)" 2>/dev/null
    rm -f "$DB_FILE-wal" "$DB_FILE-shm" 2>/dev/null
    cp "$work/suzu.db" "$DB_FILE"; ok "Database dipulihkan."
    maybe_restart_web
  fi
  rm -rf "$work"
}

# ──────────────────────────────────────────────────────────────────────
# MAIN MENU
# ──────────────────────────────────────────────────────────────────────
main_menu() {
  while :; do
    banner
    status_panel
    printf "\n${BOLD}${WT}  Menu Utama${RESET}\n"
    printf "  ${GR}1${RESET}) ${BOLD}Chat AI${RESET}            ${DIM}— ngobrol dengan AI di terminal${RESET}\n"
    printf "  ${GR}2${RESET}) Konfigurasi        ${DIM}— API key, base URL, model, domain${RESET}\n"
    printf "  ${GR}3${RESET}) Users & Premium    ${DIM}— kelola user, grant premium, limit${RESET}\n"
    printf "  ${GR}4${RESET}) Backup & Restore   ${DIM}— export/import config + database${RESET}\n"
    printf "  ${GR}5${RESET}) Tes koneksi AI     ${DIM}— cek API key berfungsi${RESET}\n"
    printf "  ${GR}0${RESET}) Keluar\n\n"
    local c; ask "menu" c ""
    case "$c" in
      1) chat_loop ;;
      2) config_menu ;;
      3) users_menu ;;
      4) backup_menu ;;
      5) banner; section "Tes Koneksi AI"; test_ai; pause ;;
      0|q|Q|exit) printf "\n  ${CY}Sampai jumpa! 👋${RESET}\n\n"; exit 0 ;;
      *) err "Pilihan tidak valid."; sleep 1 ;;
    esac
  done
}

# ──────────────────────────────────────────────────────────────────────
# Only run the UI when executed directly (allows sourcing for tests).
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  trap 'printf "\n${DIM}  (dibatalkan)${RESET}\n"' INT
  require_runtime
  main_menu
fi
