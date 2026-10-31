# core/app/browser_cache.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow cleanup for disposable Chrome network and render caches."""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

from core.app.runtime_paths import user_data_root


_CACHE_DIRECTORY_NAMES = frozenset({"cache", "code cache", "gpucache"})
_CHROME_PROFILE_NAMES = frozenset({"default", "guest profile", "system profile"})
_CHROME_PROFILE_NUMBER = re.compile(r"profile\s+[0-9]+", re.IGNORECASE)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DEFERRED_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})
if hasattr(errno, "ETXTBSY"):
    _DEFERRED_ERRNOS = _DEFERRED_ERRNOS | {errno.ETXTBSY}


def _result(
    *, cleared: int = 0, deferred: int = 0, errors: Sequence[str] = (),
) -> dict[str, object]:
    safe_errors = [str(error) for error in errors]
    return {
        "ok": not deferred and not safe_errors,
        "cleared": cleared,
        "deferred": deferred,
        "errors": safe_errors,
    }


def _is_link_or_junction(path: Path) -> bool:
    """Treat symbolic links and Windows reparse points as unsafe traversal."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _chrome_profile_name(name: str) -> bool:
    folded = name.casefold()
    return folded in _CHROME_PROFILE_NAMES or bool(_CHROME_PROFILE_NUMBER.fullmatch(name))


def _contains_link_or_junction(directory: Path) -> bool:
    pending = [directory]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_link_or_junction(child):
                    return True
                info = child.lstat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child)
    return False


def _cache_targets(profile: Path, root: Path) -> tuple[list[Path], list[str]]:
    """Find only root-level or standard Chrome-profile cache directories."""
    targets: list[Path] = []
    errors: list[str] = []
    profile_roots = [profile]
    try:
        with os.scandir(profile) as entries:
            for entry in entries:
                if not _chrome_profile_name(entry.name):
                    continue
                child = Path(entry.path)
                try:
                    if _is_link_or_junction(child):
                        errors.append("unsafe_profile_path")
                    elif entry.is_dir(follow_symlinks=False):
                        if _is_within(child, root):
                            profile_roots.append(child)
                        else:
                            errors.append("unsafe_profile_path")
                except OSError:
                    errors.append("profile_scan_failed")
    except OSError:
        return targets, ["profile_scan_failed"]

    for profile_root in profile_roots:
        for name in ("Cache", "Code Cache", "GPUCache"):
            candidate = profile_root / name
            try:
                if _is_link_or_junction(candidate):
                    errors.append("unsafe_cache_path")
                    continue
                if not candidate.is_dir():
                    continue
                if not _is_within(candidate, root):
                    errors.append("unsafe_cache_path")
                    continue
                if _contains_link_or_junction(candidate):
                    errors.append("unsafe_cache_path")
                    continue
                targets.append(candidate)
            except OSError:
                errors.append("cache_scan_failed")
    return targets, errors


def _is_deferred(error: OSError) -> bool:
    if isinstance(error, PermissionError):
        return True
    if error.errno in _DEFERRED_ERRNOS:
        return True
    return getattr(error, "winerror", None) in {5, 32, 33}


def clear_browser_caches(
    profiles_root: Path | None = None,
    exclude_profiles: Sequence[str] = (),
) -> dict[str, object]:
    """Remove known disposable caches beneath Callimachus browser profiles.

    The counts are directories fully removed or deferred. Cookies, web storage,
    databases, run artifacts, and all non-cache profile data are left intact.
    Excluded profile names are immediate children of ``profiles_root``; this
    lets a caller clear an active Chrome profile through CDP instead.
    """
    errors: list[str] = []
    cleared = 0
    deferred = 0
    try:
        requested_root = (
            Path(profiles_root) if profiles_root is not None
            else Path(user_data_root()) / "browser_profiles"
        )
        if _is_link_or_junction(requested_root):
            return _result(errors=["unsafe_profiles_root"])
        if not requested_root.exists():
            return _result()
        if not requested_root.is_dir():
            return _result(errors=["profiles_root_unavailable"])
        root = requested_root.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _result(errors=["profiles_root_unavailable"])

    excluded = {
        str(profile).casefold()
        for profile in ((exclude_profiles,) if isinstance(exclude_profiles, str) else exclude_profiles)
        if isinstance(profile, str) and profile
    }
    try:
        with os.scandir(root) as entries:
            profiles = list(entries)
    except OSError as exc:
        if _is_deferred(exc):
            return _result(deferred=1)
        return _result(errors=["profiles_scan_failed"])

    for entry in profiles:
        if entry.name.casefold() in excluded:
            continue
        profile = Path(entry.path)
        try:
            if _is_link_or_junction(profile):
                errors.append("unsafe_profile_path")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not _is_within(profile, root):
                errors.append("unsafe_profile_path")
                continue
        except OSError:
            errors.append("profile_scan_failed")
            continue

        targets, target_errors = _cache_targets(profile, root)
        errors.extend(target_errors)
        for target in targets:
            try:
                # Recheck the target immediately before deletion. The recursive
                # scan above rejects links inside the cache tree.
                if _is_link_or_junction(target) or not _is_within(target, root):
                    errors.append("unsafe_cache_path")
                    continue
                shutil.rmtree(target)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if _is_deferred(exc):
                    deferred += 1
                else:
                    errors.append("cache_remove_failed")
            else:
                cleared += 1

    return _result(cleared=cleared, deferred=deferred, errors=errors)
