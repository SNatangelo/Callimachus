#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the Intel macOS cryptography wheel from authenticated source inputs."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import venv
import zipfile

CRYPTOGRAPHY_VERSION = "50.0.1"
CRYPTOGRAPHY_URL = "https://files.pythonhosted.org/packages/bb/ad/5d6702db60b1e40b41ef513b6967ff5848f307d50f8449baf1634f5908f1/cryptography-50.0.1.tar.gz"
CRYPTOGRAPHY_SHA256 = "5dd9bda1c12b4162f6ff568eeb5e0ff956c28d14406e875cfe8a63a2d414ff20"
OPENSSL_VERSION = "3.5.9"
OPENSSL_URL = "https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz"
OPENSSL_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
RUST_VERSION = "1.99.1"
RUST_DATE = "2026-10-15"
RUST_MANIFEST_URL = "https://static.rust-lang.org/dist/channel-rust-1.99.1.toml"
RUST_MANIFEST_SHA256 = "fb152be80975c194aa5fc684b2aec3a6e14e2b855efe2faa4e5f8ae6e590f463"
RUST_COMPONENTS = {
    "rustc": "203feef2c2165d7dda1f30ccf75590f72a6abc52ce259ed1c418de84a6887698",
    "cargo": "6636dabcec164909d1c6eb9e2c596148787c84f7d3a02ff68a141569077f80b5",
    "rust-std": "484d5b88b424ddd29df196e1eb9420c8335bb5fac90a3c37140253345a8c65c4",
}
RUST_COMPONENT_VERSIONS = {
    "rustc": "1.99.1 (58cf8f9c3 2026-10-13)",
    "cargo": "0.100.0 (5f94df478 2026-08-27)",
    "rust-std": "1.99.1 (58cf8f9c3 2026-10-13)",
}
MACOS_MINIMUM = "14.4"
BUILD_REQUIREMENTS = {
    "cffi": "2.1.1",
    "maturin": "1.14.1",
    "packaging": "26.3",
    "pycparser": "2.23",
    "setuptools": "84.1.0",
    "wheel": "0.46.3",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_FAILURE_OUTPUT_RE = re.compile(
    r"\b(?:error|fatal|failed|failure)\b|undefined reference|"
    r"no rule to make target|make(?:\[\d+\])?: \*\*\*",
    re.IGNORECASE,
)


def _bounded_output_excerpt(output: str, *, limit: int = 6_000) -> str:
    if not output:
        return "(empty)"
    if len(output) <= limit:
        return output.strip() or "(empty)"

    head = output[:1_200]
    tail = output[-2_400:]
    omitted = max(0, len(output) - len(head) - len(tail))
    excerpt = f"{head}\n...[{omitted} characters omitted]...\n{tail}"

    useful_lines = []
    for line in output.splitlines():
        match = _FAILURE_OUTPUT_RE.search(line)
        if not match:
            continue
        start = max(0, match.start() - 100)
        useful_lines.append(line[start:start + 400])
    useful = "\n".join(useful_lines[-8:])[-1_800:]
    if useful and useful not in excerpt:
        excerpt += f"\nActionable lines from full output:\n{useful}"
    return excerpt


def _write_failure_logs(directory: Path, stdout: str, stderr: str) -> str:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "openssl-make.stdout.log").write_text(
            stdout, encoding="utf-8", newline="\n",
        )
        (directory / "openssl-make.stderr.log").write_text(
            stderr, encoding="utf-8", newline="\n",
        )
    except OSError as exc:
        return f"\nCould not write full OpenSSL build logs under {directory}: {exc}"
    return f"\nFull OpenSSL build logs: {directory}"


