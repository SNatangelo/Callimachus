# packaging/smoke_desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the frozen executable without opening a desktop window."""
from __future__ import annotations

import os
import json
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path

import importlib.util

_windows_gui_spec = importlib.util.spec_from_file_location(
    "_callimachus_windows_gui", Path(__file__).with_name("windows_gui.py")
)
if _windows_gui_spec is None or _windows_gui_spec.loader is None:
    raise RuntimeError("could not load the Windows GUI binary validator")
_windows_gui = importlib.util.module_from_spec(_windows_gui_spec)
_windows_gui_spec.loader.exec_module(_windows_gui)
verify_windows_gui_binary = _windows_gui.verify_windows_gui_binary


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "desktop" / "dist"
LINUX_QT_LIBRARIES = (
    "libxcb-cursor.so.0",
    "libxcb-icccm.so.4",
    "libxcb-image.so.0",
    "libxcb-keysyms.so.1",
    "libxcb-render-util.so.0",
    "libxcb-util.so.1",
)
_MACOS_MINIMUM_VERSION = (14, 4)
_MACHO_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
    }
)


def executable_path() -> Path:
    if sys.platform == "darwin":
        return DIST / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus"
    if sys.platform == "win32":
        return DIST / "Callimachus" / "Callimachus.exe"
    return DIST / "Callimachus" / "Callimachus"


def check_linux_graphics_bundle() -> None:
    """Fail if the frozen XCB plugin lacks its packaged runtime libraries."""
    internal = DIST / "Callimachus" / "_internal"
    required = (
        "PySide6/Qt/plugins/platforms/libqxcb.so",
        *LINUX_QT_LIBRARIES,
    )
    missing = [name for name in required if not (internal / name).is_file()]
    if missing:
        raise SystemExit(
            "Linux GUI libraries are missing from the package: "
            + ", ".join(missing)
        )


