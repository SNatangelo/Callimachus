#!/usr/bin/env bash
set -euo pipefail

# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

# Install the exact application Python only on a disposable GitHub macOS runner.
readonly PYTHON_VERSION="3.13.15"
readonly MIN_MACOS="14.4"
readonly INSTALLER_URL="https://www.python.org/ftp/python/3.13.15/python-3.13.15-macos11.pkg"
readonly INSTALLER_SHA256="3b7eaf7f29825f796e8267024435540ddf1f17fc9a97ad58095daa7a75bfdcd3"
readonly INSTALLER_SIZE="72078932"
readonly EXPECTED_TEAM_ID="BMM5U3QVKW"
readonly CERTIFICATES_COMMAND="/Applications/Python 3.13/Install Certificates.command"
readonly ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export LC_ALL=C PYTHONDONTWRITEBYTECODE=1

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "${GITHUB_ACTIONS:-}" == "true" ]] || die "must run inside GitHub Actions"
[[ "${RUNNER_OS:-}" == "macOS" ]] || die "must run on a GitHub macOS runner"
[[ "$(uname -s)" == "Darwin" ]] || die "must run on native macOS"
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" ]] || die "RUNNER_TEMP is missing"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GITHUB_PATH:?GITHUB_PATH is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

readonly TARGET_ARCH="$(uname -m)"
case "$TARGET_ARCH" in
    arm64|x86_64) ;;
    *) die "unsupported native macOS architecture: $TARGET_ARCH" ;;
esac

readonly TASK_ROOT="$RUNNER_TEMP/callimachus-macos-python-$PYTHON_VERSION"
readonly PACKAGE="$TASK_ROOT/python-$PYTHON_VERSION-macos11.pkg"
readonly FRAMEWORK_ROOT="/Library/Frameworks/Python.framework/Versions/3.13"
readonly BASE_PYTHON="$FRAMEWORK_ROOT/bin/python3.13"
readonly FRAMEWORK_BINARY="$FRAMEWORK_ROOT/Python"
readonly VENV_ROOT="$TASK_ROOT/venv"
readonly RUNTIME_PYTHON="$VENV_ROOT/bin/python"
readonly PYTHON_BIN="$VENV_ROOT/bin"

[[ ! -e "$TASK_ROOT" && ! -L "$TASK_ROOT" ]] || die "task directory already exists: $TASK_ROOT"
mkdir -m 700 "$TASK_ROOT"

/usr/bin/curl --fail --location --silent --show-error \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    "$INSTALLER_URL" --output "$PACKAGE"
[[ "$(/usr/bin/stat -f '%z' "$PACKAGE")" == "$INSTALLER_SIZE" ]] || die "Python.org installer size mismatch"
printf '%s  %s\n' "$INSTALLER_SHA256" "$PACKAGE" | /usr/bin/shasum -a 256 --check -

signature="$(/usr/sbin/pkgutil --check-signature "$PACKAGE")" || die "Python.org installer signature is invalid"
[[ "$signature" == *"Status: signed by a developer certificate issued by Apple for distribution"* ]] || die "Python.org installer is not signed by a trusted Apple distribution certificate"
[[ "$signature" == *"Developer ID Installer: Python Software Foundation ($EXPECTED_TEAM_ID)"* ]] || die "Python.org installer signer is not the expected Python Software Foundation identity"
printf 'Verified Python.org installer signature and package hash.\n'

sudo /usr/sbin/installer -pkg "$PACKAGE" -target /
[[ -x "$BASE_PYTHON" ]] || die "installed Python interpreter is missing: $BASE_PYTHON"
[[ -f "$FRAMEWORK_BINARY" ]] || die "installed Python framework binary is missing: $FRAMEWORK_BINARY"
[[ -f "$CERTIFICATES_COMMAND" ]] || die "Python.org CA setup command is missing: $CERTIFICATES_COMMAND"

