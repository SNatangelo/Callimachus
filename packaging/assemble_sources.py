# packaging/assemble_sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fetch and verify the separately published source archives for a release."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "release-sources.json"
CONSTRAINTS = ROOT / "packaging" / "release-constraints.txt"


def _pinned_versions(constraints: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in constraints.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.-]+)(?:;|$)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def source_entries(manifest: Path = MANIFEST, constraints: Path = CONSTRAINTS) -> list[dict[str, str]]:
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("release source manifest is empty")
    pins = _pinned_versions(constraints)
    expected = {
        "Qt 6", "Qt 5 (OpenCV)", "PySide6 bindings", "PyMuPDF", "MuPDF",
        "OpenCV Python", "FFmpeg (OpenCV)",
        "PyInstaller bootloader",
    }
    names: set[str] = set()
    files: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("invalid release source manifest entry")
        name = item.get("name")
        filename = item.get("filename")
        version = item.get("version")
        package_version = item.get("package_version")
        url = item.get("url")
        digest = item.get("sha256")
        dependency = item.get("constraint")
        if (not all(isinstance(value, str) for value in (
            name, filename, version, package_version, url, digest, dependency,
        ))
                or name in names or filename in files
                or Path(filename).name != filename or filename in {"", ".", ".."}
                or not url.startswith("https://")
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or pins.get(dependency.lower()) != package_version):
            raise RuntimeError(f"invalid or unpinned release source: {item!r}")
        names.add(name)
        files.add(filename)
    if names != expected:
        raise RuntimeError(f"release sources require {sorted(expected)}; found {sorted(names)}")
    return entries


def assemble(destination: Path, entries: list[dict[str, str]] | None = None) -> None:
    entries = source_entries() if entries is None else entries
    destination.mkdir(parents=True, exist_ok=True)
    for item in entries:
        target = destination / item["filename"]
        if target.exists():
            raise RuntimeError(f"release source asset already exists: {target}")
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(item["url"], timeout=120) as response, target.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise RuntimeError(f"release source checksum mismatch: {item['filename']}")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        target.with_name(target.name + ".sha256").write_text(
            f"{item['sha256']}  {target.name}\n", encoding="ascii",
        )
    (destination / "Callimachus-dependency-sources.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8",
    )


if __name__ == "__main__":
    assemble(Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / "release")
