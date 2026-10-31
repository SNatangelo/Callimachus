#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate Qt 6.12 / MSVC 14.39 Store build stages.

Run each stage inside the established external Windows process/RAM guard.
All paths are explicit. This tool does not acquire tools, sources, or credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess

SDK_VERSION = "10.0.26100.0"
VC_VERSION = "14.39.33519"
MODULES = "qtbase;qtsvg;qtshadertools;qtdeclarative;qtvirtualkeyboard;qtimageformats"
WINDOWS_FLAGS = "/DWINVER=0x0A00 /D_WIN32_WINNT=0x0A00"


def sdk_library_paths(sdk: Path) -> tuple[Path, Path]:
    """Select a complete SDK library pair, never mix partial layouts."""
    for root in (sdk, sdk / "Lib" / SDK_VERSION):
        ucrt, um = root / "ucrt" / "x64", root / "um" / "x64"
        if (ucrt / "ucrt.lib").is_file() and (um / "kernel32.lib").is_file():
            return ucrt, um
    raise FileNotFoundError("Pinned SDK x64 UCRT and UM libraries are missing")


def check_tool_versions(compiler_text: str, linker_text: str) -> None:
    if not re.search(r"(?<![\d.])19\.39\.33523(?:\.0)?(?![\d.])", compiler_text):
        raise RuntimeError("MSVC compiler must be version 19.39.33523")
    if not re.search(r"(?<![\d.])14\.39\.33523(?:\.0)?(?![\d.])", linker_text):
        raise RuntimeError("MSVC linker must be version 14.39.33523")


def _ascii_banner(stdout: bytes, stderr: bytes) -> str:
    """Read version tokens without depending on the localized system codepage."""
    return (stdout + stderr).decode("ascii", errors="replace")


def vcvars_environment(vs: Path) -> dict[str, str]:
    """Capture only build-tool fields from the pinned Visual Studio setup."""
    script = vs / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not script.is_file():
        raise FileNotFoundError("Pinned vcvars64.bat is missing")
    if any(char in str(script) for char in '&|<>^%!"'):
        raise ValueError("Visual Studio path contains unsupported command characters")
    command = f'cmd.exe /u /d /c call "{script}" -vcvars_ver=14.39 >nul && set'
    # Pass the Windows command line as a string: subprocess's argument-list
    # quoting is for the C runtime, not cmd.exe's batch parser.
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Pinned Visual Studio environment setup failed")
    try:
        output = result.stdout.decode("utf-16-le").lstrip("\ufeff")
    except UnicodeError as exc:
        raise RuntimeError("Pinned Visual Studio environment output could not be decoded") from exc
    allowed = {"PATH", "INCLUDE", "LIB", "LIBPATH", "VSINSTALLDIR", "VISUALSTUDIOVERSION",
               "VCTOOLSINSTALLDIR", "VCTOOLSVERSION", "VCINSTALLDIR", "VCTARGETSPATH",
               "WINDOWSSDKDIR", "WINDOWSSDKVERSION", "UNIVERSALCRTSDKDIR", "UCRTVERSION",
               "VSCMD_ARG_TGT_ARCH", "VSCMD_ARG_HOST_ARCH", "PLATFORM"}
    values = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.upper() in allowed:
            values[key.upper()] = value
    return values


