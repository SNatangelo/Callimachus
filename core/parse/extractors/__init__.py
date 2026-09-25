#!/usr/bin/env python3
# core/parse/extractors/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Registry scaffold for manuscript extractors."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

try:
    from core.parse import parser_config
except ImportError:
    import parser_config


_REGISTRY_CACHE: dict[str, object] | None = None
_EXTENSION_CACHE: dict[str, object] | None = None


def _discover_modules() -> dict[str, object]:
    modules = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        modules[name] = module
    return modules


def get_registry() -> dict[str, object]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        config = parser_config.load()
        enabled = config.get("extractors") if isinstance(config, dict) else {}
        discovered = _discover_modules()
        filtered = {
            name: module
            for name, module in discovered.items()
            if not isinstance(enabled, dict)
            or bool((enabled.get(name) or {}).get("enabled", True))
        }
        _REGISTRY_CACHE = dict(sorted(filtered.items()))
    return dict(_REGISTRY_CACHE)


def _extension_registry() -> dict[str, object]:
    global _EXTENSION_CACHE
    if _EXTENSION_CACHE is None:
        extension_map = {}
        enabled_extension_map = {}
        for name, module in _discover_modules().items():
            for ext in getattr(module, "EXTENSIONS", ()):
                normalized = str(ext).lower()
                extension_map[normalized] = module
                if name in get_registry():
                    enabled_extension_map[normalized] = module
        config = parser_config.load()
        ordered = parser_config.merge_order(
            list(extension_map),
            config.get("extractor_order") if isinstance(config, dict) else None,
            label="extractor",
        )
        _EXTENSION_CACHE = {
            ext: enabled_extension_map[ext]
            for ext in ordered
            if ext in enabled_extension_map
        }
    return dict(_EXTENSION_CACHE)


def extract_for(path: str, **kwargs):
    ext = Path(path).suffix.lower()
    module = _extension_registry().get(ext)
    if module is None:
        raise ValueError(f"unsupported manuscript extension: {ext}")
    text, fmt, meta = module.extract(path, **kwargs)
    postprocess = getattr(module, "postprocess", None)
    if callable(postprocess):
        text = postprocess(text, meta)
    return text, fmt, meta


def probe_for(path: str) -> dict[str, str]:
    """Validate file content against the extractor selected by its extension."""
    ext = Path(path).suffix.lower()
    module = _extension_registry().get(ext)
    if module is None:
        raise ValueError(f"unsupported manuscript extension: {ext}")
    try:
        from core.parse import file_probe
    except ImportError:  # pragma: no cover - standalone execution
        import file_probe  # type: ignore
    return file_probe.probe_extractor(path, module)


def supported_extensions() -> tuple[str, ...]:
    return tuple(_extension_registry())


def reset_for_tests() -> None:
    global _REGISTRY_CACHE, _EXTENSION_CACHE
    _REGISTRY_CACHE = None
    _EXTENSION_CACHE = None
