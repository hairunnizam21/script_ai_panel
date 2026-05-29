# Suzu AI — Terminal Panel

Chat with AI **and** manage everything from your server terminal — no website
needed. A single self-contained Bash TUI with a modern, Kali-style look.

![menu](https://img.shields.io/badge/UI-Kali%20style-00c8ff) ![shell](https://img.shields.io/badge/bash-TUI-5fff87)

## What it does

- **Chat AI in the terminal** — streaming replies, switch models on the fly,
  internal `<thinking>` blocks hidden automatically.
- **Config** — update API key, base URL, default model, domain. Changes apply
  **instantly** (the panel reads the config live on every request — no restart).
- **Favorite models** — save the models you use most for one-key selection.
- **Users & Premium** — list users, grant/extend/revoke Premium, set daily token
  limits, reset usage. (Optional — only if a SQLite user DB is present.)
- **Backup & Restore** — export config + database to a zip, restore on any box.

It needs only `curl` and `jq` (plus `sqlite3` for the Users features, `zip`/`unzip`
for backups). The installer pulls these in for you.

## Install on a fresh server

As root on a fresh Ubuntu/Debian box:

```bash
curl -fsSL https://raw.githubusercontent.com/hairunnizam21/script_ai_panel/setup/install.sh | sudo bash
```

You will be asked for:

- **AI base URL** (default `https://core.fiqstr.com/v1`)
- **AI API key**
- **Default model** (default `fiqstr/claude-sonnet-4.6-thinking-agentic`)

When it finishes, just type:

```bash
suzu-admin
```

Pick **1) Chat AI** and start talking to the AI. That's it — the server is now
your AI.

## Daily use

```bash
suzu-admin
```

```
  Menu Utama
  1) Chat AI            — ngobrol dengan AI di terminal
  2) Konfigurasi        — API key, base URL, model, domain
  3) Users & Premium    — kelola user, grant premium, limit
  4) Backup & Restore   — export/import config + database
  5) Tes koneksi AI     — cek API key berfungsi
  0) Keluar
```

Inside **Chat AI**:

| Command  | Action                                   |
|----------|------------------------------------------|
| `/model` | Pick a model (favorites / live list)     |
| `/new`   | Start a fresh conversation               |
| `/exit`  | Back to the menu                         |

### Updating your API key / model

Every day you can rotate the key without breaking anything:

1. `suzu-admin` → **2) Konfigurasi** → **1) Update API Key**
2. The panel saves it and immediately runs a connection test.

Because chat reads the config live, the **next message already uses the new key**
— no service to restart.

### Why "rate limit" happens

Rate limit = the AI provider temporarily refuses requests when too many come in
too fast (HTTP 429/500). It does **not** mean your key is broken. To reduce it:

- Save several keys and rotate when one is limited.
- Use a lighter model for everyday chat (`/model`).

## Moving to a new server

When your VPS expires:

1. On the **old** box: `suzu-admin` → **4) Backup & Restore** → **Export** →
   copy the resulting `*.zip` somewhere safe.
2. On the **new** box: run the install one-liner above.
3. `suzu-admin` → **4) Backup & Restore** → **Import** → point at your zip.

Your config (and users, if any) come right back.

## Configuration file

All settings live in a single env file (auto-detected):

- Standalone install: `/etc/suzu-ai/suzu.env`
- Legacy `suzu-ai-web` install: `/var/www/suzu-ai-web/.env`

Override the location with `SUZU_ENV_FILE=/path/to/file suzu-admin`.

| Key                | Meaning                                  |
|--------------------|------------------------------------------|
| `AI_API_BASE_URL`  | OpenAI-compatible base URL               |
| `AI_API_KEY`       | Your API key                             |
| `AI_DEFAULT_MODEL` | Default chat model                       |
| `SUZU_MODELS`      | Comma-separated favorite models          |
| `SUZU_DOMAIN`      | Optional domain (informational)          |

## Files

- `install.sh` — provisions a server and installs the `suzu-admin` command
- `suzu-admin.sh` — the terminal panel (chat + admin), symlinked to `suzu-admin`

## License

MIT.
