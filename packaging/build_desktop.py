# packaging/build_desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build one native standalone Callimachus desktop distribution."""
from __future__ import annotations

import ast
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import importlib.util

from collect_notices import (
    EXTERNAL_WINDOWS_RUNTIME, _bundled_binaries, _is_external_windows_runtime,
    add_native_notices, add_qt_attributions, collect, verify_notice_inventory,
)


# Load the sibling gate even when a caller imports this builder by file path.
_audit_spec = importlib.util.spec_from_file_location(
    "_callimachus_dependency_audit", Path(__file__).with_name("dependency_audit.py"),
)
if _audit_spec is None or _audit_spec.loader is None:
    raise RuntimeError("the dependency security gate is missing")
_dependency_audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(_dependency_audit)
bind_frozen_audit = _dependency_audit.bind_frozen_audit
run_prebuild_audit = _dependency_audit.run_prebuild_audit
current_source_revision = _dependency_audit._revision


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
KEYRING_BACKENDS = {
    "win32": "keyring.backends.Windows",
    "darwin": "keyring.backends.macOS",
    "linux": "keyring.backends.SecretService",
}


def _stage_settings_defaults() -> Path:
    """Stage the source settings inventory without an env-file runtime name."""
    source = ROOT / ".env.example"
    destination = BUILD_ROOT / "settings.defaults"
    if not source.is_file():
        raise RuntimeError(f"required release resource is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def _keyring_backend_module() -> str:
    try:
        return KEYRING_BACKENDS[sys.platform]
    except KeyError as exc:
        raise RuntimeError(
            f"desktop keyring backend is unsupported on {sys.platform}"
        ) from exc


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
    try:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("a non-empty VERSION file is required to build a release") from exc
    if not version:
        raise RuntimeError("a non-empty VERSION file is required to build a release")
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
        "revision": revision, "version": version,
        "dirty": dirty,
    }) + "\n", encoding="utf-8")
    return destination



def _macos_signing_identity() -> str | None:
    """Select a persistent certificate; ad-hoc builds require explicit test opt-in."""
    if sys.platform != "darwin":
        return None
    identity = os.environ.get("CALLIMACHUS_MACOS_SIGNING_IDENTITY", "").strip()
    if identity:
        if not re.fullmatch(r"[0-9A-Fa-f]{40}", identity):
            raise RuntimeError("CALLIMACHUS_MACOS_SIGNING_IDENTITY must be a certificate SHA-1 fingerprint")
        return identity.upper()
    if os.environ.get("CALLIMACHUS_MACOS_ALLOW_ADHOC") == "1":
        return None
    raise RuntimeError(
        "A persistent macOS signing certificate is required. Set "
        "CALLIMACHUS_MACOS_SIGNING_IDENTITY to its SHA-1 fingerprint. "
        "Ad-hoc builds are only available with CALLIMACHUS_MACOS_ALLOW_ADHOC=1 "
        "and do not qualify Keychain access across updates."
    )


def _finalize_macos_bundle() -> None:
    """Set the supported macOS release and reseal the completed app bundle."""
    if sys.platform != "darwin":
        return
    identity = _macos_signing_identity()
    app_bundle = BUILD_ROOT / "dist" / "Callimachus.app"
    info_plist = app_bundle / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as source:
            info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise RuntimeError(f"macOS application metadata is missing or invalid: {info_plist}") from exc
    if not isinstance(info, dict):
        raise RuntimeError(f"macOS application metadata is not a dictionary: {info_plist}")
    info["LSMinimumSystemVersion"] = "14.4"
    with info_plist.open("wb") as destination:
        plistlib.dump(info, destination, fmt=plistlib.FMT_XML, sort_keys=False)
    # V8 in Playwright's Node needs executable memory under hardened runtime.
    # Keep these exceptions on Node, never on the main application or Qt.
    node = app_bundle / "Contents" / "Frameworks" / "playwright" / "driver" / "node"
    if not node.is_file() or not node.resolve().is_relative_to(app_bundle.resolve()):
        raise RuntimeError("the bundled Playwright Node runtime is missing or escapes the app")
    subprocess.run(
        [
            "codesign", "--force", "--sign", identity or "-",
            "--options", "runtime", "--timestamp=none",
            "--entitlements", str(ROOT / "packaging" / "assets" / "macos-playwright-node.entitlements.plist"),
            str(node),
        ],
        check=True, timeout=120,
    )
    subprocess.run(
        [
            "codesign", "--force", "--sign", identity or "-",
            "--preserve-metadata=identifier,entitlements,flags",
            str(app_bundle),
        ],
        check=True, timeout=120,
    )
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app_bundle)],
        check=True, timeout=120,
    )


