"""Tool registry for the Suzu Chat AI.

Each tool is a ``Tool`` dataclass with an OpenAI-style ``schema`` (JSON Schema)
describing its parameters and a ``handler`` callable that executes it.  The
agent loop converts every assistant ``tool_calls`` entry into a handler call
and feeds the result back as a ``tool``-role message.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from . import apk, analysis, files, python_env, shell


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    workspace: str
    debug: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def invoke(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"unknown tool: {name}"})
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                args = {"value": args}
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"invalid JSON arguments: {e}", "raw": raw_args[:400]})
        try:
            result = tool.handler(args, ctx)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc() if ctx.debug else ""
            result = {"error": f"{type(e).__name__}: {e}", "trace": tb}
        if not isinstance(result, dict):
            result = {"result": result}
        return _safe_json(result)


def _safe_json(obj: Any, max_chars: int = 60_000) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=_default)
    if len(s) > max_chars:
        truncated = obj.copy() if isinstance(obj, dict) else {"result": obj}
        truncated["_truncated"] = True
        truncated["_note"] = (
            f"output trimmed from {len(s)} to {max_chars} chars — "
            "save large outputs to files and read them back"
        )
        # Trim any stdout/stderr-ish fields.
        for key in ("stdout", "stderr", "content", "preview"):
            if key in truncated and isinstance(truncated[key], str):
                truncated[key] = truncated[key][: max_chars // 2] + "\n…[trimmed]…"
        s = json.dumps(truncated, ensure_ascii=False, default=_default)
        if len(s) > max_chars:
            s = s[:max_chars]
    return s


def _default(o: Any) -> Any:
    try:
        return str(o)
    except Exception:
        return repr(o)


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    shell.register(reg, Tool)
    files.register(reg, Tool)
    apk.register(reg, Tool)
    analysis.register(reg, Tool)
    python_env.register(reg, Tool)
    return reg
