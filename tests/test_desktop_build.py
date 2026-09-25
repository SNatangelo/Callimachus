# tests/test_desktop_build.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native release assembly regressions."""
from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_intel_mac_ocr_dependency_has_platform_specific_pin():
    requirements = (ROOT / "requirements-ocr.txt").read_text(encoding="utf-8")
    constraints = (ROOT / "packaging" / "release-constraints.txt").read_text(encoding="utf-8")
    intel_marker = 'sys_platform == "darwin" and platform_machine == "x86_64"'
    other_marker = 'sys_platform != "darwin" or platform_machine != "x86_64"'

    assert f"onnxruntime>=1.23; {intel_marker}" in requirements
    assert f"onnxruntime>=1.24; {other_marker}" in requirements
    assert f"onnxruntime==1.23.2; {intel_marker}" in constraints
    assert f"onnxruntime==1.29.0; {other_marker}" in constraints


def test_linux_release_runner_installs_qt_platform_libraries():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    assert "if: runner.os == 'Linux'" in workflow
    for package in (
        "libegl1", "libgl1", "libfontconfig1", "libfreetype6", "libgtk-3-0",
        "libxcb-cursor0", "libxcb-icccm4", "libxkbcommon-x11-0",
    ):
        assert package in workflow


def test_release_runner_requires_binary_dependency_wheels():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    assert "pip install --only-binary=:all: -r requirements.txt" in workflow
    assert "pip install --only-binary=:all: pyinstaller==6.22.3 pytest -c packaging/release-constraints.txt" in workflow
    assert 'python-version: "3.13.15"' in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert "python packaging/assemble_sources.py build/source-audit" in workflow


def test_release_runner_builds_and_smokes_windows_installer():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    build = workflow.index("python packaging/build_windows_installer.py")
    smoke = workflow.index("python packaging/smoke_windows_installer.py")
    archive = workflow.index("python packaging/archive_desktop.py")
    assert build < smoke < archive
    assert workflow.count("if: runner.os == 'Windows'") == 2


