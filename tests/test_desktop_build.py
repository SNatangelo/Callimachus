# tests/test_desktop_build.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Native release assembly regressions."""
from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
import re
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


def test_qt_release_constraints_select_exact_versions_per_platform():
    constraints = (ROOT / "packaging" / "release-constraints.txt").read_text(
        encoding="utf-8"
    )
    expected = {
        "win32": {
            "pyside6": {"6.12.0"}, "pyside6-essentials": {"6.12.0"},
            "pyside6-addons": {"6.12.0"}, "shiboken6": {"6.12.0"},
            "pyside6-pdf": {"6.12.0.140"},
            "pyside6-webengine": {"6.12.0.140"},
        },
        "linux": {
            "pyside6": {"6.12.0"}, "pyside6-essentials": {"6.12.0"},
            "pyside6-addons": {"6.12.0"}, "shiboken6": {"6.12.0"},
            "pyside6-pdf": {"6.12.0.140"},
            "pyside6-webengine": {"6.12.0.140"},
        },
        "darwin": {
            "pyside6": {"6.12.0"}, "pyside6-essentials": {"6.12.0"},
            "pyside6-addons": {"6.12.0"}, "shiboken6": {"6.12.0"},
            "pyside6-pdf": {"6.12.0.140"},
            "pyside6-webengine": {"6.12.0.140"},
        },
    }
    for platform, expected_versions in expected.items():
        active = {}
        for line in constraints.splitlines():
            match = re.fullmatch(
                r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+)(?:;\s*(.*))?",
                line.strip(),
            )
            if not match:
                continue
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name not in expected_versions:
                continue
            marker = match.group(3)
            if marker == 'sys_platform == "darwin"':
                applies = platform == "darwin"
            elif marker == 'sys_platform != "darwin"':
                applies = platform != "darwin"
            else:
                applies = marker is None
            if applies:
                active.setdefault(name, set()).add(match.group(2))
        assert active == expected_versions


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


