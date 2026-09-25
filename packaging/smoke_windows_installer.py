# packaging/smoke_windows_installer.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Install the Windows setup silently on the CI runner and probe the installed app."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "desktop"
SETUP = BUILD_ROOT / "artifacts" / "Callimachus-Setup.exe"
INSTALL_ROOT = BUILD_ROOT / "smoke-install"
EXTERNAL_WINDOWS_RUNTIME = frozenset({"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"})


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows installer smoke test requires Windows")
    if not SETUP.is_file():
        raise SystemExit(f"missing installer: {SETUP}")
    if INSTALL_ROOT.exists():
        raise SystemExit(f"smoke-test install directory already exists: {INSTALL_ROOT}")
    result = subprocess.run(
        [str(SETUP), "/S", f"/D={INSTALL_ROOT}"],
        cwd=BUILD_ROOT, text=True, capture_output=True, timeout=180, check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"silent installer failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    executable = INSTALL_ROOT / "Callimachus" / "Callimachus.exe"
    if not executable.is_file():
        raise SystemExit(f"installed executable is missing: {executable}")
    if not (INSTALL_ROOT / "THIRD-PARTY-NOTICES" / "index.json").is_file():
        raise SystemExit("installed third-party notice inventory is missing")
    bundled_runtime = sorted(
        str(path.relative_to(INSTALL_ROOT)) for path in INSTALL_ROOT.rglob("*")
        if path.is_file() and path.name.lower() in EXTERNAL_WINDOWS_RUNTIME
    )
    if bundled_runtime:
        raise SystemExit(f"installer copied external Windows runtime DLLs: {bundled_runtime}")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CALLIMACHUS_DATA_DIR"] = str(BUILD_ROOT / "smoke-installer-data")
    for arguments in (("--qt-probe",), ("--package-self-test",)):
        result = subprocess.run(
            [str(executable), *arguments], cwd=INSTALL_ROOT, env=environment,
            text=True, capture_output=True, timeout=90, check=False,
        )
        if result.returncode:
            raise SystemExit(
                f"installed app smoke test {arguments} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        print(f"installed app smoke test {arguments}: OK")


if __name__ == "__main__":
    main()
