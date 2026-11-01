# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build and validate the pinned CPython 3.13 Windows Pillow wheel."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.request
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INPUTS = Path(__file__).with_name("windows-pillow-inputs.json")
RECIPE = ROOT / "packaging" / "store" / "native-recipes" / "security-source-inputs.json"
WHEEL_NAME = "pillow-13.0.0-1callimachus-cp313-cp313-win_amd64.whl"
WHEEL_LIMIT = 8 * 1024 * 1024
MAX_MEMBERS = 250_000
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
DAV1D_ORIGINAL_SHA256 = "f9971a87e5fcc74e47305490e442eec7ba187dc0f822d6f0b6838f1811cffd68"
DAV1D_PATCHED_SHA256 = "b36c49285d26b3dc0848f90e965b8a371110c369b8b68e7d96c59186e26982a7"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_inputs():
    data = read_json(INPUTS)
    if data.get("schema_version") != 1:
        raise RuntimeError("unsupported Windows Pillow input manifest")
    if len(data["dependency_archives"]) != 11:
        raise RuntimeError("Windows Pillow dependency archive list changed")
    build_names = [row["build_filename"] for row in
                   [*data["dependency_archives"], data["brotli_source"]]]
    if len(build_names) != len(set(build_names)):
        raise RuntimeError("duplicate Pillow dependency archive filename")
    rows = [data["pillow_source"], data["brotli_source"], *data["dependency_archives"],
            *data["avif_nested"], *data["python_tool_wheels"], data["host_tools"]["nasm"]]
    for row in rows:
        if row.get("url") and not row["url"].startswith("https://"):
            raise RuntimeError("pinned source and tool URLs must use HTTPS")
        name = row.get("filename")
        if name and (Path(name).name != name or "\\" in name):
            raise RuntimeError("unsafe filename in Windows Pillow input manifest")
        if row.get("sha256") and not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise RuntimeError("invalid SHA-256 in Windows Pillow input manifest")
    return data


def read_recipe():
    recipe = read_json(RECIPE)
    if not recipe.get("pillow_brotli_native_source") or not recipe.get("pillow_brotli_pin"):
        raise RuntimeError("pinned Pillow/Brotli provenance inputs are missing")
    return recipe


