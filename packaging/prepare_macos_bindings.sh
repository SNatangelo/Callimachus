#!/usr/bin/env bash
set -euo pipefail

# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

# Build the PySide bindings that need an explicit macOS 14.4 deployment target.
# This uses the host's native architecture and only overlays audited files into
# the application's Python 3.13 environment. It never edits the QtPdf bindings.

readonly TARGET_VERSION="6.12.0"
readonly MIN_MACOS="14.4"
readonly PYSIDE_SOURCE_SHA256="099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751"
readonly LIBCLANG_ARCHIVE_SHA256="0bca8fe7e2f313e12a17ca3a6d44484c5dd5e2274038869398ed2eec36b8a9e4"
readonly AQT_WHEEL_SHA256="e88dbd87226f276fdd5d05347a44578d390d93a7f176e9476fbda0a7c9635f69"
readonly PYSIDE_SOURCE_URL="https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz"
readonly LIBCLANG_ARCHIVE_URL="https://download.qt.io/development_releases/prebuilt/libclang/qt/libclang-llvmorg-20.1.0-macos-universal.7z"
readonly ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || die "must run on a native macOS runner"
readonly TARGET_ARCH="$(uname -m)"
case "$TARGET_ARCH" in
    arm64|x86_64) ;;
    *) die "unsupported native macOS architecture: $TARGET_ARCH" ;;
esac

: "${BUILD_PYTHON:?Set BUILD_PYTHON to the native Python 3.10 build executable}"
: "${RUNTIME_PYTHON:?Set RUNTIME_PYTHON to the application Python 3.13.15 executable}"
[[ -x "$BUILD_PYTHON" ]] || die "BUILD_PYTHON is not executable: $BUILD_PYTHON"
[[ -x "$RUNTIME_PYTHON" ]] || die "RUNTIME_PYTHON is not executable: $RUNTIME_PYTHON"

while IFS='=' read -r name _; do
    case "$name" in
        DYLD_FRAMEWORK_PATH|DYLD_LIBRARY_PATH|DYLD_FALLBACK_LIBRARY_PATH|PYTHONPATH|PYTHONHOME)
            unset "$name"
            ;;
    esac
done < <(env)

readonly WORK_ROOT="$(mktemp -d "${RUNNER_TEMP:-/tmp}/callimachus-pyside612-${TARGET_ARCH}.XXXXXX")"
readonly INPUT_DIR="$WORK_ROOT/input"
readonly AQT_VENV="$WORK_ROOT/aqt-venv"
readonly BUILD_VENV="$WORK_ROOT/build-venv"
readonly QT_ROOT="$WORK_ROOT/qt-root"
readonly SOURCE_ROOT="$WORK_ROOT/source"
readonly LLVM_ROOT="$WORK_ROOT/libclang-extracted"
readonly OVERLAY_RAW="$WORK_ROOT/overlay-raw"
readonly OVERLAY="$WORK_ROOT/overlay"
readonly PIP_CACHE="$WORK_ROOT/pip-cache"
readonly SOURCE_ARCHIVE="$INPUT_DIR/pyside-setup-everywhere-src-6.12.0.tar.xz"
readonly LIBCLANG_ARCHIVE="$INPUT_DIR/libclang-llvmorg-20.1.0-macos-universal.7z"
readonly AQT_WHEEL="$INPUT_DIR/aqtinstall-3.3.0-py3-none-any.whl"
readonly QT_PREFIX="$QT_ROOT/6.12.0/macos"

mkdir -p "$INPUT_DIR" "$PIP_CACHE" "$SOURCE_ROOT"
export TMPDIR="$WORK_ROOT/tmp" TMP="$WORK_ROOT/tmp" TEMP="$WORK_ROOT/tmp"
export PIP_CACHE_DIR="$PIP_CACHE" PIP_CONFIG_FILE=/dev/null NETRC=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C
mkdir -p "$TMPDIR"

python_info() {
    "$1" -c 'import platform,sys; print(".".join(map(str, sys.version_info[:3])) + "|" + platform.machine())'
}

