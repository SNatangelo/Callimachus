# packaging/store/prepare_sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Recover the pinned Store build inputs without a local binary backup.

Requires Python 3.13 or newer. Downloads are hash-checked before extraction.
This prepares sources and original upstream wheel inputs; it does not compile.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import urllib.request
import zipfile

STORE = Path(__file__).resolve().parent
PROJECT = STORE.parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _security_overrides(store: Path = STORE, project: Path = PROJECT):
    config = read_json(store / "native-recipes/security-source-inputs.json")
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("unsupported security source-input schema")
    rows = config.get("source_overrides")
    if not isinstance(rows, list):
        raise ValueError("security source overrides are missing")
    history = read_json(store / "native-recipes/python-sources/manifest.json")
    historical = {item["name"].casefold(): item for item in history}
    pins = {}
    for raw in (project / "packaging/release-constraints.txt").read_text(encoding="utf-8").splitlines():
        line = raw.partition("#")[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+_-]+)", line)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name in {"brotli", "pillow"}:
                pins.setdefault(name, []).append(match.group(2))
    if any(len(pins.get(name, [])) != 1 for name in ("brotli", "pillow")):
        raise ValueError("release constraints need one unconditional Brotli and Pillow pin")
    by_name = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed security source override")
        name = str(row.get("name", "")).casefold()
        if name not in {"brotli", "pillow"} or name in by_name:
            raise ValueError("security source overrides must contain unique Brotli and Pillow rows")
        required = ("historical_version", "version", "filename", "url", "sha256", "size_bytes")
        if any(key not in row for key in required):
            raise ValueError(f"incomplete {name} source override")
        old = historical.get(name)
        if not old or old.get("version") != row["historical_version"]:
            raise ValueError(f"{name} override does not match the historical source manifest")
        if row["version"] != pins[name][0]:
            raise ValueError(f"{name} override does not match the active release constraint")
        if (row["filename"] != f"{name}-{row['version']}.tar.gz"
                or not isinstance(row["url"], str)
                or not row["url"].startswith("https://files.pythonhosted.org/packages/")
                or row["url"].rsplit("/", 1)[-1] != row["filename"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"]))
                or type(row["size_bytes"]) is not int or row["size_bytes"] <= 0):
            raise ValueError(f"invalid verified {name} source coordinates")
        by_name[name] = row
    if set(by_name) != {"brotli", "pillow"}:
        raise ValueError("security source overrides must contain exactly Brotli and Pillow")
    pin = config.get("pillow_brotli_pin")
    if (not isinstance(pin, dict)
            or pin.get("upstream_commit") != "293218ad059d994baf1777d882911bed6a49704a"
            or pin.get("path") != ".github/dependencies.json"
            or pin.get("filename") != "pillow-13.0.0-dependencies.json"
            or pin.get("url") != "https://raw.githubusercontent.com/python-pillow/Pillow/293218ad059d994baf1777d882911bed6a49704a/.github/dependencies.json"
            or pin.get("size_bytes") != 376
            or pin.get("from_version") != "1.2.0"
            or pin.get("to_version") != by_name["brotli"]["version"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("original_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("patched_sha256", "")))):
        raise ValueError("Pillow's verified Brotli source-pin patch is incomplete")
    native_source = config.get("pillow_brotli_native_source")
    expected_native_source = {
        "filename": "brotli-1.2.1-github.tar.gz",
        "version": "1.2.1",
        "url": "https://github.com/google/brotli/archive/refs/tags/v1.2.1.tar.gz",
        "sha256": "98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f",
        "size_bytes": 646598,
    }
    if native_source != expected_native_source:
        raise ValueError("the verified native Pillow Brotli source archive is missing or changed")
    if native_source["version"] != by_name["brotli"]["version"]:
        raise ValueError("native Pillow Brotli source version differs from the active pin")
    return by_name, pin, native_source


def source_inputs(store: Path = STORE, project: Path = PROJECT):
    publication = read_json(store / "source-inputs.json")
    packages = read_json(store / "native-recipes/python-sources/manifest.json")
    security, pin, native_source = _security_overrides(store, project)
    rows = []
    for item in packages:
        row = dict(item)
        override = security.get(item["name"].casefold())
        if override:
            row.update({
                "version": override["version"],
                "filename": override["filename"],
                "url": override["url"],
                "sha256": override["sha256"],
                "size_bytes": override["size_bytes"],
            })
        row["destination"] = f"python-sources/{item['name']}-source"
        rows.append(row)
    known = {row["filename"] for row in rows}
    historical_security_sources = {
        f"{name}-{row['historical_version']}.tar.gz"
        for name, row in security.items()
    }
    for item in publication["additional_sources_in_build_archive"]:
        filename = Path(item["path"]).name
        if filename in known or filename in historical_security_sources:
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
    for item in read_json(store / "original-wheel-inputs.json"):
        rows.append({**item, "destination": f"original-inputs/{item['name']}"})
    rows.append({
        "filename": pin["filename"], "url": pin["url"],
        "sha256": pin["original_sha256"], "size_bytes": pin["size_bytes"],
        "destination": "security-source-metadata", "metadata_only": True,
    })
    rows.append({
        "filename": native_source["filename"], "url": native_source["url"],
        "sha256": native_source["sha256"], "size_bytes": native_source["size_bytes"],
        "destination": "security-native-source", "source_kind": "pillow-brotli-native",
    })
    return rows


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(row: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / row["filename"]
    if target.exists():
        if (digest(target) != row["sha256"]
                or ("size_bytes" in row and target.stat().st_size != row["size_bytes"])):
            raise ValueError(f"Existing input has the wrong SHA256 or size: {target}")
        return target
    partial = target.with_name(target.name + ".partial")
    # Exclusive creation prevents overwriting an earlier interrupted download.
    with urllib.request.urlopen(row["url"], timeout=120) as response, partial.open("xb") as output:
        shutil.copyfileobj(response, output)
    if (digest(partial) != row["sha256"]
            or ("size_bytes" in row and partial.stat().st_size != row["size_bytes"])):
        raise ValueError(f"Downloaded input has the wrong SHA256 or size: {partial}")
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


def _json_brotli_values(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == "brotli":
                found.append(child)
            found.extend(_json_brotli_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_json_brotli_values(child))
    return found


def apply_pillow_brotli_source_pin(native: Path, store: Path = STORE):
    config = read_json(store / "native-recipes/security-source-inputs.json")
    pillow = next(row for row in config["source_overrides"] if row["name"].casefold() == "pillow")
    pin = config["pillow_brotli_pin"]
    source_parent = native / "python-sources/pillow-source"
    source_roots = [
        path for path in source_parent.iterdir()
        if path.is_dir() and not path.is_symlink()
        and path.name.casefold() == f"pillow-{pillow['version']}".casefold()
    ] if source_parent.is_dir() else []
    if len(source_roots) != 1:
        raise ValueError("the recovered Pillow source tree does not match its verified source row")
    source_root = source_roots[0].resolve()
    if not source_root.is_relative_to(source_parent.resolve()):
        raise ValueError("the recovered Pillow source tree escapes its destination")
    relative = Path(pin["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != ".github/dependencies.json":
        raise ValueError("unsafe Pillow source metadata path")
    path = source_root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(source_root):
        raise ValueError("Pillow source metadata path escapes its verified source tree")
    metadata_input = native / "inputs" / pin["filename"]
    if metadata_input.is_symlink() or not metadata_input.is_file():
        raise ValueError("verified upstream Pillow build metadata input is missing")
    upstream = metadata_input.read_bytes()
    if (len(upstream) != pin["size_bytes"]
            or hashlib.sha256(upstream).hexdigest() != pin["original_sha256"]):
        raise ValueError("upstream Pillow build metadata has the wrong size or SHA256")
    if path.exists():
        raw = path.read_bytes()
        current_sha = hashlib.sha256(raw).hexdigest()
        if current_sha == pin["patched_sha256"]:
            return path
        if current_sha != pin["original_sha256"] or raw != upstream:
            raise ValueError("Pillow dependencies.json differs from the verified upstream bytes")
    else:
        raw = upstream
    try:
        decoded = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Pillow dependencies.json is not valid UTF-8 JSON") from exc
    values = _json_brotli_values(decoded)
    if values != [pin["from_version"]]:
        raise ValueError("Pillow dependencies.json does not have one exact historical Brotli pin")
    old = re.escape(pin["from_version"].encode("ascii"))
    pattern = re.compile(rb'(?i)("brotli"\s*:\s*")' + old + rb'(")')
    patched, count = pattern.subn(rb"\g<1>" + pin["to_version"].encode("ascii") + rb"\2", raw)
    if count != 1 or hashlib.sha256(patched).hexdigest() != pin["patched_sha256"]:
        raise ValueError("the staged Pillow Brotli pin patch does not match its verified digest")
    if _json_brotli_values(json.loads(patched)) != [pin["to_version"]]:
        raise ValueError("the staged Pillow Brotli pin patch failed semantic validation")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(patched)
    if hashlib.sha256(path.read_bytes()).hexdigest() != pin["patched_sha256"]:
        raise ValueError("the staged Pillow Brotli pin file failed its post-write digest")
    return path


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
        if not args.download_only and not row.get("metadata_only"):
            extract(row, archive, native)
    if not args.download_only:
        apply_pillow_brotli_source_pin(native)
        shutil.copytree(STORE / "native-recipes", native, dirs_exist_ok=True)
    print(f"Verified {len(rows)} public inputs; no compilation performed")


if __name__ == "__main__":
    main()
