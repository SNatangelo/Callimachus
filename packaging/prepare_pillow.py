# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Qualify Pillow's bundled Brotli before installing the release runtime.

Linux/macOS retain the verified upstream Pillow wheel and rebuild only its two
Brotli libraries. Windows requires the fully source-built qualified wheel.
No dependency is upgraded implicitly and no unqualified wheel is accepted.
"""
from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "packaging/store/native-recipes/security-source-inputs.json"
WHEEL_INPUTS = Path(__file__).with_name("pillow-wheel-inputs.json")
RECEIPT = "callimachus-pillow-native-security.json"


def load_sibling(name):
    spec = importlib.util.spec_from_file_location("_pillow_" + name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load packaging helper " + name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args, **kwargs):
    print("Running " + str(args[0]), flush=True)
    return subprocess.run([str(arg) for arg in args], check=True, timeout=1200, **kwargs)


def download(row, work):
    path = work / row["filename"]
    if path.exists():
        if path.stat().st_size != row["size_bytes"] or sha256(path) != row["sha256"]:
            raise RuntimeError("cached source does not match its pin: " + path.name)
        return path
    return load_sibling("bootstrap_pip").download_verified_wheel(
        path, url=row["url"], expected_size=row["size_bytes"], expected_sha256=row["sha256"])


def unpack_wheel(wheel, destination):
    """Reject traversal, links, duplicates and expansion bombs before any write."""
    with zipfile.ZipFile(wheel) as archive:
        seen, kinds, total = set(), {}, 0
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            total += item.file_size
            canonical = name.as_posix()
            if (name.is_absolute() or ".." in name.parts or "\\" in item.filename
                    or ":" in item.filename or canonical in seen
                    or canonical != item.filename.rstrip("/") or len(seen) >= 10000
                    or (item.external_attr >> 16) & 0o170000 == 0o120000
                    or item.file_size > 100 * 1024**2 or total > 150 * 1024**2):
                raise RuntimeError("unsafe wheel archive member")
            if any(kinds.get(parent.as_posix()) is False for parent in name.parents):
                raise RuntimeError("wheel file/directory collision")
            if not item.is_dir() and any(key.startswith(canonical + "/") for key in seen):
                raise RuntimeError("wheel file/directory collision")
            seen.add(canonical)
            kinds[canonical] = item.is_dir()
        archive.extractall(destination)
    infos = list(destination.glob("*.dist-info"))
    if len(infos) != 1:
        raise RuntimeError("expected one Pillow distribution")
    info = infos[0]
    metadata = (info / "METADATA").read_text(encoding="utf-8")
    if not re.search(r"(?im)^Name: pillow$", metadata) or not re.search(r"(?m)^Version: 13\.0\.0$", metadata):
        raise RuntimeError("unexpected Pillow wheel metadata")
    return info


def extract_brotli(archive, work):
    target = work / "brotli-source"
    target.mkdir()
    with tarfile.open(archive) as tar:
        members = []
        seen, kinds, total = set(), {}, 0
        for member in tar:
            if len(members) >= 10000:
                raise RuntimeError("excessive Brotli source members")
            members.append(member)
            path = PurePosixPath(member.name)
            canonical = path.as_posix()
            total += member.size
            if (not (member.isfile() or member.isdir()) or path.is_absolute()
                    or ".." in path.parts or "\\" in member.name or ":" in member.name
                    or canonical != member.name.rstrip("/") or canonical in seen
                    or member.size > 50 * 1024**2 or total > 150 * 1024**2):
                raise RuntimeError("unsafe or oversized Brotli source archive")
            if any(kinds.get(parent.as_posix()) is False for parent in path.parents):
                raise RuntimeError("Brotli source file/directory collision")
            if member.isfile() and any(key.startswith(canonical + "/") for key in seen):
                raise RuntimeError("Brotli source file/directory collision")
            seen.add(canonical)
            kinds[canonical] = member.isdir()
        tar.extractall(target, members=members, filter="data")
    roots = list(target.iterdir())
    if len(roots) != 1 or not (roots[0] / "c/common/version.h").is_file():
        raise RuntimeError("unexpected Brotli source layout")
    version = (roots[0] / "c/common/version.h").read_text()
    for key, value in (("MAJOR", "1"), ("MINOR", "2"), ("PATCH", "1")):
        if not re.search(r"#define BROTLI_VERSION_" + key + r"\s+" + value + r"\b", version):
            raise RuntimeError("Brotli source version differs from the active pin")
    return roots[0]


def compile_brotli(source, directory, system, machine):
    compiler = shutil.which("clang" if system == "Darwin" else "cc")
    if compiler is None:
        raise RuntimeError("C compiler is required for patched Brotli")
    common = sorted((source / "c/common").glob("*.c"))
    decoder = sorted((source / "c/dec").glob("*.c"))
    if len(common) != 6 or len(decoder) != 6:
        raise RuntimeError("Brotli source list changed; review the native recipe")
    flags = [compiler, "-O2", "-fPIC", "-DBROTLI_SHARED_COMPILATION", "-I", source / "c/include"]
    if system == "Darwin":
        flags += ["-arch", machine, "-mmacosx-version-min=14.4", "-dynamiclib"]
        names = {kind: "libbrotli" + kind + ".1.2.1.dylib" for kind in ("common", "dec")}
        identity = lambda name: ["-Wl,-install_name,@rpath/" + name, "-Wl,-compatibility_version,1", "-Wl,-current_version,1.2.1"]
    else:
        flags += ["-shared", "-Wl,-z,relro,-z,now", "-Wl,-rpath,$ORIGIN"]
        names = {kind: "libbrotli" + kind + "-callimachus.so.1.2.1" for kind in ("common", "dec")}
        identity = lambda name: ["-Wl,-soname," + name]
    common_path, decoder_path = (directory / names[kind] for kind in ("common", "dec"))
    run(flags + identity(names["common"]) + ["-o", common_path] + common)
    run(flags + identity(names["dec"]) + ["-o", decoder_path] + decoder + [common_path, "-lm"])
    if system == "Darwin":
        run(["install_name_tool", "-change", "@rpath/" + names["common"], "@loader_path/" + names["common"], decoder_path])
        for binary in (common_path, decoder_path):
            run(["codesign", "--force", "--sign", "-", binary])
    library = ctypes.CDLL(str(decoder_path))
    library.BrotliDecoderVersion.restype = ctypes.c_uint32
    if library.BrotliDecoderVersion() != 0x1002001:
        raise RuntimeError("compiled BrotliDecoderVersion is not 1.2.1")
    return common_path, decoder_path


def native_files(tree, system):
    if system == "Darwin":
        return sorted(path for path in tree.rglob("*") if path.is_file() and path.suffix in {".so", ".dylib"})
    return sorted(path for path in tree.rglob("*") if path.is_file() and re.search(r"\.so(?:\.\d+)*$", path.name))


def replace_libraries(tree, source, system, machine):
    old = sorted(path for path in tree.rglob("libbrotli*") if path.is_file())
    if len(old) != 2 or not all("1.2.0" in path.name for path in old) or {re.match(r"libbrotli(common|dec)", p.name).group(1) for p in old} != {"common", "dec"}:
        raise RuntimeError("upstream Pillow bundled Brotli layout changed; review before repair")
    directory = old[0].parent
    if any(path.parent != directory for path in old):
        raise RuntimeError("unexpected Brotli library directories")
    new = compile_brotli(source, directory, system, machine)
    replacements = {path.name: next(p for p in new if p.name.startswith("libbrotli" + re.match(r"libbrotli(common|dec)", path.name).group(1))) for path in old}
    changed = set(new)
    for binary in native_files(tree, system):
        if binary in old:
            continue
        if system == "Darwin":
            dependencies = run(["otool", "-L", binary], capture_output=True, text=True).stdout.splitlines()[1:]
            for line in dependencies:
                dependency = line.strip().split(" (", 1)[0]
                name = PurePosixPath(dependency).name
                if name in replacements:
                    relative = os.path.relpath(replacements[name], binary.parent).replace(os.sep, "/")
                    run(["install_name_tool", "-change", dependency, "@loader_path/" + relative, binary])
                    changed.add(binary)
        else:
            dependencies = run(["patchelf", "--print-needed", binary], capture_output=True, text=True).stdout.splitlines()
            for dependency in dependencies:
                if dependency in replacements:
                    run(["patchelf", "--replace-needed", dependency, replacements[dependency].name, binary])
                    changed.add(binary)
    for binary in old:
        binary.unlink()
    if system == "Darwin":
        for binary in sorted(changed):
            run(["codesign", "--force", "--sign", "-", binary])
    for binary in native_files(tree, system):
        command = ["otool", "-L", binary] if system == "Darwin" else ["patchelf", "--print-needed", binary]
        dependencies = run(command, capture_output=True, text=True).stdout
        if any(name in dependencies for name in replacements):
            raise RuntimeError("an upstream Brotli 1.2.0 load command remains")
    return sorted(changed)


def write_provenance(tree, info, source, recipe, binaries, *, method="shared-library-repair"):
    if method == "shared-library-repair":
        brotli = next(row for row in recipe["source_overrides"] if row["name"] == "Brotli")
    elif method == "source-build":
        brotli = recipe["pillow_brotli_native_source"]
    else:
        raise ValueError("unsupported Pillow Brotli provenance method")
    pin = recipe["pillow_brotli_pin"]
    license_path = info / "licenses" / "Brotli-1.2.1.txt"
    license_path.parent.mkdir(exist_ok=True)
    license_path.write_text("Brotli 1.2.1\nMIT License\n\n" + (source / "LICENSE").read_text(), encoding="utf-8")
    # Keep upstream combined license attribution accurate after replacing code.
    for path in info.rglob("*"):
        if path.is_file() and "license" in path.name.lower() and path != license_path:
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"(?i)(brotli[-_ ]+)1\.2\.0\b", r"\g<1>1.2.1", text)
            path.write_text(text, encoding="utf-8")
    relative = lambda path: path.relative_to(tree).as_posix()
    receipt = {
        "format": "callimachus.pillow-native-security.v1",
        "owner": {"name": "Pillow", "version": "13.0.0"},
        "method": method,
        "component": {"name": "Brotli", "version": "1.2.1",
            "source": {key: brotli[key] for key in ("filename", "url", "sha256", "size_bytes")},
            "upstream_pin": {"commit": pin["upstream_commit"], "path": pin["path"],
                "upstream_sha256": pin["original_sha256"], "patched_sha256": pin["patched_sha256"],
                "from_version": pin["from_version"], "to_version": pin["to_version"]}},
        "license": {"path": relative(license_path), "sha256": sha256(license_path)},
        "binary_files": [{"path": relative(path), "sha256": sha256(path)} for path in binaries],
    }
    (info / RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def pack_wheel(tree, info, destination, tag):
    metadata = (info / "WHEEL").read_text(encoding="utf-8")
    metadata = re.sub(r"(?m)^(?:Tag|Build):.*\n?", "", metadata)
    (info / "WHEEL").write_text(metadata.rstrip() + "\nBuild: 1callimachus\nTag: cp313-cp313-" + tag + "\n", encoding="utf-8")
    record = info / "RECORD"
    rows = []
    for path in sorted(tree.rglob("*")):
        if path.is_file() and path != record:
            digest = base64.urlsafe_b64encode(bytes.fromhex(sha256(path))).rstrip(b"=").decode("ascii")
            rows.append([path.relative_to(tree).as_posix(), "sha256=" + digest, str(path.stat().st_size)])
    rows.append([record.relative_to(tree).as_posix(), "", ""])
    with record.open("w", newline="", encoding="utf-8") as output:
        csv.writer(output, lineterminator="\n").writerows(rows)
    wheel = destination / ("pillow-13.0.0-1callimachus-cp313-cp313-" + tag + ".whl")
    with zipfile.ZipFile(wheel, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(tree).as_posix())
    return wheel


def prepare(work):
    system, machine = platform.system(), platform.machine().lower()
    if sys.version_info[:2] != (3, 13) or system not in {"Linux", "Darwin"}:
        raise RuntimeError("repair requires CPython3.13 Linux/macOS; Windows needs a qualified source-built wheel")
    if machine not in {"x86_64", "arm64"} or system == "Linux" and machine != "x86_64":
        raise RuntimeError("unsupported Pillow release architecture")
    if system == "Linux":
        family, version = platform.libc_ver()
        if family != "glibc" or tuple(map(int, version.split("."))) > (2, 35):
            raise RuntimeError("build on the Ubuntu22.04/glibc2.35 release baseline")
    recipe = json.loads(RECIPE.read_text(encoding="utf-8"))
    brotli = next(row for row in recipe["source_overrides"] if row["name"] == "Brotli")
    inputs = json.loads(WHEEL_INPUTS.read_text(encoding="utf-8"))
    wheel_row = inputs[system + "-" + machine]
    source_archive, original = download(brotli, work), download(wheel_row, work)
    with tempfile.TemporaryDirectory(prefix="repair-", dir=work) as temporary:
        temporary = Path(temporary)
        tree = temporary / "wheel"
        tree.mkdir()
        info = unpack_wheel(original, tree)
        source = extract_brotli(source_archive, temporary)
        binaries = replace_libraries(tree, source, system, machine)
        write_provenance(tree, info, source, recipe, binaries)
        tag = "linux_x86_64" if system == "Linux" else "macosx_14_0_" + machine
        wheel = pack_wheel(tree, info, work, tag)
    return wheel


def validate_qualified_wheel(wheel, work):
    from importlib.metadata import Distribution
    with tempfile.TemporaryDirectory(prefix="validate-", dir=work) as temporary:
        tree = Path(temporary)
        info = unpack_wheel(wheel, tree)
        audit = load_sibling("dependency_audit")
        audit._pillow_brotli_vendor(
            ROOT, {"pillow": "13.0.0", "brotli": "1.2.1"},
            distribution=Distribution.at(info))


def install(wheel):
    load_sibling("bootstrap_pip").verify_installed_pip()
    run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--force-reinstall", wheel])
    audit = load_sibling("dependency_audit")
    inventory = audit.installed_inventory()
    audit._pillow_brotli_vendor(ROOT, inventory)
    smoke = """import io
from PIL import Image, features, _imagingft
assert all(features.check(name) for name in ('jpg','zlib','libtiff','freetype2','littlecms2','webp','avif','jpg_2000'))
image=Image.new('RGB',(16,16),(20,40,60)); stream=io.BytesIO(); image.save(stream,format='PNG'); stream.seek(0)
assert Image.open(stream).convert('RGB').getpixel((0,0)) == (20,40,60)
print('Pillow image/codecs smoke passed')
"""
    run([sys.executable, "-c", smoke])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/security-pillow")
    parser.add_argument("--wheel", type=Path, help="already qualified source-built Windows wheel")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    wheel = args.wheel.resolve(strict=True) if args.wheel else prepare(args.work_dir.resolve())
    validate_qualified_wheel(wheel, args.work_dir)
    if args.install:
        install(wheel)
    print(str(wheel), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())