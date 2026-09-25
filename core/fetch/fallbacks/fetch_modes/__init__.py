#!/usr/bin/env python3
# core/fetch/fallbacks/fetch_modes/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Optional fetch fallback modes with auto-discovery and graceful fallback."""

from __future__ import annotations

import importlib
import os
import pkgutil

ENV_CHALLENGE_MODE = "CITATION_VERIFIER_FETCH_CHALLENGE_MODE"
DEFAULT_CHALLENGE_MODE = "off"


def discover_module_names() -> list[str]:
    return sorted(
        name
        for _finder, name, _ispkg in pkgutil.iter_modules(__path__)
        if not name.startswith("_")
    )


def load_module(module_name: str):
    try:
        return importlib.import_module(f"{__name__}.{module_name}")
    except Exception:
        return None


def _mode_name(module) -> str:
    return getattr(module, "MODE_NAME", getattr(module, "NAME", module.__name__.rsplit(".", 1)[-1]))


def _aliases(module) -> tuple[str, ...]:
    aliases = getattr(module, "ALIASES", ())
    if isinstance(aliases, str):
        aliases = (aliases,)
    return tuple(a for a in aliases if a)


def mode_choices() -> tuple[str, ...]:
    names = ["off"]
    seen = {"off"}
    for module_name in discover_module_names():
        mod = load_module(module_name)
        if mod is None:
            continue
        for value in (_mode_name(mod),) + _aliases(mod):
            if value not in seen:
                seen.add(value)
                names.append(value)
    return tuple(names)


def configured_mode_name(environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    value = (env.get(ENV_CHALLENGE_MODE) or DEFAULT_CHALLENGE_MODE).strip().lower()
    return value if value in mode_choices() else DEFAULT_CHALLENGE_MODE


def selected_module(environ: dict[str, str] | None = None):
    selected = configured_mode_name(environ)
    if selected == DEFAULT_CHALLENGE_MODE:
        return None
    for module_name in discover_module_names():
        mod = load_module(module_name)
        if mod is None:
            continue
        names = {_mode_name(mod), *_aliases(mod)}
        if selected in names:
            return mod
    return None


def status_rows(environ: dict[str, str] | None = None) -> list[dict]:
    env = environ or os.environ
    selected = configured_mode_name(env)
    rows = [{
        "name": "off",
        "selected": selected == "off",
        "loadable": True,
    }]
    for module_name in discover_module_names():
        mod = load_module(module_name)
        if mod is None:
            rows.append({"name": module_name, "selected": False, "loadable": False})
            continue
        name = _mode_name(mod)
        rows.append({
            "name": name,
            "selected": selected == name or selected in _aliases(mod),
            "loadable": True,
        })
    return rows
