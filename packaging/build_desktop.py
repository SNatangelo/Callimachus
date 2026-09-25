# packaging/build_desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build one native standalone Callimachus desktop distribution."""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from collect_notices import (
    EXTERNAL_WINDOWS_RUNTIME, _bundled_binaries, _is_external_windows_runtime,
    add_native_notices, add_qt_attributions, collect, verify_notice_inventory,
)


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "desktop"
DISCOVERED_PACKAGES = (
    "core.parse.citation_schemes",
    "core.parse.extractors",
    "core.parse.format_handlers",
    "core.parse.reference_readers",
    "core.resolve.providers",
    "core.fetch.fallbacks.fetch_modes",
    "core.fetch.http_profiles",
    "core.verify.researchers",
    "core.style",
)


def _discovered_modules() -> tuple[str, ...]:
    """Resolve file-discovered plugins before PyInstaller enters its spec context."""
    modules: set[str] = set()
    for package in DISCOVERED_PACKAGES:
        directory = ROOT.joinpath(*package.split("."))
        if not (directory / "__init__.py").is_file():
            raise RuntimeError(f"discovered plugin package is missing: {directory}")
        modules.add(package)
        for source in directory.rglob("*.py"):
            relative = source.relative_to(ROOT).with_suffix("")
            parts = relative.parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            modules.add(".".join(parts))
    return tuple(sorted(modules))


def _dispatch_modules() -> tuple[str, ...]:
    """Collect the literal run.py dispatch targets for the frozen executable."""
    assignments = {}
    tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"_COMMANDS", "_PIPELINE"}:
                    assignments[target.id] = ast.literal_eval(node.value)
    commands = assignments.get("_COMMANDS")
    pipeline = assignments.get("_PIPELINE")
    if not isinstance(commands, dict) or not isinstance(pipeline, str):
        raise RuntimeError("run.py dispatch table must remain a literal mapping")
    modules = (*commands.values(), pipeline)
    if not all(isinstance(module, str) and module.startswith("core.") for module in modules):
        raise RuntimeError("run.py contains an invalid packaged dispatch target")
    return tuple(sorted(set(modules)))


def _metadata() -> Path:
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5,
    ).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT, text=True, timeout=10,
    ).strip())
    if len(revision) != 40:
        raise RuntimeError("a complete Git commit is required to build a release")
    destination = BUILD_ROOT / "build-metadata.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({
        "revision": revision, "version": os.environ.get("GITHUB_REF_NAME", ""),
        "dirty": dirty,
    }) + "\n", encoding="utf-8")
    return destination


def build_arguments() -> list[str]:
    metadata = _metadata()
    notices_path = BUILD_ROOT / "THIRD-PARTY-NOTICES"
    if notices_path.exists():
        shutil.rmtree(notices_path)
    notices = collect(notices_path)
    data = [
        (ROOT / ".env.example", "."),
        (ROOT / "DEPLOYMENT.md", "."),
        (ROOT / "LICENSE", "."),
        (ROOT / "docs" / "guide", "docs/guide"),
        (ROOT / "docs" / "deployment", "docs/deployment"),
        (ROOT / "docs" / "architecture", "docs/architecture"),
        (ROOT / "core" / "parse" / "parsers.json", "core/parse"),
        (ROOT / "core" / "parse" / "config", "core/parse/config"),
        (ROOT / "core" / "resolve" / "providers.json", "core/resolve"),
        (ROOT / "core" / "resolve" / "providers" / "publisher_routes.json", "core/resolve/providers"),
        (ROOT / "core" / "resolve" / "providers" / "config", "core/resolve/providers/config"),
        (ROOT / "core" / "verify" / "claim_evidence" / "contracts" / "prompts",
         "core/verify/claim_evidence/contracts/prompts"),
        (ROOT / "core" / "gui" / "assets", "core/gui/assets"),
        (ROOT / "core" / "report" / "human" / "assets", "core/report/human/assets"),
        (metadata, "."),
        (notices, "THIRD-PARTY-NOTICES"),
    ]
    for source, _ in data:
        if not source.exists():
            raise RuntimeError(f"required release resource is missing: {source}")
    arguments = [
        "--noconfirm", "--clean", "--onedir", "--name", "Callimachus",
        "--distpath", str(BUILD_ROOT / "dist"),
        "--workpath", str(BUILD_ROOT / "work"),
        "--specpath", str(BUILD_ROOT),
        "--paths", str(ROOT),
        "--exclude-module", "tkinter",
        "--exclude-module", "_tkinter",
        "--collect-all", "rapidocr",
        "--collect-all", "pypdfium2",
        "--collect-all", "onnxruntime",
        "--collect-all", "playwright",
        "--collect-all", "bm25s",
    ]
    for module in _dispatch_modules():
        arguments.extend(("--hidden-import", module))
    for module in _discovered_modules():
        arguments.extend(("--hidden-import", module))
    for source, destination in data:
        arguments.extend(("--add-data", f"{source}{os.pathsep}{destination}"))
    if sys.platform == "darwin":
        arguments.extend(("--windowed", "--osx-bundle-identifier", "science.callimachus.desktop"))
    elif sys.platform == "win32":
        icon = ROOT / "packaging" / "assets" / "Callimachus.ico"
        if not icon.is_file():
            raise RuntimeError(f"Windows application icon is missing: {icon}")
        arguments.extend(("--hide-console", "hide-early", "--icon", str(icon)))
    arguments.append(str(ROOT / "run.py"))
    return arguments


