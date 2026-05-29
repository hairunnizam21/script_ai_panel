"""Conversation context-window management.

A long-lived session accumulates a large transcript on disk: every user
message, every assistant reply, and — crucially — every tool result.  Tool
results from APK work (decompile listings, ``aapt dump``, ``strings``,
``hexdump``, shell output) can each be tens of kilobytes.

The model is re-sent the *entire* transcript on every single call, so once a
session has done real work the prompt balloons to tens of thousands of tokens.
That makes each turn slow *and* degrades answer quality (the model gets "lost
in the middle" of a giant prompt).

This module trims the transcript we *send* without touching what we persist:

* Each tool-result message is capped to ``tool_result_char_cap`` characters
  (head + tail kept, middle elided).  The user still receives full artefacts as
  file uploads, so nothing is lost — the model just doesn't carry megabytes of
  raw dumps around.
* Only the most recent whole *turns* that fit within ``char_budget`` are sent.
  Older turns are dropped.  We never split a turn, so an assistant ``tool_calls``
  message always keeps its matching ``tool`` responses (required by the API).
* System messages are always kept, regardless of budget.

The returned list is a fresh copy; ``session.messages`` is never mutated.
"""

from __future__ import annotations

import copy
from typing import Any


def _approx_len(message: dict[str, Any]) -> int:
    """Cheap size estimate for one message (chars across its text fields)."""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        total += len(str(content))
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        total += len(str(fn.get("name", ""))) + len(str(fn.get("arguments", "")))
    return total


def _cap_text(text: str, cap: int) -> str:
    """Keep the head and tail of ``text``, eliding the middle."""
    if cap <= 0 or len(text) <= cap:
        return text
    # Bias towards the tail — for command output the error/result usually lives
    # at the end — but keep some of the head for context.
    head = cap // 3
    tail = cap - head
    elided = len(text) - head - tail
    return (
        text[:head]
        + f"\n…[{elided} chars elided — full output saved to the workspace]…\n"
        + text[-tail:]
    )


def _compact_tool_message(message: dict[str, Any], cap: int) -> dict[str, Any]:
    """Return a copy of a ``tool``-role message with its content capped."""
    content = message.get("content")
    if not isinstance(content, str) or cap <= 0 or len(content) <= cap:
        return message
    out = dict(message)
    out["content"] = _cap_text(content, cap)
    return out


def _split_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group non-system messages into turns.

    A turn starts at a ``user`` message and runs until the next ``user``
    message.  Leading non-user messages (rare) form their own turn so nothing
    is dropped silently.
    """
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(m)
    if current:
        turns.append(current)
    return turns


def prune_messages(
    messages: list[dict[str, Any]],
    *,
    char_budget: int = 48000,
    tool_result_char_cap: int = 8000,
) -> list[dict[str, Any]]:
    """Build a trimmed copy of ``messages`` suitable for sending to the model.

    Always keeps system messages and the most recent turn; keeps as many
    additional recent turns as fit within ``char_budget``.
    """
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    # Cap oversized tool results everywhere up front.
    capped = [
        _compact_tool_message(m, tool_result_char_cap) if m.get("role") == "tool" else m
        for m in rest
    ]

    turns = _split_turns(capped)
    if not turns:
        return [copy.deepcopy(m) for m in system]

    system_len = sum(_approx_len(m) for m in system)
    budget = max(0, char_budget - system_len)

    kept: list[list[dict[str, Any]]] = []
    used = 0
    for turn in reversed(turns):
        turn_len = sum(_approx_len(m) for m in turn)
        if kept and used + turn_len > budget:
            break
        kept.append(turn)
        used += turn_len
    kept.reverse()

    out: list[dict[str, Any]] = list(system)
    for turn in kept:
        out.extend(turn)
    return [copy.deepcopy(m) for m in out]
