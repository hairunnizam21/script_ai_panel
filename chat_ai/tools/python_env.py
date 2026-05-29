"""Python venv tools so the agent can write and run Python helpers safely."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _resolve(path: str, workspace: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(workspace) / p
    return p


def _run(argv: list[str], cwd: str | None = None, timeout: float = 600.0, env: dict | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError as e:
        return {"error": f"command not found: {argv[0]} ({e})", "argv": argv}
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s", "argv": argv}
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-15_000:],
        "stderr": (proc.stderr or "")[-15_000:],
    }


def _venv_create(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path") or ".venv"
    p = _resolve(path, ctx.workspace)
    if p.exists() and not args.get("force"):
        return {"venv": str(p), "existed": True}
    if p.exists():
        shutil.rmtree(p)
    py = sys.executable or shutil.which("python3") or "python3"
    return {**_run([py, "-m", "venv", str(p)]), "venv": str(p)}


def _pip_install(args: dict[str, Any], ctx) -> dict[str, Any]:
    venv = args.get("venv") or ".venv"
    pkgs = args.get("packages") or []
    if not isinstance(pkgs, list) or not pkgs:
        return {"error": "`packages` (list[str]) is required"}
    vp = _resolve(venv, ctx.workspace)
    pip = vp / "bin" / "pip"
    if not pip.exists():
        return {"error": f"venv pip not found: {pip}"}
    return _run([str(pip), "install", *[str(p) for p in pkgs]], timeout=900)


def _run_python(args: dict[str, Any], ctx) -> dict[str, Any]:
    venv = args.get("venv")
    code = args.get("code")
    script = args.get("script")
    if not code and not script:
        return {"error": "either `code` or `script` is required"}
    if venv:
        py = str(_resolve(venv, ctx.workspace) / "bin" / "python")
        if not Path(py).exists():
            return {"error": f"venv python not found: {py}"}
    else:
        py = sys.executable or shutil.which("python3") or "python3"
    argv = [py]
    if code:
        argv.extend(["-c", code])
    else:
        argv.append(str(_resolve(script, ctx.workspace)))
    if isinstance(args.get("argv"), list):
        argv.extend(str(x) for x in args["argv"])
    cwd = args.get("cwd") or ctx.workspace
    cwd_p = _resolve(cwd, ctx.workspace)
    return _run(argv, cwd=str(cwd_p), timeout=float(args.get("timeout", 300)))


def register(reg, Tool) -> None:
    reg.register(Tool(
        name="venv_create",
        description="Create a Python virtualenv at `path` (relative to workspace by default).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "force": {"type": "boolean"}},
        },
        handler=_venv_create,
    ))
    reg.register(Tool(
        name="pip_install",
        description="Install packages into a venv.",
        parameters={
            "type": "object",
            "properties": {
                "venv": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["packages"],
        },
        handler=_pip_install,
    ))
    reg.register(Tool(
        name="run_python",
        description="Run a Python snippet (`code`) or a script. Optionally inside a venv.",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "script": {"type": "string"},
                "venv": {"type": "string"},
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout": {"type": "number"},
            },
        },
        handler=_run_python,
    ))
