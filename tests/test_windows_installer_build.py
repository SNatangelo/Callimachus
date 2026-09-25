# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Windows installer builder and prerequisite contract regressions."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = ROOT / "packaging" / "build_windows_installer.py"
    spec = importlib.util.spec_from_file_location("windows_installer_build", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_distribution(dist: Path) -> None:
    executable = dist / "Callimachus" / "Callimachus.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"callimachus executable")
    (dist / "LICENSE").write_text("license", encoding="utf-8")
    notices = dist / "THIRD-PARTY-NOTICES"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")


def test_builder_compiles_dist_to_installer_and_sha256(monkeypatch, tmp_path):
    build = _load_builder()
    dist = tmp_path / "dist"
    _make_distribution(dist)
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build, "DIST", dist)
    monkeypatch.setattr(build, "ARTIFACTS", artifacts)
    monkeypatch.setattr(build, "OUTPUT", artifacts / "Callimachus-Setup.exe")
    monkeypatch.setattr(build.shutil, "which", lambda name: "makensis.exe" if name == "makensis" else None)
    monkeypatch.setattr(build, "_minimum_runtime_version", lambda: "14.44.35211.0")
    calls = []

    def fake_makensis(argv, **kwargs):
        calls.append((argv, kwargs))
        output_define = next(value for value in argv if value.startswith("/DOUTPUT_FILE="))
        Path(output_define.removeprefix("/DOUTPUT_FILE=")).write_bytes(b"NSIS setup")

    monkeypatch.setattr(build.subprocess, "run", fake_makensis)

    result = build.build()

    assert result == artifacts / "Callimachus-Setup.exe"
    assert result.read_bytes() == b"NSIS setup"
    digest = hashlib.sha256(result.read_bytes()).hexdigest()
    assert result.with_name(result.name + ".sha256").read_text(encoding="ascii") == (
        f"{digest}  {result.name}\n"
    )
    argv, kwargs = calls[0]
    assert argv[0] == "makensis.exe"
    assert f"/DDIST_DIR={dist}" in argv
    assert f"/DPACKAGING_DIR={ROOT / 'packaging'}" in argv
    assert f"/DNSIS_LICENSE_FILE={ROOT / 'packaging' / 'NSIS-3.10-LICENSE.txt'}" in argv
    assert "/DVC_RUNTIME_MINIMUM_VERSION=14.44.35211.0" in argv
    assert kwargs["check"] is True
    assert kwargs["cwd"] == ROOT
    assert (dist / "Callimachus" / "Callimachus.exe").is_file()


@pytest.mark.parametrize(
    "filename",
    (
        "msvcp140.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "vc_redist.x64.exe",
        "vcredist_x64.exe",
    ),
)
def test_builder_rejects_bundled_msvc_runtime_or_redistributable(monkeypatch, tmp_path, filename):
    build = _load_builder()
    dist = tmp_path / "dist"
    _make_distribution(dist)
    (dist / "Callimachus" / filename).write_bytes(b"must remain external")
    monkeypatch.setattr(build, "DIST", dist)

    with pytest.raises(RuntimeError, match="must use the installed VC runtime"):
        build._validate_distribution()


def test_builder_rejects_non_windows_invocation(monkeypatch):
    build = _load_builder()
    monkeypatch.setattr(build.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="must be built on Windows"):
        build.build()


def test_minimum_runtime_version_uses_the_externalized_build_dlls(monkeypatch, tmp_path):
    build = _load_builder()
    work = tmp_path / "work" / "Callimachus"
    work.mkdir(parents=True)
    source_paths = {
        "msvcp140.dll": tmp_path / "runtime" / "msvcp140.dll",
        "vcruntime140.dll": tmp_path / "runtime" / "vcruntime140.dll",
        "vcruntime140_1.dll": tmp_path / "runtime" / "vcruntime140_1.dll",
    }
    for path in source_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")
    analysis = [(name, str(path), "BINARY") for name, path in source_paths.items()]
    (work / "Analysis-00.toc").write_text(repr(analysis), encoding="utf-8")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    versions = {
        "msvcp140.dll": (14, 44, 35211, 0),
        "vcruntime140.dll": (14, 42, 34433, 0),
        "vcruntime140_1.dll": (14, 44, 35211, 5),
    }
    monkeypatch.setattr(build, "_file_version", lambda path: versions[path.name])

    assert build._minimum_runtime_version() == "14.44.35211.0"