def load_sibling(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("_windows_pillow_" + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load packaging helper " + name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_file(path: Path, row, *, optional_size=False):
    if not path.is_file():
        raise RuntimeError("pinned input is missing: " + path.name)
    size = row.get("size_bytes")
    if size is None and not optional_size:
        raise RuntimeError("pinned input size is missing: " + path.name)
    if size is not None and path.stat().st_size != size:
        raise RuntimeError("pinned input size mismatch: " + path.name)
    if sha256_file(path) != row["sha256"]:
        raise RuntimeError("pinned input SHA-256 mismatch: " + path.name)


def download_archive(row, destination: Path, *, optional_size=False):
    if destination.exists():
        verify_file(destination, row, optional_size=optional_size)
        return destination
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise RuntimeError("partial source archive already exists: " + partial.name)
    url = row.get("url")
    if not url or not url.startswith("https://"):
        raise RuntimeError("archive URL must use HTTPS")
    maximum = row.get("size_bytes", row.get("max_bytes"))
    if not isinstance(maximum, int) or maximum < 1:
        raise RuntimeError("archive download has no positive size bound")
    expected = row.get("size_bytes")
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("archive download requires verified TLS")
    request = urllib.request.Request(url, headers={"User-Agent": "Callimachus-Windows-Pillow/1"})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=90, context=context) as response:
            if not response.geturl().startswith("https://"):
                raise RuntimeError("source archive redirected away from HTTPS")
            announced = response.headers.get("Content-Length")
            if announced is not None:
                announced = int(announced)
                if announced > maximum or expected is not None and announced != expected:
                    raise RuntimeError("archive Content-Length differs from its pin")
            with partial.open("xb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > maximum:
                        raise RuntimeError("archive exceeds its size bound")
                    output.write(block)
                    digest.update(block)
        if expected is not None and total != expected:
            raise RuntimeError("archive size mismatch: " + destination.name)
        if digest.hexdigest() != row["sha256"]:
            raise RuntimeError("archive SHA-256 mismatch: " + destination.name)
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return destination


def _member_path(name: str):
    trimmed = name.rstrip("/")
    path = PurePosixPath(trimmed)
    if (not trimmed or path.is_absolute() or not path.parts or ".." in path.parts
            or "." in path.parts or "\\" in name or ":" in name
            or path.as_posix() != trimmed):
        raise RuntimeError("unsafe archive member path")
    return path, path.as_posix()


def _register(seen: dict[str, bool], name: str, is_dir: bool):
    key = name.casefold()
    if key in seen:
        raise RuntimeError("duplicate archive member")
    for parent in PurePosixPath(name).parents:
        p = parent.as_posix()
        if p != "." and seen.get(p.casefold()) is False:
            raise RuntimeError("archive file/directory collision")
    if not is_dir and any(existing.startswith(key + "/") for existing in seen):
        raise RuntimeError("archive file/directory collision")
    seen[key] = is_dir


def safe_extract_archive(archive_path: Path, destination: Path, root: str,
                         *, max_expanded=MAX_EXPANDED_BYTES):
    """Extract a single-root archive without links, traversal, or path collisions."""
    if destination.exists():
        raise RuntimeError("archive extraction destination already exists")
    if PurePosixPath(root).is_absolute() or len(PurePosixPath(root).parts) != 1 or ":" in root:
        raise RuntimeError("invalid archive root")
    destination.mkdir(parents=True, exist_ok=False)
    seen: dict[str, bool] = {}
    total = 0

    def target_for(name, is_dir, size):
        nonlocal total
        path, canonical = _member_path(name)
        if path.parts[0].casefold() != root.casefold():
            raise RuntimeError("archive root differs from its source pin")
        if len(seen) >= MAX_MEMBERS or size < 0 or size > MAX_MEMBER_BYTES:
            raise RuntimeError("archive member exceeds its bound")
        total += size
        if total > max_expanded:
            raise RuntimeError("archive exceeds its expanded-size bound")
        _register(seen, canonical, is_dir)
        relative = path.parts[1:]
        if not relative:
            if not is_dir:
                raise RuntimeError("archive root member is not a directory")
            return None
        output = destination.joinpath(*relative)
        current = destination
        for component in relative[:-1]:
            current = current / component
            if current.exists() and not current.is_dir():
                raise RuntimeError("archive file/directory collision")
            current.mkdir(exist_ok=True)
        if is_dir:
            if output.exists() and not output.is_dir():
                raise RuntimeError("archive file/directory collision")
            output.mkdir(exist_ok=True)
        return output

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                path, _ = _member_path(item.filename)
                mode = item.external_attr >> 16
                kind = mode & 0o170000
                if kind == 0o120000 or kind not in (0, 0o100000, 0o040000):
                    raise RuntimeError("unsupported zip archive member type")
                output = target_for(item.filename, item.is_dir(), item.file_size)
                if output is not None and not item.is_dir():
                    with archive.open(item) as source, output.open("xb") as sink:
                        shutil.copyfileobj(source, sink, length=1024 * 1024)
            if archive.testzip() is not None:
                raise RuntimeError("zip source archive failed CRC verification")
    else:
        with tarfile.open(archive_path, "r:*") as archive:
            members = archive.getmembers()
            if not members:
                raise RuntimeError("source archive is empty")
            names = {}
            for item in members:
                _, canonical = _member_path(item.name)
                names[canonical.casefold()] = item
            for item in members:
                if item.isdir():
                    is_dir, data_item = True, None
                elif item.isfile():
                    is_dir, data_item = False, item
                elif item.issym():
                    link = PurePosixPath(item.name).parent / item.linkname
                    if (link.is_absolute() or ".." in link.parts or "\\" in item.linkname
                            or ":" in item.linkname):
                        raise RuntimeError("unsafe archive symlink target")
                    _, target_name = _member_path(link.as_posix())
                    data_item = names.get(target_name.casefold())
                    if data_item is None or not data_item.isfile():
                        raise RuntimeError("archive symlink target is not an in-root regular file")
                    is_dir = False
                else:
                    raise RuntimeError("unsupported tar archive member type")
                size = data_item.size if data_item is not None else 0
                output = target_for(item.name, is_dir, size)
                if output is None or is_dir:
                    continue
                source = archive.extractfile(data_item)
                if source is None:
                    raise RuntimeError("tar member has no data")
                with source, output.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
    return destination


def _run_output(args, *, env=None, cwd=None, timeout=90):
    result = subprocess.run(
        [str(value) for value in args], cwd=cwd, env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return result.stdout.strip()


def _version_tuple(value):
    return tuple(int(part) for part in re.findall(r"\d+", value))


def discover_vs_toolchain(*, vs_root=None, toolset=None):
    if sys.platform != "win32":
        raise RuntimeError("MSVC toolchain discovery is available only on Windows")
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not program_files:
        raise RuntimeError("Program Files is unavailable")
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise RuntimeError("vswhere.exe is required to select registered Visual Studio 2022")
    query = [vswhere, "-latest", "-version", "[17.0,18.0)", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"]
    reported_root = _run_output([*query, "-property", "installationPath"])
    version = _run_output([*query, "-property", "installationVersion"])
    root = Path(vs_root).resolve(strict=True) if vs_root else Path(reported_root).resolve(strict=True)
    root_key = os.path.normcase(os.path.normpath(str(root.resolve(strict=True))))
    reported_key = os.path.normcase(os.path.normpath(str(Path(reported_root).resolve(strict=True))))
    if root_key != reported_key:
        raise RuntimeError("--vs-root must identify the latest registered VS 2022 installation")
    tools = root / "VC" / "Tools" / "MSVC"
    candidates = sorted((p for p in tools.iterdir() if p.is_dir() and p.name.startswith("14.")),
                        key=lambda p: _version_tuple(p.name))
    if toolset:
        candidates = [p for p in candidates if p.name == toolset]
    if not candidates:
        raise RuntimeError("no registered v143 MSVC toolset was found")
    selected = candidates[-1]
    if re.fullmatch(r"14\.\d+\.\d+", selected.name) is None:
        raise RuntimeError("selected MSVC toolset has an unsupported version directory")
    compiler = selected / "bin" / "Hostx64" / "x64" / "cl.exe"
    linker = selected / "bin" / "Hostx64" / "x64" / "link.exe"
    vcvars = root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    msbuild = root / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
    sdk = load_inputs()["windows"]["sdk_version"]
    kits = Path(program_files) / "Windows Kits" / "10"
    include = kits / "Include" / sdk
    lib = kits / "Lib" / sdk
    required = [include / p for p in ("ucrt", "shared", "um")]
    required += [lib / "ucrt" / "x64", lib / "um" / "x64"]
    if not all(p.is_dir() for p in required):
        raise RuntimeError("pinned Windows SDK " + sdk + " is not installed")
    if not all(p.is_file() for p in (compiler, linker, vcvars, msbuild)):
        raise RuntimeError("selected Visual Studio toolchain is incomplete")
    return {
        "root": root, "version": version, "toolset": selected.name,
        "vcvars_arch": "amd64", "vcvars_version": selected.name,
        "compiler": compiler, "linker": linker, "vcvars": vcvars, "msbuild": msbuild,
        "compiler_sha256": sha256_file(compiler), "linker_sha256": sha256_file(linker),
        "sdk": sdk,
    }


def verify_host_tools():
    result = {}
    for name, args, pattern in (
        ("ninja", ["--version"], r"^1\.13\.2$"),
        ("perl", ["-v"], r"(?<![\w.])v?5\.32\.1(?![\w.])"),
        ("git", ["--version"], r"^git version "),
    ):
        path = shutil.which(name)
        if not path:
            raise RuntimeError(name + " is required on the Windows 2022 image")
        output = _run_output([path, *args])
        if not re.search(pattern, output, re.I):
            raise RuntimeError(name + " differs from its pinned runner version: " + output)
        result[name] = output.splitlines()[0]
    return result


def cache_fingerprint(*, vs_root=None, toolset=None):
    inputs = load_inputs()
    if sys.version_info[:3] != tuple(map(int, inputs["python"]["version"].split("."))):
        raise RuntimeError("cache fingerprint requires CPython " + inputs["python"]["version"])
    if platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise RuntimeError("cache fingerprint requires x64 Python")
    vs = discover_vs_toolchain(vs_root=vs_root, toolset=toolset)
    host = verify_host_tools()
    paths = [INPUTS, RECIPE, Path(__file__), ROOT / "packaging" / "prepare_pillow.py",
             ROOT / "packaging" / "bootstrap_pip.py", ROOT / "packaging" / "dependency_audit.py",
             ROOT / "requirements.txt", ROOT / "packaging" / "release-constraints.txt"]
    hashes = {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in paths}
    payload = {
        "python": {"version": sys.version, "machine": platform.machine(),
                   "abi": getattr(sys, "abiflags", "")},
        "runner": {"image": os.getenv("ImageVersion", os.getenv("IMAGE_VERSION", "unavailable")),
                   "os": os.getenv("ImageOS", os.getenv("IMAGE_OS", "unavailable"))},
        "toolchain": {"vs": vs["version"], "toolset": vs["toolset"],
                      "vcvars_arch": vs["vcvars_arch"], "vcvars_version": vs["vcvars_version"],
                      "cl": vs["compiler_sha256"], "link": vs["linker_sha256"],
                      "sdk": vs["sdk"], **host,
                      "cmake": next(r["version"] for r in inputs["python_tool_wheels"]
                                    if r["name"] == "cmake"),
                      "nasm": inputs["host_tools"]["nasm"]["version"]},
        "source_helper_input_hashes": hashes,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "win-pillow-" + digest



def ensure_parent_packaging():
    """Install pinned audit and Brotli wheels before Pillow validation or runtime installs."""
    inputs = load_inputs()
    rows = [row for row in inputs["python_tool_wheels"]
            if row["name"] in {"packaging", "brotli"}]
    if {row["name"] for row in rows} != {"packaging", "brotli"}:
        raise RuntimeError("parent audit/bootstrap wheel pins are incomplete")
    directory = ROOT / "build" / "security-pillow" / "parent-tools"
    directory.mkdir(parents=True, exist_ok=True)
    requirements = directory / "pinned-parent-tools.txt"
    allowed = {row["filename"] for row in rows} | {requirements.name}
    if any(item.name not in allowed for item in directory.iterdir()):
        raise RuntimeError("parent packaging bootstrap directory has unexpected files")
    bootstrap = load_sibling("bootstrap_pip")
    bootstrap.verify_installed_pip()
    wheels = {}
    for row in rows:
        wheel = directory / row["filename"]
        if wheel.exists():
            verify_file(wheel, row)
        else:
            bootstrap.download_verified_wheel(
                wheel, url=row["url"], expected_size=row["size_bytes"],
                expected_sha256=row["sha256"])
            verify_file(wheel, row)
        wheels[row["name"]] = wheel
    _write_requirements(requirements, rows)
    _run_output([
        sys.executable, "-m", "pip", "--isolated", "install", "--force-reinstall",
        "--no-index", "--no-deps", "--find-links", directory, "--require-hashes", "-r", requirements,
    ], timeout=600)
    probe = "import importlib.metadata,json; print(json.dumps({n:importlib.metadata.version(n) for n in " + repr(sorted(wheels)) + "},sort_keys=True))"
    installed = json.loads(_run_output([sys.executable, "-c", probe]))
    expected = {row["name"]: row["version"] for row in rows}
    if installed != expected:
        raise RuntimeError("parent packaging/Brotli versions differ from their verified pins")
    return wheels["packaging"]

def _write_requirements(path: Path, rows):
    lines = [row["name"] + "==" + row["version"] + " --hash=sha256:" + row["sha256"]
             for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def download_tool_wheels(rows, directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    secure_pip = load_sibling("bootstrap_pip")
    secure_pip.verify_installed_pip()
    paths = {}
    for row in rows:
        path = directory / row["filename"]
        if path.exists():
            verify_file(path, row)
        else:
            secure_pip.download_verified_wheel(
                path, url=row["url"], expected_size=row["size_bytes"],
                expected_sha256=row["sha256"])
        paths[row["name"]] = path
    return paths


def create_build_venv(wheelhouse: Path, venv_dir: Path, rows):
    if venv_dir.exists():
        raise RuntimeError("isolated build-tool venv already exists")
    venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    python = venv_dir / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("isolated Windows build-tool venv is incomplete")
    common = [str(python), "-m", "pip", "--isolated", "install", "--no-index",
              "--find-links", str(wheelhouse), "--no-deps", "--only-binary=:all:",
              "--require-hashes", "-r"]
    pip_req = venv_dir.parent / "pinned-pip.txt"
    _write_requirements(pip_req, [row for row in rows if row["name"] == "pip"])
    _run_output([*common, pip_req], timeout=600)
    tools_req = venv_dir.parent / "pinned-build-tools.txt"
    _write_requirements(tools_req, [row for row in rows if row["name"] != "pip"])
    _run_output([*common, tools_req], timeout=900)
    probe = "import importlib.metadata,json; print(json.dumps({n:importlib.metadata.version(n) for n in " + repr([r["name"] for r in rows]) + "},sort_keys=True))"
    installed = json.loads(_run_output([python, "-c", probe]))
    expected = {row["name"]: row["version"] for row in rows}
    if installed != expected:
        raise RuntimeError("isolated build-tool versions differ from their pins")
    run_prebuild_dependency_audit(python, venv_dir.parent)
    return python


def patch_pillow_dependencies(source: Path, recipe, metadata_input: Path, work: Path):
    """Stage and apply the shared, digest-checked upstream Brotli metadata patch."""
    pin = recipe["pillow_brotli_pin"]
    input_row = {"filename": pin["filename"], "url": pin["url"],
                 "sha256": pin["original_sha256"], "size_bytes": pin["size_bytes"]}
    verify_file(metadata_input, input_row)
    destination = source / pin["path"]
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("fresh Pillow sdist unexpectedly contains the patched metadata path")

    overlay = work / "pillow-metadata-overlay"
    if overlay.exists():
        raise RuntimeError("Pillow metadata overlay directory must be fresh")
    staged_root = overlay / "python-sources" / "pillow-source" / source.name
    staged_root.mkdir(parents=True, exist_ok=False)
    staged_input = overlay / "inputs" / pin["filename"]
    staged_input.parent.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(metadata_input, staged_input)

    path = ROOT / "packaging" / "store" / "prepare_sources.py"
    spec = importlib.util.spec_from_file_location("_windows_pillow_prepare_sources", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the shared Pillow metadata overlay helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patched_path = module.apply_pillow_brotli_source_pin(
        overlay, store=ROOT / "packaging" / "store")
    patched = patched_path.read_bytes()
    if sha256_bytes(patched) != pin["patched_sha256"]:
        raise RuntimeError("shared Pillow metadata overlay returned an unexpected digest")
    if destination.parent.is_symlink() or destination.parent.exists() and not destination.parent.is_dir():
        raise RuntimeError("Pillow metadata parent is not a regular directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched)
    if sha256_bytes(destination.read_bytes()) != pin["patched_sha256"]:
        raise RuntimeError("staged Pillow metadata did not preserve the verified overlay")
    return destination


def run_prebuild_dependency_audit(build_python: Path, work: Path):
    """Require a fresh successful dependency_audit.py query before native compilation."""
    report = work / "prebuild-dependency-audit.json"
    log = work / "prebuild-dependency-audit.log"
    if report.exists() or log.exists():
        raise RuntimeError("prebuild dependency audit outputs must be fresh")
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"):
        env.pop(key, None)
    env.update({"PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1"})
    command = [str(build_python), str(ROOT / "packaging" / "dependency_audit.py"),
               "--report", str(report)]
    with log.open("xb") as output:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, check=False, timeout=2400)
    if completed.returncode != 0:
        raise RuntimeError("fresh precompile dependency_audit.py query failed")
    try:
        result = read_json(report)
    except (OSError, ValueError) as exc:
        raise RuntimeError("precompile dependency audit did not write valid JSON") from exc
    if result.get("status") != "passed" or not isinstance(result.get("inventory"), dict) or not result["inventory"]:
        raise RuntimeError("precompile dependency audit did not report a passed inventory")
    return report

def load_build_prepare(source: Path, expected_hash: str):
    path = source / "winbuild" / "build_prepare.py"
    if sha256_file(path) != expected_hash:
        raise RuntimeError("Pillow build_prepare.py differs from its pinned upstream source")
    spec = importlib.util.spec_from_file_location("_callimachus_pillow_build_prepare", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load verified Pillow build_prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_sdk_targets(module, sdk):
    for name in ("freetype", "lcms2"):
        mapping = module.DEPS[name]["patch"]
        rows = [(path, old, new) for path, changes in mapping.items()
                for old, new in changes.items() if "WindowsTargetPlatformVersion" in new]
        if len(rows) != 1:
            raise RuntimeError("unexpected upstream SDK patch for " + name)
        path, old, new = rows[0]
        patched = new.replace("$(WindowsSDKVersion)", sdk)
        if patched == new:
            raise RuntimeError("could not pin SDK target for " + name)
        mapping[path][old] = patched


def patch_build_commands(module, build_dir: Path):
    for name, dep in module.DEPS.items():
        if name == "libavif":
            continue
        commands = []
        for command in dep.get("build", []):
            if "--parallel" in command:
                command, count = re.subn(r"--parallel(?!\s+\d)", "--parallel 2", command)
                if count != 1:
                    raise RuntimeError("unexpected unbounded CMake command for " + name)
            if re.search(r"(?<!\S)/m(?!\S)", command):
                command, count = re.subn(r"(?<!\S)/m(?!\S)", "/m:2", command)
                if count != 1:
                    raise RuntimeError("unexpected unbounded MSBuild command for " + name)
            commands.append(command)
        dep["build"] = commands
    original = module.DEPS["libavif"]["build"]
    if sum("rustup update" in row for row in original) != 1 or sum("pip install meson" in row for row in original) != 1:
        raise RuntimeError("upstream libavif tool-install commands changed")
    if module.DEPS["libavif"].get("libs") != ["avif.lib"]:
        raise RuntimeError("upstream libavif static library output changed")
    source = (build_dir / "src" / "libavif-1.4.2" / "ext" / "aom").as_posix()
    module.DEPS["libavif"]["libs"] = [r"build-security-ci\avif.lib"]
    module.DEPS["libavif"]["build"] = [
        "cmake.exe -DCMAKE_BUILD_TYPE=MinSizeRel -DCMAKE_SYSTEM_VERSION=10.0.26100.0 "
        "-DCMAKE_VERBOSE_MAKEFILE=ON -DCMAKE_RULE_MESSAGES:BOOL=OFF "
        "-DCMAKE_C_COMPILER=cl.exe -DCMAKE_CXX_COMPILER=cl.exe "
        "-DCMAKE_C_FLAGS=-nologo -DCMAKE_CXX_FLAGS=-nologo -DBUILD_SHARED_LIBS=OFF "
        "-DAVIF_LIBSHARPYUV=LOCAL -DAVIF_LIBYUV=LOCAL -DAVIF_CODEC_AOM=LOCAL "
        "-DCONFIG_AV1_HIGHBITDEPTH=0 -DAVIF_CODEC_AOM_DECODE=OFF "
        "-DAVIF_CODEC_DAV1D=LOCAL -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON "
        "-DFETCHCONTENT_SOURCE_DIR_LIBAOM=\"" + source + "\" "
        "-DFETCHCONTENT_FULLY_DISCONNECTED=ON -G \"{cmake_generator}\" "
        "-B \"build-security-ci\" -S .",
        "cmake.exe --build \"build-security-ci\" --parallel 1 --target avif_static",
        "xcopy /Y /E \"include\" \"{inc_dir}\"",
    ]


def patch_dav1d_job_bound(avif_source: Path):
    path = avif_source / "cmake" / "Modules" / "LocalDav1d.cmake"
    original = path.read_bytes()
    if sha256_bytes(original) != DAV1D_ORIGINAL_SHA256:
        raise RuntimeError("libavif LocalDav1d.cmake differs from its source pin")
    text = original.decode("utf-8")
    dollar = chr(36)
    old = ("        BUILD_COMMAND " + dollar + "{CMAKE_COMMAND} -E env \"PATH="
           + dollar + "{PATH}\" " + dollar + "{NINJA_EXECUTABLE} -C <BINARY_DIR>\n")
    if text.count(old) != 1:
        raise RuntimeError("dav1d parallelism patch did not match exactly once")
    patched = text.replace(old, old.rstrip("\n") + " -j 2\n", 1).encode("utf-8")
    if sha256_bytes(patched) != DAV1D_PATCHED_SHA256:
        raise RuntimeError("dav1d parallelism patch differs from its qualification pin")
    path.write_bytes(patched)


def configure_vcvars_environment(build_env: Path, toolchain, sdk: str):
    rows = build_env.read_text(encoding="utf-8").splitlines()
    candidates = [index for index, line in enumerate(rows)
                  if re.match(r"\s*call\b", line, re.I)
                  and "vcvarsall.bat" in line.casefold()]
    if len(candidates) != 1:
        raise RuntimeError("could not identify one generated MSVC environment activation")
    index = candidates[0]
    match = re.fullmatch(r'(?i)(\s*call\s+"([^"]*vcvarsall\.bat)")\s+.*', rows[index])
    if match is None:
        raise RuntimeError("generated MSVC environment activation has an unexpected form")
    actual = os.path.normcase(os.path.normpath(match.group(2)))
    expected = os.path.normcase(os.path.normpath(str(toolchain["vcvars"])))
    if actual != expected:
        raise RuntimeError("generated MSVC environment does not use the selected Visual Studio")
    rows[index] = (match.group(1) + " " + toolchain["vcvars_arch"]
                   + " " + sdk + " -vcvars_ver=" + toolchain["vcvars_version"])
    build_env.write_text("\r\n".join(rows) + "\r\n", encoding="utf-8", newline="")


def write_toolchain_record(path: Path, toolchain):
    record = {
        "visual_studio_version": toolchain["version"],
        "msvc_toolset": toolchain["toolset"],
        "vcvars_arch": toolchain["vcvars_arch"],
        "vcvars_version": toolchain["vcvars_version"],
        "compiler": str(toolchain["compiler"]),
        "compiler_sha256": toolchain["compiler_sha256"],
        "linker": str(toolchain["linker"]),
        "linker_sha256": toolchain["linker_sha256"],
        "windows_sdk": toolchain["sdk"],
    }
    path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def create_dependency_scripts(module, source: Path, work: Path, inputs, toolchain):
    build_dir = work / "pillow-build"
    depends_dir = work / "depends"
    if build_dir.exists() or depends_dir.exists():
        raise RuntimeError("Pillow build outputs must be fresh")
    depends_dir.mkdir()
    archive_rows = [*inputs["dependency_archives"], inputs["brotli_source"]]
    by_filename = {row["build_filename"]: row for row in archive_rows}
    archive_paths = {row["build_filename"]: work / "downloads" / row["filename"] for row in archive_rows}

    def extract_pinned(_url, filename, prefs):
        row = by_filename.get(filename)
        archive = archive_paths.get(filename)
        if row is None or archive is None:
            raise RuntimeError("Pillow requested an unpinned dependency archive: " + filename)
        target = Path(prefs["src_dir"]) / row["root"]
        safe_extract_archive(archive, target, row["root"])
        return target

    def refuse_network(*_args, **_kwargs):
        raise RuntimeError("Pillow dependency network acquisition is disabled")

    def fresh_only(path, ignore_errors=False):
        if Path(path).exists():
            raise RuntimeError("Pillow preparation refuses to remove an existing directory")

    module.find_msvs = lambda _arch: {
        "vs_dir": str(toolchain["root"]),
        "msbuild": '"' + str(toolchain["msbuild"]) + '"',
        "vcvarsall": '"' + str(toolchain["vcvars"]) + '"',
        "nmake": "nmake.exe",
    }
    module.extract_dep = extract_pinned
    module.download_dep = refuse_network

    def quoted_cd(path):
        value = os.fspath(path)
        if '"' in value:
            raise RuntimeError("Pillow build path contains an unsafe quote")
        return 'cd /D "' + value + '"'

    module.cmd_cd = quoted_cd
    module.shutil = type("FreshOnlyShutil", (), {"rmtree": staticmethod(fresh_only)})
    patch_sdk_targets(module, inputs["windows"]["sdk_version"])
    patch_build_commands(module, build_dir)

    old_argv = sys.argv
    sys.argv = [str(source / "winbuild" / "build_prepare.py"), "--verbose",
                "--build-dir", str(build_dir), "--depends", str(depends_dir),
                "--architecture", "AMD64", "--no-imagequant", "--no-raqm"]
    try:
        module.main()
    finally:
        sys.argv = old_argv
    build_env = build_dir / "build_env.cmd"
    configure_vcvars_environment(build_env, toolchain, inputs["windows"]["sdk_version"])
    return build_dir, depends_dir


def create_git_archive(row, destination: Path, work: Path):
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required to create the pinned AVIF source archives")
    if destination.exists():
        raise RuntimeError("Git archive output already exists")
    git_root = work / "git" / row["name"]
    git_root.mkdir(parents=True, exist_ok=False)
    empty_config = work / "git-empty-global.config"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") and k not in {"GITHUB_TOKEN", "GH_TOKEN"}}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty_config),
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "Never"})
    _run_output([git, "init", "--quiet"], cwd=git_root, env=env)
    _run_output([git, "-c", "credential.helper=", "-c", "core.askPass=",
                 "fetch", "--depth=1", "--no-tags", row["repository"], row["ref"]],
                cwd=git_root, env=env, timeout=600)
    commit = _run_output([git, "rev-parse", "FETCH_HEAD^{commit}"], cwd=git_root, env=env)
    if commit.lower() != row["commit"].lower():
        raise RuntimeError("AVIF dependency fetch resolved to a different commit")
    with destination.open("xb") as output:
        subprocess.run([git, "-c", "core.autocrlf=true", "archive", "--format=tar",
                        "--prefix=" + row["archive_prefix"] + "/", commit],
                       cwd=git_root, env=env, stdout=output, stderr=subprocess.PIPE,
                       check=True, timeout=600)
    verify_file(destination, row)


def acquire_sources(inputs, work: Path):
    downloads = work / "downloads"
    downloads.mkdir(parents=True, exist_ok=False)
    pillow = download_archive(inputs["pillow_source"], downloads / inputs["pillow_source"]["filename"])
    pin = read_recipe()["pillow_brotli_pin"]
    metadata_row = {"filename": pin["filename"], "url": pin["url"],
                    "sha256": pin["original_sha256"], "size_bytes": pin["size_bytes"]}
    pillow_metadata = download_archive(metadata_row, downloads / pin["filename"])
    for row in [inputs["brotli_source"], *inputs["dependency_archives"]]:
        download_archive(row, downloads / row["filename"])
    for row in inputs["avif_nested"]:
        if row.get("repository"):
            continue
        download_archive(row, downloads / row["filename"])
    for row in inputs["avif_nested"]:
        if row.get("repository"):
            create_git_archive(row, downloads / row["filename"], work)
    nasm = inputs["host_tools"]["nasm"]
    download_archive(nasm, downloads / nasm["filename"], optional_size=True)
    return pillow, downloads, pillow_metadata


def prepare_nested_sources(inputs, downloads: Path, avif_source: Path, work: Path):
    ext = avif_source / "ext"
    expected_dirs = {row["source_dir"] for row in inputs["avif_nested"]}
    if ext.exists():
        if not ext.is_dir() or expected_dirs.intersection(p.name for p in ext.iterdir()):
            raise RuntimeError("libavif nested source targets must be new directories")
    else:
        ext.mkdir()
    for row in inputs["avif_nested"]:
        extracted = work / "nested" / row["source_dir"]
        safe_extract_archive(downloads / row["filename"], extracted, row["archive_prefix"])
        target = ext / row["source_dir"]
        shutil.copytree(extracted, target)
        if row["name"] == "dav1d" and "version: '1.5.3'" not in (target / "meson.build").read_text(encoding="utf-8"):
            raise RuntimeError("dav1d source version differs from its pin")
    patch_dav1d_job_bound(avif_source)


def select_nasm(inputs, work: Path, downloads: Path):
    row = inputs["host_tools"]["nasm"]
    found = shutil.which("nasm")
    if found and re.search(r"\bversion " + re.escape(row["version"]) + r"\b",
                           _run_output([found, "--version"]), re.I):
        return Path(found).resolve().parent
    archive = downloads / row["filename"]
    destination = work / "tools" / "nasm"
    (work / "tools").mkdir(exist_ok=False)
    safe_extract_archive(archive, destination, row["root"], max_expanded=8 * 1024 * 1024)
    executable = destination / "nasm.exe"
    if not executable.is_file() or not re.search(
            r"\bversion " + re.escape(row["version"]) + r"\b",
            _run_output([executable, "--version"]), re.I):
        raise RuntimeError("verified NASM archive did not provide the pinned tool version")
    return destination


def build_environment(build_python: Path, nasm_dir: Path, work: Path, build_dir: Path, depends: Path):
    perl = shutil.which("perl")
    ninja = shutil.which("ninja")
    if not perl or not ninja:
        raise RuntimeError("pinned Perl/Ninja are required")
    temp = work / "temp"
    temp.mkdir(exist_ok=False)
    env = os.environ.copy()
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    env["PATH"] = os.pathsep.join([
        str(build_python.parent), str(nasm_dir), str(Path(perl).resolve().parent),
        str(Path(ninja).resolve().parent), env.get("PATH", ""),
    ])
    env["PILLOW_BUILD"] = str(build_dir)
    env["PILLOW_DEPS"] = str(depends)
    env["PIP_NO_INDEX"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    cmake = build_python.parent / "cmake.exe"
    if not cmake.is_file() or "4.4.3" not in _run_output([cmake, "--version"]).splitlines()[0]:
        raise RuntimeError("pinned CMake 4.4.3 is missing from the isolated tools venv")
    return env


def _print_build_failure_tail(script: Path, log: Path):
    max_bytes = 16 * 1024
    max_lines = 100
    try:
        with log.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            start = max(0, size - max_bytes)
            source.seek(start)
            tail = source.read(max_bytes)
    except OSError:
        return

    text = tail.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
    print(f"{script.name} failed; output tail from {log.name}:", file=sys.stderr)
    if start:
        print("[earlier log bytes omitted]", file=sys.stderr)
    if text:
        print(text, file=sys.stderr)


def run_build_script(script: Path, env, log: Path):
    script_path = os.fspath(script)
    if any(character in script_path for character in ('"', "\r", "\n", "\0")):
        raise RuntimeError("batch path contains a character cmd.exe cannot quote safely")
    command = 'cmd.exe /d /s /c ""' + script_path + '""'
    try:
        with log.open("xb") as output:
            subprocess.run(command, cwd=script.parent, env=env,
                           stdout=output, stderr=subprocess.STDOUT,
                           check=True, timeout=2400)
    except subprocess.CalledProcessError:
        _print_build_failure_tail(script, log)
        raise


def build_wheel(source: Path, raw_dir: Path, cache_dir: Path, build_dir: Path,
                build_python: Path, env):
    raw_dir.mkdir(parents=True, exist_ok=False)
    build_env = build_dir / "build_env.cmd"
    if not build_env.is_file():
        raise RuntimeError("generated Pillow MSVC environment is missing")
    command = [
        str(build_python), "-m", "pip", "--isolated", "wheel", "-v",
        "--no-index", "--no-deps", "--no-build-isolation", "--wheel-dir", str(raw_dir),
        "-C", "parallel=2", "-C", "avif=enable",
        "-C", "imagequant=disable", "-C", "raqm=disable", ".",
    ]
    batch = build_dir / "build-pillow-wheel.cmd"
    if batch.exists():
        raise RuntimeError("Pillow wheel build batch file must be fresh")
    for path in (build_env, source, build_python, raw_dir):
        if '"' in str(path):
            raise RuntimeError("Pillow build path contains an unsafe quote")
    lines = [
        "@echo off",
        'call "' + str(build_env) + '"',
        "if errorlevel 1 exit /b 1",
        'set "PIP_NO_INDEX=1"',
        'set "PIP_DISABLE_PIP_VERSION_CHECK=1"',
        'set "PIP_CONFIG_FILE=NUL"',
        'set "MAX_CONCURRENCY=2"',
        'set "MAX_JOBS=2"',
        'set "CMAKE_BUILD_PARALLEL_LEVEL=2"',
        'cd /d "' + str(source) + '"',
        "if errorlevel 1 exit /b 1",
        subprocess.list2cmdline(command),
        "if errorlevel 1 exit /b 1",
        "exit /b 0",
    ]
    batch.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    run_build_script(batch, env, build_dir / "pillow-wheel-build.log")
    raw_wheels = list(raw_dir.glob("pillow-13.0.0-cp313-cp313-win_amd64.whl"))
    if len(raw_wheels) != 1 or raw_wheels[0].stat().st_size > WHEEL_LIMIT:
        raise RuntimeError("Pillow source build did not produce its expected x64 wheel")
    prepare = load_sibling("prepare_pillow")
    tree = build_dir / "qualified-wheel-tree"
    tree.mkdir(parents=True, exist_ok=False)
    info = prepare.unpack_wheel(raw_wheels[0], tree)
    source_brotli = build_dir / "src" / "brotli-1.2.1"
    binaries = sorted((tree / "PIL").glob("*.pyd"))
    if len(binaries) != 8 or not any(p.name.startswith("_imagingft.") for p in binaries):
        raise RuntimeError("Windows Pillow source build must retain all eight native extensions")
    prepare.write_provenance(tree, info, source_brotli, read_recipe(), binaries, method="source-build")
    wheel = prepare.pack_wheel(tree, info, cache_dir, "win_amd64")
    if wheel.name != WHEEL_NAME:
        raise RuntimeError("qualified Pillow wheel filename differs from its canonical name")
    return wheel


def verify_record_and_receipt(wheel: Path):
    if not wheel.is_file() or wheel.stat().st_size > WHEEL_LIMIT:
        raise RuntimeError("qualified Pillow wheel is missing or exceeds the 8 MiB cache bound")
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("qualified Pillow wheel failed CRC verification")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("qualified Pillow wheel contains duplicate members")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError("qualified wheel must contain one dist-info metadata file")
        info = PurePosixPath(metadata_names[0]).parent.as_posix()
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        metadata = metadata.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
        if not re.search(r"(?im)^Name: pillow$", metadata) or not re.search(r"(?m)^Version: 13\.0\.0$", metadata):
            raise RuntimeError("qualified Pillow wheel metadata is incorrect")
        wheel_meta = archive.read(info + "/WHEEL").decode("utf-8")
        if "Build: 1callimachus" not in wheel_meta or "Tag: cp313-cp313-win_amd64" not in wheel_meta:
            raise RuntimeError("qualified Pillow wheel tag/build metadata is incorrect")
        record_path = info + "/RECORD"
        rows = list(csv.reader(io.StringIO(archive.read(record_path).decode("utf-8"), newline="")))
        record = {}
        for row in rows:
            if len(row) != 3 or row[0] in record:
                raise RuntimeError("qualified Pillow wheel RECORD has invalid rows")
            record[row[0]] = row[1:]
        if set(record) != set(names):
            raise RuntimeError("qualified Pillow wheel RECORD does not cover the exact member set")
        for name in names:
            digest, size = record[name]
            data = archive.read(name)
            if name == record_path:
                if (digest, size) != ("", ""):
                    raise RuntimeError("qualified Pillow wheel RECORD self-row is invalid")
            else:
                actual = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
                if digest != actual or size != str(len(data)):
                    raise RuntimeError("qualified Pillow wheel RECORD hash/size mismatch")
        receipt_path = info + "/callimachus-pillow-native-security.json"
        receipt = json.loads(archive.read(receipt_path).decode("utf-8"))
        recipe = read_recipe()
        source = recipe["pillow_brotli_native_source"]
        pin = recipe["pillow_brotli_pin"]
        expected_source = {k: source[k] for k in ("filename", "url", "sha256", "size_bytes")}
        expected_pin = {"commit": pin["upstream_commit"], "path": pin["path"],
                        "upstream_sha256": pin["original_sha256"], "patched_sha256": pin["patched_sha256"],
                        "from_version": pin["from_version"], "to_version": pin["to_version"]}
        component = receipt.get("component", {})
        if (receipt.get("format") != "callimachus.pillow-native-security.v1"
                or receipt.get("owner") != {"name": "Pillow", "version": "13.0.0"}
                or receipt.get("method") != "source-build"
                or component.get("name") != "Brotli" or component.get("version") != "1.2.1"
                or component.get("source") != expected_source or component.get("upstream_pin") != expected_pin):
            raise RuntimeError("qualified Pillow source-build receipt does not match its pins")
        license_row = receipt.get("license", {})
        if (license_row.get("path") not in names
                or sha256_bytes(archive.read(license_row["path"])) != license_row.get("sha256")):
            raise RuntimeError("qualified Pillow license hash differs from its receipt")
        extensions = sorted(n for n in names if n.startswith("PIL/") and n.lower().endswith(".pyd"))
        binaries = receipt.get("binary_files", [])
        if len(extensions) != 8 or [row.get("path") for row in binaries] != extensions:
            raise RuntimeError("qualified Pillow receipt must list exactly eight native extensions")
        for row in binaries:
            if sha256_bytes(archive.read(row["path"])) != row.get("sha256"):
                raise RuntimeError("qualified Pillow extension hash differs from its receipt")
        return receipt


def verify_cache(cache_dir: Path, work_dir: Path):
    expected_sidecar = WHEEL_NAME + ".sha256"
    names = sorted(p.name for p in cache_dir.iterdir())
    if names != sorted([WHEEL_NAME, expected_sidecar]):
        raise RuntimeError("Pillow cache must contain only the wheel and its SHA-256 sidecar")
    wheel = cache_dir / WHEEL_NAME
    fields = (cache_dir / expected_sidecar).read_text(encoding="ascii").strip().split()
    if len(fields) != 2 or fields[1] != WHEEL_NAME or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
        raise RuntimeError("Pillow wheel SHA-256 sidecar is malformed")
    if sha256_file(wheel) != fields[0]:
        raise RuntimeError("Pillow cached wheel artifact hash mismatch")
    verify_record_and_receipt(wheel)
    load_sibling("prepare_pillow").validate_qualified_wheel(wheel, work_dir)
    return wheel


def _create_work_dir(path: Path):
    if path.exists():
        raise RuntimeError("Windows Pillow source-build work directory already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True, exist_ok=False)


def build_or_restore(cache_dir: Path, work_dir: Path, *, vs_root=None, toolset=None):
    if work_dir.exists():
        raise RuntimeError("Windows Pillow source-build work directory already exists")
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    if cache_dir.exists() and any(cache_dir.iterdir()):
        work_dir.mkdir(parents=True, exist_ok=False)
        return verify_cache(cache_dir, work_dir)
    work_dir.mkdir(parents=True, exist_ok=False)
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    if not cache_dir.exists():
        cache_dir.mkdir(parents=True, exist_ok=False)
    elif any(cache_dir.iterdir()):
        raise RuntimeError("Windows Pillow cache contains partial or unexpected files")

    inputs = load_inputs()
    required_python = tuple(map(int, inputs["python"]["version"].split(".")))
    if sys.platform != "win32" or sys.version_info[:3] != required_python:
        raise RuntimeError("Windows Pillow build requires Windows CPython " + inputs["python"]["version"])
    if platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        raise RuntimeError("Windows Pillow build requires x64 CPython")
    disk_root = Path(work_dir.anchor)
    if shutil.disk_usage(disk_root).free < 12 * 1024**3:
        raise RuntimeError("Windows Pillow build requires at least 12 GiB free disk space")
    toolchain = discover_vs_toolchain(vs_root=vs_root, toolset=toolset)
    write_toolchain_record(work_dir / "toolchain.json", toolchain)
    verify_host_tools()
    load_sibling("bootstrap_pip").verify_installed_pip()

    wheelhouse = work_dir / "tool-wheelhouse"
    tool_wheels = download_tool_wheels(inputs["python_tool_wheels"], wheelhouse)
    build_python = create_build_venv(wheelhouse, work_dir / "tools-venv", inputs["python_tool_wheels"])
    pillow_archive, downloads, pillow_metadata = acquire_sources(inputs, work_dir)
    source = work_dir / "source" / inputs["pillow_source"]["root"]
    safe_extract_archive(pillow_archive, source, inputs["pillow_source"]["root"])
    recipe = read_recipe()
    patch_pillow_dependencies(source, recipe, pillow_metadata, work_dir)
    module = load_build_prepare(source, inputs["pillow_source"]["build_prepare_sha256"])
    build_dir, depends = create_dependency_scripts(module, source, work_dir, inputs, toolchain)
    avif_source = build_dir / "src" / "libavif-1.4.2"
    prepare_nested_sources(inputs, downloads, avif_source, work_dir)

    nasm_dir = select_nasm(inputs, work_dir, downloads)
    env = build_environment(build_python, nasm_dir, work_dir, build_dir, depends)
    run_build_script(build_dir / "build_dep_all.cmd", env, work_dir / "dependency-build.log")
    wheel = build_wheel(source, build_dir / "raw-wheel", cache_dir, build_dir, build_python, env)
    verify_record_and_receipt(wheel)
    sidecar = cache_dir / (WHEEL_NAME + ".sha256")
    sidecar.write_text(sha256_file(wheel) + "  " + WHEEL_NAME + "\n", encoding="ascii", newline="\n")
    return verify_cache(cache_dir, work_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build" / "security-pillow" / "cache")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "security-pillow" / "win")
    parser.add_argument("--fingerprint", action="store_true", help="print a complete Windows wheel cache key")
    parser.add_argument("--ensure-parent-packaging", action="store_true",
                        help="install verified packaging 26.3 for static Pillow validation and the audit")
    parser.add_argument("--vs-root")
    parser.add_argument("--toolset")
    args = parser.parse_args(argv)
    if args.ensure_parent_packaging:
        ensure_parent_packaging()
        return 0
    if args.fingerprint:
        print("key=" + cache_fingerprint(vs_root=args.vs_root, toolset=args.toolset))
        return 0
    wheel = build_or_restore(args.cache_dir.resolve(), args.work_dir.resolve(),
                             vs_root=args.vs_root, toolset=args.toolset)
    print(str(wheel), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
