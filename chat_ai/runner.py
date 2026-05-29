"""Programmatic, non-interactive agent driver.

The interactive REPL in :mod:`chat_ai.agent` is tightly coupled to terminal
output (animations, stdout writes).  Front-ends like the Telegram bot don't
want any of that — they want to send a user message, optionally observe the
intermediate tool calls, and receive the assistant's final text.

This module provides :func:`run_turn`, a small wrapper that drives one
streaming chat-completion + tool-call loop and emits structured events via an
``on_event`` callback so callers can render progress however they like.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from .api import APIError, ChatClient, ChatRequest, DeltaAccumulator
from .config import Config
from .context import prune_messages
from .state import Session
from .tools import ToolContext, ToolRegistry

# An event callback receives ``(kind, payload)`` tuples.  Kinds:
#   "thinking"      payload = {}                                  (first model call)
#   "text_delta"    payload = {"text": str}                       (assistant tokens)
#   "tool_start"    payload = {"name", "arguments", "call_id"}    (a tool is about to run)
#   "tool_end"      payload = {"name", "call_id", "output"}       (tool finished — output is the raw JSON string)
#   "iteration"     payload = {"index": int}                      (loop iteration boundary)
#   "done"          payload = {"text": str}                       (full assistant text for this turn)
#   "error"         payload = {"error": str}                      (API/transport error)
EventCb = Callable[[str, dict], None]


def run_turn(
    client: ChatClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    cfg: Config,
    session: Session,
    user_message: Optional[str] = None,
    on_event: Optional[EventCb] = None,
) -> str:
    """Drive one user→assistant exchange end-to-end.

    Appends ``user_message`` (if provided) to ``session.messages``, runs the
    streaming model loop up to ``cfg.max_tool_iters`` iterations, executes any
    tool calls inline, and returns the final assistant text.  The session is
    persisted after every model + tool step.
    """

    def _emit(kind: str, payload: dict) -> None:
        if on_event:
            try:
                on_event(kind, payload)
            except Exception:  # noqa: BLE001 — front-ends must not break the loop
                pass

    if user_message is not None:
        session.messages.append({"role": "user", "content": user_message})
        session.save(cfg.sessions_dir)

    tools = registry.schemas()
    final_text_parts: list[str] = []

    for iteration in range(cfg.max_tool_iters):
        _emit("iteration", {"index": iteration})
        _emit("thinking", {})

        req = ChatRequest(
            model=session.model,
            messages=prune_messages(
                session.messages,
                char_budget=cfg.context_char_budget,
                tool_result_char_cap=cfg.tool_result_char_cap,
            ),
            tools=tools,
            stream=True,
            max_tokens=cfg.max_tokens or None,
        )
        accum = DeltaAccumulator()
        try:
            for event in client.stream(req):
                added = accum.push(event)
                if added:
                    _emit("text_delta", {"text": added})
        except APIError as e:
            _emit("error", {"error": str(e)})
            return "".join(final_text_parts)

        assistant_msg = accum.to_message()
        if assistant_msg.get("content") is None:
            assistant_msg["content"] = ""
        session.messages.append(assistant_msg)
        session.save(cfg.sessions_dir)

        if assistant_msg.get("content"):
            final_text_parts.append(assistant_msg["content"])

        if not accum.tool_calls:
            text = "".join(final_text_parts)
            _emit("done", {"text": text})
            return text

        # Execute each tool call and append a tool response message.
        for tc in accum.tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "") or ""
            raw_args = fn.get("arguments", "") or "{}"
            call_id = tc.get("id") or f"call_{int(time.time() * 1000)}"
            _emit(
                "tool_start",
                {"name": name, "arguments": raw_args, "call_id": call_id},
            )
            output = registry.invoke(name, raw_args, ctx)
            _emit(
                "tool_end",
                {"name": name, "call_id": call_id, "output": output},
            )
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": output,
                }
            )
            session.save(cfg.sessions_dir)
        # loop again — feed tool results back to the model.

    # Hit the iteration cap.  Return whatever text we accumulated so far.
    text = "".join(final_text_parts)
    _emit("done", {"text": text})
    return text


def summarize_tool_output(name: str, raw: str, limit: int = 200) -> str:
    """Compact one-liner for a tool result (suitable for chat status lines)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _shorten(raw, limit)
    if isinstance(data, dict):
        if data.get("error"):
            return f"error: {_shorten(str(data['error']), limit)}"
        if "exit_code" in data:
            ec = data["exit_code"]
            out = (data.get("stdout") or "").strip().splitlines()
            head = out[0] if out else ""
            return f"exit={ec}" + (f" — {_shorten(head, limit)}" if head else "")
        if "matches" in data:
            return f"{len(data['matches'])} match(es)"
        if "entries" in data:
            return f"{len(data['entries'])} entries"
        if "frameworks" in data:
            extra = f" • abis={','.join(data.get('abis', []))}" if data.get("abis") else ""
            return ", ".join(data["frameworks"]) + extra
        if "path" in data:
            return str(data["path"])
    return _shorten(json.dumps(data, ensure_ascii=False), limit)


def _shorten(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
