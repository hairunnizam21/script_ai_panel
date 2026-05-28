# Suzu AI — VPS panel scripts

One-shot installer and an interactive admin TUI for self-hosting
[suzu-ai-web](https://github.com/hairunnizam21/suzu-ai-web) on a fresh
Ubuntu VPS.

## What it sets up

- Node.js 20 + PM2
- Nginx + (optional) Let's Encrypt via certbot
- apktool + JDK 17 + zipalign + apksigner (for APK decompile/recompile)
- The suzu-ai-web app cloned to `/var/www/suzu-ai-web`
- A debug keystore at `server/keystores/debug.keystore`
- A `.env` file with your API key, base URL, default model, domain
- A global `suzu-admin` command to manage the server later

## Install (fresh VPS)

```bash
curl -fsSL https://raw.githubusercontent.com/hairunnizam21/script_ai_panel/main/install.sh | sudo bash
```

You will be prompted for:

- Public domain (e.g. `suzu-ai.online`) — leave blank for IP-only
- AI base URL (default `https://core.fiqstr.com/v1`)
- AI API key
- Default model
- Firebase project ID

When done, visit `https://<your-domain>/`.

### Custom installer flags

You can override these by exporting env vars before running:

```bash
SUZU_REPO_URL=https://github.com/hairunnizam21/suzu-ai-web.git \
SUZU_BRANCH=main \
SUZU_INSTALL_DIR=/var/www/suzu-ai-web \
SUZU_LETSENCRYPT_EMAIL=admin@example.com \
sudo -E bash install.sh
```

## Daily admin

After install, just type:

```bash
suzu-admin
```

The menu lets you:

1. Update domain (+ reissue SSL)
2. Update AI base URL
3. Update AI API key
4. Update default model
5. Set the global daily token limit (applies to all users)
6. Set a per-user daily token limit (for donors)
7. List users
8. Reset today's token usage for a specific user
9. Restart the service (`pm2 restart suzu-ai`)
10. Stream live logs
11. `git pull` + rebuild + restart
12. View current `.env` (API key masked)

The default daily limit is **2,000,000 tokens per user**, reset at UTC
midnight. Per-user limits override the default for a given user only.

## Re-installing on a new VPS

When your VPS expires or you migrate, just spin up a new Ubuntu box, point
your DNS at the new IP, and run the one-liner above. The script is
idempotent — running it again on an existing install just updates the repo.

## Files

- `install.sh` — provisions a clean Ubuntu host
- `suzu-admin.sh` — the admin TUI, symlinked to `/usr/local/bin/suzu-admin`

## License

MIT.