def test_minimum_runtime_version_includes_pyside_runtime_copy(monkeypatch, tmp_path):
    build = _load_builder()
    work = tmp_path / "work" / "Callimachus"
    work.mkdir(parents=True)
    system = tmp_path / "system"
    wheel = tmp_path / "wheel" / "PySide6"
    system.mkdir()
    wheel.mkdir(parents=True)
    sources = [
        ("msvcp140.dll", system / "msvcp140.dll", "BINARY"),
        ("vcruntime140.dll", system / "vcruntime140.dll", "BINARY"),
        ("vcruntime140_1.dll", system / "vcruntime140_1.dll", "BINARY"),
        ("PySide6\\MSVCP140.dll", wheel / "MSVCP140.dll", "BINARY"),
    ]
    for _, path, _ in sources:
        path.write_bytes(b"runtime")
    (work / "Analysis-00.toc").write_text(repr([
        (destination, str(path), kind) for destination, path, kind in sources
    ]), encoding="utf-8")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build, "_file_version", lambda path: (
        (14, 45, 1, 0) if path.parent == wheel else (14, 44, 35211, 0)
    ))

    assert build._minimum_runtime_version() == "14.45.1.0"


def test_installer_runs_runtime_preflight_before_copying_bundle():
    source = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")
    on_init = source.split("Function .onInit", 1)[1].split("FunctionEnd", 1)[0]
    app_section = source.split('Section "Callimachus" SEC_APP', 1)[1]
    preflight_call = on_init.index("install_vc_runtime.ps1")
    run_check = on_init.index("ExecWait")
    reject_path = on_init.index("SetErrorLevel $PreflightExitCode")

    assert 'RequestExecutionLevel user' in source
    assert '$LOCALAPPDATA\\Programs\\Callimachus' in source
    assert preflight_call < run_check < reject_path
    assert on_init.index('SetOutPath "$PLUGINSDIR"') < preflight_call
    assert 'File /r "${DIST_DIR}\\*"' in app_section
    assert "${DIST_DIR}" in source
    assert "RMDir /r \"$INSTDIR\\Callimachus\"" in source
    assert "RMDir /r \"$INSTDIR\\THIRD-PARTY-NOTICES\"" in source
    assert "RMDir /r \"$INSTDIR\"" not in source
    assert "DisplayVersion" not in source
    assert "NSIS-3.10.txt" in source
    assert '!if "${NSIS_VERSION}" != "v3.10"' in source
    assert "vc_redist" not in source.lower()


def test_installer_creates_and_removes_desktop_shortcut():
    source = (ROOT / "packaging" / "windows-installer.nsi").read_text(encoding="utf-8")
    app = source.split('Section "Callimachus" SEC_APP', 1)[1].split("SectionEnd", 1)[0]
    uninstall = source.split('Section "Uninstall"', 1)[1]
    assert 'CreateShortcut "$DESKTOP\\Callimachus.lnk" "$INSTDIR\\Callimachus\\Callimachus.exe"' in app
    assert 'Delete "$DESKTOP\\Callimachus.lnk"' in uninstall
    assert '!define MUI_ICON "${PACKAGING_DIR}\\assets\\Callimachus.ico"' in source


def test_runtime_preflight_checks_trust_prompts_and_blocks_silent_download():
    source = (ROOT / "packaging" / "install_vc_runtime.ps1").read_text(encoding="utf-8")

    assert 'https://aka.ms/vc14/vc_redist.x64.exe' in source
    assert "Get-AuthenticodeSignature" in source
    assert "SignatureStatus]::Valid" in source
    assert "Microsoft Corporation" in source
    assert "-Verb RunAs -Wait" in source
    assert "[Version]$MinimumRuntimeVersion" in source
    assert "-ge $MinimumRuntimeVersion" in source
    assert "RegistryView]::Registry64" in source
    assert "RegistryView]::Registry32" in source
    assert "if ($Silent)" in source
    assert "exit 15" in source
    assert "Invoke-WebRequest" in source
