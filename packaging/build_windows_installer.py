# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the per-user Windows x64 NSIS installer and its SHA-256 sidecar."""
from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import shutil
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build" / "desktop" / "dist"
ARTIFACTS = ROOT / "build" / "desktop" / "artifacts"
NSIS_SCRIPT = ROOT / "packaging" / "windows-installer.nsi"
RUNTIME_PREFLIGHT = ROOT / "packaging" / "install_vc_runtime.ps1"
NSIS_LICENSE = ROOT / "packaging" / "NSIS-3.10-LICENSE.txt"
OUTPUT = ARTIFACTS / "Callimachus-Setup.exe"
BUILD_ROOT = ROOT / "build" / "desktop"
MSVC_RUNTIME_DLL = (
    "msvcp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def _runtime_sources() -> list[Path]:
    analysis_toc = BUILD_ROOT / "work" / "Callimachus" / "Analysis-00.toc"
    if not analysis_toc.is_file():
        raise RuntimeError(f"PyInstaller analysis is missing: {analysis_toc}")
    analysis = ast.literal_eval(analysis_toc.read_text(encoding="utf-8"))
    found: dict[str, set[Path]] = {}

    def visit(value: object) -> None:
        if isinstance(value, (tuple, list)):
            if (len(value) == 3 and isinstance(value[0], str)
                    and isinstance(value[1], str)
                    and value[2] in {"BINARY", "EXTENSION"}):
                name = PureWindowsPath(value[0]).name.lower()
                if name in MSVC_RUNTIME_DLL:
                    found.setdefault(name, set()).add(Path(value[1]).resolve())
            else:
                for child in value:
                    visit(child)

    visit(analysis)
    missing = sorted(set(MSVC_RUNTIME_DLL) - found.keys())
    if missing:
        raise RuntimeError(f"PyInstaller analysis is missing VC runtime inputs: {', '.join(missing)}")
    sources = sorted({path for paths in found.values() for path in paths})
    for path in sources:
        if not path.is_file():
            raise RuntimeError(f"VC runtime source is missing from the build runner: {path}")
    return sources


def _file_version(path: Path) -> tuple[int, int, int, int]:
    version_api = ctypes.WinDLL("version", use_last_error=True)
    version_api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version_api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version_api.GetFileVersionInfoW.restype = wintypes.BOOL
    version_api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version_api.VerQueryValueW.restype = wintypes.BOOL

    dummy = wintypes.DWORD()
    size = version_api.GetFileVersionInfoSizeW(str(path), ctypes.byref(dummy))
    if not size:
        raise OSError(ctypes.get_last_error(), f"file version is unavailable: {path}")
    buffer = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise OSError(ctypes.get_last_error(), f"could not read file version: {path}")
    info_pointer = ctypes.c_void_p()
    info_size = wintypes.UINT()
    if not version_api.VerQueryValueW(buffer, "\\", ctypes.byref(info_pointer), ctypes.byref(info_size)):
        raise OSError(ctypes.get_last_error(), f"could not query file version: {path}")
    if info_size.value < ctypes.sizeof(_VSFixedFileInfo):
        raise RuntimeError(f"file version resource is incomplete: {path}")
    info = ctypes.cast(info_pointer, ctypes.POINTER(_VSFixedFileInfo)).contents
    return (
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    )


def _minimum_runtime_version() -> str:
    versions = [_file_version(path) for path in _runtime_sources()]
    # The redist's registry Version commonly ends in .0 while an individual
    # runtime DLL has a nonzero revision. The redist package version is the
    # shared major.minor.build tuple.
    version = (*max(versions)[:3], 0)
    return ".".join(str(part) for part in version)


def _validate_distribution() -> None:
    executable = DIST / "Callimachus" / "Callimachus.exe"
    if not executable.is_file():
        raise RuntimeError(f"built Windows executable is missing: {executable}")
    for resource in (DIST / "LICENSE", DIST / "THIRD-PARTY-NOTICES" / "index.json"):
        if not resource.is_file():
            raise RuntimeError(f"required release resource is missing: {resource}")

    forbidden = []
    for path in DIST.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (name in MSVC_RUNTIME_DLL
                or name.startswith(("vc_redist", "vcredist")) and path.suffix.lower() == ".exe"):
            forbidden.append(path)
    if forbidden:
        formatted = ", ".join(str(path.relative_to(DIST)) for path in sorted(forbidden))
        raise RuntimeError(f"Windows distribution must use the installed VC runtime; found bundled runtime files: {formatted}")


def build() -> Path:
    """Compile the checked PyInstaller distribution without changing it."""
    if sys.platform != "win32":
        raise RuntimeError("the Windows installer must be built on Windows")
    _validate_distribution()
    if not NSIS_SCRIPT.is_file() or not RUNTIME_PREFLIGHT.is_file() or not NSIS_LICENSE.is_file():
        raise RuntimeError("an NSIS installer source, license, or VC runtime preflight file is missing")
    compiler = shutil.which("makensis")
    if compiler is None:
        raise RuntimeError("makensis was not found; install NSIS 3.10 and retry")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    temporary_output = ARTIFACTS / f".Callimachus-Setup-{uuid.uuid4().hex}.exe"
    try:
        subprocess.run(
            [
                compiler,
                "/V2",
                f"/DDIST_DIR={DIST}",
                f"/DPACKAGING_DIR={NSIS_SCRIPT.parent}",
                f"/DNSIS_LICENSE_FILE={NSIS_LICENSE}",
                f"/DVC_RUNTIME_MINIMUM_VERSION={_minimum_runtime_version()}",
                f"/DOUTPUT_FILE={temporary_output}",
                str(NSIS_SCRIPT),
            ],
            cwd=ROOT,
            check=True,
            timeout=900,
        )
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise RuntimeError(f"NSIS did not produce an installer: {temporary_output}")

        temporary_output.replace(OUTPUT)
        with OUTPUT.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        sidecar = OUTPUT.with_name(OUTPUT.name + ".sha256")
        temporary_sidecar = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_sidecar.write_text(f"{digest}  {OUTPUT.name}\n", encoding="ascii")
            temporary_sidecar.replace(sidecar)
        finally:
            temporary_sidecar.unlink(missing_ok=True)
        return OUTPUT
    finally:
        temporary_output.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(build())


if __name__ == "__main__":
    main()
