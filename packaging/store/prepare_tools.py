"""Acquire hash-pinned public tool inputs for the Microsoft Store native build.

Requires Python 3.13.15 on Windows x64. Downloads and verifies public inputs,
materializes the Windows SDK tree, and extracts portable build helpers. It does
not run the Visual Studio installer or compile the application.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

STORE = Path(__file__).resolve().parent
PROJECT = STORE.parents[1]
MANIFEST = STORE / "tool-inputs.json"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def download(url: str, destination: Path, *, algorithm: str, expected: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        actual = digest(destination, algorithm)
        if actual != expected:
            raise ValueError(f"Existing tool input has the wrong {algorithm}: {destination}")
        return destination
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise FileExistsError(f"Interrupted download exists; inspect before removing it: {partial}")
    request = urllib.request.Request(url, headers={"User-Agent": "Callimachus-Store-build-input-recovery/1"})
    with urllib.request.urlopen(request, timeout=180) as response, partial.open("xb") as output:
        shutil.copyfileobj(response, output)
    actual = digest(partial, algorithm)
    if actual != expected:
        raise ValueError(f"Downloaded input has the wrong {algorithm}: {partial}")
    partial.rename(destination)
    return destination


def _zip_target(destination: Path, name: str) -> Path:
    member = PurePosixPath(name)
    if member.is_absolute() or any(part in ("", ".", "..") for part in member.parts):
        raise ValueError(f"Unsafe archive member: {name}")
    target = destination.joinpath(*member.parts).resolve()
    if not target.is_relative_to(destination.resolve()):
        raise ValueError(f"Unsafe archive member: {name}")
    return target


def extract_zip(archive: Path, destination: Path):
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite extracted tool input: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="store-tools-", dir=destination.parent) as staging_name:
        staging = Path(staging_name)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError(f"Refusing symbolic link in tool archive: {member.filename}")
                target = _zip_target(staging, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as input_stream, target.open("xb") as output:
                    shutil.copyfileobj(input_stream, output)
        staging.rename(destination)


def extract_sdk_zip(archive: Path, destination: Path, row: dict):
    extract_zip(archive, destination)
    payload = destination / row["payload_subdirectory"]
    if not payload.is_dir():
        raise FileNotFoundError(f"NuGet package does not contain expected payload: {payload}")


def merge_sdk_payload(source: Path, destination: Path):
    """Merge SDK payload paths, rejecting any non-identical overlap."""
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        target = destination / relative
        if source_path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            if not target.is_file() or digest(source_path) != digest(target):
                raise ValueError(f"Conflicting SDK package member: {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)


def fetch_pypi_wheel(row: dict, downloads: Path) -> Path:
    request = urllib.request.Request(row["pypi_json_url"], headers={"User-Agent": "Callimachus-Store-build-input-recovery/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        metadata = json.load(response)
    if metadata.get("info", {}).get("version") != row["version"]:
        raise ValueError(f"PyPI metadata version mismatch for {row['wheel_filename']}")
    matches = [item for item in metadata.get("urls", []) if item.get("filename") == row["wheel_filename"]]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one official PyPI wheel: {row['wheel_filename']}")
    item = matches[0]
    if not item["url"].startswith("https://files.pythonhosted.org/"):
        raise ValueError(f"Wheel URL is outside files.pythonhosted.org: {item['url']}")
    return download(item["url"], downloads / row["wheel_filename"], algorithm="sha256",
                    expected=item["digests"]["sha256"])


def extract_7z(extractor: Path, archive: Path, destination: Path):
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite extracted tool input: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="store-tools-", dir=destination.parent) as staging_name:
        staging = Path(staging_name)
        result = subprocess.run(
            [str(extractor), "x", "-y", f"-o{staging}", str(archive)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"Pinned 7-Zip extraction failed ({result.returncode}):\n{result.stdout[-4000:]}")
        staging.rename(destination)


def extract_tar_xz(archive: Path, destination: Path):
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite extracted tool input: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:xz") as source:
        roots = {PurePosixPath(member.name).parts[0] for member in source if member.name not in ("", ".")}
        if len(roots) != 1:
            raise ValueError(f"Expected one Rust archive root, found {roots}")
        with tempfile.TemporaryDirectory(prefix="store-tools-", dir=destination.parent) as staging_name:
            staging = Path(staging_name)
            source.extractall(staging, filter="data")
            root = next(iter(roots))
            extracted = staging / root
            if not extracted.is_dir():
                raise FileNotFoundError(f"Expected Rust package directory in archive: {root}")
            extracted.rename(destination)


def materialize_rust(distribution: Path) -> Path:
    """Assemble the three Windows toolchain components into a usable prefix."""
    prefix = distribution / "prefix"
    if prefix.exists():
        raise FileExistsError(f"Refusing to overwrite a Rust toolchain: {prefix}")
    components = ("rustc", "cargo", "rust-std-x86_64-pc-windows-msvc")
    for name in components:
        if not (distribution / name / "manifest.in").is_file():
            raise FileNotFoundError(f"Rust installer component is missing: {name}")
    with tempfile.TemporaryDirectory(prefix="rust-prefix-", dir=distribution) as staging_name:
        staging = Path(staging_name)
        for name in components:
            component = distribution / name
            for entry in (component / "manifest.in").read_text(encoding="utf-8").splitlines():
                kind, relative = entry.split(":", 1)
                target = _zip_target(staging, relative)
                source = _zip_target(component, relative)
                if kind == "file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and digest(target) != digest(source):
                        raise ValueError(f"Conflicting Rust component file: {relative}")
                    if not target.exists():
                        shutil.copy2(source, target)
                elif kind == "dir":
                    merge_sdk_payload(source, target)
                else:
                    raise ValueError(f"Unsupported Rust installer manifest entry: {entry}")
        for leaf in ("rustc.exe", "cargo.exe"):
            if not (staging / "bin" / leaf).is_file():
                raise FileNotFoundError(f"Rust toolchain executable is missing: {leaf}")
        staging.rename(prefix)
    return prefix


def rows(manifest: dict):
    yield manifest["bootstrap_python"]
    yield manifest["visual_studio"]["bootstrapper"]
    yield from manifest["windows_sdk"]["packages"]
    yield manifest["vclibs"]["archive"]
    yield from manifest["framework_audit_inputs"]
    yield manifest["qt_libclang"]["archive"]
    yield manifest["qt_libclang"]["extractor"]
    yield manifest["perl"]
    yield manifest["rust"]
    yield manifest["nasm"]


def _download_row(row: dict, downloads: Path, destination: Path | None = None) -> Path:
    if "sha256" not in row:
        raise ValueError(f"No integrity record for {row.get('filename', row)}")
    target = destination or downloads / row["filename"]
    return download(row["url"], target, algorithm="sha256", expected=row["sha256"])


def prepare(tools_root: Path, manifest: dict, *, download_only: bool):
    downloads = tools_root / "_downloads"
    tools_root.mkdir(parents=True, exist_ok=True)

    # Public fixed-hash archives and the Microsoft-signed audit source files.
    downloaded = {}
    for row in rows(manifest):
        if row is manifest["visual_studio"]["bootstrapper"] or row in manifest["framework_audit_inputs"]:
            target = tools_root / row.get("destination", "vclibs-desktop-14.51") / row["filename"]
            downloaded[row["filename"]] = _download_row(row, downloads, target)
        else:
            downloaded[row["filename"]] = _download_row(row, downloads)

    pypi_wheels = {}
    for name in ("cmake", "ninja"):
        row = manifest[name]
        archive = fetch_pypi_wheel(row, downloads)
        pypi_wheels[name] = (row, archive)

    if download_only:
        print(f"Verified {len(downloaded) + len(pypi_wheels)} public tool inputs; no extraction performed")
        return

    # Bootstrap interpreter and SDK package contents are unpacked without an installer.
    python_row = manifest["bootstrap_python"]
    extract_zip(downloaded[python_row["filename"]], tools_root / python_row["destination"])
    python_exe = tools_root / python_row["destination"] / python_row["executable"]
    if not python_exe.is_file():
        raise FileNotFoundError(f"NuGet bootstrap Python executable missing: {python_exe}")

    sdk_root = tools_root / "windows-sdk-headers-libs" / "Windows Kits" / "10"
    for row in manifest["windows_sdk"]["packages"]:
        package_dir = tools_root / "windows-sdk-packages" / row["id"]
        extract_sdk_zip(downloaded[row["filename"]], package_dir, row)
        target = sdk_root / row["sdk_target_subdirectory"]
        merge_sdk_payload(package_dir / row["payload_subdirectory"], target)
    version_root = sdk_root / "Include" / manifest["windows_sdk"]["files_version"]
    if not version_root.is_dir():
        raise FileNotFoundError(f"SDK headers for the pinned version were not recovered: {version_root}")

    # Extract only the exact x64 VCLibs member and check the retained build pin.
    framework = manifest["vclibs"]
    appx_path = tools_root / "vclibs-desktop-14.51" / framework["appx_filename"]
    appx_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(downloaded[framework["archive"]["filename"]]) as archive:
        content = archive.read(framework["member"])
    if hashlib.sha256(content).hexdigest() != framework["appx_sha256"]:
        raise ValueError("Nested VCLibs Appx SHA256 did not match the retained build pin")
    if appx_path.exists():
        if digest(appx_path) != framework["appx_sha256"]:
            raise ValueError(f"Existing VCLibs Appx has the wrong SHA256: {appx_path}")
    else:
        appx_path.write_bytes(content)

    # Official Qt libclang archive uses its separately hash-pinned standalone extractor.
    clang = manifest["qt_libclang"]
    extractor = downloads / clang["extractor"]["filename"]
    extract_7z(extractor, downloaded[clang["archive"]["filename"]], tools_root / clang["destination"])

    # Portable Perl/Rust tool distributions, confined to the checkout.
    perl = manifest["perl"]
    extract_zip(downloaded[perl["filename"]], tools_root / perl["destination"])
    nasm = manifest["nasm"]
    extract_zip(downloaded[nasm["filename"]], tools_root / nasm["destination"])
    rust = manifest["rust"]
    extract_tar_xz(downloaded[rust["filename"]], tools_root / rust["destination"])
    materialize_rust(tools_root / rust["destination"])

    # The CMake and Ninja wheels are verified against the immutable versioned PyPI file records.
    for name, (row, archive) in pypi_wheels.items():
        extract_zip(archive, tools_root / row["destination"])

    expected_tools = [
        tools_root / nasm["destination"] / nasm["executable"],
        tools_root / "python-build-tools/cmake/cmake/data/bin/cmake.exe",
        tools_root / "python-build-tools/ninja/ninja/data/bin/ninja.exe",
    ]
    for path in expected_tools:
        if not path.is_file():
            raise FileNotFoundError(f"Expected build tool was not recovered: {path}")
    print(f"Verified and prepared {len(downloaded) + len(pypi_wheels)} public tool inputs")
    print(f"Manual Visual Studio installation remains: {tools_root / 'vs-build-tools-bootstrap/vs_BuildTools.exe'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-root", type=Path, default=PROJECT / "build/store-tools")
    parser.add_argument("--list", action="store_true", help="Print sources and output paths without network access")
    parser.add_argument("--download-only", action="store_true", help="Download and verify archives but do not extract them")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 13):
        parser.error("Python 3.13 or newer is required; bootstrap Python 3.13.15 is recovered by this script")
    if os.name != "nt":
        parser.error("This tool manifest prepares Windows x64 build inputs")
    tools_root = args.tools_root.resolve()
    if not tools_root.is_relative_to(PROJECT.resolve()):
        parser.error("tools-root must be inside this checkout")
    manifest = read_json(MANIFEST)
    if args.list:
        inputs = [{"filename": row["filename"], "url": row["url"],
                   "verification": row["sha256"],
                   "destination": row.get("destination", "_downloads")}
                  for row in rows(manifest)]
        inputs.extend({"filename": manifest[name]["wheel_filename"],
                       "url": manifest[name]["pypi_json_url"],
                       "verification": "SHA256 from the exact-version PyPI file record",
                       "destination": manifest[name]["destination"]}
                      for name in ("cmake", "ninja"))
        print(json.dumps({"manifest": str(MANIFEST), "tools_root": str(tools_root),
                          "inputs": inputs}, indent=2))
        return
    prepare(tools_root, manifest, download_only=args.download_only)


if __name__ == "__main__":
    main()
