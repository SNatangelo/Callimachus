# core/gui/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional desktop surfaces for operator-guided workflows.

Importing this package never imports a GUI toolkit.  The deterministic core is
therefore usable on headless installations without optional GUI dependencies.
"""

__all__ = ["GuidedFetchViewModel"]


def __getattr__(name: str):
    """Load optional desktop models only when their public export is used."""
    if name == "GuidedFetchViewModel":
        from .guided_fetch_viewmodel import GuidedFetchViewModel

        globals()[name] = GuidedFetchViewModel
        return GuidedFetchViewModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
