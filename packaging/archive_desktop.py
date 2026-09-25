# packaging/archive_desktop.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Archive the whole native distribution and produce a SHA-256 sidecar."""
from __future__ import annotations

import argparse
import hashlib
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "desktop" / "dist"
ARTIFACTS = ROOT / "build" / "desktop" / "artifacts"
PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64", "macos-x64")
EXTERNAL_WINDOWS_RUNTIME = frozenset({"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"})


def archive(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported platform: {platform}")
    executable = DIST / "Callimachus" / ("Callimachus.exe" if platform.startswith("windows") else "Callimachus")
    if platform.startswith("macos"):
        executable = DIST / "Callimachus.app" / "Contents" / "MacOS" / "Callimachus"
    if not executable.is_file():
        raise RuntimeError(f"built executable is missing: {executable}")
    if platform.startswith("windows"):
        bundled_runtime = sorted(
            str(path.relative_to(DIST)) for path in DIST.rglob("*")
            if path.is_file() and (
                path.name.lower() in EXTERNAL_WINDOWS_RUNTIME
                or path.name.lower().startswith(("vc_redist", "vcredist"))
                and path.suffix.lower() == ".exe"
            )
        )
        if bundled_runtime:
            raise RuntimeError(f"Windows runtime files must not be bundled: {bundled_runtime}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if platform.startswith("windows") else ".tar.gz"
    result = ARTIFACTS / f"Callimachus-{platform}{suffix}"
    entries = sorted(DIST.iterdir())
    if suffix == ".zip":
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
            for entry in entries:
                if entry.is_dir():
                    for child in sorted(entry.rglob("*")):
                        if child.is_file():
                            target.write(child, child.relative_to(DIST))
                else:
                    target.write(entry, entry.relative_to(DIST))
    else:
        with tarfile.open(result, "w:gz", format=tarfile.PAX_FORMAT) as target:
            for entry in entries:
                target.add(entry, arcname=entry.name, recursive=True)
    with result.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    result.with_name(result.name + ".sha256").write_text(
        f"{digest}  {result.name}\n", encoding="ascii",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=PLATFORMS)
    args = parser.parse_args()
    print(archive(args.platform))


if __name__ == "__main__":
    main()