CERTIFI_VERSION="$("$BASE_PYTHON" - "$ROOT/packaging/release-constraints.txt" <<'PY'
from pathlib import Path
import sys

constraints = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
pins = [
    line.partition("#")[0].strip().split("==", 1)[1]
    for line in constraints
    if line.partition("#")[0].strip().lower().startswith("certifi==")
]
if len(pins) != 1:
    raise SystemExit("release constraints must contain exactly one certifi== pin")
print(pins[0])
PY
)" || die "could not read the pinned certifi version"
readonly CERTIFI_VERSION
PIP_CONSTRAINT="$ROOT/packaging/release-constraints.txt" PIP_NO_INPUT=1 \
    /bin/sh "$CERTIFICATES_COMMAND"
BASE_CERTIFICATE_INFO="$("$BASE_PYTHON" - <<'PY'
import certifi
from importlib.metadata import version

print(f"{version('certifi')}|{certifi.where()}")
PY
)" || die "could not locate the Python.org certifi bundle"
IFS='|' read -r BASE_CERTIFI_VERSION BASE_CA_FILE <<< "$BASE_CERTIFICATE_INFO"
[[ "$BASE_CERTIFI_VERSION" == "$CERTIFI_VERSION" ]] || die "Python.org certifi version does not match the release pin"
[[ -s "$BASE_CA_FILE" ]] || die "Python.org certifi CA bundle is missing: $BASE_CA_FILE"
SSL_CERT_FILE="$BASE_CA_FILE" "$BASE_PYTHON" - "$BASE_CA_FILE" <<'PY'
from pathlib import Path
import ssl
import sys
import urllib.request

expected = Path(sys.argv[1]).resolve(strict=True)
actual = ssl.get_default_verify_paths().cafile
if actual is None or Path(actual).resolve(strict=True) != expected:
    raise SystemExit("stdlib TLS is not using the verified Python.org CA bundle")
with urllib.request.urlopen("https://www.python.org/", timeout=30) as response:
    if response.status != 200 or not response.read(1):
        raise SystemExit("stdlib HTTPS probe returned an unexpected response")
PY

check_native_slice() {
    local binary="$1" architectures
    architectures="$(/usr/bin/lipo -archs "$binary")" || die "cannot inspect Python architecture: $binary"
    case " $architectures " in
        *" $TARGET_ARCH "*) ;;
        *) die "$binary has no native $TARGET_ARCH slice (found: $architectures)" ;;
    esac
}

minimum_versions() {
    local binary="$1"
    "$BASE_PYTHON" - "$ROOT" "$binary" "$TARGET_ARCH" "$MIN_MACOS" <<'PY'
from pathlib import Path
import os
import runpy
import subprocess
import sys

root, binary, architecture, maximum = sys.argv[1:]
audit = runpy.run_path(str(Path(root) / "packaging" / "smoke_desktop.py"))
output = subprocess.check_output(
    ["/usr/bin/otool", "-arch", architecture, "-l", binary],
    text=True,
    env={**os.environ, "LC_ALL": "C"},
)
versions = audit["_macos_minimum_versions"](output)
limit = audit["_macos_version_key"](maximum)
if not versions or limit is None:
    raise SystemExit(f"missing macOS minimum metadata: {binary}")
for version in versions:
    key = audit["_macos_version_key"](version) if version is not None else None
    if key is None or key > limit:
        raise SystemExit(f"invalid or unsupported macOS minimum {version!r}: {binary}")
print(",".join(versions))
PY
}

