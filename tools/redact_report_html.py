#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone wrapper for public human HTML report redaction."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_IMPLEMENTATION = Path(__file__).resolve().parents[1] / "core" / "report" / "human" / "redaction.py"
_SPEC = importlib.util.spec_from_file_location("callimachus_public_html_redaction", _IMPLEMENTATION)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise ImportError(f"cannot load public HTML redactor at {_IMPLEMENTATION}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
RedactionError = _MODULE.RedactionError
main = _MODULE.main
redact_html = _MODULE.redact_html

__all__ = ["RedactionError", "main", "redact_html"]

if __name__ == "__main__":
    raise SystemExit(main())
