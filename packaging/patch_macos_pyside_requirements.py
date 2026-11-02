#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Create the one-line, hash-guarded PySide 6.12 build-requirements overlay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

SOURCE_SHA256 = "38799b9089ffdd3da96d13019f565d142112eee17fc4453ad43acc85e2a1fde4"
OLD_REQUIREMENT = b"setuptools==78.1.0"
NEW_REQUIREMENT = b"setuptools==84.1.0"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def render_overlay(source: bytes, expected_sha256: str = SOURCE_SHA256) -> bytes:
    actual = sha256(source)
    if actual != expected_sha256:
        raise ValueError(f"unexpected upstream PySide requirements SHA-256: {actual}")
    lines = source.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == OLD_REQUIREMENT]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {OLD_REQUIREMENT.decode()} pin, found {len(matches)}")
    index = matches[0]
    line = lines[index]
    ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
    lines[index] = NEW_REQUIREMENT + ending
    overlay = b"".join(lines)
    if overlay.replace(NEW_REQUIREMENT, OLD_REQUIREMENT, 1) != source:
        raise ValueError("overlay changed bytes outside the guarded setuptools pin")
    if overlay.count(NEW_REQUIREMENT) != 1 or OLD_REQUIREMENT in overlay:
        raise ValueError("overlay does not contain exactly the release setuptools pin")
    return overlay


def create_overlay(source_path: Path, output_path: Path, manifest_path: Path) -> dict[str, str]:
    source = source_path.read_bytes()
    overlay = render_overlay(source)
    for path in (output_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing overlay output: {path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as stream:
        stream.write(overlay)
    manifest = {
        "source": str(source_path),
        "source_sha256": sha256(source),
        "source_setuptools": OLD_REQUIREMENT.decode(),
        "overlay_sha256": sha256(overlay),
        "overlay_setuptools": NEW_REQUIREMENT.decode(),
        "other_bytes_unchanged": True,
    }
    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        raise SystemExit("usage: patch_macos_pyside_requirements.py SOURCE OUTPUT MANIFEST")
    manifest = create_overlay(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    print(json.dumps(manifest, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