def run(
    args: list[str], *, env: dict[str, str] | None = None,
    cwd: Path | None = None, failure_log_dir: Path | None = None,
) -> str:
    try:
        result = subprocess.run(args, check=True, cwd=cwd, env=env, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        log_reference = (
            _write_failure_logs(failure_log_dir, stdout, stderr)
            if failure_log_dir is not None else ""
        )
        detail = (
            f"stdout (bounded excerpt):\n{_bounded_output_excerpt(stdout)}\n"
            f"stderr (bounded excerpt):\n{_bounded_output_excerpt(stderr)}"
        )
        raise RuntimeError(
            f"command failed ({exc.returncode}): {args[0]}{log_reference}\n{detail}"
        ) from exc
    return result.stdout.strip()


def download(url: str, destination: Path, expected: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=180) as response, destination.open("xb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch for {url}: {actual} != {expected}")


def check_rust_manifest(path: Path) -> dict[str, str]:
    manifest_bytes = path.read_bytes()
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if actual != RUST_MANIFEST_SHA256:
        raise RuntimeError(f"Rust channel manifest SHA-256 mismatch: {actual}")
    manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("date") != RUST_DATE:
        raise RuntimeError(f"unexpected Rust toolchain manifest date: {manifest.get('date')!r}")
    target = "x86_64-apple-darwin"
    urls: dict[str, str] = {}
    for component, expected_hash in RUST_COMPONENTS.items():
        package = manifest["pkg"][component]
        if package.get("version") != RUST_COMPONENT_VERSIONS[component]:
            raise RuntimeError(f"unexpected {component} version in Rust channel manifest")
        archive = package["target"][target]
        if archive.get("xz_hash") != expected_hash:
            raise RuntimeError(f"{component} archive hash differs from the pinned Rust manifest")
        url = archive.get("xz_url", "")
        expected_url = f"https://static.rust-lang.org/dist/{RUST_DATE}/{component}-{RUST_VERSION}-{target}.tar.xz"
        if url != expected_url:
            raise RuntimeError(f"unexpected Rust component URL for {component}: {url}")
        urls[component] = url
    return urls


def verify_rust_cli_versions(
    rustc_version: str,
    cargo_version: str,
    rust_verbose: str,
    cargo_verbose: str,
) -> dict[str, str]:
    cargo_metadata = re.fullmatch(
        r"0\.\d+\.\d+ \(([0-9a-f]{9,40}) (\d{4}-\d{2}-\d{2})\)",
        RUST_COMPONENT_VERSIONS["cargo"],
    )
    if cargo_metadata is None:
        raise RuntimeError("invalid authenticated Cargo component metadata")
    expected_rustc = f"rustc {RUST_COMPONENT_VERSIONS['rustc']}"
    cargo_channel = ".".join(RUST_VERSION.split(".")[:2])
    cargo_cli = re.fullmatch(
        rf"cargo ({re.escape(cargo_channel)}\.\d+) \(([0-9a-f]{{9,40}}) (\d{{4}}-\d{{2}}-\d{{2}})\)",
        cargo_version,
    )
    if rustc_version != expected_rustc:
        raise RuntimeError(f"rustc CLI version differs from authenticated metadata: {rustc_version}")
    if (
        cargo_cli is None
        or cargo_cli.group(2) != cargo_metadata.group(1)
        or cargo_cli.group(3) != cargo_metadata.group(2)
        or not cargo_verbose.splitlines()
        or cargo_verbose.splitlines()[0] != cargo_version
    ):
        raise RuntimeError(f"Cargo CLI version differs from authenticated release metadata: {cargo_version}")
    cargo_release = cargo_cli.group(1)
    rust_lines = set(rust_verbose.splitlines())
    cargo_fields = {}
    for line in cargo_verbose.splitlines()[1:]:
        key, separator, value = line.partition(": ")
        if separator:
            cargo_fields[key] = value
    if "host: x86_64-apple-darwin" not in rust_lines or cargo_fields.get("host") != "x86_64-apple-darwin":
        raise RuntimeError("Rust compiler and Cargo host triples must be x86_64-apple-darwin")
    commit_hash = cargo_fields.get("commit-hash", "")
    if (
        cargo_fields.get("release") != cargo_release
        or not re.fullmatch(r"[0-9a-f]{9,40}", commit_hash)
        or not commit_hash.startswith(cargo_metadata.group(1))
        or cargo_fields.get("commit-date") != cargo_metadata.group(2)
    ):
        raise RuntimeError("verbose Cargo commit or release differs from authenticated component metadata")
    return {
        "rustc_cli": rustc_version,
        "cargo_cli": cargo_version,
        "cargo_release": cargo_release,
        "cargo_commit_prefix": cargo_metadata.group(1),
        "cargo_commit_date": cargo_metadata.group(2),
        "cargo_host": cargo_fields["host"],
    }


def validate_apple_clang(developer_dir: Path, compiler_path: Path, version: str) -> Path:
    developer = developer_dir.resolve(strict=True)
    compiler = compiler_path.resolve(strict=True)
    if not compiler.is_file() or not compiler.is_relative_to(developer):
        raise RuntimeError(f"xcrun clang is outside the selected Apple developer directory: {compiler}")
    if not re.match(r"^Apple clang version \d+(?:\.\d+)+\b", version):
        raise RuntimeError(f"selected compiler is not Apple Clang: {version}")
    return compiler


def verify_wheel_tag(
    wheel_name: str,
    *,
    python_version: tuple[int, int] = (3, 13),
    macos_version: tuple[int, int] = (14, 4),
    architecture: str = "x86_64",
) -> dict[str, list[str]]:
    from packaging.tags import cpython_tags, mac_platforms
    from packaging.utils import canonicalize_name, parse_wheel_filename

    try:
        distribution, version, _build, tags = parse_wheel_filename(wheel_name)
    except Exception as exc:
        raise RuntimeError(f"invalid cryptography wheel filename: {wheel_name}") from exc
    if canonicalize_name(distribution) != "cryptography" or str(version) != CRYPTOGRAPHY_VERSION:
        raise RuntimeError(f"unexpected cryptography wheel identity: {wheel_name}")
    platforms = {tag.platform for tag in tags}
    compatible_platforms = list(mac_platforms(version=macos_version, arch=architecture))
    normalized_target = compatible_platforms[0] if compatible_platforms else None
    if not platforms or platforms != {normalized_target} or any(
        not platform.startswith("macosx_") or not platform.endswith(f"_{architecture}")
        for platform in platforms
    ):
        raise RuntimeError(f"wheel platform tags do not match the normalized macOS {MACOS_MINIMUM} {architecture} target: {sorted(platforms)}")
    if any(tag.abi != "abi3" or not tag.interpreter.startswith("cp") for tag in tags):
        raise RuntimeError("cryptography wheel must use CPython stable-ABI tags")
    runtime_tags = set(cpython_tags(
        python_version=python_version,
        abis=["abi3"],
        platforms=platforms,
    ))
    if not tags.intersection(runtime_tags):
        raise RuntimeError(f"cryptography wheel is not compatible with CPython {python_version[0]}.{python_version[1]}")
    return {
        "python_abi_tags": sorted({f"{tag.interpreter}-{tag.abi}" for tag in tags}),
        "platform_tags": sorted(platforms),
    }


def validate_macos_minimums(minimums: list[str], maximum: str = MACOS_MINIMUM) -> list[str]:
    def key(value: str) -> tuple[int, int, int] | None:
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
            return None
        parts = [int(part) for part in value.split(".")]
        return tuple((parts + [0, 0])[:3])

    limit = key(maximum)
    checked = [key(value) for value in minimums]
    if not minimums or limit is None or any(value is None or value > limit for value in checked):
        raise RuntimeError(f"Mach-O minimum version is missing, invalid, or above macOS {maximum}: {minimums}")
    return minimums


def read_record(wheel: Path) -> tuple[str, list[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("wheel contains duplicate archive paths")
        records = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            raise RuntimeError("wheel must contain exactly one RECORD")
        rows = list(csv.reader(io.StringIO(archive.read(records[0]).decode("utf-8"))))
    return records[0], [row[0] for row in rows if row]


def verify_wheel_record(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise RuntimeError("wheel must contain exactly one RECORD")
        record_name = record_names[0]
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
        by_name = {row[0]: row for row in rows if row}
        if len(rows) != len(names) or len(by_name) != len(rows):
            raise RuntimeError("wheel RECORD has missing or duplicate member paths")
        if set(by_name) != names:
            raise RuntimeError("wheel RECORD paths do not match wheel members")
        for name in names - {record_name}:
            body = archive.read(name)
            row = by_name[name]
            expected_hash = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")
            if len(row) != 3 or row[1] != expected_hash or row[2] != str(len(body)):
                raise RuntimeError(f"wheel RECORD checksum or size mismatch: {name}")
        if by_name[record_name] != [record_name, "", ""]:
            raise RuntimeError("wheel RECORD must leave its own hash and size empty")


def add_openssl_license(wheel: Path, license_body: bytes) -> None:
    if not license_body.strip() or b"Apache License" not in license_body:
        raise RuntimeError("pinned OpenSSL source archive has no expected Apache license text")
    record_name, _ = read_record(wheel)
    license_name = record_name.rsplit("/", 1)[0] + "/licenses/OpenSSL-LICENSE.txt"
    temporary = wheel.with_name(wheel.name + ".rewritten")
    if temporary.exists():
        raise FileExistsError(f"refusing to overwrite wheel rewrite: {temporary}")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(temporary, "w") as target:
        if license_name in source.namelist():
            raise RuntimeError("cryptography wheel already contains the OpenSSL license path")
        members: dict[str, bytes] = {}
        infos = {item.filename: item for item in source.infolist()}
        for name in source.namelist():
            if name != record_name:
                members[name] = source.read(name)
        members[license_name] = license_body
        for name, body in members.items():
            target.writestr(infos.get(name, name), body)
        rows = []
        for name, body in members.items():
            digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")
            rows.append([name, "sha256=" + digest, str(len(body))])
        rows.append([record_name, "", ""])
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        target.writestr(infos[record_name], output.getvalue().encode("utf-8"))
    temporary.replace(wheel)
    verify_wheel_record(wheel)


def inspect_extension(path: Path, root: Path) -> tuple[str, str, list[str]]:
    if run(["xcrun", "lipo", "-archs", str(path)]) != "x86_64":
        raise RuntimeError("cryptography extension is not a single-architecture Intel binary")
    audit = runpy.run_path(str(root / "packaging" / "smoke_desktop.py"))
    load_commands = run(["/usr/bin/otool", "-arch", "x86_64", "-l", str(path)])
    minimums = audit["_macos_minimum_versions"](load_commands)
    validate_macos_minimums(minimums)
    linkage = run(["/usr/bin/otool", "-L", str(path)])
    dependencies = []
    for line in linkage.splitlines()[1:]:
        item = line.strip().split(" (compatibility", 1)[0]
        if not item:
            continue
        dependencies.append(item)
        if not item.startswith(("/usr/lib/", "/System/Library/")):
            raise RuntimeError(f"cryptography extension has non-system dynamic dependency: {item}")
    if any(re.search(r"lib(?:ssl|crypto)(?:\.|$)", Path(item).name) for item in dependencies):
        raise RuntimeError("cryptography extension dynamically links OpenSSL instead of the pinned static build")
    return ",".join(minimums), hashlib.sha256(load_commands.encode()).hexdigest(), dependencies


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    runtime_python = Path(os.environ.get("RUNTIME_PYTHON", ""))
    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.system() != "Darwin" or platform.machine() != "x86_64":
        raise SystemExit("Intel cryptography source build is restricted to the native GitHub macOS x64 runner")
    for name in tuple(os.environ):
        if name.startswith("PIP_") or name.startswith(("CARGO_", "RUSTUP_")):
            os.environ.pop(name)
    os.environ.update({
        "PIP_INDEX_URL": "https://pypi.org/simple",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for name in ("PYTHONPATH", "PYTHONHOME", "RUSTFLAGS", "RUSTC", "RUSTDOC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "OPENSSL_DIR", "OPENSSL_LIB_DIR", "OPENSSL_INCLUDE_DIR", "OPENSSL_LIBS", "OPENSSL_STATIC", "DEVELOPER_DIR", "TOOLCHAINS", "SDKROOT", "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "CPPFLAGS", "CXXFLAGS", "PKG_CONFIG", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        os.environ.pop(name, None)
    if not runtime_python.is_file():
        raise SystemExit("RUNTIME_PYTHON must name the verified Python.org application interpreter")
    runtime = run([str(runtime_python), "-c", "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{platform.machine()}')"])
    if runtime != "3.13.15|x86_64":
        raise SystemExit(f"expected runtime Python 3.13.15 x86_64, found {runtime}")
    output_root = root / "build" / "security-cryptography"
    if output_root.exists():
        raise SystemExit(f"refusing to overwrite existing cryptography build outputs: {output_root}")
    output_root.mkdir(parents=True)
    wheelhouse = output_root / "wheelhouse"
    wheelhouse.mkdir()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    work = Path(tempfile.mkdtemp(prefix="callimachus-cryptography-x86_64-", dir=runner_temp))
    try:
        source_archive = work / "cryptography-50.0.1.tar.gz"
        openssl_archive = work / "openssl-3.5.9.tar.gz"
        rust_manifest_path = work / "channel-rust-1.99.1.toml"
        download(CRYPTOGRAPHY_URL, source_archive, CRYPTOGRAPHY_SHA256)
        download(OPENSSL_URL, openssl_archive, OPENSSL_SHA256)
        download(RUST_MANIFEST_URL, rust_manifest_path, RUST_MANIFEST_SHA256)
        rust_urls = check_rust_manifest(rust_manifest_path)
        rust_archives: dict[str, Path] = {}
        for component, url in rust_urls.items():
            path = work / f"{component}-1.99.1-x86_64-apple-darwin.tar.xz"
            download(url, path, RUST_COMPONENTS[component])
            rust_archives[component] = path

        source_root = work / "cryptography-source"
        source_root.mkdir()
        with tarfile.open(source_archive, "r:gz") as archive:
            archive.extractall(source_root, filter="data")
        source_dirs = list(source_root.glob("cryptography-50.0.1"))
        if len(source_dirs) != 1:
            raise RuntimeError("unexpected cryptography source archive layout")
        source_dir = source_dirs[0]
        cargo_locks = list(source_dir.rglob("Cargo.lock"))
        if len(cargo_locks) != 1:
            raise RuntimeError(f"expected one authenticated Cargo.lock, found {len(cargo_locks)}")
        cargo_lock = cargo_locks[0]
        cargo_manifest = cargo_lock.parent / "Cargo.toml"
        if not cargo_manifest.is_file():
            raise RuntimeError("authenticated Cargo.lock has no adjacent Cargo.toml")
        cargo_lock_sha = sha256_file(cargo_lock)
        with tarfile.open(openssl_archive, "r:gz") as archive:
            license_members = [m for m in archive.getmembers() if m.name == "openssl-3.5.9/LICENSE.txt"]
            if len(license_members) != 1:
                raise RuntimeError("pinned OpenSSL source archive does not have one LICENSE.txt")
            license_stream = archive.extractfile(license_members[0])
            if license_stream is None:
                raise RuntimeError("could not read OpenSSL license text")
            openssl_license = license_stream.read()
            license_stream.close()

        openssl_root = work / "openssl-source"
        openssl_root.mkdir()
        with tarfile.open(openssl_archive, "r:gz") as archive:
            archive.extractall(openssl_root, filter="data")
        openssl_source = openssl_root / "openssl-3.5.9"
        if not (openssl_source / "Configure").is_file():
            raise RuntimeError("pinned OpenSSL source archive layout is unexpected")
        developer_dir = Path(run(["/usr/bin/xcode-select", "-p"])).resolve(strict=True)
        clang_candidate = Path(run(["/usr/bin/xcrun", "--find", "clang"]))
        clang_version = run([str(clang_candidate), "--version"]).splitlines()[0]
        clang = validate_apple_clang(developer_dir, clang_candidate, clang_version)
        run(["/usr/bin/codesign", "--verify", "--strict", str(clang)])
        sdk = Path(run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"])).resolve(strict=True)
        if not sdk.is_relative_to(developer_dir):
            raise RuntimeError(f"macOS SDK is outside the selected Apple developer directory: {sdk}")
        openssl_prefix = work / "openssl-prefix"
        build_env = os.environ.copy()
        for name in ("LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "CPPFLAGS", "CXXFLAGS", "PKG_CONFIG", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            build_env.pop(name, None)
        for name in tuple(build_env):
            if name.startswith(("CARGO_", "RUSTUP_")) or name == "RUSTFLAGS":
                build_env.pop(name)
        build_env.update({
            "CC": str(clang),
            "CFLAGS": f"-O2 -fPIC -arch x86_64 -mmacosx-version-min={MACOS_MINIMUM} -isysroot {sdk}",
            "LDFLAGS": f"-arch x86_64 -mmacosx-version-min={MACOS_MINIMUM} -isysroot {sdk}",
            "MACOSX_DEPLOYMENT_TARGET": MACOS_MINIMUM,
            "SDKROOT": str(sdk),
            "DEVELOPER_DIR": str(developer_dir),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PERL": "/usr/bin/perl",
        })
        run(["./Configure", "darwin64-x86_64-cc", "no-shared", "no-tests", f"--prefix={openssl_prefix}", f"--openssldir={openssl_prefix / 'ssl'}"], env=build_env, cwd=openssl_source)
        run(["/usr/bin/make", "-j2", "install_sw"], env=build_env, cwd=openssl_source,
            failure_log_dir=root / "build" / "desktop" / "macos-cryptography-failure-logs")
        openssl_version = run([str(openssl_prefix / "bin" / "openssl"), "version"], env=build_env)
        if not openssl_version.startswith("OpenSSL 3.5.9 "):
            raise RuntimeError(f"built static OpenSSL is not the pinned 3.5.9 release: {openssl_version}")
        if not all((openssl_prefix / "lib" / name).is_file() for name in ("libssl.a", "libcrypto.a")):
            raise RuntimeError("OpenSSL static archives are missing")
        if list((openssl_prefix / "lib").glob("*.dylib")):
            raise RuntimeError("OpenSSL build unexpectedly installed dynamic libraries")

        rust_prefix = work / "rust-prefix"
        rust_prefix.mkdir()
        for component, archive_path in rust_archives.items():
            extracted = work / f"rust-{component}-extract"
            extracted.mkdir()
            run(["/usr/bin/tar", "-xf", str(archive_path), "-C", str(extracted)])
            installers = list(extracted.glob(f"{component}-1.99.1-x86_64-apple-darwin/install.sh"))
            if len(installers) != 1:
                raise RuntimeError(f"missing verified Rust {component} installer")
            run(["/bin/sh", str(installers[0]), f"--prefix={rust_prefix}", "--disable-ldconfig"])
        cargo = rust_prefix / "bin" / "cargo"
        rustc = rust_prefix / "bin" / "rustc"
        rust_version = run([str(rustc), "--version"])
        cargo_version = run([str(cargo), "--version"])
        rust_verbose = run([str(rustc), "-vV"])
        cargo_verbose = run([str(cargo), "-Vv"])
        rust_cli_proof = verify_rust_cli_versions(rust_version, cargo_version, rust_verbose, cargo_verbose)
        cargo_home = work / "cargo-home"
        cargo_home.mkdir()
        env = build_env.copy()
        env.update({
            "PATH": str(rust_prefix / "bin") + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
            "CARGO_HOME": str(cargo_home),
            "CARGO_NET_RETRY": "3",
        })
        run([str(cargo), "fetch", "--locked", "--manifest-path", str(cargo_manifest)], env=env)
        if sha256_file(cargo_lock) != cargo_lock_sha:
            raise RuntimeError("cryptography source Cargo.lock changed during dependency fetch")

        venv_path = output_root / "build-venv"
        venv.EnvBuilder(with_pip=True, clear=False).create(venv_path)
        vpy = venv_path / "bin" / "python"
        run([str(vpy), str(root / "packaging" / "bootstrap_pip.py")])
        build_constraints = root / "packaging" / "macos-cryptography-build-constraints.txt"
        release_constraints = root / "packaging" / "release-constraints.txt"
        requirements = [f"{name}=={version}" for name, version in BUILD_REQUIREMENTS.items()]
        run([str(vpy), "-m", "pip", "install", "--only-binary=:all:", "-c", str(release_constraints), "-c", str(build_constraints), *requirements])
        run([str(vpy), "-m", "pip", "check"])
        run([str(vpy), str(root / "packaging" / "dependency_audit.py"), "--report", str(output_root / "build-dependency-audit.json")])

        rust_env = env.copy()
        rust_env.update({
            "OPENSSL_DIR": str(openssl_prefix),
            "OPENSSL_STATIC": "1",
            "MACOSX_DEPLOYMENT_TARGET": MACOS_MINIMUM,
            "RUSTFLAGS": f"-C link-arg=-mmacosx-version-min={MACOS_MINIMUM}",
            "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER": str(clang),
            "CARGO_NET_OFFLINE": "true",
            "PIP_INDEX_URL": "https://pypi.org/simple",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
        })
        run([str(vpy), "-m", "pip", "wheel", "--no-build-isolation", "--no-cache-dir", "--only-binary=cffi,pycparser", "--wheel-dir", str(wheelhouse), "-c", str(release_constraints), "-c", str(build_constraints), str(source_archive)], env=rust_env)
        wheels = list(wheelhouse.glob("cryptography-50.0.1-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one built cryptography wheel, found {wheels}")
        wheel = wheels[0]
        wheel_tag_proof = verify_wheel_tag(wheel.name)
        verify_wheel_record(wheel)
        add_openssl_license(wheel, openssl_license)
        openssl_license_output = output_root / "OpenSSL-LICENSE.txt"
        with openssl_license_output.open("xb") as output:
            output.write(openssl_license)
        extracted_extension = work / "cryptography-rust-extension.so"
        with zipfile.ZipFile(wheel) as archive:
            candidates = [name for name in archive.namelist() if re.fullmatch(r"cryptography/hazmat/bindings/_rust(?:\.abi3)?\.so", name)]
            if len(candidates) != 1:
                raise RuntimeError(f"expected one cryptography Rust extension in wheel: {candidates}")
            extracted_extension.write_bytes(archive.read(candidates[0]))
            wheel_license_names = [name for name in archive.namelist() if name.endswith("/licenses/OpenSSL-LICENSE.txt")]
            if len(wheel_license_names) != 1:
                raise RuntimeError("static OpenSSL license is not present exactly once in wheel RECORD")
        minimums, load_commands_sha, dynamic_dependencies = inspect_extension(extracted_extension, root)

        run([str(vpy), "-m", "pip", "install", "--only-binary=:all:", "--find-links", str(wheelhouse), "-c", str(release_constraints), "-c", str(build_constraints), "cryptography==50.0.1"], env=rust_env)
        run([str(vpy), "-m", "pip", "check"])
        smoke = run([str(vpy), "-c", "from cryptography.hazmat.backends.openssl.backend import backend; from cryptography.hazmat.primitives.ciphers.aead import AESGCM; key=b'\\x00'*32; nonce=b'\\x01'*12; cipher=AESGCM(key); assert cipher.decrypt(nonce,cipher.encrypt(nonce,b'qualified',None),None)==b'qualified'; print(backend.openssl_version_text())"], env=rust_env)
        if not smoke.startswith("OpenSSL 3.5.9 "):
            raise RuntimeError(f"cryptography did not use the pinned static OpenSSL: {smoke}")
        run([str(vpy), str(root / "packaging" / "dependency_audit.py"), "--report", str(output_root / "cryptography-dependency-audit.json")])
        (output_root / "build-environment.txt").write_text(run([str(vpy), "-m", "pip", "freeze", "--all"]) + "\n", encoding="utf-8", newline="\n")
        if sha256_file(cargo_lock) != cargo_lock_sha:
            raise RuntimeError("cryptography Cargo.lock changed during the source build")
        provenance = {
            "platform": "macos-x64",
            "deployment_target": MACOS_MINIMUM,
            "runtime_python": runtime,
            "cryptography_version": CRYPTOGRAPHY_VERSION,
            "cryptography_source_url": CRYPTOGRAPHY_URL,
            "cryptography_source_sha256": sha256_file(source_archive),
            "cryptography_wheel": wheel.name,
            "cryptography_wheel_sha256": sha256_file(wheel),
            "openssl_version": openssl_version,
            "openssl_source_url": OPENSSL_URL,
            "openssl_source_sha256": sha256_file(openssl_archive),
            "openssl_license_sha256": hashlib.sha256(openssl_license).hexdigest(),
            "openssl_static": True,
            "rust_manifest_url": RUST_MANIFEST_URL,
            "rust_manifest_sha256": sha256_file(rust_manifest_path),
            "rust_component_versions": RUST_COMPONENT_VERSIONS,
            "rust_component_xz_sha256": RUST_COMPONENTS,
            "rust_component_xz_urls": rust_urls,
            "rustc": rust_version,
            "cargo": cargo_version,
            "cargo_verbose": cargo_verbose,
            "rust_cli_proof": rust_cli_proof,
            "cargo_lock_sha256": cargo_lock_sha,
            "apple_developer_dir": str(developer_dir),
            "clang_path": str(clang),
            "clang_version": clang_version,
            "clang_sha256": sha256_file(clang),
            "macos_sdk": str(sdk),
            "cryptography_wheel_tags": wheel_tag_proof,
            "extension_path": candidates[0],
            "extension_architecture": "x86_64",
            "extension_minimums": minimums,
            "extension_load_commands_sha256": load_commands_sha,
            "extension_dynamic_dependencies": dynamic_dependencies,
            "crypto_openssl_smoke": smoke,
            "build_dependencies": BUILD_REQUIREMENTS,
        }
        provenance_path = root / "build" / "desktop" / "macos-cryptography-provenance-x86_64.json"
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        with provenance_path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(provenance, output, indent=2)
            output.write("\n")
        print(f"Built and verified Intel cryptography {CRYPTOGRAPHY_VERSION} wheel for macOS {MACOS_MINIMUM}.", flush=True)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())