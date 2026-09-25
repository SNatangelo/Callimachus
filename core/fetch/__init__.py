# core/fetch/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Public entry points for deterministic full-text fetching."""

from .service import (
    classify_html_content,
    fetch_fulltext,
    main,
    new_fetch_run_context,
    process_browser_responses,
)

__all__ = (
    "classify_html_content",
    "fetch_fulltext",
    "main",
    "new_fetch_run_context",
    "process_browser_responses",
)