build_info="$(python_info "$BUILD_PYTHON")"
runtime_info="$(python_info "$RUNTIME_PYTHON")"
case "$TARGET_ARCH" in
    arm64|x86_64) readonly BUILD_PYTHON_VERSION="3.10.11" ;;
    *) die "unsupported native macOS architecture: $TARGET_ARCH" ;;
esac
[[ "$build_info" == "${BUILD_PYTHON_VERSION}|$TARGET_ARCH" ]] || die "expected native Python ${BUILD_PYTHON_VERSION} for $TARGET_ARCH, found $build_info"
[[ "$runtime_info" == "3.13.15|$TARGET_ARCH" ]] || die "expected native Python 3.13.15, found $runtime_info"
build_include="$($BUILD_PYTHON -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
[[ -f "$build_include/Python.h" ]] || die "native build Python headers are missing: $build_include/Python.h"
build_deployment_target="$($BUILD_PYTHON -c 'import sysconfig; print(sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET") or "")')" || die "could not inspect build Python deployment target"
[[ -n "$build_deployment_target" ]] || die "build Python does not declare MACOSX_DEPLOYMENT_TARGET"
"$BUILD_PYTHON" - "$ROOT" "$build_deployment_target" "$MIN_MACOS" <<'PY'
from pathlib import Path
import runpy
import sys

root, target, maximum = sys.argv[1:]
audit = runpy.run_path(str(Path(root) / "packaging" / "smoke_desktop.py"))
target_key = audit["_macos_version_key"](target)
maximum_key = audit["_macos_version_key"](maximum)
if target_key is None or maximum_key is None or target_key > maximum_key:
    raise SystemExit(
        f"build Python targets macOS {target!r}; PySide cannot target {maximum}"
    )
print(f"Build Python deployment target {target} permits macOS {maximum}.", flush=True)
PY

check_macho() {
    local file="$1" require_only="${2:-yes}" arches
    [[ -f "$file" ]] || die "Mach-O file is missing: $file"
    arches="$(xcrun lipo -archs "$file")" || die "cannot inspect architecture: $file"
    case " $arches " in
        *" $TARGET_ARCH "*) ;;
        *) die "$file has no $TARGET_ARCH slice (found: $arches)" ;;
    esac
    if [[ "$require_only" == "yes" && "$arches" != "$TARGET_ARCH" ]]; then
        die "$file must contain only $TARGET_ARCH (found: $arches)"
    fi
    "$RUNTIME_PYTHON" - "$ROOT" "$file" "$MIN_MACOS" "$TARGET_ARCH" <<'PY'
from pathlib import Path
import runpy
import subprocess
import sys

root, path, maximum, architecture = sys.argv[1:]
audit = runpy.run_path(str(Path(root) / "packaging" / "smoke_desktop.py"))
output = subprocess.check_output(
    ["/usr/bin/otool", "-arch", architecture, "-l", path], text=True,
)
versions = audit["_macos_minimum_versions"](output)
limit = audit["_macos_version_key"](maximum)
if not versions or limit is None:
    raise SystemExit(f"missing macOS minimum metadata: {path}")
for version in versions:
    key = audit["_macos_version_key"](version) if version is not None else None
    if key is None or key > limit:
        raise SystemExit(f"invalid or unsupported macOS minimum {version!r}: {path}")
PY
}

check_architecture() {
    local file="$1" arches
    [[ -f "$file" ]] || die "architecture input is missing: $file"
    arches="$(xcrun lipo -archs "$file")" || die "cannot inspect architecture: $file"
    case " $arches " in
        *" $TARGET_ARCH "*) ;;
        *) die "$file has no $TARGET_ARCH slice (found: $arches)" ;;
    esac
}

# The build-only interpreter is never packaged; require its native architecture
# and a sysconfig deployment target no newer than the requested 14.4 build.
# This is required because PySide refuses a deployment target below its build
# interpreter's own MACOSX_DEPLOYMENT_TARGET.
check_architecture "$BUILD_PYTHON"
# The application interpreter is shipped, so keep the strict 14.4 Mach-O gate.
check_macho "$RUNTIME_PYTHON" no

