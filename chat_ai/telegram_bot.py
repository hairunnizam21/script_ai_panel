"""Telegram front-end for the Suzu Chat AI.

Stdlib-only, long-polling Bot API client.  Reuses the same ``chat_ai`` agent
loop and the 31 tools available to the terminal CLI — so anything the user
can do in ``suzu-chat-ai`` they can also do over Telegram: chat, build APK,
decompile / recompile, analyse uploaded files, run reverse-engineering
tools, write Python scripts, and so on.

Highlights
----------
* **One session per Telegram chat.**  State is persisted to
  ``$SUZU_STATE_DIR/sessions`` exactly like the terminal CLI — so a
  conversation survives bot restarts and is shared across the two
  front-ends if you choose to point ``/resume`` at the same id.
* **Allowlist.**  ``TELEGRAM_ALLOWED_USER_IDS`` is a comma-separated list of
  Telegram user ids that may interact with the bot.  Anyone else gets a
  polite "not authorized" reply.
* **File uploads.**  Documents / photos sent to the bot are downloaded into
  the active session's workspace and announced to the model so it can
  immediately reason about them ("here is an APK, please detect framework
  and decompile").
* **Live progress.**  While the agent is thinking / calling tools, the bot
  edits a status message ("Thinking…", "Running apk_decompile…") and
  drives a ``typing`` chat action so the UI stays responsive.

Environment
-----------
* ``TELEGRAM_BOT_TOKEN``       — required, from @BotFather.
* ``TELEGRAM_ALLOWED_USER_IDS``— required, comma-separated ids.
* ``TELEGRAM_BOT_USERNAME``    — optional, used in the welcome banner.
* All ``chat_ai`` env vars (``AI_API_KEY``, ``AI_API_BASE_URL``,
  ``AI_DEFAULT_MODEL``, ``SUZU_STATE_DIR``, …).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .api import APIError, ChatClient
from .config import Config
from .prompts import render_system_prompt
from .runner import run_turn, summarize_tool_output
from .state import Session, list_sessions, latest_session_id
from .tools import ToolContext, build_default_registry

log = logging.getLogger("suzu.telegram")

# Telegram message bodies are capped at 4096 chars.  Leave a small margin for
# the bot-controlled prefix we attach to chunks (" (1/3)" etc.).
MAX_MSG_CHARS = 3900

# How often the typing indicator must be refreshed.  Telegram clears it after
# ~5 s of silence, so we ping it every 4 s while the agent is busy.
TYPING_REFRESH_S = 4.0

# Maximum file size we attempt to download from a user upload (Telegram Bot API
# caps this at 20 MB for ``getFile`` and 50 MB for uploads — we err on the
# safe side and let the model deal with anything over the limit).
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

# Default poll timeout sent to ``getUpdates``.  Telegram supports up to 50 s.
POLL_TIMEOUT_S = 30


# --------------------------------------------------------------------------- #
# Telegram Bot API client (stdlib only)
# --------------------------------------------------------------------------- #


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    """Tiny ``urllib``-based Bot API wrapper.

    Only the methods we actually use are implemented; everything else can be
    reached via :meth:`call`.
    """

    def __init__(self, token: str, *, timeout: float = 35.0) -> None:
        if not token:
            raise TelegramError("empty bot token")
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    # -- low level ---------------------------------------------------------- #

    def call(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, tuple[str, bytes, str]]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Invoke ``method`` and return the ``result`` field of the reply."""
        url = f"{self.base}/{method}"
        if files:
            body, content_type = _build_multipart(params or {}, files)
            headers = {"Content-Type": content_type, "User-Agent": "suzu-telegram-bot/0.1"}
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        else:
            data = json.dumps(params or {}).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "suzu-telegram-bot/0.1",
            }
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise TelegramError(f"{method} HTTP {e.code}: {body[:400]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TelegramError(f"{method} network error: {e!r}") from e
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TelegramError(f"{method} bad JSON: {e}; body={raw[:200]}") from e
        if not parsed.get("ok"):
            raise TelegramError(f"{method} not ok: {parsed.get('description')!r}")
        return parsed.get("result")

    # -- helpers ------------------------------------------------------------ #

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "edited_message"],
            },
            timeout=timeout + 10,
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("sendMessage", params)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            return self.call("editMessageText", params)
        except TelegramError as e:
            # Telegram returns 400 if the text is identical — that's fine.
            if "message is not modified" in str(e).lower():
                return None
            raise

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError as e:
            log.debug("sendChatAction failed: %s", e)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
        url = f"{self.file_base}/{file_path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "suzu-telegram-bot/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(dest, "wb") as f:
            written = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise TelegramError(
                        f"file exceeds {max_bytes} bytes; refusing to keep downloading"
                    )
                f.write(chunk)
            return written

    def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: Optional[str] = None,
        reply_to: Optional[int] = None,
    ) -> dict[str, Any]:
        mime, _ = mimetypes.guess_type(path.name)
        mime = mime or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption[:1024]
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        return self.call(
            "sendDocument",
            params,
            files={"document": (path.name, data, mime)},
        )


