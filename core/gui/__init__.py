# core/gui/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional desktop surfaces for operator-guided workflows.

Importing this package never imports a GUI toolkit.  The deterministic core is
therefore usable on headless installations without optional GUI dependencies.
"""

from .guided_fetch_viewmodel import GuidedFetchViewModel

__all__ = ["GuidedFetchViewModel"]
