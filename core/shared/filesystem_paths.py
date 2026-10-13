# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Windows extended paths for filesystem I/O, with logical-path projection."""

from __future__ import annotations

import ntpath
import os
from pathlib import Path


def _windows_extended_path(path: str) -> str:
    """Return an absolute Windows path using the extended-length namespace."""
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return path

    normalized = ntpath.normpath(path)
    drive, tail = ntpath.splitdrive(normalized)
    if normalized.startswith("\\\\") or (drive and tail.startswith("\\")):
        absolute = normalized
    elif drive:
        # Preserve Windows drive-relative semantics for unusual inputs.
        absolute = ntpath.normpath(ntpath.abspath(normalized))
    else:
        current = _without_windows_extended_prefix(os.getcwd())
        if tail.startswith("\\"):
            absolute = ntpath.splitdrive(current)[0] + normalized
        else:
            absolute = ntpath.normpath(ntpath.join(current, normalized))
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    drive, tail = ntpath.splitdrive(absolute)
    if drive and tail.startswith("\\"):
        return "\\\\?\\" + absolute
    return absolute


def _without_windows_extended_prefix(path: str) -> str:
    """Return the ordinary drive or UNC spelling of an extended path."""
    if path[:8].casefold() == "\\\\?\\unc\\".casefold():
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def io_path(path: str | os.PathLike[str]) -> str:
    """Prepare a path for filesystem I/O without changing non-Windows paths."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError("filesystem paths must be text paths")
    if os.name != "nt":
        return value
    return _windows_extended_path(value)


def logical_path(
    path: str | os.PathLike[str], *, strict: bool = False, resolve: bool = True,
) -> Path:
    """Return a canonical unprefixed path for containment and provenance checks.

    Windows resolution runs through the extended namespace so long components
    remain addressable. The result is normalized back to the ordinary drive or
    UNC spelling used by stored paths and audit data.
    """
    if os.name != "nt":
        candidate = Path(path)
        return (
            candidate.resolve(strict=strict)
            if resolve else Path(os.path.abspath(candidate))
        )

    candidate = Path(io_path(path))
    if resolve:
        candidate = candidate.resolve(strict=strict)
    return Path(_without_windows_extended_prefix(str(candidate)))