download_pinned() {
    local url="$1" destination="$2" expected="$3"
    "$BUILD_PYTHON" - "$url" "$destination" "$expected" <<'PY'
import hashlib
import sys
import urllib.request
from pathlib import Path

url, destination, expected = sys.argv[1:]
target = Path(destination)
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=180) as response, target.open("xb") as output:
    while block := response.read(1024 * 1024):
        output.write(block)
        digest.update(block)
actual = digest.hexdigest()
if actual != expected:
    raise SystemExit(f"SHA-256 mismatch for {target.name}: {actual} != {expected}")
print(f"Verified {target.name}: {actual}", flush=True)
PY
}

download_pinned "$PYSIDE_SOURCE_URL" "$SOURCE_ARCHIVE" "$PYSIDE_SOURCE_SHA256"
download_pinned "$LIBCLANG_ARCHIVE_URL" "$LIBCLANG_ARCHIVE" "$LIBCLANG_ARCHIVE_SHA256"
"$BUILD_PYTHON" "$ROOT/packaging/bootstrap_pip.py"
"$BUILD_PYTHON" -m pip download --only-binary=:all: --no-deps --dest "$INPUT_DIR" aqtinstall==3.3.0
[[ -f "$AQT_WHEEL" ]] || die "pinned aqtinstall wheel was not downloaded"
"$BUILD_PYTHON" - "$AQT_WHEEL" "$AQT_WHEEL_SHA256" <<'PY'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as source:
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
actual = digest.hexdigest()
if actual != sys.argv[2]:
    raise SystemExit(f"SHA-256 mismatch for {path.name}: {actual} != {sys.argv[2]}")
print(f"Verified {path.name}: {actual}", flush=True)
PY

# Keep the SDK and source-build toolchains isolated from the app runtime.
"$BUILD_PYTHON" -m venv "$AQT_VENV"
"$AQT_VENV/bin/python" "$ROOT/packaging/bootstrap_pip.py"
"$AQT_VENV/bin/python" -m pip install --only-binary=:all: \
    "$AQT_WHEEL" cmake==4.4.4 ninja==1.13.2 py7zr==1.1.3 packaging==26.3 setuptools==84.1.0
"$AQT_VENV/bin/python" -m pip check
"$AQT_VENV/bin/python" "$ROOT/packaging/dependency_audit.py" --report "$WORK_ROOT/aqt-dependency-audit.json"
"$AQT_VENV/bin/python" -m pip freeze --all > "$WORK_ROOT/toolchain-environment.txt"

"$AQT_VENV/bin/aqt" list-qt mac desktop --archives "$TARGET_VERSION" clang_64 > "$WORK_ROOT/qt-archives.txt"
"$AQT_VENV/bin/python" - "$WORK_ROOT/qt-archives.txt" <<'PY'
from pathlib import Path
import sys

available = set(Path(sys.argv[1]).read_text(encoding="utf-8").split())
required = {"qtbase", "qtsvg"}
if not required <= available:
    raise SystemExit(f"Qt 6.12.0 mac SDK is missing archives: {sorted(required - available)}")
PY
"$AQT_VENV/bin/aqt" install-qt mac desktop "$TARGET_VERSION" clang_64 \
    --outputdir "$QT_ROOT" --archives qtbase qtsvg

"$AQT_VENV/bin/python" - "$LIBCLANG_ARCHIVE" "$LLVM_ROOT" <<'PY'
from pathlib import Path
import sys
import py7zr

archive, destination = Path(sys.argv[1]), Path(sys.argv[2])
destination.mkdir()
with py7zr.SevenZipFile(archive) as source:
    source.extractall(destination)
headers = list(destination.rglob("include/clang-c/Index.h"))
if len(headers) != 1:
    raise SystemExit(f"expected one libclang header tree, found {headers}")
prefix = headers[0].parents[2].resolve()
for relative in ("include/clang-c/Index.h", "lib/libclang.a", "lib/cmake/clang/ClangConfig.cmake"):
    if not (prefix / relative).is_file():
        raise SystemExit(f"libclang input is incomplete: {prefix / relative}")
