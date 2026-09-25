#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/interactive_fallback_closed.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in interactive fallback that also opens explicitly closed routes.

Use only when the user has legitimate publisher or institutional access.  No
credentials are supplied or access controls bypassed; the shared browser mode
only records what that authenticated session exposes.
"""

from __future__ import annotations

from core.fetch.fallbacks.fetch_modes import interactive_browser
from core.fetch.fallbacks.fetch_modes import interactive_fallback

NAME = "interactive_fallback_closed"
MODE_NAME = "interactive_fallback_closed"
ALIASES = ("interactive_recovery_closed", "browser_fallback_closed")
CLOSED_ACCESS_MODE = True
USES_RESOLVE_MAP = True


def groups(need, refs, fetch_results, *, resolve_map=None):
    """Include ordinary evidenced routes and explicitly closed routes."""
    return interactive_fallback.groups(
        need, refs, fetch_results, resolve_map=resolve_map, include_closed=True
    )


def recover(grouped, run_dir: str) -> dict:
    return interactive_browser.recover(grouped, run_dir)