def _finalize_linux_bundle() -> None:
    """Keep the public Linux command as a libc-checking launcher."""
    if sys.platform != "linux":
        return
    bundle = BUILD_ROOT / "dist" / "Callimachus"
    executable = bundle / "Callimachus"
    internal_executable = bundle / "Callimachus.bin"
    launcher_source = ROOT / "packaging" / "linux-launcher.sh"
    if not executable.is_file():
        raise RuntimeError(f"Linux application executable is missing: {executable}")
    if internal_executable.exists():
        raise RuntimeError(f"Linux internal executable already exists: {internal_executable}")
    if not launcher_source.is_file():
        raise RuntimeError(f"Linux application launcher is missing: {launcher_source}")

    executable.replace(internal_executable)
    internal_executable.chmod(internal_executable.stat().st_mode | 0o111)
    shutil.copyfile(launcher_source, executable)
    executable.chmod(0o755)


def build_arguments() -> list[str]:
    signing_identity = _macos_signing_identity()
    metadata = _metadata()
    notices_path = BUILD_ROOT / "THIRD-PARTY-NOTICES"
    if notices_path.exists():
        shutil.rmtree(notices_path)
    notices = collect(notices_path)
    data = [
        (_stage_settings_defaults(), "."),
        (ROOT / "DEPLOYMENT.md", "."),
        (ROOT / "LICENSE", "."),
        (ROOT / "VERSION", "."),
        (ROOT / "docs" / "README.md", "docs"),
        (ROOT / "docs" / "reference", "docs/reference"),
        (ROOT / "docs" / "user", "docs/user"),
        (ROOT / "docs" / "legal", "docs/legal"),
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
        "--exclude-module", "keyrings.alt",
        "--copy-metadata", "keyring",
        "--collect-all", "rapidocr",
        "--collect-all", "pypdfium2",
        "--collect-all", "onnxruntime",
        "--collect-all", "playwright",
        "--collect-all", "bm25s",
    ]
    for module in _dispatch_modules():
        arguments.extend(("--hidden-import", module))
    arguments.extend(("--hidden-import", _keyring_backend_module()))
    for module in _discovered_modules():
        arguments.extend(("--hidden-import", module))
    for source, destination in data:
        arguments.extend(("--add-data", f"{source}{os.pathsep}{destination}"))
    if sys.platform == "darwin":
        icon = ROOT / "packaging" / "assets" / "Callimachus.icns"
        if not icon.is_file():
            raise RuntimeError(f"macOS application icon is missing: {icon}")
        arguments.extend(("--icon", str(icon)))
        arguments.extend(("--windowed", "--osx-bundle-identifier", "science.callimachus.desktop"))
        if signing_identity is not None:
            arguments.extend(("--codesign-identity", signing_identity))
            if os.environ.get("CALLIMACHUS_MACOS_SELF_SIGNED") == "1":
                entitlements = ROOT / "packaging" / "assets" / "macos-self-signed.entitlements.plist"
                if not entitlements.is_file():
                    raise RuntimeError("self-signed macOS entitlements are missing")
                arguments.extend(("--osx-entitlements-file", str(entitlements)))
    elif sys.platform == "win32":
        icon = ROOT / "packaging" / "assets" / "Callimachus.ico"
        if not icon.is_file():
            raise RuntimeError(f"Windows application icon is missing: {icon}")
        arguments.extend(("--windowed", "--icon", str(icon)))
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
    source_revision = current_source_revision(ROOT)
    dependency_report = run_prebuild_audit(ROOT)
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
    _finalize_macos_bundle()
    _finalize_linux_bundle()
    bind_frozen_audit(ROOT, distribution, dependency_report, source_revision=source_revision)


if __name__ == "__main__":
    main()