(destination / "prefix.txt").write_text(str(prefix) + "\n", encoding="utf-8")
print(f"Verified static libclang prefix: {prefix}", flush=True)
PY
readonly LLVM_PREFIX="$(cat "$LLVM_ROOT/prefix.txt")"
check_architecture "$LLVM_PREFIX/lib/libclang.a"

readonly QTPATHS_BIN="$QT_PREFIX/bin/qtpaths6"
readonly CMAKE_BIN="$AQT_VENV/bin/cmake"
readonly NINJA_BIN="$AQT_VENV/bin/ninja"
[[ -x "$QTPATHS_BIN" ]] || die "Qt paths tool is missing: $QTPATHS_BIN"
[[ -x "$CMAKE_BIN" && -x "$NINJA_BIN" ]] || die "CMake or Ninja is missing"
[[ "$("$QTPATHS_BIN" --query QT_VERSION)" == "$TARGET_VERSION" ]] || die "the installed Qt SDK is not $TARGET_VERSION"
readonly QT_LIBDIR="$("$QTPATHS_BIN" --query QT_INSTALL_LIBS)"
[[ -d "$QT_LIBDIR" ]] || die "Qt framework directory is missing: $QT_LIBDIR"
for module in Core Gui Widgets Network DBus Svg Test; do
    framework="$QT_LIBDIR/Qt$module.framework"
    framework_binary="$framework/Versions/Current/Qt$module"
    [[ -f "$framework_binary" ]] || framework_binary="$framework/Qt$module"
    [[ -f "$framework_binary" ]] || die "Qt$module framework is missing from the SDK"
    check_macho "$framework_binary" no
done

MACOS_SDK="$(xcrun --sdk macosx --show-sdk-path)" || die "could not locate the macOS SDK"
[[ -d "$MACOS_SDK" ]] || die "macOS SDK is missing: $MACOS_SDK"
export LLVM_INSTALL_DIR="$LLVM_PREFIX" MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS"
export PATH="$QT_PREFIX/bin:$AQT_VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin"

tar -xJf "$SOURCE_ARCHIVE" -C "$SOURCE_ROOT"
readonly SOURCE_DIR="$("$BUILD_PYTHON" - "$SOURCE_ROOT" <<'PY'
from pathlib import Path
import sys

matches = list(Path(sys.argv[1]).glob("*/setup.py"))
if len(matches) != 1:
    raise SystemExit(f"expected one PySide setup.py, found {matches}")
print(matches[0].parent)
PY
)"
[[ -f "$SOURCE_DIR/requirements.txt" ]] || die "PySide source requirements are missing"

"$BUILD_PYTHON" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" "$ROOT/packaging/bootstrap_pip.py"
"$BUILD_VENV/bin/python" -m pip install --only-binary=:all: \
    -c "$ROOT/packaging/release-constraints.txt" setuptools==84.1.0 wheel==0.46.3
"$BUILD_VENV/bin/python" -m pip install \
    -c "$ROOT/packaging/release-constraints.txt" -r "$SOURCE_DIR/requirements.txt"
"$BUILD_VENV/bin/python" -m pip check
"$BUILD_VENV/bin/python" "$ROOT/packaging/dependency_audit.py" --report "$WORK_ROOT/pyside-dependency-audit.json"
"$BUILD_VENV/bin/python" -m pip freeze --all > "$WORK_ROOT/source-build-environment.txt"

(
    cd "$SOURCE_DIR"
    unset CFLAGS CXXFLAGS CPPFLAGS LDFLAGS
    export LLVM_INSTALL_DIR MACOSX_DEPLOYMENT_TARGET="$MIN_MACOS"
    export PATH="$QT_PREFIX/bin:$AQT_VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    "$BUILD_VENV/bin/python" setup.py install \
        --qtpaths="$QTPATHS_BIN" --cmake="$CMAKE_BIN" --ignore-git \
        --qt-conf-prefix='"Qt"' --rpath=@loader_path/Qt/lib \
        --skip-docs --no-qt-tools --verbose-build --parallel=2 \
        --module-subset=Core,Gui,Widgets,Network,DBus,Svg,Test \
        --limited-api=yes --macos-arch="$TARGET_ARCH" \
        --macos-sysroot="$MACOS_SDK" --macos-deployment-target="$MIN_MACOS"
) 2>&1 | tee "$WORK_ROOT/pyside-build.log"

