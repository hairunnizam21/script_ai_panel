"""APK-specific tools: detect type, decompile, recompile, sign, install, info."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve(path: str, workspace: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(workspace) / p
    return p


def _run(argv: list[str], cwd: str | None = None, timeout: float = 900.0) -> dict[str, Any]:
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


_KEYSTORE_DEFAULTS = {
    "keystore": "/var/www/suzu-ai-web/server/keystores/debug.keystore",
    "storepass": "android",
    "keypass": "android",
    "alias": "androiddebugkey",
}


def _resolve_keystore(args: dict[str, Any]) -> dict[str, str]:
    ks = dict(_KEYSTORE_DEFAULTS)
    for k in ks:
        v = args.get(k)
        if v:
            ks[k] = str(v)
    # Allow overrides from env.
    env_ks = os.environ.get("SUZU_KEYSTORE")
    if env_ks and not args.get("keystore"):
        ks["keystore"] = env_ks
    return ks


# --------------------------------------------------------------------------- #
# APK type detection
# --------------------------------------------------------------------------- #


def _detect_apk_type(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    if not p.exists():
        return {"error": f"apk not found: {p}"}

    findings: dict[str, Any] = {
        "apk": str(p),
        "size": p.stat().st_size,
        "frameworks": [],
        "abis": [],
        "files_of_interest": [],
        "package": None,
        "version_name": None,
        "version_code": None,
        "min_sdk": None,
        "target_sdk": None,
    }

    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as e:
        return {"error": f"not a valid zip/APK: {e}"}

    f = set(names)

    abis = set()
    for n in names:
        m = re.match(r"^lib/([^/]+)/", n)
        if m:
            abis.add(m.group(1))
    findings["abis"] = sorted(abis)

    has_so = lambda needle: any(n.startswith("lib/") and n.endswith(f"/{needle}") for n in names)

    if has_so("libflutter.so") or has_so("libapp.so"):
        findings["frameworks"].append("flutter")
    if has_so("libil2cpp.so") or "assets/bin/Data/Managed/Metadata/global-metadata.dat" in f:
        findings["frameworks"].append("unity-il2cpp")
    if has_so("libmono.so") or any(n.startswith("assemblies/") for n in names):
        findings["frameworks"].append("xamarin/.net")
    if "assets/index.android.bundle" in f or any(
        n.startswith("assets/") and n.endswith(".bundle") for n in names
    ):
        findings["frameworks"].append("react-native")
    if has_so("libhermes.so"):
        findings["frameworks"].append("hermes")
    if any(n.endswith(".dex") for n in names):
        findings["frameworks"].append("dalvik/dex")
    if any(n.startswith("kotlin/") or n.endswith(".kotlin_module") for n in names):
        findings["frameworks"].append("kotlin")
    if has_so("libfb.so") or has_so("libfbjni.so"):
        findings["frameworks"].append("react-native-fb")
    if has_so("libreactnative.so") or has_so("libreactnativejni.so"):
        findings["frameworks"].append("react-native")
    if not findings["frameworks"]:
        findings["frameworks"].append("native-java")

    for needle in (
        "AndroidManifest.xml",
        "classes.dex",
        "assets/flutter_assets/kernel_blob.bin",
        "assets/index.android.bundle",
        "assets/bin/Data/Managed/Metadata/global-metadata.dat",
        "META-INF/MANIFEST.MF",
    ):
        if needle in f:
            findings["files_of_interest"].append(needle)

    # Try aapt2 / aapt for badging info.
    aapt = _which("aapt2") or _which("aapt")
    if aapt:
        info = _run([aapt, "dump", "badging", str(p)], timeout=60)
        out = info.get("stdout", "")
        m = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", out)
        if m:
            findings["package"] = m.group(1)
            findings["version_code"] = m.group(2)
            findings["version_name"] = m.group(3)
        m = re.search(r"sdkVersion:'([^']+)'", out)
        if m:
            findings["min_sdk"] = m.group(1)
        m = re.search(r"targetSdkVersion:'([^']+)'", out)
        if m:
            findings["target_sdk"] = m.group(1)

    return findings


# --------------------------------------------------------------------------- #
# Decompile / recompile
# --------------------------------------------------------------------------- #


def _decompile(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    if not p.exists():
        return {"error": f"apk not found: {p}"}
    out_dir = args.get("out_dir") or str(_resolve(p.stem + ".decompiled", ctx.workspace))
    out_p = _resolve(out_dir, ctx.workspace)
    if out_p.exists() and not args.get("force"):
        return {"error": f"output exists: {out_p} — pass force=true to overwrite"}
    if out_p.exists():
        shutil.rmtree(out_p)
    apktool = _which("apktool")
    if not apktool:
        return {"error": "apktool not installed — install via install.sh or `apt install apktool`"}
    argv = [apktool, "d", str(p), "-o", str(out_p)]
    if args.get("no_src"):
        argv.append("--no-src")
    if args.get("no_res"):
        argv.append("--no-res")
    result = _run(argv, timeout=900)
    result["out_dir"] = str(out_p)
    return result


def _recompile(args: dict[str, Any], ctx) -> dict[str, Any]:
    src_dir = args.get("src_dir")
    if not src_dir:
        return {"error": "`src_dir` is required (path from decompile)"}
    sp = _resolve(src_dir, ctx.workspace)
    if not sp.exists():
        return {"error": f"src_dir not found: {sp}"}
    out_apk = args.get("out_apk") or str(sp.with_suffix(".unsigned.apk"))
    op = _resolve(out_apk, ctx.workspace)
    apktool = _which("apktool")
    if not apktool:
        return {"error": "apktool not installed"}
    argv = [apktool, "b", str(sp), "-o", str(op)]
    if args.get("use_aapt2", True):
        argv.append("--use-aapt2")
    result = _run(argv, timeout=900)
    result["out_apk"] = str(op)
    return result


def _zipalign(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    sp = _resolve(apk, ctx.workspace)
    out = args.get("out") or str(sp.with_suffix(".aligned.apk"))
    op = _resolve(out, ctx.workspace)
    zipalign = _which("zipalign")
    if not zipalign:
        return {"error": "zipalign not installed"}
    result = _run([zipalign, "-p", "-f", "4", str(sp), str(op)], timeout=300)
    result["out"] = str(op)
    return result


def _sign(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    sp = _resolve(apk, ctx.workspace)
    out = args.get("out") or str(sp.with_suffix(".signed.apk"))
    op = _resolve(out, ctx.workspace)
    ks = _resolve_keystore(args)
    if not Path(ks["keystore"]).exists():
        return {"error": f"keystore not found at {ks['keystore']} — create it or pass keystore=<path>"}
    apksigner = _which("apksigner")
    if not apksigner:
        return {"error": "apksigner not installed"}
    # Copy first (apksigner can sign in place but we want a separate file).
    shutil.copy2(sp, op)
    argv = [
        apksigner, "sign",
        "--ks", ks["keystore"],
        "--ks-key-alias", ks["alias"],
        "--ks-pass", f"pass:{ks['storepass']}",
        "--key-pass", f"pass:{ks['keypass']}",
        str(op),
    ]
    result = _run(argv, timeout=300)
    result["out"] = str(op)
    return result


def _verify_signature(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    apksigner = _which("apksigner")
    if not apksigner:
        return {"error": "apksigner not installed"}
    return _run([apksigner, "verify", "--print-certs", "-v", str(p)], timeout=120)


def _aapt_dump(args: dict[str, Any], ctx) -> dict[str, Any]:
    apk = args.get("apk")
    if not apk:
        return {"error": "`apk` is required"}
    p = _resolve(apk, ctx.workspace)
    aapt = _which("aapt2") or _which("aapt")
    if not aapt:
        return {"error": "aapt/aapt2 not installed"}
    what = args.get("what", "badging")
    return _run([aapt, "dump", what, str(p)], timeout=120)


def _build_full(args: dict[str, Any], ctx) -> dict[str, Any]:
    """Convenience: recompile -> zipalign -> sign in one shot.

    Useful when the model wants to ask for "rebuild the APK" without
    orchestrating each step.
    """

    src_dir = args.get("src_dir")
    if not src_dir:
        return {"error": "`src_dir` is required"}
    out_apk = args.get("out_apk")
    if not out_apk:
        sp = _resolve(src_dir, ctx.workspace)
        out_apk = str(sp.with_suffix(".final.apk"))
    op = _resolve(out_apk, ctx.workspace)
    workspace = ctx.workspace

    unsigned = str(_resolve(Path(out_apk).stem + ".unsigned.apk", workspace))
    aligned = str(_resolve(Path(out_apk).stem + ".aligned.apk", workspace))

    r1 = _recompile({"src_dir": src_dir, "out_apk": unsigned, "use_aapt2": args.get("use_aapt2", True)}, ctx)
    if r1.get("exit_code") not in (0, None) and r1.get("error"):
        return {"step": "recompile", **r1}
    r2 = _zipalign({"apk": unsigned, "out": aligned}, ctx)
    if r2.get("exit_code") not in (0, None) and r2.get("error"):
        return {"step": "zipalign", **r2}
    r3 = _sign(
        {
            "apk": aligned,
            "out": str(op),
            "keystore": args.get("keystore"),
            "storepass": args.get("storepass"),
            "keypass": args.get("keypass"),
            "alias": args.get("alias"),
        },
        ctx,
    )
    if r3.get("exit_code") not in (0, None) and r3.get("error"):
        return {"step": "sign", **r3}
    return {"final_apk": str(op), "recompile": r1, "zipalign": r2, "sign": r3}


# --------------------------------------------------------------------------- #
# Project build
# --------------------------------------------------------------------------- #


def _detect_project(args: dict[str, Any], ctx) -> dict[str, Any]:
    path = args.get("path", ".")
    p = _resolve(path, ctx.workspace)
    if not p.is_dir():
        return {"error": f"not a directory: {p}"}
    out: dict[str, Any] = {"path": str(p), "kinds": []}
    files = {f.name for f in p.iterdir() if f.is_file()}
    dirs = {d.name for d in p.iterdir() if d.is_dir()}
    if "pubspec.yaml" in files:
        out["kinds"].append("flutter")
    if "package.json" in files and ("android" in dirs or "ios" in dirs):
        out["kinds"].append("react-native")
    if "build.gradle" in files or "build.gradle.kts" in files or "settings.gradle" in files or "settings.gradle.kts" in files:
        out["kinds"].append("gradle-android")
    if (p / "app" / "build.gradle").exists() or (p / "app" / "build.gradle.kts").exists():
        out["kinds"].append("gradle-android")
    if "apktool.yml" in files:
        out["kinds"].append("apktool-project")
    if any(n.endswith(".csproj") or n.endswith(".sln") for n in files):
        out["kinds"].append("xamarin/.net")
    if (p / "Assets").is_dir() and (p / "ProjectSettings").is_dir():
        out["kinds"].append("unity")
    if not out["kinds"]:
        out["kinds"].append("unknown")
    out["files"] = sorted(files)[:200]
    out["dirs"] = sorted(dirs)[:200]
    return out


def _build_project(args: dict[str, Any], ctx) -> dict[str, Any]:
    """Build an APK from a source project. Auto-detect the build system if not provided."""

    project = args.get("project", ".")
    pp = _resolve(project, ctx.workspace)
    if not pp.is_dir():
        return {"error": f"project not a directory: {pp}"}
    kind = args.get("kind")
    if not kind:
        det = _detect_project({"path": str(pp)}, ctx)
        if det.get("kinds"):
            kind = det["kinds"][0]
    flavor = args.get("variant", "release")
    if kind == "flutter":
        flutter = _which("flutter")
        if not flutter:
            return {"error": "flutter not installed. Install Flutter SDK first (see install.sh notes)."}
        argv = [flutter, "build", "apk", f"--{flavor}"]
        return _run(argv, cwd=str(pp), timeout=1800)
    if kind in ("gradle-android", "react-native"):
        android_dir = pp / "android" if (pp / "android").is_dir() else pp
        gradlew = android_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
        if gradlew.exists():
            os.chmod(gradlew, 0o755)
            task = f"assemble{flavor.capitalize()}"
            return _run([str(gradlew), task], cwd=str(android_dir), timeout=1800)
        # Fallback: system gradle
        gradle = _which("gradle")
        if gradle:
            task = f"assemble{flavor.capitalize()}"
            return _run([gradle, task], cwd=str(android_dir), timeout=1800)
        return {"error": "Neither ./gradlew nor system gradle found"}
    if kind == "apktool-project":
        return _recompile({"src_dir": str(pp)}, ctx)
    if kind == "xamarin/.net":
        msbuild = _which("msbuild") or _which("dotnet")
        if not msbuild:
            return {"error": "neither msbuild nor dotnet found"}
        argv = [msbuild] if msbuild.endswith("msbuild") else [msbuild, "build"]
        argv.extend(["-c", flavor.capitalize()])
        return _run(argv, cwd=str(pp), timeout=1800)
    return {"error": f"don't know how to build kind={kind!r} — try kind='gradle-android' or 'flutter'"}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def register(reg, Tool) -> None:
    reg.register(Tool(
        name="detect_apk_type",
        description=(
            "Inspect an APK and report the framework(s) used (Java, Kotlin, Flutter, "
            "React Native, Unity IL2CPP, Xamarin, hermes, native-only), supported ABIs, "
            "and basic package metadata (package name, versionCode, versionName, sdk levels)."
        ),
        parameters={
            "type": "object",
            "properties": {"apk": {"type": "string", "description": "Path to the .apk file."}},
            "required": ["apk"],
        },
        handler=_detect_apk_type,
    ))
    reg.register(Tool(
        name="apk_decompile",
        description="Decompile an APK with apktool. Use `force=true` to overwrite an existing out_dir.",
        parameters={
            "type": "object",
            "properties": {
                "apk": {"type": "string"},
                "out_dir": {"type": "string"},
                "force": {"type": "boolean"},
                "no_src": {"type": "boolean", "description": "Skip smali sources (faster, resource-only)."},
                "no_res": {"type": "boolean", "description": "Skip resources."},
            },
            "required": ["apk"],
        },
        handler=_decompile,
    ))
    reg.register(Tool(
        name="apk_recompile",
        description="Recompile an apktool-decoded project back into an (unsigned) APK.",
        parameters={
            "type": "object",
            "properties": {
                "src_dir": {"type": "string"},
                "out_apk": {"type": "string"},
                "use_aapt2": {"type": "boolean", "description": "Default true."},
            },
            "required": ["src_dir"],
        },
        handler=_recompile,
    ))
    reg.register(Tool(
        name="apk_zipalign",
        description="Run zipalign -p 4 on an APK.",
        parameters={
            "type": "object",
            "properties": {"apk": {"type": "string"}, "out": {"type": "string"}},
            "required": ["apk"],
        },
        handler=_zipalign,
    ))
    reg.register(Tool(
        name="apk_sign",
        description=(
            "Sign an APK with apksigner using the local debug keystore (default: "
            "/var/www/suzu-ai-web/server/keystores/debug.keystore). "
            "Pass `keystore` to override."
        ),
        parameters={
            "type": "object",
            "properties": {
                "apk": {"type": "string"},
                "out": {"type": "string"},
                "keystore": {"type": "string"},
                "storepass": {"type": "string"},
                "keypass": {"type": "string"},
                "alias": {"type": "string"},
            },
            "required": ["apk"],
        },
        handler=_sign,
    ))
    reg.register(Tool(
        name="apk_verify_signature",
        description="Verify the signature of an APK using apksigner.",
        parameters={
            "type": "object",
            "properties": {"apk": {"type": "string"}},
            "required": ["apk"],
        },
        handler=_verify_signature,
    ))
    reg.register(Tool(
        name="apk_aapt_dump",
        description="Run `aapt2 dump <what>` on an APK. `what` defaults to 'badging'.",
        parameters={
            "type": "object",
            "properties": {
                "apk": {"type": "string"},
                "what": {"type": "string", "description": "badging | permissions | resources | strings | xmltree:<file>"},
            },
            "required": ["apk"],
        },
        handler=_aapt_dump,
    ))
    reg.register(Tool(
        name="apk_build_full",
        description="One-shot: recompile + zipalign + sign. Returns the final signed APK path.",
        parameters={
            "type": "object",
            "properties": {
                "src_dir": {"type": "string"},
                "out_apk": {"type": "string"},
                "use_aapt2": {"type": "boolean"},
                "keystore": {"type": "string"},
                "storepass": {"type": "string"},
                "keypass": {"type": "string"},
                "alias": {"type": "string"},
            },
            "required": ["src_dir"],
        },
        handler=_build_full,
    ))
    reg.register(Tool(
        name="detect_project",
        description="Detect the build system of a source project (gradle-android, flutter, react-native, apktool-project, xamarin, unity).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        handler=_detect_project,
    ))
    reg.register(Tool(
        name="build_project",
        description=(
            "Build an APK from a source project. Auto-detects the build system "
            "(gradle, flutter, react-native, apktool, xamarin) when `kind` is not provided. "
            "Variant defaults to 'release'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project root."},
                "kind": {"type": "string", "description": "Override auto-detection."},
                "variant": {"type": "string", "description": "release | debug | profile (Flutter)."},
            },
        },
        handler=_build_project,
    ))
