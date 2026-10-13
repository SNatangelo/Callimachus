# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import ntpath
import os
from pathlib import Path

from core.shared.filesystem_paths import (
    _windows_extended_path,
    _without_windows_extended_prefix,
    io_path,
    logical_path,
)


def test_windows_drive_and_unc_paths_use_extended_io_spelling():
    drive_path = r"C:\runs\case\sources\scan.pdf"
    unc_path = r"\\server\share\runs\case\sources\scan.pdf"

    assert _windows_extended_path(drive_path) == r"\\?\C:\runs\case\sources\scan.pdf"
    assert _windows_extended_path(unc_path) == (
        r"\\?\UNC\server\share\runs\case\sources\scan.pdf"
    )
    assert _without_windows_extended_prefix(
        r"\\?\C:\runs\case\scan.pdf"
    ) == r"C:\runs\case\scan.pdf"
    assert _without_windows_extended_prefix(
        r"\\?\UNC\server\share\runs\case\scan.pdf"
    ) == r"\\server\share\runs\case\scan.pdf"


def test_relative_windows_io_path_becomes_absolute_and_logical_path_is_unprefixed():
    if os.name != "nt":
        return

    io_value = io_path(r"relative\sources\scan.pdf")
    logical_value = logical_path(r"relative\sources\scan.pdf", resolve=False)

    assert io_value.startswith("\\\\?\\")
    assert ntpath.isabs(_without_windows_extended_prefix(io_value))
    assert not str(logical_value).startswith("\\\\?\\")
    assert str(logical_value) == _without_windows_extended_prefix(io_value)


def test_non_windows_io_path_preserves_the_supplied_spelling():
    if os.name == "nt":
        return

    path = "relative/../source.pdf"
    assert io_path(path) == path
    assert logical_path(path, resolve=False) == Path(os.path.abspath(path))