def _externalize_windows_runtime(analysis_toc: Path) -> frozenset[str]:
    if sys.platform != "win32":
        return frozenset()
    bundle = BUILD_ROOT / "dist" / "Callimachus"
    python_root = Path(sys.base_prefix).resolve()
    external: set[str] = set()
    for bundled_name, source in _bundled_binaries(analysis_toc):
        name = PureWindowsPath(bundled_name).name.lower()
        if name not in EXTERNAL_WINDOWS_RUNTIME:
            continue
        if not _is_external_windows_runtime(source, python_root):
            raise RuntimeError(f"unexpected Windows runtime source: {bundled_name} <- {source}")
        external.add(bundled_name.lower())
    if {PureWindowsPath(name).name for name in external} != EXTERNAL_WINDOWS_RUNTIME:
        raise RuntimeError("expected all three Windows runtime DLLs in analysis")
    packaged = [path for path in bundle.rglob("*") if path.is_file()
                and path.name.lower() in EXTERNAL_WINDOWS_RUNTIME]
    if (len(packaged) != len(external)
            or {path.name.lower() for path in packaged} != EXTERNAL_WINDOWS_RUNTIME):
        raise RuntimeError("unexpected Windows runtime DLL layout in the frozen bundle")
    for path in packaged:
        path.unlink()
    return frozenset(external)


def main() -> None:
    cache = BUILD_ROOT / "cache"
    temporary = BUILD_ROOT / "tmp"
    cache.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(cache))
    os.environ.setdefault("TMPDIR", str(temporary))
    from PyInstaller.__main__ import run

    arguments = build_arguments()
    previous_path = os.environ.get("PATH")
    try:
        if sys.platform == "win32":
            windows_root = os.environ.get("SystemRoot")
            if not windows_root:
                raise RuntimeError("SystemRoot is required for a Windows desktop build")
            # Hosted runners expose unrelated JDK/toolchain DLLs through PATH.
            # PyInstaller must discover dependencies from Python and Windows only.
            locations = [Path(sys.executable).parent, Path(sys.base_prefix),
                         Path(windows_root) / "System32", Path(windows_root)]
            os.environ["PATH"] = os.pathsep.join(dict.fromkeys(map(str, locations)))
        run(arguments)
    finally:
        if previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous_path
    notices = BUILD_ROOT / "THIRD-PARTY-NOTICES"
    analysis_toc = BUILD_ROOT / "work" / "Callimachus" / "Analysis-00.toc"
    external_binaries = _externalize_windows_runtime(analysis_toc)
    add_native_notices(notices, analysis_toc, external_binaries)
    add_qt_attributions(notices, analysis_toc)
    verify_notice_inventory(notices)
    distribution = BUILD_ROOT / "dist"
    embedded_notices = list(distribution.rglob("THIRD-PARTY-NOTICES/index.json"))
    if not embedded_notices:
        raise RuntimeError("bundled third-party notice inventory is missing")
    for index in embedded_notices:
        shutil.copytree(notices, index.parent, dirs_exist_ok=True)
        verify_notice_inventory(index.parent)
    packaged_notices = distribution / "THIRD-PARTY-NOTICES"
    if packaged_notices.exists():
        shutil.rmtree(packaged_notices)
    shutil.copytree(
        notices, packaged_notices,
    )
    verify_notice_inventory(packaged_notices)
    shutil.copy2(ROOT / "LICENSE", distribution / "LICENSE")


if __name__ == "__main__":
    main()