readonly -a REBUILT_PATHS=(
    "PySide6/QtCore.abi3.so"
    "PySide6/QtGui.abi3.so"
    "PySide6/QtWidgets.abi3.so"
    "PySide6/QtNetwork.abi3.so"
    "PySide6/QtDBus.abi3.so"
    "PySide6/QtSvg.abi3.so"
    "PySide6/QtTest.abi3.so"
    "PySide6/libpyside6.abi3.6.12.dylib"
    "shiboken6/Shiboken.abi3.so"
    "shiboken6/libshiboken6.abi3.6.12.dylib"
)
readonly BUILD_SITE_PACKAGES="$("$BUILD_VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
mkdir -p "$OVERLAY_RAW" "$OVERLAY"
for relative_path in "${REBUILT_PATHS[@]}"; do
    source_file="$BUILD_SITE_PACKAGES/$relative_path"
    check_macho "$source_file"
    destination="$OVERLAY_RAW/$relative_path"
    mkdir -p "$(dirname "$destination")"
    cp -p "$source_file" "$destination"
done

# Normalize only the staged copies: keep all build minimums and use package-relative RPATHs.
"$AQT_VENV/bin/python" - "$OVERLAY_RAW" "$OVERLAY" "$TARGET_ARCH" "$MIN_MACOS" "$ROOT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import shutil

source_root, output_root = Path(sys.argv[1]), Path(sys.argv[2])
architecture, maximum, repository = sys.argv[3:]
import runpy
audit = runpy.run_path(str(Path(repository) / "packaging" / "smoke_desktop.py"))
minimum_limit = audit["_macos_version_key"](maximum)
if minimum_limit is None:
    raise SystemExit("invalid requested macOS minimum")
paths = [f"PySide6/Qt{name}.abi3.so" for name in ("Core", "Gui", "Widgets", "Network", "DBus", "Svg", "Test")]
paths += ["PySide6/libpyside6.abi3.6.12.dylib", "shiboken6/Shiboken.abi3.so", "shiboken6/libshiboken6.abi3.6.12.dylib"]

def run(*args):
    return subprocess.check_output(args, text=True, env={**os.environ, "LC_ALL": "C"})

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def load_commands(path):
    return run("/usr/bin/otool", "-arch", architecture, "-l", str(path))

def rpaths(text):
    values = []
    for block in re.findall(r"cmd LC_RPATH\b.*?(?=Load command|\Z)", text, re.S):
        match = re.search(r"\bpath\s+(.+?)\s+\(offset \d+\)", block)
        if match:
            values.append(match.group(1))
    return list(dict.fromkeys(values))

def minimums(text):
    values = audit["_macos_minimum_versions"](text)
    if not values or any(value is None or audit["_macos_version_key"](value) is None for value in values):
        raise SystemExit("macOS minimum metadata is missing or invalid")
    return list(values)

records = []
for name in paths:
    original = source_root / name
    target = output_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, target)
    before = load_commands(target)
    before_minimums = minimums(before)
    before_rpaths = rpaths(before)
    for value in before_rpaths:
        subprocess.run(["/usr/bin/install_name_tool", "-delete_rpath", value, str(target)], check=True)
    relative_rpaths = ["@loader_path"]
    if name.startswith("PySide6/"):
        relative_rpaths += ["@loader_path/../shiboken6", "@loader_path/Qt/lib"]
    for value in relative_rpaths:
        subprocess.run(["/usr/bin/install_name_tool", "-add_rpath", value, str(target)], check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(target)], check=True, capture_output=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(target)], check=True, capture_output=True)
    after = load_commands(target)
    after_minimums = minimums(after)
    if before_minimums != after_minimums:
        raise SystemExit(f"relocation changed minimum OS metadata: {name}")
    if not all(audit["_macos_version_key"](value) <= minimum_limit for value in after_minimums):
        raise SystemExit(f"binding minimum exceeds macOS {maximum}: {name}: {after_minimums}")
    if rpaths(after) != relative_rpaths:
        raise SystemExit(f"unexpected RPATHs after relocation: {name}: {rpaths(after)}")
    for block in re.findall(r"cmd LC_(?:LOAD_DYLIB|LOAD_WEAK_DYLIB|REEXPORT_DYLIB)\b.*?(?=Load command|\Z)", after, re.S):
        match = re.search(r"\bname\s+(.+?)\s+\(offset \d+\)", block)
        if match and match.group(1).startswith("/") and not match.group(1).startswith(("/usr/lib/", "/System/Library/")):
            raise SystemExit(f"absolute non-system dependency in {name}: {match.group(1)}")
    records.append({
        "path": name,
        "source_sha256": sha256(original),
        "sha256": sha256(target),
        "architecture": architecture,
        "minimums_before": before_minimums,
        "minimums_after": after_minimums,
        "rpaths": relative_rpaths,
    })