def _build_multipart(
    fields: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Encode a multipart/form-data body."""
    boundary = "----SuzuBot" + uuid.uuid4().hex
    sep = f"--{boundary}\r\n".encode()
    end = f"--{boundary}--\r\n".encode()
    body: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        body.append(sep)
        body.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.append(_field_value_bytes(value) + b"\r\n")
    for name, (filename, content, mime) in files.items():
        body.append(sep)
        body.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        body.append(f"Content-Type: {mime}\r\n\r\n".encode())
        body.append(content + b"\r\n")
    body.append(end)
    return b"".join(body), f"multipart/form-data; boundary={boundary}"


def _field_value_bytes(value: Any) -> bytes:
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, (int, float)):
        return str(value).encode("utf-8")
    if isinstance(value, (dict, list)):
        return json.dumps(value).encode("utf-8")
    return str(value).encode("utf-8")


# --------------------------------------------------------------------------- #
# Per-chat session map
# --------------------------------------------------------------------------- #


@dataclass
class ChatBinding:
    """Persistent ``chat_id`` → ``session_id`` mapping, plus per-chat config."""

    sessions_dir: Path
    workspaces_dir: Path
    path: Path
    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, state_dir: Path, sessions_dir: Path, workspaces_dir: Path) -> "ChatBinding":
        path = state_dir / "telegram" / "chats.json"
        data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        return cls(sessions_dir, workspaces_dir, path, data)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def session_for(self, chat_id: int, model: str) -> Session:
        key = str(chat_id)
        sid = self.data.get(key, {}).get("session_id")
        if sid:
            try:
                return Session.load(self.sessions_dir, sid)
            except (OSError, json.JSONDecodeError):
                log.warning("stale session %s for chat %s; creating a fresh one", sid, key)
        s = Session.new(self.sessions_dir, self.workspaces_dir, model=model)
        s.title = f"Telegram chat {key}"
        s.save(self.sessions_dir)
        self.data[key] = {"session_id": s.id, "created_at": time.time()}
        self._save()
        return s

    def rebind(self, chat_id: int, session: Session) -> None:
        self.data[str(chat_id)] = {"session_id": session.id, "created_at": time.time()}
        self._save()


# --------------------------------------------------------------------------- #
# Typing indicator helper
# --------------------------------------------------------------------------- #


class TypingPing:
    """Refreshes the ``typing`` chat-action every few seconds in the background."""

    def __init__(self, api: TelegramAPI, chat_id: int, action: str = "typing"):
        self.api = api
        self.chat_id = chat_id
        self.action = action
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "TypingPing":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.api.send_chat_action(self.chat_id, self.action)
            self._stop.wait(TYPING_REFRESH_S)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #


@dataclass
class BotConfig:
    bot_token: str
    allowed_user_ids: set[int]
    bot_username: str = ""

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN is empty — set it in /etc/suzu-panel/.env (or the "
                "session env file) and restart the bot."
            )
        raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        ids: set[int] = set()
        for tok in raw_ids.replace(";", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                ids.add(int(tok))
            except ValueError:
                log.warning("ignoring non-numeric allowed user id: %r", tok)
        if not ids:
            raise TelegramError(
                "TELEGRAM_ALLOWED_USER_IDS is empty — the bot would be open to "
                "the world.  Set it to a comma-separated list of Telegram user "
                "ids (e.g. '12345,67890') and restart."
            )
        username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
        return cls(bot_token=token, allowed_user_ids=ids, bot_username=username)


class Bot:
    def __init__(self, bot_cfg: BotConfig, cfg: Config):
        self.bot_cfg = bot_cfg
        self.cfg = cfg
        self.api = TelegramAPI(bot_cfg.bot_token)
        self.registry = build_default_registry()
        self.binding = ChatBinding.load(cfg.state_dir, cfg.sessions_dir, cfg.workspaces_dir)
        self.client = ChatClient(cfg.api_base_url, cfg.api_key, timeout=cfg.request_timeout)
        me = self.api.get_me()
        log.info(
            "Suzu Telegram bot ready: @%s (id=%s) allowed=%s",
            me.get("username"),
            me.get("id"),
            sorted(bot_cfg.allowed_user_ids),
        )
        if not bot_cfg.bot_username and me.get("username"):
            self.bot_cfg.bot_username = me["username"]

    # -- main loop ---------------------------------------------------------- #

    def run(self) -> None:
        offset = 0
        backoff = 1.0
        while True:
            try:
                updates = self.api.get_updates(offset=offset, timeout=POLL_TIMEOUT_S)
                backoff = 1.0
            except TelegramError as e:
                log.error("getUpdates failed: %s (sleeping %.1fs)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            for upd in updates:
                offset = max(offset, upd.get("update_id", 0) + 1)
                try:
                    self._dispatch(upd)
                except Exception:  # noqa: BLE001 — never let one update kill the loop
                    log.exception("error handling update %s", upd.get("update_id"))

    # -- dispatch ----------------------------------------------------------- #

    def _dispatch(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        sender = msg.get("from") or {}
        uid = sender.get("id")
        if not self._is_allowed(uid):
            log.info("rejecting message from user %s (%s)", uid, sender.get("username"))
            try:
                self.api.send_message(
                    msg["chat"]["id"],
                    "Maaf, awak tidak dibenarkan menggunakan bot ini. "
                    f"Telegram id awak: {uid}.\n\nKalau awak rasa ini patut "
                    "dibenarkan, hubungi pentadbir bot.",
                )
            except TelegramError as e:
                log.debug("reject reply failed: %s", e)
            return
        self._handle_message(msg)

    def _is_allowed(self, uid: Optional[int]) -> bool:
        return uid is not None and uid in self.bot_cfg.allowed_user_ids

    # -- message types ------------------------------------------------------ #

    def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text.startswith("/"):
            if self._handle_command(msg, text):
                return

        # Make sure we have a session before downloading anything, so files
        # land inside the workspace.
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self._ensure_system_prompt(session)

        downloaded: list[Path] = []
        if "document" in msg:
            downloaded += self._download_document(session, msg["document"])
        if "photo" in msg:
            downloaded += self._download_photo(session, msg["photo"])
        if "video" in msg:
            downloaded += self._download_document(session, msg["video"])
        if "audio" in msg:
            downloaded += self._download_document(session, msg["audio"])
        if "voice" in msg:
            downloaded += self._download_document(session, msg["voice"])
        if "animation" in msg:
            downloaded += self._download_document(session, msg["animation"])

        if downloaded:
            note_lines = [
                f"User uploaded file: {p} (size {p.stat().st_size} bytes)"
                for p in downloaded
            ]
            ack = "\n".join(
                f"📥 saved: `{p.name}` → `{p}` ({_human_size(p.stat().st_size)})"
                for p in downloaded
            )
            self.api.send_message(chat_id, ack, parse_mode="Markdown")
            text = (text + "\n\n" + "\n".join(note_lines)).strip() if text else "\n".join(note_lines)

        if not text:
            return

        self._route_to_agent(chat_id, session, text, reply_to=msg.get("message_id"))

    # -- commands ----------------------------------------------------------- #

    def _handle_command(self, msg: dict[str, Any], text: str) -> bool:
        chat_id = msg["chat"]["id"]
        head, _, rest = text.partition(" ")
        head = head.split("@", 1)[0].lower()  # strip "@botname"
        rest = rest.strip()

        if head == "/start":
            self._cmd_start(chat_id)
            return True
        if head == "/help":
            self._cmd_help(chat_id)
            return True
        if head == "/status":
            self._cmd_status(chat_id)
            return True
        if head == "/new":
            self._cmd_new(chat_id)
            return True
        if head in ("/sessions", "/projects"):
            self._cmd_sessions(chat_id)
            return True
        if head == "/workspace":
            self._cmd_workspace(chat_id)
            return True
        if head == "/clear":
            self._cmd_clear(chat_id)
            return True
        if head == "/model":
            self._cmd_model(chat_id, rest)
            return True
        if head == "/whoami":
            sender = msg.get("from") or {}
            self.api.send_message(
                chat_id,
                f"id: `{sender.get('id')}`\nusername: @{sender.get('username','')}\n"
                f"name: {sender.get('first_name','')} {sender.get('last_name','')}".strip(),
                parse_mode="Markdown",
            )
            return True
        # Unknown slash command — let the model see it as plain text.
        return False

    def _cmd_start(self, chat_id: int) -> None:
        uname = self.bot_cfg.bot_username or "bot"
        text = (
            f"👋 *Selamat datang ke Suzu Chat AI* (@{uname})\n\n"
            "Saya pakar di dalam APK reverse engineering — boleh build, "
            "decompile, recompile, sign, analyse file, dan banyak lagi.\n\n"
            "Hantar mesej biasa untuk berborak, atau forward APK/file untuk "
            "saya analyse terus.\n\n"
            "_Slash commands:_\n"
            "  /help — list semua command\n"
            "  /status — status tools di server\n"
            "  /new — start session baru (history kosong)\n"
            "  /sessions — list session\n"
            "  /workspace — print path workspace\n"
            "  /model NAME — tukar model\n"
            "  /clear — kosongkan history (kekal system prompt)\n"
            "  /whoami — Telegram id awak"
        )
        self.api.send_message(chat_id, text, parse_mode="Markdown")

    def _cmd_help(self, chat_id: int) -> None:
        self._cmd_start(chat_id)

    def _cmd_status(self, chat_id: int) -> None:
        tools = [
            "apktool", "zipalign", "apksigner", "aapt2",
            "jadx", "d2j-dex2jar", "smali", "baksmali",
            "file", "strings", "python3", "java",
        ]
        lines = ["*Tools available di server:*", ""]
        for t in tools:
            p = shutil.which(t)
            mark = "✅" if p else "❌"
            lines.append(f"{mark} `{t}` → `{p or 'MISSING'}`")
        lines.append("")
        lines.append(f"Model     : `{self.cfg.default_model}`")
        lines.append(f"API base  : `{self.cfg.api_base_url}`")
        lines.append(f"Workspace : `{self.cfg.workspaces_dir}`")
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_new(self, chat_id: int) -> None:
        session = Session.new(
            self.cfg.sessions_dir,
            self.cfg.workspaces_dir,
            model=self.cfg.default_model,
        )
        session.title = f"Telegram chat {chat_id}"
        session.save(self.cfg.sessions_dir)
        self.binding.rebind(chat_id, session)
        self._ensure_system_prompt(session)
        self.api.send_message(
            chat_id,
            f"🆕 Session baru: `{session.id}`\nWorkspace: `{session.workspace}`",
            parse_mode="Markdown",
        )

    def _cmd_sessions(self, chat_id: int) -> None:
        items = list_sessions(self.cfg.sessions_dir)
        if not items:
            self.api.send_message(chat_id, "(no sessions yet)")
            return
        # Highlight the one currently bound to this chat.
        bound = self.binding.data.get(str(chat_id), {}).get("session_id")
        lines = ["*Sessions (latest 15):*", ""]
        for s in items[:15]:
            star = "⭐" if s["id"] == bound else "  "
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
            lines.append(
                f"{star} `{s['id']}` · {when} · msgs={s['messages']} · {s.get('title','')}"
            )
        self.api.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

    def _cmd_workspace(self, chat_id: int) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        self.api.send_message(
            chat_id,
            f"`{session.workspace}`",
            parse_mode="Markdown",
        )

    def _cmd_clear(self, chat_id: int) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        sys_msgs = [m for m in session.messages if m.get("role") == "system"]
        session.messages = sys_msgs
        session.save(self.cfg.sessions_dir)
        self.api.send_message(chat_id, "🧹 History dikosongkan.")

    def _cmd_model(self, chat_id: int, rest: str) -> None:
        session = self.binding.session_for(chat_id, model=self.cfg.default_model)
        if not rest:
            self.api.send_message(chat_id, f"current model: `{session.model}`", parse_mode="Markdown")
            return
        session.model = rest.split()[0]
        session.save(self.cfg.sessions_dir)
        self.api.send_message(chat_id, f"✅ model: `{session.model}`", parse_mode="Markdown")

    # -- session helpers --------------------------------------------------- #

    def _ensure_system_prompt(self, session: Session) -> None:
        if not any(m.get("role") == "system" for m in session.messages):
            session.messages.insert(
                0,
                {"role": "system", "content": render_system_prompt(session.workspace, session.model)},
            )
            session.save(self.cfg.sessions_dir)

    # -- file downloads ---------------------------------------------------- #

    def _download_document(self, session: Session, doc: dict[str, Any]) -> list[Path]:
        try:
            info = self.api.get_file(doc["file_id"])
        except TelegramError as e:
            log.warning("getFile failed for %s: %s", doc.get("file_id"), e)
            return []
        remote = info.get("file_path") or ""
        if not remote:
            return []
        size = info.get("file_size") or doc.get("file_size") or 0
        if size and size > MAX_DOWNLOAD_BYTES:
            return []
        name = doc.get("file_name") or Path(remote).name or f"file_{int(time.time())}"
        dest = Path(session.workspace) / "uploads" / _safe_filename(name)
        try:
            self.api.download_file(remote, dest)
        except TelegramError as e:
            log.warning("download failed for %s: %s", remote, e)
            return []
        log.info("downloaded %s -> %s", remote, dest)
        return [dest]

    def _download_photo(self, session: Session, photo_sizes: list[dict[str, Any]]) -> list[Path]:
        if not photo_sizes:
            return []
        # Pick the largest variant.
        largest = max(photo_sizes, key=lambda p: p.get("file_size") or (p.get("width", 0) * p.get("height", 0)))
        try:
            info = self.api.get_file(largest["file_id"])
        except TelegramError as e:
            log.warning("getFile photo failed: %s", e)
            return []
        remote = info.get("file_path") or ""
        if not remote:
            return []
        name = Path(remote).name or f"photo_{int(time.time())}.jpg"
        dest = Path(session.workspace) / "uploads" / _safe_filename(name)
        try:
            self.api.download_file(remote, dest)
        except TelegramError as e:
            log.warning("download photo failed: %s", e)
            return []
        return [dest]

    # -- agent routing ----------------------------------------------------- #

    def _route_to_agent(
        self,
        chat_id: int,
        session: Session,
        text: str,
        *,
        reply_to: Optional[int] = None,
    ) -> None:
        # Initial status message we'll keep editing.
        try:
            status_msg = self.api.send_message(
                chat_id, "💭 Thinking…", reply_to=reply_to
            )
        except TelegramError as e:
            log.warning("could not send status message: %s", e)
            status_msg = None
        status_msg_id: Optional[int] = (status_msg or {}).get("message_id")

        # Throttled status editor — Telegram rate-limits edits.
        last_edit_ts = [0.0]

        def edit_status(label: str) -> None:
            if status_msg_id is None:
                return
            now = time.time()
            if now - last_edit_ts[0] < 0.6:
                return
            last_edit_ts[0] = now
            try:
                self.api.edit_message_text(chat_id, status_msg_id, label)
            except TelegramError as e:
                log.debug("edit status failed: %s", e)

        # Snapshot files in workspace before the turn so we can detect new ones.
        pre_files = _snapshot_files(Path(session.workspace))

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            if kind == "thinking":
                edit_status("💭 Thinking…")
            elif kind == "tool_start":
                name = payload.get("name", "tool")
                edit_status(f"🔧 Running `{name}`…")
            elif kind == "tool_end":
                name = payload.get("name", "tool")
                summary = summarize_tool_output(name, payload.get("output", ""))
                edit_status(f"↪️ `{name}` → {summary[:100]}")
            elif kind == "error":
                edit_status(f"❗ {payload.get('error','error')[:200]}")

        ctx = ToolContext(workspace=Path(session.workspace), debug=self.cfg.debug)
        with TypingPing(self.api, chat_id):
            try:
                final_text = run_turn(
                    self.client,
                    self.registry,
                    ctx,
                    self.cfg,
                    session,
                    user_message=text,
                    on_event=on_event,
                )
            except APIError as e:
                final_text = f"API error: {e}"
            except Exception as e:  # noqa: BLE001
                log.exception("agent crashed")
                final_text = f"Internal error: {e}"

        # Replace the status message with the first chunk of the answer.
        chunks = list(_chunk_message(final_text or "(empty response)"))
        first = chunks[0]
        try:
            if status_msg_id is not None:
                self.api.edit_message_text(chat_id, status_msg_id, first)
            else:
                self.api.send_message(chat_id, first)
        except TelegramError as e:
            log.warning("final edit failed (%s) — sending as new message", e)
            self.api.send_message(chat_id, first)
        for extra in chunks[1:]:
            try:
                self.api.send_message(chat_id, extra)
            except TelegramError as e:
                log.warning("send chunk failed: %s", e)

        # Detect new files produced in the workspace and upload them back.
        post_files = _snapshot_files(Path(session.workspace))
        new_files = _interesting_new_files(pre_files, post_files)
        for fp in new_files[:5]:  # cap to avoid flooding
            try:
                self.api.send_document(
                    chat_id,
                    fp,
                    caption=f"📦 {fp.relative_to(Path(session.workspace))} ({_human_size(fp.stat().st_size)})",
                )
            except TelegramError as e:
                log.warning("send_document failed for %s: %s", fp, e)


# --------------------------------------------------------------------------- #
# Workspace helpers
# --------------------------------------------------------------------------- #


_UPLOADABLE_EXTS = {
    ".apk", ".aab", ".jar", ".dex", ".so", ".aar",
    ".zip", ".tar", ".gz", ".tgz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".txt", ".log", ".json", ".xml", ".smali", ".java", ".kt",
    ".pdf",
}


def _snapshot_files(root: Path) -> dict[Path, float]:
    """Return ``{path: mtime}`` for all files under ``root``."""
    snap: dict[Path, float] = {}
    if not root.exists():
        return snap
    for p in root.rglob("*"):
        if p.is_file():
            try:
                snap[p] = p.stat().st_mtime
            except OSError:
                pass
    return snap


def _interesting_new_files(pre: dict[Path, float], post: dict[Path, float]) -> list[Path]:
    """Return files that are new (or significantly changed) and likely useful to upload."""
    new: list[Path] = []
    for p, mtime in post.items():
        if p in pre and pre[p] >= mtime:
            continue
        # Skip uploads/* — that's where user uploads land; we don't want to
        # echo them back.
        try:
            rel = p.relative_to(p.anchor)
            if "uploads" in rel.parts:
                continue
        except ValueError:
            pass
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in _UPLOADABLE_EXTS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > 45 * 1024 * 1024:  # Telegram outbound limit ~50 MB
            continue
        new.append(p)
    # Newest first.
    new.sort(key=lambda x: post.get(x, 0.0), reverse=True)
    return new


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("..", "_")
    return name[:200] or f"file_{int(time.time())}"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------- #
# Message chunking
# --------------------------------------------------------------------------- #


def _chunk_message(text: str, *, limit: int = MAX_MSG_CHARS) -> Iterable[str]:
    text = text or ""
    if len(text) <= limit:
        yield text
        return
    # Try to split on paragraph then line then word boundaries.
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        piece, remaining = remaining[:cut].rstrip(), remaining[cut:].lstrip()
        if piece:
            yield piece


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SUZU_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        bot_cfg = BotConfig.from_env()
        cfg = Config.load()
    except TelegramError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not cfg.api_key:
        print(
            "AI_API_KEY is empty.  Set it in the env file pointed at by "
            "SUZU_ENV_FILE before running the bot.",
            file=sys.stderr,
        )
        return 2
    try:
        bot = Bot(bot_cfg, cfg)
    except TelegramError as e:
        print(f"failed to initialise Telegram client: {e}", file=sys.stderr)
        return 3
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("interrupted — shutting down")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
