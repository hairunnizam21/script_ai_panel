"""File-system tools: read/write/edit/list/glob/grep."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any


def _resolve(path: str, workspace: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(workspace) / p
    return p


def _read_file(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"file not found: {p}"}
    if p.is_dir():
        return {"error": f"path is a directory: {p}"}
    max_bytes = int(args.get("max_bytes", 200_000))
    offset = int(args.get("offset", 0))
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(max_bytes)
    except OSError as e:
        return {"error": f"read failed: {e}"}
    try:
        text = data.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        text = data.hex()
        is_binary = True
    return {
        "path": str(p),
        "size": size,
        "offset": offset,
        "bytes_read": len(data),
        "is_binary": is_binary,
        "content": text,
        "truncated": offset + len(data) < size,
    }


def _write_file(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        return {"error": "`path` is required"}
    if not isinstance(content, str):
        return {"error": "`content` must be a string"}
    p = _resolve(path, ctx.workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if args.get("append") else "wb"
    try:
        with p.open(mode) as f:
            f.write(content.encode("utf-8"))
    except OSError as e:
        return {"error": f"write failed: {e}"}
    return {"path": str(p), "bytes": len(content.encode("utf-8")), "appended": bool(args.get("append"))}


def _edit_file(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    old = args.get("old_string")
    new = args.get("new_string", "")
    if not path or old is None:
        return {"error": "`path` and `old_string` are required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"file not found: {p}"}
    try:
        original = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"cannot read as utf-8: {e}"}
    count = original.count(old)
    if count == 0:
        return {"error": "old_string not found"}
    if count > 1 and not args.get("replace_all"):
        return {"error": f"old_string occurs {count} times — pass replace_all=true or include more context"}
    updated = original.replace(old, new, -1 if args.get("replace_all") else 1)
    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as e:
        return {"error": f"write failed: {e}"}
    return {"path": str(p), "replaced": count if args.get("replace_all") else 1}


def _list_dir(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path", ".")
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"not found: {p}"}
    if not p.is_dir():
        return {"error": f"not a directory: {p}"}
    show_hidden = bool(args.get("show_hidden"))
    entries = []
    try:
        for entry in sorted(p.iterdir()):
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": st.st_size if entry.is_file() else None,
                    }
                )
            except OSError:
                entries.append({"name": entry.name, "type": "unknown"})
    except PermissionError as e:
        return {"error": f"{e}"}
    return {"path": str(p), "count": len(entries), "entries": entries[:500]}


def _glob(args: dict[str, Any], ctx) -> dict[str, Any]:
    pattern = args.get("pattern")
    if not pattern:
        return {"error": "`pattern` is required"}
    root = _resolve(args.get("path", "."), ctx.workspace)
    if not root.exists():
        return {"error": f"not found: {root}"}
    max_results = int(args.get("max_results", 500))
    matches: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root)
            if fnmatch.fnmatch(str(rel), pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(str(full))
                if len(matches) >= max_results:
                    return {"root": str(root), "pattern": pattern, "matches": matches, "truncated": True}
    return {"root": str(root), "pattern": pattern, "matches": matches, "truncated": False}


def _grep(args: dict[str, Any], ctx) -> dict[str, Any]:
    pattern = args.get("pattern")
    if not pattern:
        return {"error": "`pattern` is required"}
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return {"error": f"invalid regex: {e}"}
    root = _resolve(args.get("path", "."), ctx.workspace)
    if not root.exists():
        return {"error": f"not found: {root}"}
    glob_pat = args.get("glob") or "*"
    max_results = int(args.get("max_results", 200))
    hits: list[dict[str, Any]] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not fnmatch.fnmatch(name, glob_pat):
                continue
            full = Path(dirpath) / name
            try:
                with full.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            hits.append(
                                {
                                    "file": str(full),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:400],
                                }
                            )
                            if len(hits) >= max_results:
                                return {"root": str(root), "pattern": pattern, "hits": hits, "truncated": True}
            except OSError:
                continue
    return {"root": str(root), "pattern": pattern, "hits": hits, "truncated": False}


def _mkdir(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"mkdir failed: {e}"}
    return {"path": str(p)}


def _move(args: dict[str, Any], ctx) -> dict[str, Any]:
    src = args.get("src")
    dst = args.get("dst")
    if not src or not dst:
        return {"error": "`src` and `dst` are required"}
    sp = _resolve(src, ctx.workspace)
    dp = _resolve(dst, ctx.workspace)
    if not sp.exists():
        return {"error": f"src not found: {sp}"}
    dp.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(sp), str(dp))
    except OSError as e:
        return {"error": f"move failed: {e}"}
    return {"src": str(sp), "dst": str(dp)}


def _remove(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path")
    if not path:
        return {"error": "`path` is required"}
    p = _resolve(path, ctx.workspace)
    if not p.exists():
        return {"error": f"not found: {p}"}
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as e:
        return {"error": f"remove failed: {e}"}
    return {"removed": str(p)}


def register(reg, Tool) -> None:
    reg.register(Tool(
        name="read_file",
        description="Read a UTF-8 text file (or hex-dump a binary). Large files are paginated via offset/max_bytes.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "Byte offset to start reading from."},
                "max_bytes": {"type": "integer", "description": "Default 200000."},
            },
            "required": ["path"],
        },
        handler=_read_file,
    ))
    reg.register(Tool(
        name="write_file",
        description="Create or overwrite a file with the given text content. Set append=true to append.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ))
    reg.register(Tool(
        name="edit_file",
        description="Replace `old_string` with `new_string` in a file. Set replace_all=true to replace every occurrence.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string"],
        },
        handler=_edit_file,
    ))
    reg.register(Tool(
        name="list_dir",
        description="List directory entries (files + folders) in the given path.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Defaults to the workspace root."},
                "show_hidden": {"type": "boolean"},
            },
        },
        handler=_list_dir,
    ))
    reg.register(Tool(
        name="glob",
        description="Recursively find files matching a fnmatch pattern (e.g. '**/*.smali' or 'AndroidManifest.xml').",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        handler=_glob,
    ))
    reg.register(Tool(
        name="grep",
        description="Recursively grep file contents with a Python regex. Optionally limit to filenames matching `glob`.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        handler=_grep,
    ))
    reg.register(Tool(
        name="mkdir",
        description="Create a directory (mkdir -p).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_mkdir,
    ))
    reg.register(Tool(
        name="move",
        description="Move/rename a file or directory.",
        parameters={
            "type": "object",
            "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
            "required": ["src", "dst"],
        },
        handler=_move,
    ))
    reg.register(Tool(
        name="remove",
        description="Delete a file or directory (recursive).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_remove,
    ))
