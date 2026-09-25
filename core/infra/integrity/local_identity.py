# core/infra/integrity/local_identity.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""OS-derived identity for local, explicitly unattested answer admission.

Windows has no POSIX UID. Its full process-token SID identifies the principal;
the final SID component (RID) populates the existing numeric provenance column.
A RID is only meaningful with the complete SID in producer_identity. Neither
value is an authority attestation or an access check.
"""
from __future__ import annotations

import os
import re

from .authority import AuthorityError


_SID = re.compile(r"S-1-(?:[0-9]+-)+([0-9]+)\Z", re.ASCII)


def _windows_process_sid() -> str:
    """Read TokenUser from the OS, not USERNAME/USER or a shell command.

    https://learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation
    https://learn.microsoft.com/windows/win32/api/sddl/nf-sddl-convertsidtostringsidw
    """
    import ctypes
    from ctypes import wintypes

    # Restrict DLL loading to System32 (LOAD_LIBRARY_SEARCH_SYSTEM32).
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
    security = ctypes.WinDLL("advapi32.dll", use_last_error=True, winmode=0x800)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    ]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    token = wintypes.HANDLE()
    if not security.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token),  # TOKEN_QUERY
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        ok = security.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if ok or ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError("cannot size Windows TokenUser information")
        if not ctypes.sizeof(TokenUser) <= required.value <= 65536:
            raise OSError("Windows TokenUser size is invalid")
        buffer = ctypes.create_string_buffer(required.value)
        if not security.GetTokenInformation(
            token, 1, buffer, len(buffer), ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(user.user.sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def local_process_identity() -> tuple[int, str | None]:
    """Return numeric OS identity and an explicit non-POSIX principal, if any."""
    if os.name == "nt":
        try:
            sid = _windows_process_sid()
        except (OSError, AttributeError) as exc:
            raise AuthorityError("cannot determine Windows process-token identity") from exc
        match = _SID.fullmatch(sid) if isinstance(sid, str) else None
        if match is None or int(match.group(1)) > 0xFFFFFFFF:
            raise AuthorityError("Windows process-token SID is invalid")
        return int(match.group(1)), f"windows-sid:{sid}"
    getuid = getattr(os, "getuid", None)
    if os.name != "posix" or not callable(getuid):
        raise AuthorityError("local process identity is unsupported on this platform")
    uid = getuid()
    if type(uid) is not int or uid < 0:
        raise AuthorityError("local process UID is invalid")
    return uid, None
