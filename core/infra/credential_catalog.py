# core/infra/credential_catalog.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Lazy, value-free credential descriptor aggregation."""

from __future__ import annotations

_LOCAL = (
    {
        "provider": "google_books",
        "env_name": "GOOGLE_BOOKS_API_KEY",
        "channels": ("resolve",),
        "label": "Google Books",
    },
    {
        "provider": "ncbi",
        "env_name": "NCBI_API_KEY",
        "channels": ("resolve",),
        "label": "NCBI",
    },
    {
        "provider": "ncbi",
        "env_name": "ENTREZ_API_KEY",
        "channels": ("resolve",),
        "label": "NCBI",
    },
    {
        "provider": "tavily",
        "env_name": "TAVILY_API_KEY",
        "channels": ("search",),
        "label": "Tavily",
    },
    {
        "provider": "mojeek",
        "env_name": "MOJEEK_API_KEY",
        "channels": ("search",),
        "label": "Mojeek",
    },
)


def credential_catalog() -> tuple[dict, ...]:
    """Discover provider declarations only when an inventory is requested."""
    from core.resolve import providers
    rows = [dict(item) for item in _LOCAL]
    for module in providers.get_registry().values():
        rows.extend(providers.credential_descriptors(module))
    result = tuple(
        sorted(rows, key=lambda row: (row["provider"], row["env_name"]))
    )
    if len({(row["provider"], row["env_name"]) for row in result}) != len(
        result
    ):
        raise ValueError("duplicate credential descriptor")
    if len({row["env_name"] for row in result}) != len(result):
        raise ValueError("credential environment name is assigned more than once")
    return result


def capture_credential_inventory(environ) -> tuple[dict[str, object], ...]:
    """Capture only non-blank presence flags; credential values never escape."""
    return tuple(
        {
            "provider": item["provider"],
            "env_name": item["env_name"],
            "present_at_start": bool(
                str(environ.get(item["env_name"], "")).strip()
            ),
        }
        for item in credential_catalog()
    )
