# core/search/router.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Backend ordering and fail-closed search orchestration."""

from __future__ import annotations

import os

from .backends import html, mojeek, tavily
from . import transport

ENV_BACKENDS = "CITATION_VERIFIER_SEARCH_BACKENDS"
DEFAULT_ORDER = ("tavily", "mojeek", "html")

_BACKENDS = {"tavily": tavily.search, "mojeek": mojeek.search, "html": html.search}


def backend_order() -> list[str]:
    """Return configured known backends in their requested order."""
    raw = (os.environ.get(ENV_BACKENDS) or "").strip()
    names = [name.strip().lower() for name in raw.split(",")] if raw else list(DEFAULT_ORDER)
    return [name for name in names if name in _BACKENDS]


def search(query: str, *, max_results: int = 6, run_dir: str | None = None) -> list[dict] | None:
    results, _provenance = search_with_provenance(
        query, max_results=max_results, run_dir=run_dir,
    )
    return results


def search_with_provenance(
    query: str, *, max_results: int = 6, run_dir: str | None = None,
) -> tuple[list[dict] | None, dict]:
    """Return normal results and non-secret backend provenance.

    An empty list is a real answer and stops the chain. Trying another engine
    after that would widen the search and weaken the meaning of the result.
    """
    if not query or not query.strip():
        return [], {"outcome": "answered", "selected_backend": None, "attempts": []}
    attempts = []
    with transport.capture_observations() as observations:
        for name in backend_order():
            before = len(observations)
            got = (
                _BACKENDS[name](query, max_results, run_dir=run_dir)
                if run_dir else _BACKENDS[name](query, max_results)
            )
            observed = observations[before:]
            latest = observed[-1] if observed else {}
            attempt = {"backend": name, "outcome": "answered" if got is not None else "refused"}
            if latest:
                attempt.update(latest)
            attempts.append(attempt)
            if got is not None:
                return [result for result in got if result.get("url")][:max_results], {
                    "outcome": "answered", "selected_backend": name, "attempts": attempts,
                }
    return None, {
        "outcome": "refused" if attempts else "no_answer",
        "selected_backend": None, "attempts": attempts,
    }