Path(output_root, "manifest.json").write_text(json.dumps({
    "source_sha256": "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751",
    "deployment_target": maximum,
    "architecture": architecture,
    "deployment_targets_unchanged": True,
    "binaries": records,
}, indent=2) + "\n", encoding="utf-8")
print(f"Audited and relocated {len(records)} {architecture} PySide bindings for macOS {maximum}.", flush=True)
PY

readonly RUNTIME_SITE_PACKAGES="$("$RUNTIME_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
[[ -d "$RUNTIME_SITE_PACKAGES/PySide6" && -d "$RUNTIME_SITE_PACKAGES/shiboken6" ]] || die "the application runtime is missing PySide6/Shiboken"
"$RUNTIME_PYTHON" - "$RUNTIME_SITE_PACKAGES" "$OVERLAY" "$WORK_ROOT/pdf-before.json" <<'PY'
import hashlib
import json
from importlib.metadata import version
from pathlib import Path
import shutil
import sys

site, overlay, manifest = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3])
if version("PySide6") != "6.12.0" or version("shiboken6") != "6.12.0":
    raise SystemExit("runtime PySide6 and shiboken6 must both be version 6.12.0")
pdf_files = [site / "PySide6" / "QtPdf.abi3.so", site / "PySide6" / "QtPdfWidgets.abi3.so"]
if any(not path.is_file() for path in pdf_files):
    raise SystemExit(f"official QtPdf runtime bindings are missing: {pdf_files}")
def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()
pdf_hashes = {path.name: digest(path) for path in pdf_files}
for source in overlay.rglob("*"):
    if not source.is_file() or source.name == "manifest.json":
        continue
    relative = source.relative_to(overlay)
    destination = site / relative
    if not destination.resolve().is_relative_to(site) or not destination.parent.is_dir():
        raise SystemExit(f"overlay path escapes or is incomplete: {destination}")
    shutil.copy2(source, destination)
manifest.write_text(json.dumps({"site_packages": site.name, "preserved_qtpdf_sha256": pdf_hashes}, indent=2) + "\n", encoding="utf-8")
if {path.name: digest(path) for path in pdf_files} != pdf_hashes:
    raise SystemExit("official QtPdf/QtPdfWidgets files changed while applying the overlay")
PY

# Import the built overlay with app Python and prove loaded Qt frameworks resolve from its Qt/lib.
QT_QPA_PLATFORM=offscreen "$RUNTIME_PYTHON" - "$RUNTIME_SITE_PACKAGES" <<'PY'
import ctypes
from pathlib import Path
import sys
import tempfile
from PySide6 import QtCore, QtDBus, QtGui, QtNetwork, QtPdf, QtPdfWidgets, QtSvg, QtTest, QtWidgets
import shiboken6

site = Path(sys.argv[1]).resolve()
qt_lib = (site / "PySide6" / "Qt" / "lib").resolve()
assert QtCore.qVersion() == "6.12.0", QtCore.qVersion()
assert shiboken6.__version__ == "6.12.0", shiboken6.__version__

