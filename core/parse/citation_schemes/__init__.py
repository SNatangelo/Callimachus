#!/usr/bin/env python3
# core/parse/citation_schemes/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Registry scaffold for citation-scheme modules."""

from __future__ import annotations

import importlib
import pkgutil

try:
    from core.parse import parser_config
except ImportError:
    import parser_config


_REGISTRY_CACHE: dict[str, object] | None = None
NUMERIC_NAME = "numeric"
AUTHOR_YEAR_NAME = "author-year"
INLINE_DOI_NAME = "inline-doi"
MLA_NAME = "mla"


def _discover_modules() -> dict[str, object]:
    modules = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        scheme_name = getattr(module, "NAME", name)
        modules[str(scheme_name)] = module
    return modules


def get_registry() -> dict[str, object]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        config = parser_config.load()
        enabled = config.get("schemes") if isinstance(config, dict) else {}
        discovered = _discover_modules()
        ordered_names = parser_config.merge_order(
            list(discovered),
            config.get("scheme_order") if isinstance(config, dict) else None,
            label="citation scheme",
        )
        _REGISTRY_CACHE = {
            name: discovered[name]
            for name in ordered_names
            if name in discovered
            and (
                not isinstance(enabled, dict)
                or bool((enabled.get(name) or {}).get("enabled", True))
            )
        }
    return dict(_REGISTRY_CACHE)


def get(name: str):
    return get_registry().get(name)


def require(name: str):
    module = get(name)
    if module is None:
        raise RuntimeError(f"{name} citation scheme module is not available")
    return module


def require_numeric():
    return require(NUMERIC_NAME)


def require_author_year():
    return require(AUTHOR_YEAR_NAME)


def require_inline_doi():
    return require(INLINE_DOI_NAME)


def all_schemes() -> tuple[object, ...]:
    registry = get_registry()
    return tuple(registry.values())


def _prefer_by_config(registry: dict[str, object], names: tuple[str, ...]):
    for name in registry:
        if name in names:
            return registry[name]
    raise ValueError(f"none of the configured citation schemes matched {names!r}")


def mode_names(*, include_auto: bool = False) -> tuple[str, ...]:
    names = tuple(get_registry())
    if include_auto:
        return ("auto",) + names
    return names


def synthesizes_references(mode_or_module) -> bool:
    module = mode_or_module
    if isinstance(mode_or_module, str):
        module = get(mode_or_module)
    return callable(getattr(module, "synthesize_references", None))


def _detect_score(module, body: str, sentences: list[str], references: list[dict]) -> float:
    return float(module.detect(body, sentences, references))


def select(body: str, sentences: list[str], references: list[dict], *, mode: str):
    registry = get_registry()
    if mode != "auto":
        module = registry.get(mode)
        if module is None:
            raise ValueError(f"unknown citation scheme: {mode}")
        return module
    if not registry:
        raise ValueError("no citation schemes registered")
    numeric = registry.get(NUMERIC_NAME)
    if numeric is None:
        raise ValueError("numeric citation scheme is required for auto selection")
    author_year = registry.get(AUTHOR_YEAR_NAME)
    inline_doi = registry.get(INLINE_DOI_NAME)
    mla = registry.get(MLA_NAME)
    n_numeric = _detect_score(numeric, body, sentences, references)
    n_ay = _detect_score(author_year, body, sentences, references) if author_year else float("-inf")
    n_doi = _detect_score(inline_doi, body, sentences, references) if inline_doi else float("-inf")
    # MLA is the most specific signal — an in-text author-page citation whose surname the
    # Works Cited carries, and whose locator is a page, not a year.  A numeric or
    # author-year manuscript scores 0 here (no author, or a year the page guard rejects),
    # so this only wins where the manuscript really is MLA.
    n_mla = _detect_score(mla, body, sentences, references) if mla else float("-inf")
    if mla is not None and n_mla > 0 and n_mla >= max(n_numeric, n_ay, n_doi):
        return mla
    if max(n_numeric, n_ay, n_doi) <= 0:
        return numeric
    if (
        author_year is not None
        and inline_doi is not None
        and n_ay == n_doi
        and n_ay > n_numeric
    ):
        return _prefer_by_config(registry, (AUTHOR_YEAR_NAME, INLINE_DOI_NAME))
    if inline_doi is not None and n_doi >= n_numeric and n_doi > n_ay:
        return inline_doi
    if author_year is not None and n_ay > n_numeric:
        return author_year
    return numeric


def reset_for_tests() -> None:
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None
