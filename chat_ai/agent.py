"""Main interactive agent loop for the Suzu Chat AI.

This is the program launched by ``python3 -m chat_ai``.  It handles:

* Session creation / resume / listing
* The slash commands documented in ``chat_ai.prompts``
* The streaming chat-completion + tool-call loop with progress animations
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

from . import animations as anim
from .animations import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    banner_box,
    style,
    supports_color,
)
from .api import APIError, ChatClient, ChatRequest, DeltaAccumulator
from .config import Config
from .prompts import render_system_prompt
from .state import Session, delete_session, latest_session_id, list_sessions
from .tools import ToolContext, build_default_registry


EXIT_TO_MENU = 10
EXIT_QUIT = 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="suzu-chat-ai")
    parser.add_argument("--new", action="store_true", help="Start a brand-new session.")
    parser.add_argument("--resume", metavar="SID", help="Resume a specific session id.")
    parser.add_argument("--last", action="store_true", help="Resume the most recently used session.")
    parser.add_argument("--list", action="store_true", help="List sessions and exit.")
    parser.add_argument("--delete", metavar="SID", help="Delete a session and exit.")
    parser.add_argument("--model", help="Override the model for this run.")
    parser.add_argument("--once", metavar="PROMPT", help="Send a single prompt and exit (non-interactive).")
    args = parser.parse_args(argv)

    cfg = Config.load()

    if args.list:
        return _cmd_list(cfg)
    if args.delete:
        ok = delete_session(cfg.sessions_dir, args.delete, cfg.workspaces_dir)
        print(("deleted " if ok else "not found ") + args.delete)
        return 0 if ok else 1

    if not cfg.api_key:
        sys.stderr.write(style(
            "AI_API_KEY is empty. Set it in /var/www/suzu-ai-web/.env (option 3 in suzu-admin),\n"
            "or export AI_API_KEY=... before running.\n",
            RED,
        ))
        return 2

    model = args.model or cfg.default_model
    session = _pick_session(cfg, args, model)
    if session is None:
        return 1

    client = ChatClient(cfg.api_base_url, cfg.api_key, timeout=cfg.request_timeout)
    registry = build_default_registry()
    ctx = ToolContext(workspace=session.workspace, debug=cfg.debug)

    _print_banner(session, cfg, model)

    if not session.messages:
        session.messages.append(
            {"role": "system", "content": render_system_prompt(session.workspace, model)}
        )
        session.save(cfg.sessions_dir)

    # One-shot mode (used by tests and for scripting).
    if args.once:
        return _one_shot(client, registry, ctx, cfg, session, args.once)

    return _interactive(client, registry, ctx, cfg, session)


# --------------------------------------------------------------------------- #
# Session selection
# --------------------------------------------------------------------------- #


def _pick_session(cfg: Config, args, model: str) -> Session | None:
    if args.new:
        s = Session.new(cfg.sessions_dir, cfg.workspaces_dir, model=model)
        s.save(cfg.sessions_dir)
        return s
    if args.resume:
        try:
            return Session.load(cfg.sessions_dir, args.resume)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(style(f"Cannot load session {args.resume}: {e}\n", RED))
            return None
    if args.last:
        sid = latest_session_id(cfg.sessions_dir)
        if sid:
            return Session.load(cfg.sessions_dir, sid)
    # Default: resume the most recent if it's < 7 days old, otherwise new.
    sid = latest_session_id(cfg.sessions_dir)
    if sid:
        try:
            existing = Session.load(cfg.sessions_dir, sid)
            if time.time() - existing.updated_at < 7 * 24 * 3600:
                return existing
        except (OSError, json.JSONDecodeError):
            pass
    s = Session.new(cfg.sessions_dir, cfg.workspaces_dir, model=model)
    s.save(cfg.sessions_dir)
    return s


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #


def _print_banner(session: Session, cfg: Config, model: str) -> None:
    print(banner_box("Suzu Chat AI", f"APK reverse-engineering & build assistant"))
    print(style(f"  Session : {session.id}", DIM))
    print(style(f"  Workspace: {session.workspace}", DIM))
    print(style(f"  Model   : {model}", DIM))
    print(style(f"  Base URL: {cfg.api_base_url}", DIM))
    print(style("  Tip     : /help for slash commands, /menu to return to admin panel.", DIM))
    print()


def _print_assistant_prefix() -> None:
    sys.stdout.write(style("Suzu", BOLD + CYAN) + style(" › ", DIM))
    sys.stdout.flush()


def _print_user_prompt() -> str:
    sys.stdout.write(style("you", BOLD + GREEN) + style(" › ", DIM))
    sys.stdout.flush()
    try:
        return input()
    except EOFError:
        return "/exit"


def _cmd_list(cfg: Config) -> int:
    sessions = list_sessions(cfg.sessions_dir)
    if not sessions:
        print("(no sessions yet)")
        return 0
    print(f"{'ID':32}  {'Updated':19}  {'Msgs':>5}  Title")
    for s in sessions[:50]:
        print(
            "{id:32}  {when:19}  {msgs:>5}  {title}".format(
                id=s["id"],
                when=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["updated_at"])),
                msgs=s["messages"],
                title=s.get("title", ""),
            )
        )
    return 0


# --------------------------------------------------------------------------- #
# Interactive loop
# --------------------------------------------------------------------------- #


def _interactive(client: ChatClient, registry, ctx: ToolContext, cfg: Config, session: Session) -> int:
    while True:
        try:
            user = _print_user_prompt()
        except KeyboardInterrupt:
            print()
            continue

        user = user.strip()
        if not user:
            continue

        if user.startswith("/"):
            action = _handle_slash(user, cfg, session)
            if action == "exit":
                print(style("Bye.", DIM))
                return EXIT_QUIT
            if action == "menu":
                print(style("Returning to admin menu…", DIM))
                return EXIT_TO_MENU
            if action == "reload":
                session = Session.load(cfg.sessions_dir, session.id)
                ctx = ToolContext(workspace=session.workspace, debug=cfg.debug)
            continue

        session.messages.append({"role": "user", "content": user})
        session.save(cfg.sessions_dir)

        try:
            _agent_turn(client, registry, ctx, cfg, session)
        except APIError as e:
            print(style(f"\n[API error] {e}", RED))
        except KeyboardInterrupt:
            print(style("\n(interrupted — partial reply discarded)", YELLOW))
            # Drop the last user message so a Ctrl-C doesn't leave the history
            # in an awkward "user with no answer" state.
            if session.messages and session.messages[-1].get("role") == "user":
                pass  # keep — the user can ask again or use /undo
        session.save(cfg.sessions_dir)


def _handle_slash(cmd: str, cfg: Config, session: Session) -> str | None:
    parts = cmd.split()
    head = parts[0].lower()
    rest = parts[1:]
    if head in ("/exit", "/quit", "/bye"):
        return "exit"
    if head == "/menu":
        return "menu"
    if head == "/help":
        _print_help()
        return None
    if head == "/clear":
        # Keep the system message, drop the rest.
        sys_msgs = [m for m in session.messages if m.get("role") == "system"]
        session.messages = sys_msgs
        session.save(cfg.sessions_dir)
        print(style("(history cleared)", DIM))
        return None
    if head == "/model":
        if not rest:
            print(f"current model: {session.model}")
            return None
        session.model = rest[0]
        session.save(cfg.sessions_dir)
        print(style(f"(model set to {session.model})", DIM))
        return None
    if head in ("/projects", "/sessions"):
        items = list_sessions(cfg.sessions_dir)
        if not items:
            print("(no sessions)")
            return None
        for s in items[:20]:
            marker = "*" if s["id"] == session.id else " "
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
            print(f" {marker} {s['id']:32}  {when}  msgs={s['messages']:<4}  {s.get('title','')}")
        return None
    if head == "/new":
        s = Session.new(cfg.sessions_dir, cfg.workspaces_dir, model=session.model)
        s.save(cfg.sessions_dir)
        print(style(f"(new session {s.id} created — use /resume to switch)", DIM))
        return None
    if head == "/resume":
        if not rest:
            sid = latest_session_id(cfg.sessions_dir)
        else:
            sid = rest[0]
        if not sid:
            print(style("no session to resume", YELLOW))
            return None
        try:
            session2 = Session.load(cfg.sessions_dir, sid)
        except (OSError, json.JSONDecodeError) as e:
            print(style(f"cannot load: {e}", RED))
            return None
        # Mutate session in place so the loop sees the change.
        session.__dict__.update(session2.__dict__)
        print(style(f"(resumed {session.id} — {len(session.messages)} messages)", DIM))
        return None
    if head == "/workspace":
        print(session.workspace)
        return None
    if head == "/title":
        new = " ".join(rest).strip()
        if new:
            session.title = new
            session.save(cfg.sessions_dir)
            print(style(f"(title: {new})", DIM))
        else:
            print(session.title)
        return None
    if head == "/cost":
        msgs = len(session.messages)
        chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in session.messages)
        print(f"messages={msgs}  approx_chars={chars}")
        return None
    print(style(f"unknown command: {head}. Try /help.", YELLOW))
    return None


def _print_help() -> None:
    text = """
    Slash commands:
      /menu                    Exit chat and return to the admin TUI
      /exit | /quit            Quit the chat session entirely
      /clear                   Clear conversation history (keep system prompt)
      /model <name>            Switch model for this session
      /projects                List all sessions
      /new                     Create a new blank session (does not switch)
      /resume [SID]            Resume a session (latest if SID omitted)
      /workspace               Print the workspace path of this session
      /title <text>            Rename the current session
      /cost                    Quick history size summary
      /help                    Show this help
    """
    print(textwrap.dedent(text).strip())


# --------------------------------------------------------------------------- #
# One agent turn
# --------------------------------------------------------------------------- #


def _agent_turn(client: ChatClient, registry, ctx: ToolContext, cfg: Config, session: Session) -> None:
    tools = registry.schemas()
    for iteration in range(cfg.max_tool_iters):
        req = ChatRequest(
            model=session.model,
            messages=session.messages,
            tools=tools,
            stream=True,
        )
        spinner = anim.Spinner(label="Thinking", color=MAGENTA)
        spinner.start()
        accum = DeltaAccumulator()
        printed_prefix = False
        try:
            for event in client.stream(req):
                added = accum.push(event)
                if added:
                    if not printed_prefix:
                        spinner.stop()
                        _print_assistant_prefix()
                        printed_prefix = True
                    sys.stdout.write(added)
                    sys.stdout.flush()
                # If we're streaming tool calls, update the spinner label.
                if accum.tool_calls and not printed_prefix:
                    names = ",".join(
                        tc.get("function", {}).get("name", "") or "" for tc in accum.tool_calls
                    )
                    spinner.set_label(f"Calling {names}", color=CYAN)
        finally:
            spinner.stop()

        if printed_prefix:
            sys.stdout.write("\n")
            sys.stdout.flush()

        assistant_msg = accum.to_message()
        # Ensure ``content`` is at least an empty string when there were tool calls
        # (some providers reject ``null``).
        if assistant_msg.get("content") is None:
            assistant_msg["content"] = ""
        session.messages.append(assistant_msg)
        session.save(cfg.sessions_dir)

        if not accum.tool_calls:
            return

        # Execute each tool call and append a tool response message.
        for tc in accum.tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "") or ""
            raw_args = fn.get("arguments", "") or "{}"
            call_id = tc.get("id") or f"call_{int(time.time()*1000)}"
            preview = _short(raw_args, 120)
            label = f"Running {name}({preview})"
            with anim.working(label=label, color=anim.BLUE) as sp:
                output = registry.invoke(name, raw_args, ctx)
                sp.set_label(f"{name} done", color=GREEN)
            if cfg.show_tool_io:
                _render_tool_result(name, output)
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": output,
                }
            )
            session.save(cfg.sessions_dir)
        # Loop back: let the model see tool results and decide next step.
    print(style(f"\n[stopped: hit max_tool_iters={cfg.max_tool_iters}]", YELLOW))


def _render_tool_result(name: str, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"raw": raw[:200]}
    if isinstance(data, dict) and data.get("error"):
        print(style(f"  ✗ {name}: {data['error']}", RED))
        return
    summary = _summarize_tool_output(name, data)
    print(style(f"  ↪ {name}: {summary}", DIM))


def _summarize_tool_output(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)[:200]
    if "exit_code" in data:
        ec = data["exit_code"]
        out = data.get("stdout") or ""
        first = out.strip().splitlines()[:1]
        head = first[0][:160] if first else ""
        return f"exit={ec}" + (f" — {head}" if head else "")
    if "matches" in data:
        return f"{len(data['matches'])} match(es)"
    if "entries" in data:
        return f"{len(data['entries'])} entries"
    if "hits" in data:
        return f"{len(data['hits'])} hit(s)"
    if "frameworks" in data:
        return ", ".join(data["frameworks"]) + (f" • abis={','.join(data.get('abis',[]))}" if data.get("abis") else "")
    return _short(json.dumps(data, ensure_ascii=False), 160)


def _short(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# One-shot helper (testing / scripting)
# --------------------------------------------------------------------------- #


def _one_shot(client: ChatClient, registry, ctx: ToolContext, cfg: Config, session: Session, prompt: str) -> int:
    session.messages.append({"role": "user", "content": prompt})
    session.save(cfg.sessions_dir)
    try:
        _agent_turn(client, registry, ctx, cfg, session)
    except APIError as e:
        print(style(f"\n[API error] {e}", RED))
        return 1
    return 0
