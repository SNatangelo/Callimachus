# packaging/smoke_desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the frozen executable without opening a desktop window."""
from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path


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


def main() -> None:
    executable = executable_path()
    if not executable.is_file():
        raise SystemExit(f"missing frozen executable: {executable}")
    if sys.platform == "linux":
        check_linux_graphics_bundle()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
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


if __name__ == "__main__":
    main()