def test_smoke_reports_qt_failure_before_aggregate_check(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke", "smoke_desktop.py")
    executable = tmp_path / "Callimachus"
    executable.write_bytes(b"executable")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    monkeypatch.setattr(smoke, "executable_path", lambda: executable)
    monkeypatch.setattr(smoke, "check_linux_graphics_bundle", lambda: None)
    calls = []

    def run(argv, **_kwargs):
        calls.append(tuple(argv[1:]))
        if argv[1:2] == ["parse"]:
            debug = Path(argv[argv.index("--debug") + 1])
            debug.parent.mkdir(parents=True, exist_ok=True)
            debug.write_text("parsed", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"n_claims":1}', "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    smoke.main()
    assert calls[:2] == [("--qt-probe",), ("--package-self-test",)]
    assert calls[-1][0] == "parse"


def test_linux_smoke_requires_packaged_xcb_libraries(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_xcb", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    internal = tmp_path / "Callimachus" / "_internal"
    with pytest.raises(SystemExit, match="libqxcb.so"):
        smoke.check_linux_graphics_bundle()

    for name in (
        "PySide6/Qt/plugins/platforms/libqxcb.so",
        *smoke.LINUX_QT_LIBRARIES,
    ):
        target = internal / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"native library")
    smoke.check_linux_graphics_bundle()


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_includes_runtime_and_notice_resources(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build", "build_desktop.py")
    metadata = tmp_path / "build-metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build, "_metadata", lambda: metadata)
    def collect_notices(path):
        path.mkdir()
        return path

    monkeypatch.setattr(build, "collect", collect_notices)
    monkeypatch.setattr(build.sys, "platform", "linux")
    args = build.build_arguments()

    assert args[-1] == str(ROOT / "run.py")
    for package in ("rapidocr", "pypdfium2", "onnxruntime", "playwright", "bm25s"):
        assert args[args.index(package) - 1] == "--collect-all"
    assert args[args.index("core.app.commands.desktop") - 1] == "--hidden-import"
    assert args[args.index("core.app.run") - 1] == "--hidden-import"
    for module in build._discovered_modules():
        assert args[args.index(module) - 1] == "--hidden-import"
    assert "core.parse.citation_schemes.author_year" in build._discovered_modules()
    assert "core.resolve.providers.clinical_trials.ctgov" in build._discovered_modules()
    import run as entrypoint
    assert set(build._dispatch_modules()) == set(entrypoint._COMMANDS.values()) | {entrypoint._PIPELINE}
    assert args[args.index("tkinter") - 1] == "--exclude-module"
    data = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--add-data"]
    for resource in (
        ".env.example", "DEPLOYMENT.md", "docs/guide", "build-metadata.json",
        "THIRD-PARTY-NOTICES", "core/parse/config", "core/resolve/providers.json",
        "core/verify/claim_evidence/contracts/prompts", "core/gui/assets",
        "core/report/human/assets",
    ):
        assert any(resource in value.replace("\\", "/") for value in data)

    monkeypatch.setattr(build.sys, "platform", "win32")
    windows_args = build.build_arguments()
    assert windows_args[windows_args.index("--icon") + 1] == str(
        ROOT / "packaging" / "assets" / "Callimachus.ico"
    )


def test_final_notices_replace_bundled_and_adjacent_indexes(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_finalize", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    notices = tmp_path / "THIRD-PARTY-NOTICES"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    embedded = tmp_path / "dist" / "Callimachus" / "_internal" / "THIRD-PARTY-NOTICES"

    def fake_build(_arguments):
        embedded.mkdir(parents=True)
        (embedded / "index.json").write_text("[]", encoding="utf-8")

    pyinstaller = ModuleType("PyInstaller")
    entry = ModuleType("PyInstaller.__main__")
    entry.run = fake_build
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", entry)
    monkeypatch.setattr(build, "build_arguments", lambda: [])
    monkeypatch.setattr(build, "_externalize_windows_runtime", lambda _toc: frozenset())
    monkeypatch.setattr(build, "add_native_notices", lambda path, _toc, _external: (path / "python.txt").write_text("PSF"))
    monkeypatch.setattr(build, "add_qt_attributions", lambda path, _toc: (path / "index.json").write_text("[1]"))
    checked = []

    def verify(path):
        checked.append(path)
        assert (path / "index.json").read_text(encoding="utf-8") == "[1]"
        assert (path / "python.txt").read_text(encoding="utf-8") == "PSF"

    monkeypatch.setattr(build, "verify_notice_inventory", verify)

    build.main()

    assert (embedded / "index.json").read_text(encoding="utf-8") == "[1]"
    assert (tmp_path / "dist" / "THIRD-PARTY-NOTICES" / "python.txt").read_text() == "PSF"
    assert checked == [notices, embedded, tmp_path / "dist" / "THIRD-PARTY-NOTICES"]


def test_windows_build_ignores_unrelated_runner_toolchain_dlls(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_windows_path", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build, "build_arguments", lambda: ["run.py"])
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build.sys, "base_prefix", str(tmp_path / "python"))
    monkeypatch.setattr(build.sys, "executable", str(tmp_path / "python" / "python.exe"))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setenv("PATH", str(tmp_path / "Java"))
    captured = []
    pyinstaller = ModuleType("PyInstaller")
    entry = ModuleType("PyInstaller.__main__")

    def fake_build(_arguments):
        captured.append(build.os.environ["PATH"])
        raise RuntimeError("stopped after checking PATH")

    entry.run = fake_build
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", entry)
    with pytest.raises(RuntimeError, match="stopped after checking PATH"):
        build.main()
    assert str(tmp_path / "Java") not in captured[0]
    assert str(tmp_path / "python") in captured[0]
    assert str(tmp_path / "Windows" / "System32") in captured[0]
    assert build.os.environ["PATH"] == str(tmp_path / "Java")


def test_windows_bundle_uses_system_vc_runtime(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_external_runtime", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "win32")
    python_root = tmp_path / "python"
    windows_root = tmp_path / "Windows"
    python_root.mkdir()
    (windows_root / "System32").mkdir(parents=True)
    monkeypatch.setattr(build.sys, "base_prefix", str(python_root))
    monkeypatch.setenv("SystemRoot", str(windows_root))
    source_paths = {
        "msvcp140.dll": windows_root / "System32" / "msvcp140.dll",
        "vcruntime140.dll": python_root / "vcruntime140.dll",
        "vcruntime140_1.dll": python_root / "vcruntime140_1.dll",
    }
    bundle = tmp_path / "dist" / "Callimachus" / "_internal"
    bundle.mkdir(parents=True)
    for name, source in source_paths.items():
        source.write_bytes(b"system runtime")
        (bundle / name).write_bytes(b"bundled runtime")
    (bundle / "Qt6Core.dll").write_bytes(b"bundled Qt")
    monkeypatch.setattr(build, "_bundled_binaries", lambda _toc: [
        (name.upper() if name == "msvcp140.dll" else name, source)
        for name, source in sorted(source_paths.items())
    ])

    external = build._externalize_windows_runtime(tmp_path / "Analysis-00.toc")

    assert external == build.EXTERNAL_WINDOWS_RUNTIME
    assert all(not (bundle / name).exists() for name in external)
    assert (bundle / "Qt6Core.dll").is_file()


def test_windows_bundle_excludes_pyside_runtime_copy(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_pyside_runtime", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "win32")
    python_root = tmp_path / "python"
    windows_root = tmp_path / "Windows"
    (windows_root / "System32").mkdir(parents=True)
    wheel = python_root / "Lib" / "site-packages" / "PySide6"
    wheel.mkdir(parents=True)
    monkeypatch.setattr(build.sys, "base_prefix", str(python_root))
    monkeypatch.setenv("SystemRoot", str(windows_root))
    sources = [
        ("msvcp140.dll", windows_root / "System32" / "msvcp140.dll"),
        ("vcruntime140.dll", python_root / "vcruntime140.dll"),
        ("vcruntime140_1.dll", python_root / "vcruntime140_1.dll"),
        ("PySide6\\MSVCP140.dll", wheel / "msvcp140.dll"),
    ]
    bundle = tmp_path / "dist" / "Callimachus" / "_internal"
    bundle.mkdir(parents=True)
    (bundle / "PySide6").mkdir()
    for destination, source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"runtime")
        (bundle / destination.replace("\\", "/")).write_bytes(b"runtime")
    monkeypatch.setattr(build, "_bundled_binaries", lambda _toc: sources)

    excluded = build._externalize_windows_runtime(tmp_path / "Analysis-00.toc")

    assert len(excluded) == 4
    assert "pyside6\\msvcp140.dll" in excluded
    assert not (bundle / "PySide6" / "MSVCP140.dll").exists()
    assert not any(path.name.lower() in build.EXTERNAL_WINDOWS_RUNTIME for path in bundle.rglob("*"))


def test_windows_bundle_rejects_unexpected_runtime_source(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_invalid_runtime", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build, "_bundled_binaries", lambda _toc: [
        ("msvcp140.dll", tmp_path / "vendor-wheel" / "msvcp140.dll"),
    ])
    with pytest.raises(RuntimeError, match="unexpected Windows runtime source"):
        build._externalize_windows_runtime(tmp_path / "Analysis-00.toc")


def test_windows_bundle_rejects_duplicate_runtime_file(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_duplicate_runtime", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build, "_bundled_binaries", lambda _toc: [
        (name, tmp_path / name) for name in build.EXTERNAL_WINDOWS_RUNTIME
    ])
    monkeypatch.setattr(build, "_is_external_windows_runtime", lambda *_args: True)
    bundle = tmp_path / "dist" / "Callimachus" / "_internal"
    bundle.mkdir(parents=True)
    for name in build.EXTERNAL_WINDOWS_RUNTIME:
        (bundle / name).write_bytes(b"runtime")
    duplicate = bundle / "other"
    duplicate.mkdir()
    (duplicate / "vcruntime140.dll").write_bytes(b"other runtime")

    with pytest.raises(RuntimeError, match="unexpected Windows runtime DLL layout"):
        build._externalize_windows_runtime(tmp_path / "Analysis-00.toc")
    assert (duplicate / "vcruntime140.dll").is_file()


def test_archive_keeps_entire_distribution_and_checksum(monkeypatch, tmp_path):
    archiver = _load("desktop_archive", "archive_desktop.py")
    dist = tmp_path / "dist"
    executable = dist / "Callimachus" / "Callimachus"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"executable")
    (dist / "THIRD-PARTY-NOTICES").mkdir()
    (dist / "THIRD-PARTY-NOTICES" / "README.txt").write_text("notices", encoding="utf-8")
    monkeypatch.setattr(archiver, "DIST", dist)
    monkeypatch.setattr(archiver, "ARTIFACTS", tmp_path / "artifacts")

    archive = archiver.archive("linux-x64")
    with tarfile.open(archive) as content:
        names = content.getnames()
    assert "Callimachus/Callimachus" in names
    assert "THIRD-PARTY-NOTICES/README.txt" in names
    assert archive.with_name(archive.name + ".sha256").read_text(encoding="ascii") == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )


def test_archive_rejects_missing_executable(monkeypatch, tmp_path):
    archiver = _load("desktop_archive_missing", "archive_desktop.py")
    monkeypatch.setattr(archiver, "DIST", tmp_path)
    monkeypatch.setattr(archiver, "ARTIFACTS", tmp_path / "artifacts")
    with pytest.raises(RuntimeError, match="built executable is missing"):
        archiver.archive("linux-x64")


@pytest.mark.parametrize("runtime_name", ("VCRUNTIME140_1.DLL", "vc_redist.x64.exe"))
def test_windows_archive_rejects_bundled_vc_runtime(monkeypatch, tmp_path, runtime_name):
    archiver = _load("desktop_archive_external_runtime", "archive_desktop.py")
    dist = tmp_path / "dist"
    bundle = dist / "Callimachus"
    bundle.mkdir(parents=True)
    (bundle / "Callimachus.exe").write_bytes(b"executable")
    (bundle / "_internal").mkdir()
    (bundle / "_internal" / runtime_name).write_bytes(b"runtime")
    monkeypatch.setattr(archiver, "DIST", dist)
    monkeypatch.setattr(archiver, "ARTIFACTS", tmp_path / "artifacts")

    with pytest.raises(RuntimeError, match="Windows runtime files must not be bundled"):
        archiver.archive("windows-x64")
    assert not (tmp_path / "artifacts").exists()


