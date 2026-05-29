"""Lower-level reverse-engineering analysis tools."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def _resolve(path: str, workspace: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(workspace) / p
    return p


def _run(argv: list[str], cwd: str | None = None, timeout: float = 600.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
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


def _which(name: str) -> str | None:
    return shutil.which(name)


def _jadx(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    if not p.exists():
        return {"error": f"apk not found: {p}"}
    out_dir = args.get("out_dir") or str(_resolve(p.stem + ".jadx", ctx.workspace))
    op = _resolve(out_dir, ctx.workspace)
    jadx = _which("jadx") or "/opt/jadx/bin/jadx"
    if not Path(jadx).exists() and not shutil.which("jadx"):
        return {"error": "jadx not installed — see install.sh"}
    argv = [jadx, "-d", str(op), str(p)]
    if args.get("no_res"):
        argv.append("--no-res")
    if args.get("show_bad_code"):
        argv.append("--show-bad-code")
    result = _run(argv, timeout=1500)
    result["out_dir"] = str(op)
    return result


def _dex2jar(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    if not p.exists():
        return {"error": f"apk not found: {p}"}
    out = args.get("out") or str(_resolve(p.stem + ".jar", ctx.workspace))
    op = _resolve(out, ctx.workspace)
    d2j = _which("d2j-dex2jar") or _which("dex2jar")
    if not d2j:
        return {"error": "d2j-dex2jar not installed"}
    return _run([d2j, "-f", str(p), "-o", str(op)], timeout=600)


def _strings(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"not found: {p}"}
    strings = _which("strings")
    if not strings:
        return {"error": "strings(1) not installed (binutils)"}
    min_len = int(args.get("min_len", 6))
    argv = [strings, f"-n{min_len}", str(p)]
    result = _run(argv, timeout=300)
    # Optional filter.
    needle = args.get("contains")
    if needle and isinstance(needle, str):
        lines = result.get("stdout", "").splitlines()
        filtered = [ln for ln in lines if needle in ln]
        result["stdout"] = "\n".join(filtered[:2000])
        result["filtered_count"] = len(filtered)
    return result


def _hexdump(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"not found: {p}"}
    offset = int(args.get("offset", 0))
    length = int(args.get("length", 1024))
    try:
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(length)
    except OSError as e:
        return {"error": f"{e}"}
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join((chr(b) if 32 <= b < 127 else ".") for b in chunk)
        lines.append(f"{offset+i:08x}  {hex_part:<47}  |{ascii_part}|")
    return {
        "path": str(p),
        "offset": offset,
        "length": len(data),
        "hexdump": "\n".join(lines),
    }


def _file_type(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"not found: {p}"}
    file_cmd = _which("file")
    if not file_cmd:
        return {"error": "file(1) not installed"}
    return _run([file_cmd, "-b", str(p)], timeout=60)


def _smali_disasm(args: dict[str, Any], ctx) -> dict[str, Any]:
    dex = args.get("dex")
    if not dex:
        return {"error": "`dex` is required"}
    p = _resolve(dex, ctx.workspace)
    if not p.exists():
        return {"error": f"dex not found: {p}"}
    out_dir = args.get("out_dir") or str(_resolve(p.stem + ".smali", ctx.workspace))
    op = _resolve(out_dir, ctx.workspace)
    baksmali = _which("baksmali")
    if not baksmali:
        return {"error": "baksmali not installed"}
    return _run([baksmali, "d", str(p), "-o", str(op)], timeout=600)


def _smali_asm(args: dict[str, Any], ctx) -> dict[str, Any]:
    src = args.get("src")
    if not src:
        return {"error": "`src` is required (smali source dir)"}
    p = _resolve(src, ctx.workspace)
    if not p.exists():
        return {"error": f"src not found: {p}"}
    out = args.get("out") or str(p.with_suffix(".dex"))
    op = _resolve(out, ctx.workspace)
    smali = _which("smali")
    if not smali:
        return {"error": "smali not installed"}
    return _run([smali, "a", str(p), "-o", str(op)], timeout=600)


def register(reg, Tool) -> None:
    reg.register(Tool(
        name="jadx_decompile",
        description="Decompile APK or DEX to Java source using jadx. Outputs to `out_dir`.",
        parameters={
            "type": "object",
            "properties": {
                "apk": {"type": "string"},
                "out_dir": {"type": "string"},
                "no_res": {"type": "boolean"},
                "show_bad_code": {"type": "boolean"},
            },
            "required": ["apk"],
        },
        handler=_jadx,
    ))
    reg.register(Tool(
        name="dex2jar",
        description="Convert classes.dex (or an APK containing dex files) into a JAR.",
        parameters={
            "type": "object",
            "properties": {"apk": {"type": "string"}, "out": {"type": "string"}},
            "required": ["apk"],
        },
        handler=_dex2jar,
    ))
    reg.register(Tool(
        name="strings",
        description="Run strings(1) on a binary file. Optionally filter via `contains`.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "min_len": {"type": "integer"},
                "contains": {"type": "string"},
            },
            "required": ["path"],
        },
        handler=_strings,
    ))
    reg.register(Tool(
        name="hexdump",
        description="Dump a region of a file as hex+ASCII. Defaults to 1024 bytes from offset 0.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "length": {"type": "integer"},
            },
            "required": ["path"],
        },
        handler=_hexdump,
    ))
    reg.register(Tool(
        name="file_type",
        description="Detect a file's type via `file -b`.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_file_type,
    ))
    reg.register(Tool(
        name="smali_disasm",
        description="Disassemble a classes.dex into smali source using baksmali.",
        parameters={
            "type": "object",
            "properties": {"dex": {"type": "string"}, "out_dir": {"type": "string"}},
            "required": ["dex"],
        },
        handler=_smali_disasm,
    ))
    reg.register(Tool(
        name="smali_asm",
        description="Assemble a smali source tree back into a DEX file using smali.",
        parameters={
            "type": "object",
            "properties": {"src": {"type": "string"}, "out": {"type": "string"}},
            "required": ["src"],
        },
        handler=_smali_asm,
    ))
