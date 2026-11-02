# packaging/windows_gui.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Small PE-header checks for Windows GUI release artifacts."""
from __future__ import annotations

import os
from pathlib import Path


_DOS_HEADER_SIZE = 64
_PE_SIGNATURE_SIZE = 4
_COFF_HEADER_SIZE = 20
_OPTIONAL_HEADER_SUBSYSTEM_OFFSET = 68
_OPTIONAL_HEADER_SUBSYSTEM_SIZE = 2
_MAX_OPTIONAL_HEADER_SIZE = 4096
_IMAGE_SUBSYSTEM_WINDOWS_GUI = 2


def verify_windows_gui_binary(path: Path) -> None:
    """Raise ``ValueError`` unless *path* is a PE32/PE32+ GUI executable.

    Only the DOS, PE/COFF, and bounded optional headers are read. The check is
    intended for release smoke gates; it does not map sections or load the EXE.
    """
    path = Path(path)
    try:
        with path.open("rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            if file_size < _DOS_HEADER_SIZE:
                raise ValueError(f"{path}: truncated DOS header")
            dos_header = source.read(_DOS_HEADER_SIZE)
            if len(dos_header) != _DOS_HEADER_SIZE:
                raise ValueError(f"{path}: truncated DOS header")
            if dos_header[:2] != b"MZ":
                raise ValueError(f"{path}: invalid DOS signature (expected MZ)")

            pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
            if pe_offset < _DOS_HEADER_SIZE:
                raise ValueError(f"{path}: invalid PE header offset {pe_offset}")
            fixed_headers_end = pe_offset + _PE_SIGNATURE_SIZE + _COFF_HEADER_SIZE
            if fixed_headers_end > file_size:
                raise ValueError(f"{path}: truncated PE/COFF headers")

            source.seek(pe_offset)
            if source.read(_PE_SIGNATURE_SIZE) != b"PE\0\0":
                raise ValueError(f"{path}: invalid PE signature")
            coff_header = source.read(_COFF_HEADER_SIZE)
            if len(coff_header) != _COFF_HEADER_SIZE:
                raise ValueError(f"{path}: truncated COFF header")

            optional_header_size = int.from_bytes(coff_header[16:18], "little")
            minimum_optional_size = (
                _OPTIONAL_HEADER_SUBSYSTEM_OFFSET + _OPTIONAL_HEADER_SUBSYSTEM_SIZE
            )
            if not minimum_optional_size <= optional_header_size <= _MAX_OPTIONAL_HEADER_SIZE:
                raise ValueError(
                    f"{path}: invalid optional header size {optional_header_size}"
                )
            optional_header_end = fixed_headers_end + optional_header_size
            if optional_header_end > file_size:
                raise ValueError(f"{path}: truncated optional header")
            optional_header = source.read(optional_header_size)
            if len(optional_header) != optional_header_size:
                raise ValueError(f"{path}: truncated optional header")

    except OSError as exc:
        raise ValueError(f"{path}: cannot read Windows executable headers: {exc}") from exc

    optional_magic = int.from_bytes(optional_header[:2], "little")
    if optional_magic not in (0x10B, 0x20B):
        raise ValueError(
            f"{path}: invalid PE optional header magic 0x{optional_magic:04x}"
        )
    subsystem = int.from_bytes(
        optional_header[
            _OPTIONAL_HEADER_SUBSYSTEM_OFFSET:
            _OPTIONAL_HEADER_SUBSYSTEM_OFFSET + _OPTIONAL_HEADER_SUBSYSTEM_SIZE
        ],
        "little",
    )
    if subsystem != _IMAGE_SUBSYSTEM_WINDOWS_GUI:
        raise ValueError(
            f"{path}: Windows subsystem {subsystem} is not IMAGE_SUBSYSTEM_WINDOWS_GUI (2)"
        )