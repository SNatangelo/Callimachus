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
    smoke_frozen_ocr(executable, environment)


if __name__ == "__main__":
    main()
