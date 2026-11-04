#!/usr/bin/env python3
"""Pin a Snapcraft build to an immutable, checksummed GitHub release asset."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


REPOSITORY = "SNatangelo/Callimachus"
ASSET_NAME = "Callimachus-linux-x64.tar.gz"
SNAPCRAFT_YAML = Path(__file__).resolve().parents[1] / "snap" / "snapcraft.yaml"


def replace_once(text: str, pattern: str, replacement: str) -> str:
    updated, count = re.subn(pattern, lambda _: replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError(f"expected exactly one Snapcraft field matching {pattern!r}; got {count}")
    return updated


def main(tag: str) -> None:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError(f"expected a stable release tag (vMAJOR.MINOR.PATCH), got {tag!r}")

    url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{quote(tag)}"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Callimachus-Snapcraft"})
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release.get("draft") or release.get("prerelease") or release.get("tag_name") != tag:
        raise RuntimeError("the requested tag is not a published stable release")
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == ASSET_NAME]
    if len(assets) != 1:
        raise RuntimeError(f"expected one asset named {ASSET_NAME}; found {len(assets)}")
    asset = assets[0]
    digest = asset.get("digest", "")
    download_url = asset.get("browser_download_url", "")
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET_NAME}"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or download_url != expected_url:
        raise RuntimeError("release asset digest or download URL is missing or inconsistent")

    recipe = SNAPCRAFT_YAML.read_text(encoding="utf-8")
    recipe = replace_once(recipe, r"^version: .*$", f"version: '{tag[1:]}'")
    recipe = replace_once(recipe, r"^    source: https://github\.com/SNatangelo/Callimachus/releases/download/.*$", f"    source: {download_url}")
    recipe = replace_once(recipe, r"^    source-checksum: sha256/[0-9a-f]{64}$", f"    source-checksum: sha256/{digest[7:]}")
    SNAPCRAFT_YAML.write_text(recipe, encoding="utf-8")
    print(f"Pinned Snapcraft to {tag}: {digest}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python packaging/prepare_snap_release.py vX.Y.Z")
    main(sys.argv[1])