def test_release_runner_requires_binary_dependency_wheels(tmp_path):
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    bindings = (ROOT / "packaging" / "prepare_macos_bindings.sh").read_text(
        encoding="utf-8"
    )
    runtime_setup = (ROOT / "packaging" / "prepare_macos_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "Install Certificates.command" in runtime_setup
    assert 'PIP_CONSTRAINT="$ROOT/packaging/release-constraints.txt"' in runtime_setup
    assert 'startswith("certifi==")' in runtime_setup
    assert '"stdlib_ca_certifi_version": certifi_version' in runtime_setup
    assert 'printf \'SSL_CERT_FILE=%s\\n\' "$BASE_CA_FILE"' in runtime_setup
    assert "urllib.request.urlopen(\"https://www.python.org/\", timeout=30)" in runtime_setup
    assert "_create_unverified_context" not in runtime_setup

    for name, binary, failure in (
        ("INTERPRETER_MINIMUMS", "$BASE_PYTHON", "interpreter"),
        ("FRAMEWORK_MINIMUMS", "$FRAMEWORK_BINARY", "framework"),
        ("VENV_MINIMUMS", "$RUNTIME_PYTHON", "virtual environment"),
    ):
        assignment = (
            f'{name}="$(minimum_versions "{binary}")" '
            f'|| die "could not validate {failure} macOS minimum"'
        )
        assert assignment in runtime_setup
        assert f"readonly {name}" in runtime_setup
        assert f"readonly {name}=\"$(minimum_versions" not in runtime_setup

    failed_assignment = next(
        line for line in runtime_setup.splitlines()
        if line.startswith('INTERPRETER_MINIMUMS="$(minimum_versions ')
    )
    # The Windows system32 bash.exe is a WSL launcher, so the POSIX failure
    # behavior is executed by Ubuntu CI while Windows still checks the exact
    # fail-closed assignment and readonly follow-up above.
    if os.name != "nt":
        failure_probe = subprocess.run(
            [
                "bash", "-c", "\n".join((
                    "set -u",
                    "BASE_PYTHON=failing-fixture",
                    "minimum_versions() { return 17; }",
                    "die() { printf 'checked failure path\\n'; exit 42; }",
                    failed_assignment,
                    "printf 'unreachable\\n'",
                )),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert failure_probe.returncode == 42
        assert "checked failure path" in failure_probe.stdout
        assert "unreachable" not in failure_probe.stdout

    assert "pip install --only-binary=:all: -r requirements.txt" in workflow
    assert "pip install --only-binary=:all: pyinstaller==6.22.3 pytest -c packaging/release-constraints.txt" in workflow
    assert 'python-version: "3.13.15"' in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert "python packaging/assemble_sources.py build/source-audit" in workflow

    # Keep the stable-ABI build tool at a version whose sysconfig target lets
    # PySide honor the requested 14.4 deployment target on both architectures.
    assert 'python-version: "3.10.11"' in workflow
    assert 'arm64|x86_64) readonly BUILD_PYTHON_VERSION="3.10.11" ;;' in bindings
    assert '[[ "$build_info" == "${BUILD_PYTHON_VERSION}|$TARGET_ARCH" ]]' in bindings
    deployment_target_gate = bindings.index("build_deployment_target=")
    target_version_check = bindings.index('target_key = audit["_macos_version_key"](target)')
    assert 'sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET")' in bindings
    assert "target_key > maximum_key" in bindings
    assert "build Python targets macOS" in bindings
    assert deployment_target_gate < target_version_check < bindings.index("download_pinned() {")
    assert 'check_architecture "$BUILD_PYTHON"' in bindings
    assert 'check_macho "$RUNTIME_PYTHON" no' in bindings
    assert 'readonly MIN_MACOS="14.4"' in bindings
    assert '[[ "$runtime_info" == "3.13.15|$TARGET_ARCH" ]]' in bindings
    assert 'document.load(str(pdf_path)) != QtPdf.QPdfDocument.Error.None_' in bindings
    assert "int(document.load" not in bindings
    assert "document.close()" in bindings

    # Execute the release script's actual Qt 6.12 PDF probe before its macOS
    # dyld-only audit, using a closed-on-disk fixture in this Windows-safe child.
    pdf_probe_marker = (
        'QT_QPA_PLATFORM=offscreen "$RUNTIME_PYTHON" - "$RUNTIME_SITE_PACKAGES" '
        "<<'PY'\n"
    )
    pdf_probe_start = bindings.index(pdf_probe_marker) + len(pdf_probe_marker)
    pdf_probe_end = bindings.index("\ndyld = ctypes.CDLL", pdf_probe_start)
    pdf_probe_source = bindings[pdf_probe_start:pdf_probe_end]
    pdf_probe_source = pdf_probe_source.replace(
        "from PySide6 import QtCore, QtDBus, QtGui, QtNetwork, QtPdf, QtPdfWidgets, QtSvg, QtTest, QtWidgets",
        "from PySide6 import QtCore, QtPdf, QtPdfWidgets, QtWidgets",
    )
    # Keep the fixture outside the child process. Windows may keep a PDF file
    # mapped until process exit, while the production macOS runner permits the
    # inline probe's TemporaryDirectory to clean up before that exit.
    fixture_context = "with tempfile.TemporaryDirectory() as temporary:\n"
    fixture_start = pdf_probe_source.index(fixture_context) + len(fixture_context)
    probe_tail = pdf_probe_source[fixture_start:]
    fixture_lines = (
        '    pdf_path = Path(temporary) / "single-page.pdf"\n'
        "    pdf_path.write_bytes(one_page_pdf())\n"
    )
    assert probe_tail.startswith(fixture_lines)
    probe_body = probe_tail[len(fixture_lines):]
    body_lines = probe_body.splitlines()
    assert all(not line.strip() or line.startswith("    ") for line in body_lines)
    pdf_probe_source = (
        pdf_probe_source[:fixture_start - len(fixture_context)]
        + "pdf_path = Path(sys.argv[2])\n"
        + "pdf_path.write_bytes(one_page_pdf())\n"
        + "\n".join(line[4:] if line else "" for line in body_lines)
        + "\n"
    )
    pyside_site = Path(__import__("PySide6").__file__).resolve().parent.parent
    safe_environment = {
        name: value
        for name, value in os.environ.items()
        if name.casefold() in {
            "path", "systemroot", "windir", "comspec", "pathext",
            "ld_library_path", "home", "userprofile", "temp", "tmp", "tmpdir",
        }
    }
    safe_environment.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONDONTWRITEBYTECODE": "1"})
    pdf_fixture = tmp_path / "single-page.pdf"
    pdf_probe = subprocess.run(
        [sys.executable, "-c", pdf_probe_source, str(pyside_site), str(pdf_fixture)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=safe_environment,
    )
    assert pdf_probe.returncode == 0, pdf_probe.stdout + "\n" + pdf_probe.stderr
    assert "QPdfView loaded one page." in pdf_probe.stdout

    # The app runtime comes from the verified universal2 package before pip
    # runs; setup-python's build-only tool is never restored as the runtime.
    macos_runtime = workflow.index(
        "- name: Install verified macOS application Python 3.13.15"
    )
    dependency_install = workflow.index(
        "- name: Install runtime and build dependencies on the runner"
    )
    pinned_ca = workflow.index(
        "- name: Verify pinned macOS CA bundle for standard-library HTTPS"
    )
    binding_setup = workflow.index("- name: Set up macOS binding build Python")
    binding_build = workflow.index(
        "- name: Build macOS 14.4 PySide bindings from verified source"
    )
    runtime_restore = workflow.index("- name: Restore the application Python runtime")
    package_tests = workflow.index("- name: Test package contracts and desktop paths")
    assert (
        macos_runtime
        < dependency_install
        < pinned_ca
        < binding_setup
        < binding_build
        < runtime_restore
        < package_tests
    )
    assert "if: runner.os != 'macOS'" in workflow
    assert "RUNTIME_PYTHON: ${{ steps.macos-runtime.outputs.python-path }}" in workflow
    ca_step = workflow[pinned_ca:binding_setup]
    assert "if: runner.os == 'macOS'" in ca_step
    assert "BASE_CERTIFI_VERSION: ${{ steps.macos-runtime.outputs.certifi-version }}" in ca_step
    assert 'certifi==([^\\s#]+)' in ca_step
    assert 'os.environ.get("BASE_CERTIFI_VERSION")' in ca_step
    assert '"SSL_CERT_FILE"' in ca_step
    assert 'github_env.open("a"' in ca_step
    assert "urllib.request.urlopen(\"https://www.python.org/\", timeout=30)" in ca_step
    assert "_create_unverified_context" not in ca_step
    restore_block = workflow[runtime_restore:package_tests]
    assert "uses: actions/setup-python@" not in restore_block
    for variable in (
        "pythonLocation=", "Python_ROOT_DIR=", "Python2_ROOT_DIR=",
        "Python3_ROOT_DIR=", "PKG_CONFIG_PATH=",
    ):
        assert variable in restore_block

    assert "GITHUB_ACTIONS" in runtime_setup and "RUNNER_OS" in runtime_setup
    assert "RUNNER_TEMP" in runtime_setup and "uname -s" in runtime_setup
    assert 'readonly PYTHON_VERSION="3.13.15"' in runtime_setup
    assert 'readonly MIN_MACOS="14.4"' in runtime_setup
    assert (
        "https://www.python.org/ftp/python/3.13.15/python-3.13.15-macos11.pkg"
        in runtime_setup
    )
    installer_digest_match = re.search(
        r'^readonly INSTALLER_SHA256="([0-9a-f]+)"$',
        runtime_setup,
        re.MULTILINE,
    )
    assert installer_digest_match is not None
    installer_digest = installer_digest_match.group(1)
    assert len(installer_digest) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", installer_digest)
    assert installer_digest == (
        "3b7eaf7f29825f796e8267024435540ddf1f17fc9a97ad58095daa7a75bfdcd3"
    )
    checksum_validation = runtime_setup.index(
        "/usr/bin/shasum -a 256 --check -"
    )
    package_install = runtime_setup.index("sudo /usr/sbin/installer")
    assert checksum_validation < package_install
    assert "pkgutil --check-signature" in runtime_setup
    assert "Python Software Foundation" in runtime_setup
    assert "BMM5U3QVKW" in runtime_setup
    assert "_macos_minimum_versions" in runtime_setup
    assert "_macos_version_key" in runtime_setup
    assert "otool" in runtime_setup and "14.4" in runtime_setup
    assert "macos-python-runtime-provenance-" in runtime_setup
    assert '>> "$GITHUB_PATH"' in runtime_setup
    assert 'python-path=%s\\n' in runtime_setup
    assert "sudo /usr/sbin/installer" in runtime_setup
    provenance = workflow.split("- name: Add macOS binding build provenance", 1)[1].split(
        "- name:", 1
    )[0]
    assert "macos-python-runtime-provenance-${arch}.json" in provenance
    assert "runtime-python-provenance.json" in provenance


def test_release_runner_builds_and_smokes_windows_installer():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    build = workflow.index("python packaging/build_windows_installer.py")
    smoke = workflow.index("python packaging/smoke_windows_installer.py")
    archive = workflow.index("python packaging/archive_desktop.py")
    assert build < smoke < archive
    for command in (
        "python packaging/build_windows_installer.py",
        "python packaging/smoke_windows_installer.py",
    ):
        matching_steps = [
            step for step in workflow.split("\n      - ") if command in step
        ]
        assert len(matching_steps) == 1
        assert "\n        if: runner.os == 'Windows'\n" in matching_steps[0]


def test_smoke_reports_qt_failure_before_aggregate_check(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke", "smoke_desktop.py")
    executable = tmp_path / "Callimachus"
    executable.write_bytes(b"executable")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("SMOKE_PROBE_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("SMOKE_PROBE_API_KEY", "synthetic-inherited-secret")
    monkeypatch.setenv("CITATION_VERIFIER_SIGNING_KEY", "synthetic-inherited-signing-key")
    monkeypatch.setenv("CITATION_VERIFIER_SIGNING_KEY_FILE", str(tmp_path / "unread-signing-key"))
    monkeypatch.setattr(smoke, "executable_path", lambda: executable)
    monkeypatch.setattr(smoke, "verify_windows_gui_binary", lambda _path: None)
    monkeypatch.setattr(smoke, "check_linux_graphics_bundle", lambda: None)
    monkeypatch.setattr(smoke, "check_macos_minimum_version", lambda: None)
    monkeypatch.setattr(smoke, "create_image_only_pdf", lambda path: path.parent.mkdir(parents=True, exist_ok=True))
    calls = []

    def run(argv, **kwargs):
        calls.append(tuple(argv[1:]))
        assert kwargs["env"]["SMOKE_PROBE_API_KEY"] == ""
        assert kwargs["env"]["CITATION_VERIFIER_SIGNING_KEY"] == ""
        assert "CITATION_VERIFIER_SIGNING_KEY_FILE" not in kwargs["env"]
        if argv[1:2] == ["parse"]:
            debug = Path(argv[argv.index("--debug") + 1])
            debug.parent.mkdir(parents=True, exist_ok=True)
            debug.write_text("parsed", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"n_claims":1}', "")
        if argv[1:2] == ["ocr"] and "--pdf" in argv:
            assert kwargs["env"]["PATH"] == ""
            output = Path(argv[argv.index("--out") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "Callimachus verifies optical character recognition.", encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                argv, 0, '{"ok":true,"method":"rapidocr+pypdfium2"}', ""
            )
        if argv[1:] == ["--package-self-test"]:
            return subprocess.CompletedProcess(
                argv, 0,
                "Library PDF/DOCX round-trip passed; "
                "Historical report export probe passed; GUI shell/settings probe passed",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    monkeypatch.setattr(smoke, "smoke_browser_driver", lambda _environment: None)
    smoke.main()
    assert os.environ["SMOKE_PROBE_API_KEY"] == "synthetic-inherited-secret"
    assert os.environ["CITATION_VERIFIER_SIGNING_KEY"] == "synthetic-inherited-signing-key"
    assert os.environ["CITATION_VERIFIER_SIGNING_KEY_FILE"] == str(tmp_path / "unread-signing-key")
    assert calls[:2] == [("--qt-probe",), ("--package-self-test",)]
    assert calls[-2][0] == "parse"
    assert calls[-1] == ("ocr", "--pdf", str(tmp_path / "build/desktop/smoke-ocr-scan.pdf"),
                         "--out", str(tmp_path / "build/desktop/smoke-ocr-output.txt"),
                         "--dpi", "150")


@pytest.mark.parametrize(
    ("stdout", "missing_marker"),
    [
        pytest.param(
            "Historical report export probe passed; GUI shell/settings probe passed",
            "Library PDF/DOCX round-trip passed",
            id="missing-library-marker",
        ),
        pytest.param(
            "Library PDF/DOCX round-trip passed; GUI shell/settings probe passed",
            "Historical report export probe passed",
            id="missing-history-marker",
        ),
        pytest.param(
            "Library PDF/DOCX round-trip passed; Historical report export probe passed",
            "GUI shell/settings probe passed",
            id="missing-gui-marker",
        ),
    ],
)
def test_smoke_rejects_package_self_test_without_required_marker(
    monkeypatch, tmp_path, stdout, missing_marker,
):
    smoke = _load("desktop_smoke_without_required_marker", "smoke_desktop.py")
    executable = tmp_path / "Callimachus"
    executable.write_bytes(b"executable")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("SMOKE_PROBE_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(smoke, "executable_path", lambda: executable)
    monkeypatch.setattr(smoke, "verify_windows_gui_binary", lambda _path: None)
    monkeypatch.setattr(smoke, "check_linux_graphics_bundle", lambda: None)
    monkeypatch.setattr(smoke, "check_macos_minimum_version", lambda: None)
    monkeypatch.setattr(
        smoke.subprocess, "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout, ""),
    )

    with pytest.raises(SystemExit, match=re.escape(missing_marker)):
        smoke.main()


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


def test_macos_smoke_checks_the_built_application_minimum(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_macos_minimum", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    info_plist = tmp_path / "Callimachus.app" / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    info_plist.write_bytes(smoke.plistlib.dumps({"LSMinimumSystemVersion": "14.4"}))
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus"
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "Load command 0\n      cmd LC_BUILD_VERSION\n    minos 14.4\n      sdk 14.4\n",
            "",
        ),
    )

    smoke.check_macos_minimum_version()

    info_plist.write_bytes(smoke.plistlib.dumps({"LSMinimumSystemVersion": "13.0"}))
    with pytest.raises(SystemExit, match="minimum version must be 14.4"):
        smoke.check_macos_minimum_version()


def test_macos_smoke_rejects_native_binding_with_minimum_26(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_macos_binding_minimum", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "Frameworks" / "QtCore.abi3.so"
    )
    output = (
        "Load command 0\n"
        "      cmd LC_BUILD_VERSION\n"
        "  cmdsize 32\n"
        "  platform MACOS\n"
        "    minos 26.0\n"
        "      sdk 26.0\n"
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    with pytest.raises(SystemExit, match="must be 14.4 or lower"):
        smoke.check_macos_minimum_version()


def test_macos_smoke_accepts_minimum_14_4_with_sdk_26(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_macos_sdk_26", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "Frameworks" / "libshiboken.abi3.so"
    )
    output = (
        "Load command 0\n"
        "      cmd LC_BUILD_VERSION\n"
        "  cmdsize 32\n"
        "  platform MACOS\n"
        "    minos 14.4\n"
        "      sdk 26.0\n"
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    smoke.check_macos_minimum_version()


@pytest.mark.parametrize(
    "magic",
    [
        pytest.param(b"\xfe\xed\xfa\xce", id="macho32-big-endian"),
        pytest.param(b"\xce\xfa\xed\xfe", id="macho32-little-endian"),
        pytest.param(b"\xfe\xed\xfa\xcf", id="macho64-big-endian"),
        pytest.param(b"\xcf\xfa\xed\xfe", id="macho64-little-endian"),
        pytest.param(b"\xca\xfe\xba\xbe", id="fat-big-endian"),
        pytest.param(b"\xbe\xba\xfe\xca", id="fat-little-endian"),
        pytest.param(b"\xca\xfe\xba\xbf", id="fat64-big-endian"),
        pytest.param(b"\xbf\xba\xfe\xca", id="fat64-little-endian"),
    ],
)
def test_macos_smoke_inspects_each_macho_magic(monkeypatch, tmp_path, magic):
    smoke = _load("desktop_smoke_macos_magic", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus",
        magic=magic,
    )
    output = (
        "Load command 0\n"
        "      cmd LC_VERSION_MIN_MACOSX\n"
        "  cmdsize 16\n"
        "  version 14.4\n"
        "      sdk 26.0\n"
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    smoke.check_macos_minimum_version()


def test_macos_smoke_requires_a_native_binary(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_macos_no_binary", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    resource = tmp_path / "Callimachus.app" / "Contents" / "Resources" / "readme.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("not a native binary", encoding="utf-8")

    with pytest.raises(SystemExit, match="contains no native Mach-O binaries"):
        smoke.check_macos_minimum_version()


@pytest.mark.parametrize(
    "failure",
    [pytest.param("missing-tool", id="missing-otool"), pytest.param("nonzero", id="otool-error")],
)
def test_macos_smoke_fails_closed_when_otool_fails(monkeypatch, tmp_path, failure):
    smoke = _load("desktop_smoke_macos_otool_failure", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus"
    )

    def run(argv, **_kwargs):
        if failure == "missing-tool":
            raise FileNotFoundError("otool")
        return subprocess.CompletedProcess(argv, 1, "", "load command inspection failed")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    with pytest.raises(SystemExit, match="otool"):
        smoke.check_macos_minimum_version()


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("", id="no-load-commands"),
        pytest.param(
            "Load command 0\n      cmd LC_BUILD_VERSION\n      sdk 26.0\n",
            id="missing-minimum",
        ),
        pytest.param(
            "Load command 0\n      cmd LC_BUILD_VERSION\n    minos unknown\n",
            id="invalid-minimum",
        ),
    ],
)
def test_macos_smoke_rejects_missing_or_invalid_otool_minimum(
    monkeypatch, tmp_path, output,
):
    smoke = _load("desktop_smoke_macos_invalid_minimum", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    _write_macos_smoke_plist(smoke, tmp_path)
    _write_macos_macho(
        tmp_path / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus"
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, output, ""),
    )

    with pytest.raises(SystemExit, match="minimum version is missing or invalid"):
        smoke.check_macos_minimum_version()


def _write_macos_smoke_plist(smoke, tmp_path):
    info_plist = tmp_path / "Callimachus.app" / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True, exist_ok=True)
    info_plist.write_bytes(smoke.plistlib.dumps({"LSMinimumSystemVersion": "14.4"}))


def _write_macos_macho(path, *, magic=b"\xcf\xfa\xed\xfe"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"test binary contents")
    return path


@pytest.fixture(autouse=True)
def _diagnostic_signing_opt_in(monkeypatch):
    monkeypatch.setenv("CALLIMACHUS_MACOS_ALLOW_ADHOC", "1")
    monkeypatch.delenv("CALLIMACHUS_MACOS_SIGNING_IDENTITY", raising=False)
    monkeypatch.delenv("CALLIMACHUS_MACOS_SELF_SIGNED", raising=False)


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "ref_name",
    [
        pytest.param("Callimachus-v1.1.0", id="branch-ref"),
        pytest.param("v1.1.0", id="tag-ref"),
        pytest.param(None, id="no-ref"),
    ],
)
def test_build_metadata_uses_project_version_for_every_git_ref(
    monkeypatch, tmp_path, ref_name,
):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_metadata", "build_desktop.py")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path / "build")
    (tmp_path / "VERSION").write_text(" 1.1.0 \n", encoding="utf-8")
    if ref_name is None:
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REF_NAME", ref_name)

    git_calls = []

    def check_output(arguments, **kwargs):
        git_calls.append((arguments, kwargs))
        if arguments == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        if arguments == ["git", "status", "--porcelain", "--untracked-files=normal"]:
            return " M tracked.py\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(build.subprocess, "check_output", check_output)
    metadata_path = build._metadata()

    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "revision": "a" * 40,
        "version": "1.1.0",
        "dirty": True,
    }
    assert [arguments for arguments, _ in git_calls] == [
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain", "--untracked-files=normal"],
    ]


@pytest.mark.parametrize("version_contents", [None, "", " \n\t"])
def test_build_metadata_rejects_missing_or_empty_version(
    monkeypatch, tmp_path, version_contents,
):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_metadata_invalid", "build_desktop.py")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path / "build")
    if version_contents is not None:
        (tmp_path / "VERSION").write_text(version_contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-empty VERSION"):
        build._metadata()


def test_manual_source_audit_uploads_verified_archives_without_publishing():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(
        encoding="utf-8"
    )
    source_audit = workflow.split("\n  source-audit:\n", 1)[1].split(
        "\n  release:\n", 1
    )[0]
    assert "if: github.event_name == 'workflow_dispatch'" in source_audit
    assert "run: python packaging/assemble_sources.py build/source-audit" in source_audit
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7" in source_audit
    assert "name: callimachus-dependency-sources" in source_audit
    assert "path: build/source-audit/*" in source_audit
    assert "if-no-files-found: error" in source_audit
    assert "retention-days: 1" in source_audit
    assert "gh release create" not in source_audit

    release = workflow.split("\n  release:\n", 1)[1]
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in release
    assert 'gh release create "$RELEASE_TAG" release/* --verify-tag --notes-file docs/releases/2.0.2.md' in release


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
    assert args[args.index("keyring") - 1] == "--copy-metadata"
    assert args[args.index("keyrings.alt") - 1] == "--exclude-module"
    assert args[args.index("keyring.backends.SecretService") - 1] == "--hidden-import"
    staged_defaults = tmp_path / "settings.defaults"
    assert staged_defaults.read_bytes() == (ROOT / ".env.example").read_bytes()
    import run as entrypoint
    assert set(build._dispatch_modules()) == set(entrypoint._COMMANDS.values()) | {entrypoint._PIPELINE}
    assert args[args.index("tkinter") - 1] == "--exclude-module"
    data = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--add-data"]
    for resource in (
        "settings.defaults", "DEPLOYMENT.md", "docs/README.md", "docs/reference", "docs/user", "docs/legal", "build-metadata.json",
        "THIRD-PARTY-NOTICES", "core/parse/config", "core/resolve/providers.json",
        "core/verify/claim_evidence/contracts/prompts", "core/gui/assets",
        "core/report/human/assets",
    ):
        assert any(resource in value.replace("\\", "/") for value in data)
    assert not any(".env" in value for value in data)

    monkeypatch.setattr(build.sys, "platform", "win32")
    windows_args = build.build_arguments()
    assert "--windowed" in windows_args
    assert "--hide-console" not in windows_args
    assert windows_args[windows_args.index("keyring.backends.Windows") - 1] == "--hidden-import"
    assert windows_args[windows_args.index("--icon") + 1] == str(
        ROOT / "packaging" / "assets" / "Callimachus.ico"
    )

    monkeypatch.setattr(build.sys, "platform", "darwin")
    macos_args = build.build_arguments()
    assert macos_args[macos_args.index("keyring.backends.macOS") - 1] == "--hidden-import"
    assert macos_args[macos_args.index("--icon") + 1] == str(
        ROOT / "packaging" / "assets" / "Callimachus.icns"
    )
    assert "--windowed" in macos_args


def test_macos_build_rejects_missing_application_icon(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_missing_macos_icon", "build_desktop.py")
    metadata = tmp_path / "build-metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build, "_metadata", lambda: metadata)
    monkeypatch.setattr(build, "collect", lambda path: path.mkdir() or path)
    monkeypatch.setattr(build.sys, "platform", "darwin")
    icon_path = ROOT / "packaging" / "assets" / "Callimachus.icns"
    original_is_file = Path.is_file

    def is_file(path):
        if path == icon_path:
            return False
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    with pytest.raises(RuntimeError, match="macOS application icon is missing"):
        build.build_arguments()


def test_macos_application_icon_is_valid_multisize_icns():
    import struct

    icon = (ROOT / "packaging" / "assets" / "Callimachus.icns").read_bytes()
    assert icon[:4] == b"icns"
    assert struct.unpack(">I", icon[4:8])[0] == len(icon)

    entries = {}
    offset = 8
    while offset < len(icon):
        kind = icon[offset:offset + 4]
        length = struct.unpack(">I", icon[offset + 4:offset + 8])[0]
        assert length >= 8
        entries[kind] = icon[offset + 8:offset + length]
        offset += length
    assert offset == len(icon)
    assert {b"ic07", b"ic08", b"ic09"} <= entries.keys()
    for kind, size in ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512)):
        png = entries[kind]
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == (size, size)


def test_final_notices_and_public_linux_launcher_are_assembled(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_finalize", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "linux")
    monkeypatch.setattr(build, "_finalize_macos_bundle", lambda: None)
    notices = tmp_path / "THIRD-PARTY-NOTICES"
    notices.mkdir()
    (notices / "index.json").write_text("[]", encoding="utf-8")
    embedded = tmp_path / "dist" / "Callimachus" / "_internal" / "THIRD-PARTY-NOTICES"
    events = []
    dependency_report = {"status": "passed"}
    monkeypatch.setattr(
        build, "current_source_revision",
        lambda current_root: events.append(("source-revision", current_root)) or "a" * 40,
    )
    monkeypatch.setattr(
        build, "run_prebuild_audit",
        lambda current_root: events.append(("audit", current_root)) or dependency_report,
    )

    def bind_audit(current_root, distribution, report, *, source_revision):
        assert source_revision == "a" * 40
        assert report is dependency_report
        assert (distribution / "LICENSE").is_file()
        assert (distribution / "THIRD-PARTY-NOTICES" / "index.json").is_file()
        events.append(("bind", current_root, distribution))

    monkeypatch.setattr(build, "bind_frozen_audit", bind_audit)

    def fake_build(_arguments):
        events.append(("pyinstaller",))
        executable = tmp_path / "dist" / "Callimachus" / "Callimachus"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"PyInstaller executable")
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
    assert events == [
        ("source-revision", build.ROOT),
        ("audit", build.ROOT),
        ("pyinstaller",),
        ("bind", build.ROOT, tmp_path / "dist"),
    ]
    bundle = tmp_path / "dist" / "Callimachus"
    public_launcher = bundle / "Callimachus"
    assert public_launcher.read_bytes() == (ROOT / "packaging" / "linux-launcher.sh").read_bytes()
    assert (bundle / "Callimachus.bin").read_bytes() == b"PyInstaller executable"
    if build.os.name == "posix":
        assert public_launcher.stat().st_mode & 0o111


def test_macos_bundle_sets_minimum_and_reseals_after_resources(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_macos_metadata", "build_desktop.py")
    monkeypatch.setenv("CALLIMACHUS_MACOS_SIGNING_IDENTITY", "A" * 40)
    app_bundle = tmp_path / "dist" / "Callimachus.app"
    info_plist = app_bundle / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    info_plist.write_bytes(build.plistlib.dumps({"CFBundleIdentifier": "example.app"}))
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(
        build.subprocess, "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    node = app_bundle / "Contents/Frameworks/playwright/driver/node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"synthetic node")
    build._finalize_macos_bundle()

    info = build.plistlib.loads(info_plist.read_bytes())
    assert info["CFBundleIdentifier"] == "example.app"
    assert info["LSMinimumSystemVersion"] == "14.4"
    assert calls == [
        (
            ["codesign", "--force", "--sign", "A" * 40,
             "--options", "runtime", "--timestamp=none",
             "--entitlements", str(ROOT / "packaging/assets/macos-playwright-node.entitlements.plist"),
             str(node)],
            {"check": True, "timeout": 120},
        ),
        (
            [
                "codesign", "--force", "--sign", "A" * 40,
                "--preserve-metadata=identifier,entitlements,flags",
                str(app_bundle),
            ],
            {"check": True, "timeout": 120},
        ),
        (
            ["codesign", "--verify", "--deep", "--strict", str(app_bundle)],
            {"check": True, "timeout": 120},
        ),
    ]


def test_non_macos_bundle_finalization_does_not_change_resources(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_non_macos_metadata", "build_desktop.py")
    monkeypatch.setattr(build, "BUILD_ROOT", tmp_path)
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected signing"))

    build._finalize_macos_bundle()


def test_windows_build_ignores_unrelated_runner_toolchain_dlls(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_windows_path", "build_desktop.py")
    events = []
    monkeypatch.setattr(
        build, "current_source_revision",
        lambda current_root: events.append(("source-revision", current_root)) or "a" * 40,
    )
    monkeypatch.setattr(
        build, "run_prebuild_audit",
        lambda current_root: events.append(("audit", current_root)) or {"status": "passed"},
    )
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
        events.append(("pyinstaller",))
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
    assert events == [
        ("source-revision", build.ROOT), ("audit", build.ROOT), ("pyinstaller",),
    ]


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
    verified = []
    monkeypatch.setattr(
        archiver, "verify_frozen_dependencies",
        lambda root, distribution: verified.append((root, distribution)),
    )

    archive = archiver.archive("linux-x64")
    with tarfile.open(archive) as content:
        names = content.getnames()
    assert "Callimachus/Callimachus" in names
    assert "THIRD-PARTY-NOTICES/README.txt" in names
    checksum = archive.with_name(archive.name + ".sha256").read_bytes()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum == f"{digest}  {archive.name}\n".encode("utf-8")
    assert len(digest) == 64
    assert all(byte in b"0123456789abcdef" for byte in digest.encode("ascii"))
    assert checksum.count(b"\n") == 1 and b"\r" not in checksum

    windows_executable = executable.with_name("Callimachus.exe")
    windows_executable.write_bytes(b"windows executable")
    windows_archive = archiver.archive("windows-x64")
    windows_checksum_path = windows_archive.with_name(windows_archive.name + ".sha256")
    windows_checksum = windows_checksum_path.read_bytes()
    windows_digest = hashlib.sha256(windows_archive.read_bytes()).hexdigest()
    windows_expected_checksum = f"{windows_digest}  {windows_archive.name}\n".encode("utf-8")
    assert windows_checksum == windows_expected_checksum
    assert len(windows_digest) == 64
    assert all(byte in b"0123456789abcdef" for byte in windows_digest.encode("ascii"))
    assert windows_checksum.count(b"\n") == 1 and b"\r" not in windows_checksum
    assert verified == [(archiver.ROOT, dist), (archiver.ROOT, dist)]


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
    monkeypatch.setattr(smoke, "verify_windows_gui_binary", lambda _path: None)
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


def test_frozen_ocr_smoke_rejects_external_backend(monkeypatch, tmp_path):
    smoke = _load("desktop_smoke_external_ocr", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    executable = tmp_path / "Callimachus"
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv[1:]), kwargs))
        output = Path(argv[argv.index("--out") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("Callimachus optical character recognition", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, '{"ok":true,"method":"tesseract"}', ""
        )

    monkeypatch.setattr(smoke, "create_image_only_pdf", lambda path: path.parent.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(smoke.subprocess, "run", run)
    with pytest.raises(SystemExit, match="bundled backend"):
        smoke.smoke_frozen_ocr(executable, {"PATH": "system-tools"})
    arguments, kwargs = calls[0]
    assert arguments[0] == "ocr"
    assert "--help" not in arguments
    assert kwargs["env"]["PATH"] == ""
    assert kwargs["timeout"] == 180


def test_release_sources_are_pinned_to_build_dependencies():
    sources = _load("desktop_sources", "assemble_sources.py")
    entries = sources.source_entries()
    qt_sources = {
        item["name"]: item for item in entries
        if item["name"] in {"Qt 6", "PySide6 bindings"}
    }
    assert {item["name"] for item in entries} == {
        "Qt 6", "Qt 5 (OpenCV)", "PySide6 bindings", "PyMuPDF", "MuPDF",
        "OpenCV Python", "FFmpeg (OpenCV)", "PyInstaller bootloader",
    }
    assert all(item["url"].startswith("https://") for item in entries)
    assert {
        item["name"]: (
            item["constraint"], item["package_version"], item["version"],
            item["filename"], item["url"], item["sha256"],
        )
        for item in qt_sources.values()
    } == {
        "Qt 6": (
            "PySide6", "6.12.0", "6.12.0", "qt-everywhere-src-6.12.0.tar.xz",
            (
                "https://download.qt.io/official_releases/qt/6.12/6.12.0/single/"
                "qt-everywhere-src-6.12.0.tar.xz"
            ),
            "98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165",
        ),
        "PySide6 bindings": (
            "PySide6", "6.12.0", "6.12.0",
            "pyside-setup-everywhere-src-6.12.0.tar.xz",
            (
                "https://download.qt.io/official_releases/QtForPython/pyside6/"
                "PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz"
            ),
            "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751",
        ),
    }


def test_release_source_validator_rejects_unpinned_source_version(tmp_path):
    sources = _load("desktop_sources_all_pins", "assemble_sources.py")
    pins = sources._pinned_versions(ROOT / "packaging" / "release-constraints.txt")

    assert pins["pyside6"] == frozenset({"6.12.0"})
    assert pins["pyside6-pdf"] == frozenset({"6.12.0.140"})
    assert pins["pyside6-webengine"] == frozenset({"6.12.0.140"})

    manifest = tmp_path / "release-sources.json"
    entries = json.loads((ROOT / "packaging" / "release-sources.json").read_text())
    next(item for item in entries if item["name"] == "Qt 6")["package_version"] = "6.12.1"
    manifest.write_text(json.dumps(entries), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid or unpinned release source"):
        sources.source_entries(manifest=manifest)


def test_release_source_validator_rejects_orphan_source(tmp_path):
    sources = _load("desktop_sources_orphan", "assemble_sources.py")
    manifest = tmp_path / "release-sources.json"
    entries = json.loads((ROOT / "packaging" / "release-sources.json").read_text())
    orphan = entries[0].copy()
    orphan["name"] = "Unexpected Qt source"
    orphan["filename"] = "unexpected-qt-source-6.12.0.tar.xz"
    entries.append(orphan)
    manifest.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(RuntimeError, match="release sources require"):
        sources.source_entries(manifest=manifest)


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


@pytest.mark.parametrize("value", ["", "-", "not-a-certificate", "A" * 39, "A" * 41])
def test_macos_production_signing_requires_persistent_certificate(monkeypatch, value):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_signing_required", "build_desktop.py")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.delenv("CALLIMACHUS_MACOS_ALLOW_ADHOC", raising=False)
    monkeypatch.setenv("CALLIMACHUS_MACOS_SIGNING_IDENTITY", value)
    with pytest.raises(RuntimeError):
        build._macos_signing_identity()


def test_macos_stable_identity_is_forwarded_into_nested_code_signing(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_signing_nested", "build_desktop.py")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setenv("CALLIMACHUS_MACOS_SIGNING_IDENTITY", "ab" * 20)
    monkeypatch.setattr(build, "_metadata", lambda: ROOT / "VERSION")
    monkeypatch.setattr(build, "collect", lambda *_args: ROOT / "packaging")
    monkeypatch.setattr(build, "_stage_settings_defaults", lambda: ROOT / ".env.example")
    args = build.build_arguments()
    assert args[args.index("--codesign-identity") + 1] == "AB" * 20
    assert args[args.index("--osx-bundle-identifier") + 1] == "science.callimachus.desktop"


@pytest.mark.parametrize("self_signed", [False, True])
def test_self_signed_library_exception_is_explicit_and_narrow(monkeypatch, self_signed):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_signing_entitlements", "build_desktop.py")
    monkeypatch.setattr(build.sys,"platform","darwin")
    monkeypatch.setenv("CALLIMACHUS_MACOS_SIGNING_IDENTITY","ab"*20)
    monkeypatch.setenv("CALLIMACHUS_MACOS_SELF_SIGNED","1" if self_signed else "0")
    monkeypatch.setattr(build,"_metadata",lambda:ROOT/"VERSION")
    monkeypatch.setattr(build,"collect",lambda *_:ROOT/"packaging")
    monkeypatch.setattr(build,"_stage_settings_defaults",lambda:ROOT/".env.example")
    args=build.build_arguments()
    assert ("--osx-entitlements-file" in args) == self_signed
    if self_signed:
        p=Path(args[args.index("--osx-entitlements-file")+1])
        assert build.plistlib.loads(p.read_bytes()) == {"com.apple.security.cs.disable-library-validation":True}


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("status", [0, -5])
def test_browser_driver_smoke_runs_bundled_javascript(monkeypatch, tmp_path, platform, status):
    smoke = _load("browser_driver_smoke", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    monkeypatch.setattr(smoke.sys, "platform", platform)
    bundle = tmp_path / ("Callimachus.app" if platform == "darwin" else "Callimachus")
    driver = bundle / ("Contents/Frameworks" if platform == "darwin" else "_internal") / "playwright/driver"
    (driver / "package").mkdir(parents=True)
    node = driver / ("node.exe" if platform == "win32" else "node")
    node.write_bytes(b"node")
    (driver / "package/cli.js").write_text("synthetic cli", encoding="utf-8")
    (driver / "package/package.json").write_text('{"version":"1.62.0"}', encoding="utf-8")
    def run(command, **kwargs):
        assert command == [str(node), str(driver / "package/cli.js"), "--version"]
        assert "NODE_OPTIONS" not in kwargs["env"]
        assert "NODE_PATH" not in kwargs["env"]
        return subprocess.CompletedProcess(command, status, "Version 1.62.0\n" if status == 0 else "", "")
    monkeypatch.setattr(smoke.subprocess, "run", run)
    if status:
        with pytest.raises(SystemExit, match="frozen browser driver failed"):
            smoke.smoke_browser_driver({"NODE_OPTIONS":"--require=untrusted.js"})
    else:
        smoke.smoke_browser_driver({"NODE_OPTIONS":"--require=untrusted.js"})


def test_browser_driver_smoke_rejects_missing_runtime(monkeypatch, tmp_path):
    smoke = _load("browser_driver_missing_smoke", "smoke_desktop.py")
    monkeypatch.setattr(smoke, "DIST", tmp_path)
    with pytest.raises(SystemExit, match="browser driver is missing"):
        smoke.smoke_browser_driver({})


def test_desktop_dependency_audit_blocks_before_pyinstaller(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_audit_block", "build_desktop.py")
    tool_calls = []

    def blocked(_root):
        raise RuntimeError("dependency audit blocked")

    monkeypatch.setattr(build, "current_source_revision", lambda _root: "a" * 40)
    monkeypatch.setattr(build, "run_prebuild_audit", blocked)
    pyinstaller = ModuleType("PyInstaller")
    entry = ModuleType("PyInstaller.__main__")
    entry.run = lambda *_args: tool_calls.append("PyInstaller")
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", entry)

    with pytest.raises(RuntimeError, match="dependency audit blocked"):
        build.main()

    assert tool_calls == []




def test_desktop_rejects_source_before_audit_or_pyinstaller(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    build = _load("desktop_build_dirty_source_block", "build_desktop.py")
    events = []

    def reject_source(_root):
        events.append("source")
        raise RuntimeError("clean source Git checkout required")

    monkeypatch.setattr(build, "current_source_revision", reject_source)
    monkeypatch.setattr(build, "run_prebuild_audit", lambda _root: events.append("audit"))
    pyinstaller = ModuleType("PyInstaller")
    entry = ModuleType("PyInstaller.__main__")
    entry.run = lambda *_args: events.append("PyInstaller")
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", entry)

    with pytest.raises(RuntimeError, match="clean source Git checkout"):
        build.main()

    assert events == ["source"]


def test_archive_dependency_audit_blocks_before_archive_creation(monkeypatch, tmp_path):
    archiver = _load("desktop_archive_audit_block", "archive_desktop.py")
    dist = tmp_path / "dist"
    executable = dist / "Callimachus" / "Callimachus"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"executable")
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(archiver, "DIST", dist)
    monkeypatch.setattr(archiver, "ARTIFACTS", artifacts)
    calls = []

    def blocked(root, distribution):
        calls.append((root, distribution))
        raise RuntimeError("fresh frozen audit blocked")

    monkeypatch.setattr(archiver, "verify_frozen_dependencies", blocked)

    with pytest.raises(RuntimeError, match="fresh frozen audit blocked"):
        archiver.archive("linux-x64")

    assert calls == [(archiver.ROOT, dist)]
    assert not artifacts.exists()


def test_macos_pyside_overlay_changes_only_the_verified_setuptools_pin(tmp_path):
    overlay_helper = _load("macos_pyside_requirements_overlay", "patch_macos_pyside_requirements.py")
    upstream = b"""# Build dependencies
setuptools==78.1.0
packaging==24.2
build==1.2.2.post1
wheel==0.46.3
distro==1.9.0; sys_platform == 'linux'
patchelf==0.17.2; sys_platform == 'linux'
# 2.2.0 is the last version that supports Python 3.10
numpy==2.2.0; python_version == '3.10'
numpy==2.4.6; python_version > '3.10'
mypy>=1.15.0    # note: 3.13 with disable-gil is not compiled yet
"""
    assert overlay_helper.sha256(upstream) == overlay_helper.SOURCE_SHA256
    overlay = overlay_helper.render_overlay(upstream)
    assert overlay == upstream.replace(b"setuptools==78.1.0", b"setuptools==84.1.0", 1)

    source_path = tmp_path / "requirements.txt"
    output_path = tmp_path / "requirements-overlay.txt"
    manifest_path = tmp_path / "requirements-overlay.json"
    source_path.write_bytes(upstream)
    manifest = overlay_helper.create_overlay(source_path, output_path, manifest_path)
    assert source_path.read_bytes() == upstream
    assert output_path.read_bytes() == overlay
    assert manifest["source_sha256"] == overlay_helper.SOURCE_SHA256
    assert manifest["overlay_sha256"] == overlay_helper.sha256(overlay)
    assert manifest["other_bytes_unchanged"] is True


def test_macos_pyside_overlay_fails_closed_on_changed_or_duplicate_upstream_pins():
    overlay_helper = _load("macos_pyside_requirements_overlay_guard", "patch_macos_pyside_requirements.py")
    upstream = b"setuptools==78.1.0\n"
    with pytest.raises(ValueError, match="SHA-256"):
        overlay_helper.render_overlay(upstream)
    duplicate = b"setuptools==78.1.0\nsetuptools==78.1.0\n"
    with pytest.raises(ValueError, match="exactly one"):
        overlay_helper.render_overlay(duplicate, overlay_helper.sha256(duplicate))


def test_macos_pyside_installer_uses_the_overlay_for_its_second_install():
    script = (ROOT / "packaging" / "prepare_macos_bindings.sh").read_text(encoding="utf-8")
    overlay_call = script.index("patch_macos_pyside_requirements.py")
    install_start = script.index('"$BUILD_VENV/bin/python" -m pip install \\\n    -c "$ROOT/packaging/release-constraints.txt" -r "$SOURCE_REQUIREMENTS_OVERLAY"')
    raw_requirements = script.index('"$SOURCE_DIR/requirements.txt"', overlay_call)
    assert overlay_call < raw_requirements < install_start
    assert "setuptools==84.1.0 wheel==0.46.3" in script
    assert "build_requirements_overlay_sha256" in script


def test_macos_aqt_commands_run_from_work_root(tmp_path):
    script = (ROOT / "packaging" / "prepare_macos_bindings.sh").read_text(encoding="utf-8")
    aqt_blocks = re.findall(
        r'(?ms)^\(\n    cd -- "\$WORK_ROOT"\n'
        r'    "\$AQT_VENV/bin/aqt" .*?^\)',
        script,
    )
    assert len(aqt_blocks) == 2
    assert "list-qt" in aqt_blocks[0]
    assert "install-qt" in aqt_blocks[1]

    if os.name == "nt":
        pytest.skip("Bash working-directory fixture runs on POSIX release runners")

    repo = tmp_path / "checkout"
    work_root = tmp_path / "runner-temp"
    fake_bin = tmp_path / "aqt-venv" / "bin"
    repo.mkdir()
    work_root.mkdir()
    fake_bin.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    aqt = fake_bin / "aqt"
    aqt.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\t%s\\n' "$PWD" "$*" >> "$AQT_TRACE"
printf 'fixture log\\n' >> aqtinstall.log
if [[ "$1" == list-qt ]]; then printf 'qtbase qtsvg\\n'; fi
""",
        encoding="utf-8",
    )
    aqt.chmod(0o755)
    trace = tmp_path / "aqt-invocations.txt"
    env = os.environ.copy()
    env.update({
        "AQT_VENV": str(aqt.parent.parent),
        "AQT_TRACE": str(trace),
        "WORK_ROOT": str(work_root),
        "QT_ROOT": str(tmp_path / "qt-root"),
        "TARGET_VERSION": "6.12.0",
    })
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", "\n".join(aqt_blocks)],
        cwd=repo,
        env=env,
        check=True,
    )

    calls = trace.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all(line.startswith(str(work_root) + "\t") for line in calls)
    assert calls[0].split("\t", 1)[1].startswith("list-qt ")
    assert calls[1].split("\t", 1)[1].startswith("install-qt ")
    assert (work_root / "aqtinstall.log").is_file()
    assert (work_root / "qt-archives.txt").read_text(encoding="utf-8") == "qtbase qtsvg\n"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == ""
    assert not (repo / "aqtinstall.log").exists()


def test_static_openssl_license_is_added_with_valid_cryptography_wheel_record(tmp_path):
    import base64
    import csv
    import io
    import zipfile

    helper = _load("macos_cryptography_wheel_helper", "prepare_macos_cryptography.py")
    wheel = tmp_path / "cryptography-50.0.1-cp39-abi3-macosx_14_4_x86_64.whl"
    dist_info = "cryptography-50.0.1.dist-info"
    members = {
        f"{dist_info}/METADATA": b"Name: cryptography\nVersion: 50.0.1\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\n",
        "cryptography/hazmat/bindings/_rust.abi3.so": b"extension fixture",
    }
    rows = []
    for name, body in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")
        rows.append([name, "sha256=" + digest, str(len(body))])
    record = f"{dist_info}/RECORD"
    rows.append([record, "", ""])
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
        archive.writestr(record, stream.getvalue())

    helper.verify_wheel_record(wheel)
    license_body = b"Apache License\nVersion 2.0\nOpenSSL 3.5.9 notice fixture\n"
    helper.add_openssl_license(wheel, license_body)
    helper.verify_wheel_record(wheel)
    with zipfile.ZipFile(wheel) as archive:
        license_name = f"{dist_info}/licenses/OpenSSL-LICENSE.txt"
        assert archive.read(license_name) == license_body
        assert record in archive.namelist()


def test_macos_cryptography_failure_logs_preserve_both_streams(tmp_path, monkeypatch):
    helper = _load("macos_cryptography_failure_diagnostics", "prepare_macos_cryptography.py")
    stdout = (
        "OpenSSL compile output begins\n"
        + "stdout-noise\n" * 2_000
        + "FATAL OPENSSL DIAGNOSTIC SENTINEL\n"
        + "stdout-noise\n" * 2_000
        + "OpenSSL stdout tail\n"
    )
    stderr = (
        "ranlib: file: libcrypto.a(member.o) has no symbols\n" * 4_000
        + "OpenSSL stderr tail sentinel\n"
    )
    command = ["/usr/bin/make", "-j2", "install_sw"]

    def fail_make(args, **_kwargs):
        raise subprocess.CalledProcessError(2, args, output=stdout, stderr=stderr)

    monkeypatch.setattr(helper.subprocess, "run", fail_make)
    log_dir = tmp_path / "macos-cryptography-failure-logs"

    with pytest.raises(RuntimeError, match="command failed") as error:
        helper.run(command, failure_log_dir=log_dir)

    message = str(error.value)
    assert "FATAL OPENSSL DIAGNOSTIC SENTINEL" in message
    assert "OpenSSL stderr tail sentinel" in message
    assert "stdout (bounded excerpt):" in message
    assert "stderr (bounded excerpt):" in message
    assert len(message) < 20_000
    assert (log_dir / "openssl-make.stdout.log").read_text(encoding="utf-8") == stdout
    assert (log_dir / "openssl-make.stderr.log").read_text(encoding="utf-8") == stderr
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)
    assert error.value.__cause__.returncode == 2


def test_macos_cryptography_failure_logs_are_uploaded_only_for_failed_macos_jobs():
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
    start = workflow.index("- name: Upload macOS cryptography build failure logs")
    end = workflow.index("\n      - uses: actions/upload-artifact@", start)
    step = workflow[start:end]

    assert "if: failure() && runner.os == 'macOS'" in step
    assert "uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in step
    assert "name: macos-cryptography-build-failure-$" + "{{ matrix.platform }}" in step
    assert "path: build/desktop/macos-cryptography-failure-logs/" in step
    assert "if-no-files-found: ignore" in step
    assert "env:" not in step


def test_macos_cryptography_source_build_pins_and_architecture_gate_are_explicit():
    helper = _load("macos_cryptography_source_build", "prepare_macos_cryptography.py")
    constraints = (ROOT / "packaging" / "macos-cryptography-build-constraints.txt").read_text(encoding="utf-8")
    release = (ROOT / "packaging" / "release-constraints.txt").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "desktop-release.yml").read_text(encoding="utf-8")
    pins = {line.split("==", 1)[0]: line.split("==", 1)[1] for line in constraints.splitlines() if "==" in line}

    assert pins == helper.BUILD_REQUIREMENTS
    assert release.count("cryptography==50.0.1") == 1
    assert helper.OPENSSL_VERSION == "3.5.9"
    assert helper.RUST_VERSION == "1.99.1"
    assert helper.MACOS_MINIMUM == "14.4"
    assert "runner.os == 'macOS' && runner.arch == 'X64'" in workflow
    packaging_step = workflow.index("name: Install pinned packaging for Intel cryptography wheel validation")
    cryptography_step = workflow.index("name: Build verified Intel cryptography wheel for macOS 14.4")
    packaging_setup = workflow[packaging_step:cryptography_step]
    assert packaging_step < cryptography_step
    assert "packaging==26.3" in packaging_setup
    assert "packaging/macos-cryptography-build-constraints.txt" in packaging_setup
    assert "--find-links build/security-cryptography/wheelhouse" in workflow


def test_rust_manifest_accepts_pinned_dated_urls_and_cargo_component_version(tmp_path, monkeypatch):
    import hashlib

    helper = _load("macos_rust_manifest_components", "prepare_macos_cryptography.py")
    target = "x86_64-apple-darwin"
    rows = ['date = "2026-10-15"', '']
    for component in ("rustc", "cargo", "rust-std"):
        rows.extend([
            f"[pkg.{component}]",
            f'version = "{helper.RUST_COMPONENT_VERSIONS[component]}"',
            f"[pkg.{component}.target.{target}]",
            f'available = true',
            f'xz_hash = "{helper.RUST_COMPONENTS[component]}"',
            f'xz_url = "https://static.rust-lang.org/dist/2026-10-15/{component}-1.99.1-{target}.tar.xz"',
            "",
        ])
    manifest = "\n".join(rows).encode("utf-8")
    path = tmp_path / "channel-rust-1.99.1.toml"
    path.write_bytes(manifest)
    monkeypatch.setattr(helper, "RUST_MANIFEST_SHA256", hashlib.sha256(manifest).hexdigest())

    urls = helper.check_rust_manifest(path)
    assert urls["cargo"] == f"https://static.rust-lang.org/dist/2026-10-15/cargo-1.99.1-{target}.tar.xz"
    assert helper.RUST_COMPONENT_VERSIONS["cargo"] == "0.100.0 (5f94df478 2026-08-27)"

    rustc_cli = "rustc 1.99.1 (58cf8f9c3 2026-10-13)"
    cargo_cli = "cargo 1.99.0 (5f94df478 2026-08-27)"
    rust_verbose = "rustc 1.99.1 (58cf8f9c3 2026-10-13)\nhost: x86_64-apple-darwin"
    cargo_verbose = "\n".join([
        cargo_cli,
        "release: 1.99.0",
        "commit-hash: 5f94df478",
        "commit-date: 2026-08-27",
        "host: x86_64-apple-darwin",
    ])
    proof = helper.verify_rust_cli_versions(rustc_cli, cargo_cli, rust_verbose, cargo_verbose)
    assert proof["cargo_release"] == "1.99.0"
    assert proof["cargo_commit_prefix"] == "5f94df478"
    assert proof["cargo_host"] == "x86_64-apple-darwin"
    latest_cli = cargo_cli.replace("1.99.0", "1.99.1")
    latest_verbose = cargo_verbose.replace("1.99.0", "1.99.1")
    assert helper.verify_rust_cli_versions(rustc_cli, latest_cli, rust_verbose, latest_verbose)["cargo_release"] == "1.99.1"
    with pytest.raises(RuntimeError, match="Cargo CLI version"):
        helper.verify_rust_cli_versions(rustc_cli, "cargo 0.100.0 (5f94df478 2026-08-27)", rust_verbose, cargo_verbose)
    for wrong_release in ("1.98.9", "1.100.0", "2.99.0"):
        wrong_cli = cargo_cli.replace("1.99.0", wrong_release)
        wrong_verbose = cargo_verbose.replace("1.99.0", wrong_release)
        with pytest.raises(RuntimeError, match="Cargo CLI version"):
            helper.verify_rust_cli_versions(rustc_cli, wrong_cli, rust_verbose, wrong_verbose)
    with pytest.raises(RuntimeError, match="commit or release"):
        helper.verify_rust_cli_versions(rustc_cli, cargo_cli, rust_verbose, cargo_verbose.replace("commit-hash: 5f94df478", "commit-hash: deadbeef0"))
    with pytest.raises(RuntimeError, match="host triples"):
        helper.verify_rust_cli_versions(rustc_cli, cargo_cli, rust_verbose, cargo_verbose.replace("host: x86_64-apple-darwin", "host: aarch64-apple-darwin"))

    bad_url = manifest.replace(b"/dist/2026-10-15/cargo-", b"/dist/cargo-")
    path.write_bytes(bad_url)
    monkeypatch.setattr(helper, "RUST_MANIFEST_SHA256", hashlib.sha256(bad_url).hexdigest())
    with pytest.raises(RuntimeError, match="unexpected Rust component URL for cargo"):
        helper.check_rust_manifest(path)

    bad_cargo = manifest.replace(b"0.100.0 (5f94df478 2026-08-27)", b"1.99.1 (58cf8f9c3 2026-10-13)")
    path.write_bytes(bad_cargo)
    monkeypatch.setattr(helper, "RUST_MANIFEST_SHA256", hashlib.sha256(bad_cargo).hexdigest())
    with pytest.raises(RuntimeError, match="unexpected cargo version"):
        helper.check_rust_manifest(path)


def test_selected_xcode_apple_clang_path_is_accepted_and_homebrew_is_rejected(tmp_path):
    helper = _load("macos_apple_clang_selection", "prepare_macos_cryptography.py")
    developer = tmp_path / "Xcode_26.1.app" / "Contents" / "Developer"
    compiler = developer / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin" / "clang"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"Apple clang fixture")
    version = "Apple clang version 17.0.0 (clang-1700.0.13.3)"

    assert helper.validate_apple_clang(developer, compiler, version) == compiler.resolve()

    brew = tmp_path / "opt" / "homebrew" / "bin" / "clang"
    brew.parent.mkdir(parents=True)
    brew.write_bytes(b"clang fixture")
    with pytest.raises(RuntimeError, match="outside the selected Apple developer directory"):
        helper.validate_apple_clang(developer, brew, version)
    with pytest.raises(RuntimeError, match="not Apple Clang"):
        helper.validate_apple_clang(developer, compiler, "clang version 17.0.0")


def test_macos_14_4_accepts_normalized_14_0_wheel_tag_and_rejects_bad_arch_or_minimum():
    helper = _load("macos_wheel_platform_compatibility", "prepare_macos_cryptography.py")
    proof = helper.verify_wheel_tag("cryptography-50.0.1-cp39-abi3-macosx_14_0_x86_64.whl")
    assert proof["platform_tags"] == ["macosx_14_0_x86_64"]
    assert proof["python_abi_tags"] == ["cp39-abi3"]
    assert helper.validate_macos_minimums(["14.0", "14.4"]) == ["14.0", "14.4"]

    with pytest.raises(RuntimeError, match="platform tags"):
        helper.verify_wheel_tag("cryptography-50.0.1-cp39-abi3-macosx_14_0_arm64.whl")
    with pytest.raises(RuntimeError, match="platform tags"):
        helper.verify_wheel_tag("cryptography-50.0.1-cp39-abi3-macosx_15_0_x86_64.whl")
    with pytest.raises(RuntimeError, match="stable-ABI"):
        helper.verify_wheel_tag("cryptography-50.0.1-cp313-cp313-macosx_14_0_x86_64.whl")
    with pytest.raises(RuntimeError, match="above macOS 14.4"):
        helper.validate_macos_minimums(["14.4", "15.0"])