check_native_slice "$BASE_PYTHON"
check_native_slice "$FRAMEWORK_BINARY"
readonly PYTHON_INFO="$("$BASE_PYTHON" -c 'import platform,sys; print(".".join(map(str, sys.version_info[:3])) + "|" + platform.machine())')"
[[ "$PYTHON_INFO" == "$PYTHON_VERSION|$TARGET_ARCH" ]] || die "expected native Python $PYTHON_VERSION, found $PYTHON_INFO"
INTERPRETER_MINIMUMS="$(minimum_versions "$BASE_PYTHON")" || die "could not validate interpreter macOS minimum"
readonly INTERPRETER_MINIMUMS
FRAMEWORK_MINIMUMS="$(minimum_versions "$FRAMEWORK_BINARY")" || die "could not validate framework macOS minimum"
readonly FRAMEWORK_MINIMUMS

"$BASE_PYTHON" -m venv "$VENV_ROOT"
[[ -x "$RUNTIME_PYTHON" ]] || die "application Python virtual environment was not created"
readonly RUNTIME_INFO="$("$RUNTIME_PYTHON" -c 'import platform,sys; print(".".join(map(str, sys.version_info[:3])) + "|" + platform.machine())')"
[[ "$RUNTIME_INFO" == "$PYTHON_VERSION|$TARGET_ARCH" ]] || die "virtual environment has unexpected Python: $RUNTIME_INFO"
check_native_slice "$RUNTIME_PYTHON"
VENV_MINIMUMS="$(minimum_versions "$RUNTIME_PYTHON")" || die "could not validate virtual environment macOS minimum"
readonly VENV_MINIMUMS
"$RUNTIME_PYTHON" -m pip --version >/dev/null

"$RUNTIME_PYTHON" - "$ROOT" "$INSTALLER_URL" "$INSTALLER_SHA256" "$TARGET_ARCH" \
    "$INTERPRETER_MINIMUMS" "$FRAMEWORK_MINIMUMS" "$VENV_MINIMUMS" "$MIN_MACOS" "$CERTIFI_VERSION" <<'PY'
import json
from pathlib import Path
import sys

root, url, digest, architecture, interpreter, framework, venv, minimum, certifi_version = sys.argv[1:]
manifest = {
    "source_url": url,
    "installer_sha256": digest,
    "python_version": "3.13.15",
    "native_architecture": architecture,
    "required_minimum_macos": minimum,
    "stdlib_ca_certifi_version": certifi_version,
    "interpreter_minimums": interpreter.split(","),
    "framework_minimums": framework.split(","),
    "virtual_environment_interpreter_minimums": venv.split(","),
}
path = Path(root) / "build" / "desktop" / f"macos-python-runtime-provenance-{architecture}.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"Wrote verified macOS runtime provenance for {architecture}.", flush=True)
PY

rm -f "$PACKAGE"
{
    printf 'python-path=%s\n' "$RUNTIME_PYTHON"
    printf 'python-bin=%s\n' "$PYTHON_BIN"
    printf 'python-location=%s\n' "$VENV_ROOT"
    printf 'python-root=%s\n' "$FRAMEWORK_ROOT"
    printf 'pkg-config-path=%s\n' "$FRAMEWORK_ROOT/lib/pkgconfig"
    printf 'certifi-version=%s\n' "$CERTIFI_VERSION"
} >> "$GITHUB_OUTPUT"
printf '%s\n' "$PYTHON_BIN" >> "$GITHUB_PATH"
{
    printf 'pythonLocation=%s\n' "$VENV_ROOT"
    printf 'Python_ROOT_DIR=%s\n' "$FRAMEWORK_ROOT"
    printf 'Python2_ROOT_DIR=\n'
    printf 'Python3_ROOT_DIR=%s\n' "$FRAMEWORK_ROOT"
    printf 'PKG_CONFIG_PATH=%s\n' "$FRAMEWORK_ROOT/lib/pkgconfig"
    printf 'SSL_CERT_FILE=%s\n' "$BASE_CA_FILE"
} >> "$GITHUB_ENV"
printf 'Prepared verified CPython %s for macOS %s; runtime Mach-O minimum is at most %s.\n' \
    "$PYTHON_VERSION" "$TARGET_ARCH" "$MIN_MACOS"