def check_macos_minimum_version() -> None:
    """Fail if the built app or one of its native binaries requires newer macOS."""
    app_bundle = DIST / "Callimachus.app"
    info_plist = app_bundle / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as source:
            info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SystemExit(f"macOS application metadata is missing or invalid: {info_plist}") from exc
    if not isinstance(info, dict) or info.get("LSMinimumSystemVersion") != "14.4":
        value = info.get("LSMinimumSystemVersion") if isinstance(info, dict) else None
        raise SystemExit(
            f"macOS application minimum version must be 14.4; found {value!r}"
        )

    binaries = _macos_native_binaries(app_bundle)
    if not binaries:
        raise SystemExit(f"macOS application contains no native Mach-O binaries: {app_bundle}")
    for binary in binaries:
        try:
            result = subprocess.run(
                ["otool", "-l", str(binary)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemExit(f"otool could not inspect macOS binary {binary}: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise SystemExit(
                f"otool failed while inspecting macOS binary {binary}: {detail}"
            )

        minimum_versions = _macos_minimum_versions(result.stdout)
        if not minimum_versions or any(version is None for version in minimum_versions):
            raise SystemExit(
                f"macOS binary minimum version is missing or invalid in otool output: {binary}"
            )
        for version in minimum_versions:
            version_key = _macos_version_key(version)
            if version_key is None:
                raise SystemExit(
                    f"macOS binary minimum version is missing or invalid in otool output: "
                    f"{binary}: {version!r}"
                )
            if version_key > _MACOS_MINIMUM_VERSION:
                raise SystemExit(
                    f"macOS binary minimum version must be 14.4 or lower; "
                    f"found {version!r} in {binary}"
                )


def _macos_native_binaries(app_bundle: Path) -> list[Path]:
    """Return regular, non-symlink files in the bundle with Mach-O magic."""
    binaries = []
    directories = [app_bundle]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                ordered_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise SystemExit(f"cannot scan macOS application bundle {directory}: {exc}") from exc
        for entry in ordered_entries:
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    directories.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(mode):
                    continue
                with open(entry.path, "rb") as source:
                    magic = source.read(4)
            except OSError as exc:
                raise SystemExit(f"cannot inspect macOS application file {entry.path}: {exc}") from exc
            if magic in _MACHO_MAGICS:
                binaries.append(Path(entry.path))
    return binaries


def _macos_minimum_versions(output: str) -> tuple[str | None, ...]:
    """Extract each macOS deployment target reported by ``otool -l``."""
    versions = []
    command = None
    current_version = None
    current_seen = False
    current_invalid = False

    def finish_command() -> None:
        if command is not None:
            versions.append(
                current_version if current_seen and not current_invalid else None
            )

    for line in output.splitlines():
        if re.fullmatch(r"\s*Load command\s+\d+\s*", line):
            finish_command()
            command = None
            current_version = None
            current_seen = False
            current_invalid = False
            continue

        command_match = re.fullmatch(r"\s*cmd\s+([A-Za-z0-9_]+)\s*", line)
        if command_match:
            finish_command()
            command = command_match.group(1)
            if command not in {"LC_BUILD_VERSION", "LC_VERSION_MIN_MACOSX"}:
                command = None
            current_version = None
            current_seen = False
            current_invalid = False
            continue

        if command is None:
            continue
        field = "minos" if command == "LC_BUILD_VERSION" else "version"
        field_match = re.fullmatch(rf"\s*{field}\s+(\S+)\s*", line)
        if field_match:
            if current_seen:
                current_invalid = True
            current_version = field_match.group(1)
            current_seen = True

    finish_command()
    return tuple(versions)


def _macos_version_key(value: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(?:\.\d+)+", value):
        return None
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 2 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def create_image_only_pdf(path: Path) -> None:
    """Create a readable one-page scan with no embedded text layer."""
    import fitz

    source = fitz.open()
    page = source.new_page(width=612, height=792)
    page.insert_text((54, 72), "Callimachus OCR smoke test", fontsize=20)
    page.insert_textbox(
        fitz.Rect(54, 120, 558, 600),
        "This page verifies optical character recognition in the frozen desktop build.\n"
        "The Callimachus application should recognize these readable words from an image.\n"
        "Bundled OCR must run without external programs on the system PATH.\n"
        "The PDF contains a scanned page with no selectable text in the document.\n"
        "The packaged models read the image and return its words to the application.\n"
        "A successful result includes the title and these sentences in the exported text file.\n"
        "This sample contains enough readable prose to exercise the normal text quality checks.",
        fontsize=14, lineheight=1.5,
    )
    pixmap = page.get_pixmap(dpi=150, alpha=False)
    image = pixmap.tobytes("png")
    scan = fitz.open()
    scan_page = scan.new_page(width=page.rect.width, height=page.rect.height)
    scan_page.insert_image(scan_page.rect, stream=image)
    path.parent.mkdir(parents=True, exist_ok=True)
    scan.save(path)
    scan.close()
    source.close()


def smoke_frozen_ocr(executable: Path, environment: dict[str, str]) -> None:
    smoke_root = ROOT / "build" / "desktop"
    scan = smoke_root / "smoke-ocr-scan.pdf"
    output = smoke_root / "smoke-ocr-output.txt"
    create_image_only_pdf(scan)
    ocr_environment = environment.copy()
    ocr_environment["PATH"] = ""
    arguments = ("ocr", "--pdf", str(scan), "--out", str(output), "--dpi", "150")
    result = subprocess.run(
        [str(executable), *arguments], cwd=DIST, env=ocr_environment,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=180, check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"frozen OCR smoke test failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"frozen OCR returned invalid JSON ({exc}):\n{result.stdout}\n{result.stderr}"
        ) from exc
    if not payload.get("ok") or payload.get("method") != "rapidocr+pypdfium2":
        raise SystemExit(
            f"frozen OCR did not use the bundled backend: {payload}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    try:
        recognized = output.read_text(encoding="utf-8").casefold()
    except OSError as exc:
        raise SystemExit(
            f"frozen OCR did not create its text output: {exc}\n"
            f"{result.stdout}\n{result.stderr}"
        ) from exc
    if "callimachus" not in recognized or "optical character recognition" not in recognized:
        raise SystemExit(
            "frozen OCR output did not contain expected recognized text:\n"
            f"{recognized}\n{result.stdout}\n{result.stderr}"
        )
    print("frozen smoke test OCR scan: OK")


def smoke_browser_driver(environment: dict[str, str]) -> None:
    """Execute the distributed Playwright JS driver without a browser or network."""
    bundle = DIST / ("Callimachus.app" if sys.platform == "darwin" else "Callimachus")
    internal = bundle / ("Contents/Frameworks" if sys.platform == "darwin" else "_internal")
    driver = internal / "playwright" / "driver"
    node = driver / ("node.exe" if sys.platform == "win32" else "node")
    cli = driver / "package" / "cli.js"
    metadata = driver / "package" / "package.json"
    for path in (node, cli, metadata):
        if not path.is_file() or not path.resolve().is_relative_to(bundle.resolve()):
            raise SystemExit("bundled browser driver is missing or outside the application: " + str(path))
    try:
        version = json.loads(metadata.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit("bundled browser driver metadata is invalid") from exc
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise SystemExit("bundled browser driver version is invalid")
    driver_environment = environment.copy()
    # Prevent shell/Node startup hooks inherited from the build machine.
    for name in ("NODE_OPTIONS", "NODE_PATH"):
        driver_environment.pop(name, None)
    result = subprocess.run(
        [str(node), str(cli), "--version"], cwd=bundle, env=driver_environment,
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=30, check=False,
    )
    if result.returncode or result.stdout.strip() != "Version " + version:
        raise SystemExit(
            f"frozen browser driver failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    print("frozen smoke test browser driver: OK")


def _secret_names_for_smoke() -> set[str]:
    """Read the secret-name inventory without loading settings or vault values."""
    source_root = str(ROOT)
    inserted = source_root not in sys.path
    if inserted:
        sys.path.insert(0, source_root)
    try:
        from core.app.settings.credentials import secret_names_from_template

        return secret_names_from_template(ROOT / ".env.example")
    finally:
        if inserted:
            sys.path.remove(source_root)


def main() -> None:
    executable = executable_path()
    if not executable.is_file():
        raise SystemExit(f"missing frozen executable: {executable}")
    if sys.platform == "win32":
        try:
            verify_windows_gui_binary(executable)
        except ValueError as exc:
            raise SystemExit(f"frozen executable is not a Windows GUI binary: {exc}") from exc
    if sys.platform == "linux":
        check_linux_graphics_bundle()
    elif sys.platform == "darwin":
        check_macos_minimum_version()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for name in _secret_names_for_smoke():
        environment[name] = ""
    environment.pop("CITATION_VERIFIER_SIGNING_KEY_FILE", None)
    environment["CALLIMACHUS_DATA_DIR"] = str(ROOT / "build" / "desktop" / "smoke-data")
    for arguments in (("--qt-probe",), ("--package-self-test",), ("--help",), ("ocr", "--help")):
        result = subprocess.run(
            [str(executable), *arguments], cwd=DIST, env=environment,
            text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=90, check=False,
        )
        if result.returncode:
            raise SystemExit(
                f"frozen smoke test {arguments} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        if arguments == ("--package-self-test",):
            required_markers = (
                "Library PDF/DOCX round-trip passed",
                "Historical report export probe passed",
                "GUI shell/settings probe passed",
            )
            missing_markers = [
                marker for marker in required_markers if marker not in result.stdout
            ]
            if missing_markers:
                raise SystemExit(
                    "frozen package self-test did not report: "
                    + ", ".join(missing_markers)
                )
        print(f"frozen smoke test {arguments}: OK")

    fixture = ROOT / "tests" / "fixtures" / "parse_golden" / "inputs" / "pdf_author_year_basic.pdf"
    debug = ROOT / "build" / "desktop" / "smoke-parse-debug.md"
    arguments = ("parse", "--input", str(fixture), "--debug", str(debug), "--auto")
    result = subprocess.run(
        [str(executable), *arguments], cwd=DIST, env=environment,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=90, check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"frozen smoke test {arguments} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"frozen parser returned invalid JSON: {exc}") from exc
    if not parsed.get("ok") or parsed.get("n_claims", 0) < 1 or not debug.is_file():
        raise SystemExit(f"frozen parser did not produce claims and debug output: {parsed}")
    print("frozen smoke test parse PDF: OK")
    smoke_frozen_ocr(executable, environment)
    smoke_browser_driver(environment)


if __name__ == "__main__":
    main()
