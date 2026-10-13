"""Recover the pinned Store build inputs without a local binary backup.

Requires Python 3.13 or newer. Downloads are hash-checked before extraction.
This prepares sources and original upstream wheel inputs; it does not compile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request
import zipfile

STORE = Path(__file__).resolve().parent
PROJECT = STORE.parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def source_inputs():
    publication = read_json(STORE / "source-inputs.json")
    packages = read_json(STORE / "native-recipes/python-sources/manifest.json")
    rows = []
    for item in packages:
        rows.append({**item, "destination": f"python-sources/{item['name']}-source"})
    known = {row["filename"] for row in rows}
    for item in publication["additional_sources_in_build_archive"]:
        filename = Path(item["path"]).name
        if filename in known:
            continue
        rows.append({"filename": filename, "url": item["upstream_url"],
                     "sha256": item["sha256"], "destination": "."})
        known.add(filename)
    needed = {"qt-everywhere-src-6.11.2.tar.xz", "pyside-setup-everywhere-src-6.11.2.tar.xz",
              "mupdf-1.28.0-source.tar.gz"}
    for item in publication["existing_release_source_assets"]:
        if item["filename"] in needed:
            rows.append({**item, "destination": "qt-sources" if item["filename"].startswith(
                ("qt-", "pyside-")) else "."})
    for item in read_json(STORE / "original-wheel-inputs.json"):
        rows.append({**item, "destination": f"original-inputs/{item['name']}"})
    return rows


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(row: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / row["filename"]
    if target.exists():
        if digest(target) != row["sha256"]:
            raise ValueError(f"Existing input has the wrong SHA256: {target}")
        return target
    partial = target.with_name(target.name + ".partial")
    # Exclusive creation prevents overwriting an earlier interrupted download.
    with urllib.request.urlopen(row["url"], timeout=120) as response, partial.open("xb") as output:
        shutil.copyfileobj(response, output)
    if digest(partial) != row["sha256"]:
        raise ValueError(f"Downloaded input has the wrong SHA256: {partial}")
    partial.rename(target)
    return target


def extract(row: dict, archive: Path, native: Path):
    destination = native / row["destination"]
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError(f"Unsafe wheel member: {member.filename}")
                if target.exists():
                    raise FileExistsError(f"Refusing to overwrite an extracted input: {target}")
            source.extractall(destination)
    else:
        with tarfile.open(archive) as source:
            roots = {Path(member.name).parts[0] for member in source if member.name not in ("", ".")}
            if len(roots) != 1:
                raise ValueError(f"Expected one source root in {archive.name}, found {roots}")
            root = next(iter(roots))
            if root in ("..", "/", "\\") or not (destination / root).resolve().is_relative_to(destination.resolve()):
                raise ValueError(f"Unsafe source root: {root}")
            if (destination / root).exists():
                raise FileExistsError(f"Refusing to overwrite source tree: {destination / root}")
            source.extractall(destination, filter="data")
            # Upstream libyaml's archive root differs from the native recipe path.
            if archive.name == "libyaml-0.2.5.tar.gz" and root != "libyaml-0.2.5":
                (destination / root).rename(destination / "libyaml-0.2.5")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-root", type=Path, default=PROJECT / "build/desktop/native-14.40")
    parser.add_argument("--list", action="store_true", help="Print the input plan without downloading")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args(argv)
    rows = source_inputs()
    if args.list:
        print(json.dumps(rows, indent=2))
        return
    native = args.native_root.resolve()
    if not native.is_relative_to(PROJECT.resolve()):
        parser.error("native-root must be inside this checkout")
    for row in rows:
        print(f"Recovering {row['filename']}", flush=True)
        archive = fetch(row, native / "inputs")
        if not args.download_only:
            extract(row, archive, native)
    if not args.download_only:
        shutil.copytree(STORE / "native-recipes", native, dirs_exist_ok=True)
    print(f"Verified {len(rows)} public inputs; no compilation performed")


if __name__ == "__main__":
    main()