def one_page_pdf():
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length 0 >>\nstream\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(body + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(data)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
with tempfile.TemporaryDirectory() as temporary:
    pdf_path = Path(temporary) / "single-page.pdf"
    pdf_path.write_bytes(one_page_pdf())
    document = QtPdf.QPdfDocument()
    try:
        if document.load(str(pdf_path)) != QtPdf.QPdfDocument.Error.None_:
            raise SystemExit("QPdfDocument could not load the synthetic PDF")
        if document.status() == QtPdf.QPdfDocument.Status.Loading:
            loop = QtCore.QEventLoop()
            timer = QtCore.QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            document.statusChanged.connect(
                lambda status: loop.quit()
                if status != QtPdf.QPdfDocument.Status.Loading else None
            )
            timer.start(15000)
            loop.exec()
        if document.status() != QtPdf.QPdfDocument.Status.Ready:
            raise SystemExit(f"QPdfDocument did not finish loading: {document.status()}")
        if int(document.pageCount()) != 1:
            raise SystemExit(f"QPdfDocument did not report one page: {document.pageCount()}")
        view = QtPdfWidgets.QPdfView()
        view.setDocument(document)
        if view.document() is not document:
            raise SystemExit("QPdfView did not retain the loaded QPdfDocument")
        view.show()
        app.processEvents()
        print("QPdfView loaded one page.", flush=True)
    finally:
        document.close()

dyld = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
dyld._dyld_image_count.restype = ctypes.c_uint32
dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
dyld._dyld_get_image_name.restype = ctypes.c_char_p
loaded = []
for index in range(dyld._dyld_image_count()):
    raw = dyld._dyld_get_image_name(index)
    if not raw:
        continue
    image = Path(raw.decode("utf-8", "replace"))
    if ".framework/" in str(image) and image.name.startswith("Qt"):
        loaded.append(image.resolve())
if not loaded or any(not image.is_relative_to(qt_lib) for image in loaded):
    raise SystemExit(f"Qt frameworks resolved outside the Python runtime's Qt/lib: {loaded}")
print(f"Imported the overlay; QPdfView loaded one page; {len(loaded)} Qt frameworks resolved from {qt_lib}.", flush=True)
PY

# Publish a compact provenance file alongside the platform package artifact.
"$BUILD_PYTHON" - "$ROOT" "$WORK_ROOT" "$TARGET_ARCH" "$build_info" "$runtime_info" "$CMAKE_BIN" "$NINJA_BIN" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root, work, arch, build_info, runtime_info, cmake, ninja = sys.argv[1:]
work = Path(work)
overlay = work / "overlay"
site_info = json.loads((work / "pdf-before.json").read_text(encoding="utf-8"))
def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
def version(command):
    return subprocess.check_output(command, text=True).splitlines()[0].strip()
manifest = {
    "platform": f"macos-{'arm64' if arch == 'arm64' else 'x64'}",
    "deployment_target": "14.4",
    "pyside_source_sha256": "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751",
    "static_libclang_archive_sha256": "0bca8fe7e2f313e12a17ca3a6d44484c5dd5e2274038869398ed2eec36b8a9e4",
    "aqtinstall_wheel_sha256": "e88dbd87226f276fdd5d05347a44578d390d93a7f176e9476fbda0a7c9635f69",
    "qt_version": "6.12.0",
    "build_python": build_info,
    "runtime_python": runtime_info,
    "cmake": version([cmake, "--version"]),
    "ninja": version([ninja, "--version"]),
    "aqt_environment": (work / "toolchain-environment.txt").read_text(encoding="utf-8").splitlines(),
    "source_build_environment": (work / "source-build-environment.txt").read_text(encoding="utf-8").splitlines(),
    "preserved_qtpdf_sha256": site_info["preserved_qtpdf_sha256"],
    "bindings": json.loads((overlay / "manifest.json").read_text(encoding="utf-8"))["binaries"],
}
output = Path(root) / "build" / "desktop" / f"macos-binding-provenance-{arch}.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"Wrote binding build provenance: {output}", flush=True)
PY

printf 'macOS %s PySide bindings built from the pinned 6.12.0 source for deployment target %s.\n' "$TARGET_ARCH" "$MIN_MACOS"
