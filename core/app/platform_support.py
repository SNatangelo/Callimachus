# core/app/platform_support.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Detect the minimum operating-system versions for packaged desktop builds."""
from __future__ import annotations

import ctypes
import os
import platform
import re


WINDOWS_MINIMUM = (10, 0, 17763)
MACOS_MINIMUM = (14, 4)
GLIBC_MINIMUM = (2, 35)

_GLIBC_VERSION = re.compile(r"glibc ([0-9]+)\.([0-9]+)(?:\.[0-9]+)?\Z")
_NUMERIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")


class _RtlOsVersionInfoExW(ctypes.Structure):
    _fields_ = [
        ("dwOSVersionInfoSize", ctypes.c_ulong),
        ("dwMajorVersion", ctypes.c_ulong),
        ("dwMinorVersion", ctypes.c_ulong),
        ("dwBuildNumber", ctypes.c_ulong),
        ("dwPlatformId", ctypes.c_ulong),
        ("szCSDVersion", ctypes.c_wchar * 128),
        ("wServicePackMajor", ctypes.c_ushort),
        ("wServicePackMinor", ctypes.c_ushort),
        ("wSuiteMask", ctypes.c_ushort),
        ("wProductType", ctypes.c_ubyte),
        ("wReserved", ctypes.c_ubyte),
    ]


def windows_version_supported(version: tuple[int, int, int] | None) -> bool:
    """Return whether a real Windows version/build meets the desktop floor."""
    return version is not None and version >= WINDOWS_MINIMUM


def macos_version_supported(version: str | None) -> bool:
    """Return whether a numeric macOS product version meets the desktop floor."""
    if not version or not _NUMERIC_VERSION.fullmatch(version):
        return False
    parts = tuple(int(part) for part in version.split("."))
    return parts[:2] >= MACOS_MINIMUM


def glibc_version_tuple(version: str | None) -> tuple[int, int] | None:
    """Parse only the GNU libc version reported by ``CS_GNU_LIBC_VERSION``."""
    if not version:
        return None
    match = _GLIBC_VERSION.fullmatch(version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _windows_has_package_identity() -> bool:
    """Identify MSIX processes whose manifest already enforces the OS floor."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_package_name = kernel32.GetCurrentPackageFullName
        get_package_name.argtypes = [
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_wchar),
        ]
        get_package_name.restype = ctypes.c_long
        required_length = ctypes.c_uint32(0)
        insufficient_buffer = 122
        if get_package_name(ctypes.byref(required_length), None) != insufficient_buffer:
            return False
        if required_length.value < 2:
            return False
        package_name = ctypes.create_unicode_buffer(required_length.value)
        return (
            get_package_name(ctypes.byref(required_length), package_name) == 0
            and bool(package_name.value)
        )
    except Exception:
        return False


def _windows_version() -> tuple[int, int, int] | None:
    """Read the actual Windows build with RtlGetVersion, independent of manifests."""
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        get_version = ntdll.RtlGetVersion
        get_version.argtypes = [ctypes.POINTER(_RtlOsVersionInfoExW)]
        get_version.restype = ctypes.c_long
        info = _RtlOsVersionInfoExW()
        info.dwOSVersionInfoSize = ctypes.sizeof(info)
        if get_version(ctypes.byref(info)) != 0:
            return None
        return info.dwMajorVersion, info.dwMinorVersion, info.dwBuildNumber
    except Exception:
        return None


def _linux_glibc_version() -> str | None:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION")
    except Exception:
        return None


def startup_compatibility_error() -> str | None:
    """Return a user-facing error for an unsupported or unknown desktop OS."""
    system = platform.system()
    if system == "Windows":
        if _windows_has_package_identity():
            return None
        version = _windows_version()
        minimum = "Windows 10 version 1809 (build 17763)"
        if version is None:
            return f"Callimachus requires {minimum} or later; the Windows version could not be determined."
        if not windows_version_supported(version):
            major, minor, build = version
            return (
                f"Callimachus requires {minimum} or later; detected Windows "
                f"{major}.{minor} build {build}."
            )
        return None

    if system == "Darwin":
        try:
            version = platform.mac_ver()[0]
        except Exception:
            version = None
        if not version:
            return "Callimachus requires macOS 14.4 or later; the macOS version could not be determined."
        if not macos_version_supported(version):
            return f"Callimachus requires macOS 14.4 or later; detected macOS {version}."
        return None

    if system == "Linux":
        version = _linux_glibc_version()
        parsed = glibc_version_tuple(version)
        if parsed is None:
            return (
                "Callimachus requires GNU glibc 2.35 or later; "
                "this system's GNU glibc version could not be determined."
            )
        if parsed < GLIBC_MINIMUM:
            return (
                "Callimachus requires GNU glibc 2.35 or later; detected GNU glibc "
                f"{parsed[0]}.{parsed[1]}."
            )
        return None

    return (
        "Callimachus desktop builds require Windows 10 build 17763 or later, "
        "macOS 14.4 or later, or GNU glibc 2.35 or later; this platform could not be identified."
    )