def environment(args: argparse.Namespace) -> dict[str, str]:
    if os.name != "nt":
        raise RuntimeError("The Store native recipe requires Windows")
    vs, sdk = args.vs.resolve(), args.sdk.resolve()
    vc = vs / "VC" / "Tools" / "MSVC" / VC_VERSION
    cl, link = vc / "bin" / "Hostx64" / "x64" / "cl.exe", vc / "bin" / "Hostx64" / "x64" / "link.exe"
    for path in (cl, link, args.cmake, args.ninja, args.python, args.perl, args.clang / "lib" / "cmake" / "clang"):
        if not path.exists():
            raise FileNotFoundError(f"Required build input is missing: {path}")
    env = {key.upper(): value for key, value in os.environ.items()}
    env.update(vcvars_environment(vs))
    env.pop("CC", None)
    env.pop("CXX", None)
    cl_result = subprocess.run([str(cl)], capture_output=True, check=False, env=env)
    link_result = subprocess.run([str(link), "/?"], capture_output=True, check=False, env=env)
    check_tool_versions(_ascii_banner(cl_result.stdout, cl_result.stderr),
                        _ascii_banner(link_result.stdout, link_result.stderr))
    python_result = subprocess.run([str(args.python), "--version"], capture_output=True, check=False)
    if python_result.returncode or _ascii_banner(python_result.stdout, python_result.stderr).strip() != "Python 3.13.15":
        raise RuntimeError("Store builder requires CPython 3.13.15")
    ucrt, um = sdk_library_paths(sdk)
    includes = [vc / "include", *(sdk / "Include" / SDK_VERSION / leaf for leaf in
                ("ucrt", "shared", "um", "winrt", "cppwinrt"))]
    if not all(path.is_dir() for path in includes):
        raise FileNotFoundError("Pinned SDK or MSVC headers are missing")
    env["PATH"] = os.pathsep.join(map(str, [cl.parent, sdk / "bin" / SDK_VERSION / "x64",
        args.cmake.parent, args.ninja.parent, args.python.parent, args.perl.parent])) + os.pathsep + env.get("PATH", "")
    env["INCLUDE"] = os.pathsep.join(map(str, includes))
    env["LIB"] = os.pathsep.join(map(str, [vc / "lib" / "x64", ucrt, um]))
    env.update({"VCTOOLSINSTALLDIR": str(vc) + os.sep, "VCTOOLSVERSION": VC_VERSION,
        "VCINSTALLDIR": str(vs / "VC") + os.sep, "WINDOWSSDKDIR": str(sdk) + os.sep,
        "WINDOWSSDKVERSION": SDK_VERSION + os.sep, "UNIVERSALCRTSDKDIR": str(sdk) + os.sep,
        "UCRTVERSION": SDK_VERSION, "VSCMD_ARG_TGT_ARCH": "x64", "VSCMD_ARG_HOST_ARCH": "x64",
        "PLATFORM": "x64", "LLVM_INSTALL_DIR": str(args.clang.resolve()),
        "CMAKE_BUILD_PARALLEL_LEVEL": "2", "NINJAFLAGS": "-j2", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


QWE_ORIGINAL_SHA256 = "52d34bcfd7c0d40b6832badb59bd1d8ba5e1fa12dac788b9299bf6c2b2bc9f96"
QWE_OLD = "  args = [script_path, cpu_arg, ]\n  return _LoadEnvFromBat(args)"
QWE_NEW = r"""  if cpu == 'x86':
    return _LoadEnvFromBat([script_path, cpu_arg])
  if cpu != 'x64':
    raise ValueError('Pinned Qt PDF toolchain supports only x64')
  sdk = os.path.normpath(win_sdk_path)
  if os.path.normcase(sdk) != os.path.normcase(os.path.normpath(os.environ['QWE_PINNED_SDK'])):
    raise ValueError('Unexpected SDK root')
  vc = os.path.join(visual_studio_path, 'VC', 'Tools', 'MSVC', '14.39.33519')
  vcbin = os.path.join(vc, 'bin', 'Hostx64', 'x64')
  includes = [os.path.join(vc, 'include')] + [os.path.join(sdk, 'Include', '10.0.26100.0', x) for x in ('ucrt', 'shared', 'um', 'winrt', 'cppwinrt')]
  libraries = None
  for root in (sdk, os.path.join(sdk, 'Lib', '10.0.26100.0')):
    pair = [os.path.join(root, 'ucrt', 'x64'), os.path.join(root, 'um', 'x64')]
    if os.path.isfile(os.path.join(pair[0], 'ucrt.lib')) and os.path.isfile(os.path.join(pair[1], 'kernel32.lib')):
      libraries = pair
      break
  if not libraries or not all(os.path.isdir(p) for p in includes) or not os.path.isfile(os.path.join(vcbin, 'cl.exe')):
    raise ValueError('Pinned SDK or compiler files are missing')
  env = {k.upper(): v for k, v in _LoadEnvFromBat([script_path, cpu_arg, '-vcvars_ver=14.39']).items()}
  if env.get('VCTOOLSVERSION', '').rstrip(os.sep) != '14.39.33519':
    raise ValueError('Unexpected compiler version')
  env['INCLUDE'] = ';'.join(includes)
  env['LIB'] = ';'.join([os.path.join(vc, 'lib', 'x64')] + libraries)
  env['LIBPATH'] = os.path.join(vc, 'lib', 'x64')
  env['PATH'] = ';'.join([vcbin, os.path.join(sdk, 'bin', '10.0.26100.0', 'x64')] + [p for p in env['PATH'].split(';') if 'windows kits' not in os.path.normcase(os.path.normpath(p)) and os.path.normcase(os.path.join('vc','tools','msvc')) not in os.path.normcase(os.path.normpath(p))])
  env['WINDOWSSDKDIR'] = sdk + os.sep
  env['WINDOWSSDKVERSION'] = '10.0.26100.0' + os.sep
  env['UNIVERSALCRTSDKDIR'] = sdk + os.sep
  env['UCRTVERSION'] = '10.0.26100.0'
  if env.get('VSCMD_ARG_TGT_ARCH', '').lower() != 'x64' or not os.path.isfile(os.path.join(vcbin, 'link.exe')) or not os.path.isdir(os.path.join(sdk, 'bin', '10.0.26100.0', 'x64')):
    raise ValueError('Pinned x64 toolchain is incomplete')
  return env"""

def patch_qwe_source(path: Path) -> str:
    """Apply the pinned build-script patch only to the verified vendor input."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest == QWE_ORIGINAL_SHA256:
        text = raw.decode('utf-8')
        if text.count(QWE_OLD) != 1:
            raise RuntimeError('Unexpected Qt build-script structure')
        patched = text.replace(QWE_OLD, QWE_NEW).encode('utf-8')
        if hashlib.sha256(patched).hexdigest() != QWE_PATCHED_SHA256:
            raise RuntimeError('Qt build-script patched hash mismatch')
        path.write_bytes(patched)
        return QWE_PATCHED_SHA256
    if digest == QWE_PATCHED_SHA256:
        return digest
    raise RuntimeError('Qt build-script hash is not an approved version')

QWE_PATCHED_SHA256 = "3c09033b2e9ffe7aa9c0a1c45fa43cad7ce5b21a6a340ec11282f96c7f2963cb"


def validate_qwe_block(path: Path, vs: Path, sdk: Path) -> None:
    """Reject a GN-generated environment unless its actual compiler inputs match."""
    raw = path.read_bytes().decode("utf-8")
    env = {}
    for item in raw.split("\0"):
        if "=" in item:
            key, value = item.split("=", 1)
            key = key.upper()
            if key in env:
                raise RuntimeError("Duplicate GN environment key")
            env[key] = value
    vs, sdk = vs.resolve(), sdk.resolve()
    vc = vs / "VC/Tools/MSVC" / VC_VERSION
    ucrt, um = sdk_library_paths(sdk)
    normalize = lambda p: os.path.normcase(os.path.normpath(str(p)))
    expected_include = [vc / "include", *(sdk / "Include" / SDK_VERSION / part for part in ("ucrt", "shared", "um", "winrt", "cppwinrt"))]
    for key, expected in (("INCLUDE", expected_include), ("LIB", [vc / "lib/x64", ucrt, um]), ("LIBPATH", [vc / "lib/x64"])):
        if [normalize(p) for p in env.get(key, "").split(";")] != [normalize(p) for p in expected]:
            raise RuntimeError(f"GN environment has unexpected {key}")
    expected_bin = normalize(vc / "bin/Hostx64/x64")
    paths = env.get("PATH", "").split(";")
    if len(paths) < 2 or normalize(paths[1]) != normalize(sdk / "bin" / SDK_VERSION / "x64"):
        raise RuntimeError("GN environment has unexpected SDK binary path")
    if any("windows kits" in normalize(p) or normalize(os.path.join("vc", "tools", "msvc")) in normalize(p) for p in paths[2:]):
        raise RuntimeError("GN environment contains an unpinned tool path")
    if normalize(paths[0]) != expected_bin or env.get("VCTOOLSVERSION", "").rstrip("\\") != VC_VERSION or env.get("VSCMD_ARG_TGT_ARCH") != "x64":
        raise RuntimeError("GN environment has unexpected compiler or architecture")
    if normalize(env.get("WINDOWSSDKDIR", "")) != normalize(sdk) or env.get("WINDOWSSDKVERSION", "").rstrip("\\/") != SDK_VERSION:
        raise RuntimeError("GN environment has unexpected SDK")


PYSIDE_PACKAGER_ORIGINAL_SHA256 = "eb13f479dbc209963321fd02d48dd3986e1f7bbfc17237de20a380a220be8ae9"
PYSIDE_DESIGNER_OLD = "        if not is_pypy:\n            copydir(\"{install_dir}/plugins/designer\","
PYSIDE_DESIGNER_NEW = r"""        subset = OPTION['MODULE_SUBSET']
        modules = [part.strip().casefold().removeprefix('qt') for part in subset.split(',')] if isinstance(subset, str) else []
        explicit_subset_without_designer = bool(modules) and all(modules) and 'designer' not in modules
        designer_plugin = Path(_vars['install_dir']) / 'plugins' / 'designer'
        if not is_pypy and (designer_plugin.is_dir() or not explicit_subset_without_designer):
            copydir("{install_dir}/plugins/designer","""
PYSIDE_PACKAGER_PATCHED_SHA256 = "18f3387d31d41b1c6dbb18cf8cfd74b0c10676c93bf5b76464675b687b71225b"

def patch_pyside_packager(path: Path) -> str:
    """Skip only a missing Designer plugin excluded by an explicit module subset."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest == PYSIDE_PACKAGER_PATCHED_SHA256:
        return digest
    if digest != PYSIDE_PACKAGER_ORIGINAL_SHA256:
        raise RuntimeError("PySide Windows packager hash is not approved")
    text = raw.decode("utf-8")
    if text.count(PYSIDE_DESIGNER_OLD) != 1:
        raise RuntimeError("Unexpected PySide packager structure")
    patched = text.replace(PYSIDE_DESIGNER_OLD, PYSIDE_DESIGNER_NEW).encode("utf-8")
    if hashlib.sha256(patched).hexdigest() != PYSIDE_PACKAGER_PATCHED_SHA256:
        raise RuntimeError("PySide packager patched hash mismatch")
    path.write_bytes(patched)
    return PYSIDE_PACKAGER_PATCHED_SHA256


def check_existing_cache(cache: Path, expected: dict[str, str]) -> None:
    """Reject a configured tree whose compiler, linker or SDK differs."""
    if not cache.exists():
        return
    entries = {}
    for line in cache.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        entries[key.split(":", 1)[0]] = value
    for key, value in expected.items():
        actual = entries.get(key)
        if actual is None:
            raise RuntimeError(f"Existing CMake cache is missing {key}")
        if key == "CMAKE_SYSTEM_VERSION":
            matches = actual == value
        else:
            matches = os.path.normcase(os.path.normpath(actual)) == os.path.normcase(os.path.normpath(value))
        if not matches:
            raise RuntimeError(f"Existing CMake cache has an unexpected {key}")


def stage_command(args: argparse.Namespace) -> list[str]:
    cmake, build, prefix = str(args.cmake), args.build, args.prefix
    vc_bin = args.vs.resolve() / "VC" / "Tools" / "MSVC" / VC_VERSION / "bin" / "Hostx64" / "x64"
    compiler, linker = str(vc_bin / "cl.exe"), str(vc_bin / "link.exe")
    if args.stage != "build-pyside":
        cache = build / args.stage.split("-")[1] / "CMakeCache.txt"
        if args.stage not in {"configure-qt", "configure-pdf"} and not cache.is_file():
            raise RuntimeError("Build or install requires a verified CMake cache")
        check_existing_cache(cache, {"CMAKE_C_COMPILER": compiler, "CMAKE_CXX_COMPILER": compiler,
                                     "CMAKE_LINKER": linker, "CMAKE_SYSTEM_VERSION": SDK_VERSION})
    common = [f"-DCMAKE_C_COMPILER={compiler}", f"-DCMAKE_CXX_COMPILER={compiler}",
              f"-DCMAKE_LINKER={linker}", f"-DCMAKE_MAKE_PROGRAM={args.ninja}", f"-DCMAKE_INSTALL_PREFIX={prefix}",
              f"-DPython3_EXECUTABLE={args.python}", f"-DCMAKE_C_FLAGS={WINDOWS_FLAGS}",
              f"-DCMAKE_CXX_FLAGS={WINDOWS_FLAGS}", f"-DCMAKE_SYSTEM_VERSION={SDK_VERSION}"]
    if args.stage == "configure-qt":
        return [cmake, "-S", str(args.qt), "-B", str(build / "qt"), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", f"-DQT_BUILD_SUBMODULES={MODULES}",
            "-DBUILD_qtmultimedia=OFF", "-DBUILD_qtquick3d=OFF", "-DBUILD_qtquicktimeline=OFF",
            "-DQT_BUILD_TESTS=OFF", "-DQT_BUILD_EXAMPLES=OFF", *common]
    if args.stage in {"build-qt", "build-pdf"}:
        return [cmake, "--build", str(build / args.stage.split("-")[1]), "--parallel", "2"]
    if args.stage in {"install-qt", "install-pdf"}:
        return [cmake, "--install", str(build / args.stage.split("-")[1])]
    if args.stage == "configure-pdf":
        return [cmake, "-S", str(args.pdf), "-B", str(build / "pdf"), "-G", "Ninja",
            f"-DCMAKE_PREFIX_PATH={prefix}", "-DCMAKE_BUILD_TYPE=Release",
            "-DFEATURE_qtwebengine_build=OFF", "-DFEATURE_qtpdf_build=ON", *common]
    if (args.pyside / "build").exists():
        raise RuntimeError("PySide build directory already exists; use a fresh verified source tree")
    return [str(args.python), "setup.py", "build", "--build-type=all", "--ignore-git",
        "--skip-docs", "--limited-api=yes", "--parallel=2", "--shorter-paths",
        f"--qtpaths={prefix / 'bin' / 'qtpaths6.exe'}", f"--cmake={cmake}",
        "--module-subset=Core,Gui,Network,Svg,Widgets,Test,Pdf,PdfWidgets"]


PATH_ARGUMENTS = ("vs", "sdk", "cmake", "ninja", "python", "perl", "clang", "qt", "pdf", "pyside", "build", "prefix")


def normalize_paths(args: argparse.Namespace) -> None:
    """Make paths absolute once, without resolving Windows SUBST aliases."""
    for name in PATH_ARGUMENTS:
        setattr(args, name, Path(os.path.abspath(os.fspath(getattr(args, name)))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("configure-qt", "build-qt", "install-qt", "configure-pdf", "build-pdf", "install-pdf", "build-pyside"))
    for name in PATH_ARGUMENTS:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    normalize_paths(args)
    env = environment(args)
    if args.stage in {"configure-pdf", "build-pdf"}:
        env["QWE_PINNED_SDK"] = str(args.sdk.resolve())
        patch_qwe_source(args.pdf / "src/3rdparty/chromium/build/toolchain/win/qwe_setup_toolchain.py")
    args.build.mkdir(parents=True, exist_ok=True)
    temp = args.build / "tmp"
    temp.mkdir(exist_ok=True)
    env.update({"TEMP": str(temp), "TMP": str(temp)})
    if args.stage == "build-pdf":
        block = args.build / "pdf/src/pdf/Release/AMD64/environment.x64"
        if block.exists():
            validate_qwe_block(block, args.vs, args.sdk)
    if args.stage == "build-pyside":
        patch_pyside_packager(args.pyside / "build_scripts/platforms/windows_desktop.py")
    cwd = args.pyside if args.stage == "build-pyside" else args.build
    result = subprocess.run(stage_command(args), cwd=cwd, env=env, check=False).returncode
    if args.stage == "build-pdf" and result == 0:
        validate_qwe_block(block, args.vs, args.sdk)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