def test_windows_installer_smoke_probes_installed_app(monkeypatch, tmp_path):
    smoke = _load("desktop_installer_smoke", "smoke_windows_installer.py")
    setup = tmp_path / "Callimachus-Setup.exe"
    setup.write_bytes(b"installer")
    install_root = tmp_path / "smoke-install"
    monkeypatch.setattr(smoke, "SETUP", setup)
    monkeypatch.setattr(smoke, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "INSTALL_ROOT", install_root)
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == str(setup):
            executable = install_root / "Callimachus" / "Callimachus.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"app")
            notices = install_root / "THIRD-PARTY-NOTICES"
            notices.mkdir()
            (notices / "index.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    smoke.main()
    assert calls[0] == [str(setup), "/S", f"/D={install_root}"]
    assert calls[1][1:] == ["--qt-probe"]
    assert calls[2][1:] == ["--package-self-test"]


def test_release_sources_are_pinned_to_build_dependencies():
    sources = _load("desktop_sources", "assemble_sources.py")
    entries = sources.source_entries()
    assert {item["name"] for item in entries} == {
        "Qt 6", "Qt 5 (OpenCV)", "PySide6 bindings", "PyMuPDF", "MuPDF",
        "OpenCV Python", "FFmpeg (OpenCV)", "PyInstaller bootloader",
    }
    assert all(item["url"].startswith("https://") for item in entries)


def test_release_source_assembly_checks_bytes_and_writes_hashes(monkeypatch, tmp_path):
    sources = _load("desktop_sources_assembly", "assemble_sources.py")
    body = b"exact upstream source archive"
    item = {
        "name": "Qt", "filename": "qt-source.tar.xz", "version": "6.11.2",
        "constraint": "PySide6", "url": "https://example.test/qt-source.tar.xz",
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(body))
    sources.assemble(tmp_path, [item])
    assert (tmp_path / item["filename"]).read_bytes() == body
    assert (tmp_path / "Callimachus-dependency-sources.json").read_text() == (
        json.dumps([item], indent=2) + "\n"
    )
    assert (tmp_path / "qt-source.tar.xz.sha256").read_text() == (
        f"{item['sha256']}  qt-source.tar.xz\n"
    )


def test_release_source_assembly_rejects_wrong_bytes(monkeypatch, tmp_path):
    sources = _load("desktop_sources_mismatch", "assemble_sources.py")
    item = {
        "name": "Qt", "filename": "qt-source.tar.xz", "version": "6.11.2",
        "constraint": "PySide6", "url": "https://example.test/qt-source.tar.xz",
        "sha256": "0" * 64,
    }
    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"wrong"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        sources.assemble(tmp_path, [item])
    assert not (tmp_path / "qt-source.tar.xz").exists()


def test_ubuntu_source_packages_require_exact_provenance(tmp_path):
    sources = _load("ubuntu_sources_index", "collect_ubuntu_sources.py")
    index = tmp_path / "index.json"
    index.write_text(json.dumps([{
        "directory": "system-packages/libexample-1.0",
        "source_package": "example-source", "source_version": "1.0-1ubuntu1",
    }]), encoding="utf-8")
    assert sources.source_packages(index) == [("example-source", "1.0-1ubuntu1")]
    index.write_text(json.dumps([{
        "directory": "system-packages/libexample-1.0", "source_package": "example-source",
    }]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no exact source package"):
        sources.source_packages(index)


def test_ubuntu_source_archive_contains_downloaded_source_and_hash(monkeypatch, tmp_path):
    sources = _load("ubuntu_sources_archive", "collect_ubuntu_sources.py")
    payload = b"source"
    dsc = (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        "Source: example\nVersion: 1.0\nChecksums-Sha256:\n"
        f" {hashlib.sha256(payload).hexdigest()} {len(payload)} example_1.0.orig.tar.xz\n"
        "Files:\n-----BEGIN PGP SIGNATURE-----\n-----END PGP SIGNATURE-----\n"
    ).encode("utf-8")

    def download(url, **_kwargs):
        return io.BytesIO(dsc if url.endswith("example_1.0.dsc") else payload)

    monkeypatch.setattr(sources.urllib.request, "urlopen", download)
    archive = sources.collect([("example", "1.0")], tmp_path)
    with tarfile.open(archive) as content:
        names = content.getnames()
        manifest = json.load(content.extractfile("ubuntu-system-sources/manifest.json"))
    assert "ubuntu-system-sources/example-1.0/example_1.0.dsc" in names
    assert manifest[0]["package"] == "example"
    assert archive.with_name(archive.name + ".sha256").read_text(encoding="ascii") == (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )


def test_ubuntu_source_download_rejects_wrong_bytes(monkeypatch, tmp_path):
    sources = _load("ubuntu_sources_mismatch", "collect_ubuntu_sources.py")
    dsc = (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        "Source: example\nVersion: 1.0\nChecksums-Sha256:\n"
        f" {hashlib.sha256(b'correct').hexdigest()} 7 example_1.0.orig.tar.xz\n"
        "Files:\n-----BEGIN PGP SIGNATURE-----\n-----END PGP SIGNATURE-----\n"
    ).encode("utf-8")
    monkeypatch.setattr(
        sources.urllib.request, "urlopen",
        lambda url, **_kwargs: io.BytesIO(dsc if url.endswith(".dsc") else b"wrong"),
    )
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        sources.collect([("example", "1.0")], tmp_path)
