#!/usr/bin/env python3
# core/fetch/http_profiles/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional HTTP request profiles with auto-discovery and graceful fallback."""

from __future__ import annotations

import importlib
import os
import pkgutil

ENV_HTTP_PROFILE = "CITATION_VERIFIER_HTTP_PROFILE"
DEFAULT_PROFILE = "browser_like"


def discover_names() -> list[str]:
    return sorted(
        name
        for _finder, name, _ispkg in pkgutil.iter_modules(__path__)
        if not name.startswith("_")
    )


def load_module(name: str):
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except Exception:
        return None


def configured_name(environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    discovered = discover_names()
    value = (env.get(ENV_HTTP_PROFILE) or DEFAULT_PROFILE).strip().lower()
    if value in discovered:
        return value
    return DEFAULT_PROFILE if DEFAULT_PROFILE in discovered else (discovered[0] if discovered else "")


def active_module(environ: dict[str, str] | None = None):
    name = configured_name(environ)
    mod = load_module(name)
    if mod is not None:
        return mod
    if name != DEFAULT_PROFILE:
        return load_module(DEFAULT_PROFILE)
    return None


def status_rows(environ: dict[str, str] | None = None) -> list[dict]:
    env = environ or os.environ
    selected = configured_name(env)
    rows = []
    for name in discover_names():
        mod = load_module(name)
        rows.append(
            {
                "name": name,
                "selected": name == selected,
                "loadable": mod is not None,
            }
        )
    return rows
