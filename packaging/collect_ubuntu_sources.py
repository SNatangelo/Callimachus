# packaging/collect_ubuntu_sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Archive exact Ubuntu source packages for system libraries in the Linux bundle."""
from __future__ import annotations

import hashlib
import json
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "desktop"
NOTICES = BUILD_ROOT / "THIRD-PARTY-NOTICES" / "index.json"
LAUNCHPAD_SOURCES = "https://launchpad.net/ubuntu/+archive/primary/+sourcefiles"


def _sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def source_packages(index: Path = NOTICES) -> list[tuple[str, str]]:
    inventory = json.loads(index.read_text(encoding="utf-8"))
    packages: set[tuple[str, str]] = set()
    for item in inventory:
        if not str(item.get("directory", "")).startswith("system-packages/"):
            continue
        name = item.get("source_package")
        version = item.get("source_version")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name)
                or not isinstance(version, str)
                or not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version)):
            raise RuntimeError(f"bundled system library has no exact source package: {item!r}")
        packages.add((name, version))
    if not packages:
        raise RuntimeError("Linux package has no indexed Ubuntu source packages")
    return sorted(packages)


def _source_files(name: str, version: str, target: Path) -> list[Path]:
    escaped_name = urllib.parse.quote(name, safe="")
    escaped_version = urllib.parse.quote(version, safe="")
    base = f"{LAUNCHPAD_SOURCES}/{escaped_name}/{escaped_version}"
    filename_version = version.rsplit(":", 1)[-1]
    dsc_name = f"{name}_{filename_version}.dsc"
    with urllib.request.urlopen(f"{base}/{urllib.parse.quote(dsc_name, safe='')}", timeout=120) as response:
        dsc = response.read()
    text = dsc.decode("utf-8")
    if (not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----\n")
            or "\n-----END PGP SIGNATURE-----" not in text
            or f"\nSource: {name}\n" not in text
            or f"\nVersion: {version}\n" not in text
            or "\nChecksums-Sha256:\n" not in text):
        raise RuntimeError(f"invalid Launchpad source descriptor: {name}={version}")
    checksum_section = text.split("\nChecksums-Sha256:\n", 1)[1]
    entries: list[tuple[str, int, str]] = []
    for line in checksum_section.splitlines():
        if not line.startswith(" "):
            break
        match = re.fullmatch(r" ([0-9a-f]{64}) ([0-9]+) ([A-Za-z0-9][A-Za-z0-9.+~_-]*)", line)
        if not match:
            raise RuntimeError(f"invalid Launchpad source checksum: {name}={version}")
        entries.append((match.group(1), int(match.group(2)), match.group(3)))
    if not entries or len({filename for _, _, filename in entries}) != len(entries):
        raise RuntimeError(f"missing or duplicate Launchpad source files: {name}={version}")
    (target / dsc_name).write_bytes(dsc)
    files = [target / dsc_name]
    for expected_hash, expected_size, filename in entries:
        path = target / filename
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(f"{base}/{urllib.parse.quote(filename, safe='')}", timeout=120) as response, path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise RuntimeError(f"Launchpad source checksum mismatch: {name}={version} {filename}")
        files.append(path)
    return files


def collect(packages: list[tuple[str, str]], root: Path = BUILD_ROOT) -> Path:
    payload = root / "ubuntu-system-sources"
    payload.mkdir(parents=True, exist_ok=False)
    manifest = []
    for name, version in packages:
        target = payload / f"{name}-{version}"
        target.mkdir()
        files = sorted(_source_files(name, version, target))
        if not any(path.suffix == ".dsc" for path in files) or len(files) < 2:
            raise RuntimeError(f"incomplete Ubuntu source package: {name}={version}")
        manifest.append({
            "package": name, "version": version,
            "files": [
                {"path": path.relative_to(payload).as_posix(),
                 "sha256": _sha256(path)}
                for path in files
            ],
        })
    (payload / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    output_dir = root / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "Callimachus-linux-system-sources.tar.gz"
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(payload, arcname="ubuntu-system-sources")
    digest = _sha256(output)
    output.with_name(output.name + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii",
    )
    return output


if __name__ == "__main__":
    collect(source_packages())
