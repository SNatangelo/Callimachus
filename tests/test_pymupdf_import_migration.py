# tests/test_pymupdf_import_migration.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import re


def test_core_uses_canonical_pymupdf_import():
    offenders = []
    for path in Path("core").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*import\s+fitz(?:\s|$)", line):
                offenders.append(f"{path}:{number}")
    assert offenders == []


def test_pymupdf_alias_exposes_legacy_api_surface():
    import pymupdf as fitz
    assert callable(fitz.open)
    assert hasattr(fitz, "LINK_NAMED")
