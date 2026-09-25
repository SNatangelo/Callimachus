# core/search/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Pluggable web-search API with deterministic backend fall-through.

``search()`` deliberately distinguishes two outcomes required by callers:

* a list means a backend answered, possibly with no results;
* ``None`` means no configured backend answered at all.

The second outcome is not evidence that nothing exists. Resolve and Verify must
keep it unresolved/retryable rather than silently turning an outage into a
claim about the world. Backend selection lives in :mod:`core.search.router`;
transport and provider-specific parsing live in their own modules.
"""

from .router import backend_order, search

__all__ = ("backend_order", "search")
