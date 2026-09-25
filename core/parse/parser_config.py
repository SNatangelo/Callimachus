#!/usr/bin/env python3
# core/parse/parser_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared parser configuration loader."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import warnings


DEFAULT_EXTRACTOR_ORDER = [
    ".docx",
    ".htm",
    ".html",
    ".markdown",
    ".md",
    ".pdf",
    ".tex",
    ".txt",
    ".xhtml",
]

DEFAULT_EXTRACTORS = {
    "docx": {"enabled": True},
    "html": {"enabled": True},
    "markdown": {"enabled": True},
    "pdf": {"enabled": True},
    "tex": {"enabled": True},
    "txt": {"enabled": True},
}

DEFAULT_SCHEME_ORDER = [
    "author-year",
    "inline-doi",
    "numeric",
]

DEFAULT_SCHEMES = {
    "author-year": {"enabled": True},
    "inline-doi": {"enabled": True},
    "numeric": {"enabled": True},
}

_TOP_LEVEL_KEYS = {
    "extractor_order",
    "extractors",
    "scheme_order",
    "schemes",
}

_FILE_CACHE: dict[str, object] | None = None
_HERE = Path(__file__).resolve().parent
_CONFIG_PATH = _HERE / "parsers.json"


def _warn(message: str) -> None:
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def _normalize_name_list(raw, *, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warn(f"{label} must be a list; ignoring invalid value")
        return []
    out: list[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            _warn(f"{label} contains a non-string item; skipping it")
            continue
        value = item.strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def merge_order(
    available: list[str],
    configured: list[str] | None,
    *,
    label: str,
    warn_unknown: bool = True,
) -> list[str]:
    available_list = [str(item).strip().lower() for item in available if str(item).strip()]
    known = set(available_list)
    ordered: list[str] = []
    seen = set()
    for name in configured or []:
        normalized = str(name).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in known:
            ordered.append(normalized)
        elif warn_unknown:
            _warn(f"Unknown {label} '{normalized}' in configured order; ignoring it")
    for name in sorted(known):
        if name not in seen:
            ordered.append(name)
    return ordered


def _defaults() -> dict[str, object]:
    return {
        "extractor_order": list(DEFAULT_EXTRACTOR_ORDER),
        "extractors": deepcopy(DEFAULT_EXTRACTORS),
        "scheme_order": list(DEFAULT_SCHEME_ORDER),
        "schemes": deepcopy(DEFAULT_SCHEMES),
    }


def _read_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"Could not read parser config '{path}': {exc}")
        return {}
    if not isinstance(raw, dict):
        _warn(f"Parser config '{path}' must be a JSON object; ignoring it")
        return {}
    return raw


def _merge_file_data(raw: dict[str, object]) -> dict[str, object]:
    merged = _defaults()
    for key in raw.keys():
        if key not in _TOP_LEVEL_KEYS:
            _warn(f"Unknown parser config key '{key}'; ignoring it")

    extractor_order = _normalize_name_list(raw.get("extractor_order"), label="extractor_order")
    if extractor_order:
        merged["extractor_order"] = extractor_order

    scheme_order = _normalize_name_list(raw.get("scheme_order"), label="scheme_order")
    if scheme_order:
        merged["scheme_order"] = scheme_order

    extractors = raw.get("extractors")
    if isinstance(extractors, dict):
        merged_extractors = merged["extractors"]
        assert isinstance(merged_extractors, dict)
        for name, config in extractors.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                _warn("extractors entries must be extractor-name -> object; skipping invalid entry")
                continue
            normalized = name.strip().lower()
            if not normalized:
                continue
            base = merged_extractors.setdefault(normalized, {})
            assert isinstance(base, dict)
            base.update(deepcopy(config))
    elif extractors is not None:
        _warn("extractors must be an object; ignoring invalid value")

    schemes = raw.get("schemes")
    if isinstance(schemes, dict):
        merged_schemes = merged["schemes"]
        assert isinstance(merged_schemes, dict)
        for name, config in schemes.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                _warn("schemes entries must be scheme-name -> object; skipping invalid entry")
                continue
            normalized = name.strip().lower()
            if not normalized:
                continue
            base = merged_schemes.setdefault(normalized, {})
            assert isinstance(base, dict)
            base.update(deepcopy(config))
    elif schemes is not None:
        _warn("schemes must be an object; ignoring invalid value")

    return merged


def load(path: str | Path | None = None) -> dict[str, object]:
    global _FILE_CACHE
    target = Path(path) if path is not None else _CONFIG_PATH
    if path is None and _FILE_CACHE is not None:
        return deepcopy(_FILE_CACHE)
    merged = _merge_file_data(_read_file(target))
    if path is None:
        _FILE_CACHE = deepcopy(merged)
    return merged


def reset_for_tests() -> None:
    global _FILE_CACHE
    _FILE_CACHE = None
