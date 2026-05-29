"""ANSI animations for the Chat AI: spinners, typing indicators, status banners.

All animations run in a background thread so the agent loop can keep working.
They print to stderr so they do not pollute the assistant's textual output.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_DOT_FRAMES = (".  ", ".. ", "...", " ..", "  .", "   ")

# Soft palette — works on dark + light terminals.
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RED = "\033[31m"
GRAY = "\033[90m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stderr.isatty():
        return False
    return True


def style(text: str, *codes: str) -> str:
    if not supports_color():
        return text
    return "".join(codes) + text + RESET


class Spinner:
    """A background spinner with a mutable label.

    Designed to be cheap and re-entrant.  Multiple ``start()`` calls without
    ``stop()`` are coalesced (later wins).
    """

    def __init__(self, label: str = "Working", color: str = CYAN):
        self._label = label
        self._color = color
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_label(self, label: str, color: str | None = None) -> None:
        with self._lock:
            self._label = label
            if color is not None:
                self._color = color

    def _run(self) -> None:
        i = 0
        last_len = 0
        while not self._stop.is_set():
            with self._lock:
                label = self._label
                color = self._color
            frame = _FRAMES[i % len(_FRAMES)]
            dots = _DOT_FRAMES[i % len(_DOT_FRAMES)]
            if supports_color():
                line = f"\r{color}{frame}{RESET} {DIM}{label}{dots}{RESET}"
            else:
                line = f"\r{frame} {label}{dots}"
            pad = max(0, last_len - len(line))
            sys.stderr.write(line + (" " * pad))
            sys.stderr.flush()
            last_len = len(line)
            i += 1
            self._stop.wait(0.10)
        # Clear the line.
        sys.stderr.write("\r" + " " * max(last_len, 0) + "\r")
        sys.stderr.flush()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not sys.stderr.isatty():
            # In non-TTY (e.g. pipes), just print the label once.
            sys.stderr.write(f"[{self._label}…]\n")
            sys.stderr.flush()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None


@contextmanager
def working(label: str = "Working", color: str = CYAN) -> Iterator[Spinner]:
    s = Spinner(label=label, color=color)
    s.start()
    try:
        yield s
    finally:
        s.stop()


# Convenience helpers ---------------------------------------------------------


def thinking() -> Spinner:
    s = Spinner(label="Thinking", color=MAGENTA)
    s.start()
    return s


def analyzing(what: str = "") -> Spinner:
    label = "Analyzing" if not what else f"Analyzing {what}"
    s = Spinner(label=label, color=BLUE)
    s.start()
    return s


def typing() -> Spinner:
    s = Spinner(label="Typing", color=CYAN)
    s.start()
    return s


def banner_box(title: str, subtitle: str = "") -> str:
    if not supports_color():
        bar = "=" * max(40, len(title) + 8)
        out = [bar, f"  {title}"]
        if subtitle:
            out.append(f"  {subtitle}")
        out.append(bar)
        return "\n".join(out)
    bar = "═" * 46
    lines = [
        f"{BOLD}{CYAN}╔{bar}╗{RESET}",
        f"{BOLD}{CYAN}║{RESET}  {BOLD}{title.center(42)}{RESET}  {BOLD}{CYAN}║{RESET}",
    ]
    if subtitle:
        lines.append(
            f"{BOLD}{CYAN}║{RESET}  {DIM}{subtitle.center(42)}{RESET}  {BOLD}{CYAN}║{RESET}"
        )
    lines.append(f"{BOLD}{CYAN}╚{bar}╝{RESET}")
    return "\n".join(lines)


def stream_typewriter(text: str, delay: float = 0.0) -> None:
    """Print ``text`` to stdout with an optional micro-delay between chars.

    With ``delay <= 0`` this is a normal print but flushed.
    """

    if delay <= 0 or not sys.stdout.isatty():
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
