#!/usr/bin/env python3
# core/parse/reference_readers/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Registry for reference-entry readers, discovered by file drop.

This is the other half of `core/parse/citation_schemes/`.  A citation scheme reads
the MARKERS a manuscript puts in its prose — "(Smith 2020)", "[14]", "(Morrison 42)"
— and answers "which reference does this sentence point at".  A reference reader
answers the opposite question: given ONE entry, what work does it name?

They are separate registries because they take different inputs (a whole body of
sentences versus a single entry) and because a manuscript pairs them freely: a
law-review paper carries numeric markers in the text and Bluebook entries in the
footnotes, and neither half implies the other.

## Contract

Every reader module defines:

- `NAME` — the reader's name, recorded on what it reads.
- `read(entry) -> dict | None` — the fields, or None when the entry is not in
  this reader's format.

`read` returning None IS the detection: a reader that cannot recognise an entry
declines it, and the dispatcher tries the next.  There is deliberately no separate
`detect()` — a confidence score nothing consumes would be scaffolding, and the
readers here are built to decline rather than guess.

The returned dict carries at least:

- `reader` — which reader produced this.
- `title` — the work's title.
- `anchor` — what positional evidence identified it. The anchor is the reader's
  main asset: it says WHERE in the entry the title was delimited, which is what a
  bare title string cannot say. A gloss lifted from an explanatory parenthetical
  and a genuine two-word title are the same string; only the anchor tells them
  apart.

## Ordering

`ORDER` (default 100, ties broken by name) fixes the sequence the dispatcher
tries. Readers demanding the most positional evidence go first, so a reader that
matches on strong structure is never pre-empted by one that matches on less.
"""

from __future__ import annotations

import importlib
import pkgutil

_REGISTRY_CACHE: list | None = None


def _discover() -> list:
    modules = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        modules.append(importlib.import_module(f"{__name__}.{info.name}"))
    return sorted(modules, key=lambda m: (getattr(m, "ORDER", 100), getattr(m, "NAME", "")))


def get_registry() -> list:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _discover()
    return list(_REGISTRY_CACHE)


def read(entry: str, readers=None) -> dict | None:
    """The first reader that recognises `entry`, or None if none does.

    `readers` restricts the attempt to the readers a manuscript elected. Passing
    none of them reads nothing: a reader is offered an entry only where the whole
    reference list showed it was written in that reader's format.
    """
    for module in (get_registry() if readers is None else readers):
        record = module.read(entry)
        if record:
            return record
    return None


# A reference list must be this fraction Bluebook before its Bluebook reader is
# believed.  The law-review paper claims 19.9% of its 418 entries and every other
# paper in the corpus claims 0.0% of its 990, so anything inside that gap
# separates them; 5% is placed near the bottom because the true rate is diluted by
# entries that are not works at all — 84 of that paper's notes are back-references
# ("Id. at 96.") which no reader can or should claim.
#
# The floor exists for the case a per-entry guard misses: a reader that misreads a
# stray entry or two in a foreign list cannot, on that evidence, take over the
# list.
_MIN_COVERAGE = 0.05


def elect(entries) -> list:
    """The readers a manuscript's reference list supports, strongest first.

    Election is by aggregate agreement rather than by a separate style heuristic:
    a reader that recognises a real share of the list has already demonstrated the
    format, and asking it directly cannot drift from what it will actually read.

    This is the structural half of keeping a reader off the papers it was not
    written for. The other half is the reader's own refusal to claim an entry it
    does not recognise, and neither replaces the other: election limits exposure,
    refusal makes the reader correct. A reader safe only because of who calls it
    is one new caller away from being wrong.
    """
    candidates = [e for e in entries if e]
    if not candidates:
        return []
    elected = []
    for module in get_registry():
        claimed = sum(1 for entry in candidates if module.read(entry))
        if claimed / len(candidates) >= _MIN_COVERAGE:
            elected.append((claimed, module))
    return [module for _, module in sorted(elected, key=lambda p: -p[0])]
