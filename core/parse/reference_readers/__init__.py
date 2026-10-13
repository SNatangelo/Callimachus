#!/usr/bin/env python3
# core/parse/reference_readers/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Discoverable structural readers for individual bibliographic entries.

Readers implement ``parse_fields(entry)`` and return field values with spans in
the NFC-normalized source entry. The dispatcher validates each record, merges
compatible readings, and abstains on conflicting field interpretations.
"""

from __future__ import annotations

import importlib
import pkgutil
import unicodedata

try:
    from core.parse import parser_config
except ImportError:
    import parser_config

_REGISTRY_CACHE: list | None = None


def _discover() -> list:
    modules = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if callable(getattr(module, "parse_fields", None)):
            modules.append(module)
    names = [getattr(m, "NAME", None) or getattr(m, "__name__", "unknown").rsplit(".", 1)[-1]
             for m in modules]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError("duplicate reference reader name: " + ", ".join(duplicates))
    config = parser_config.load()
    enabled = config.get("reference_readers", {})
    order = parser_config.merge_order(
        names,
        config.get("reference_reader_order"), label="reference reader",
    )
    by_name = {getattr(m, "NAME", None) or getattr(m, "__name__", "unknown").rsplit(".", 1)[-1]: m
               for m in modules}
    return [by_name[name] for name in order
            if not isinstance(enabled, dict)
            or bool((enabled.get(name) or {}).get("enabled", True))]


def get_registry() -> list:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _discover()
    return list(_REGISTRY_CACHE)


def _generic_text_enabled() -> bool:
    """Whether the shipped generic-text reader is enabled in the registry."""
    return any(
        getattr(module, "NAME", None) == "generic-text"
        for module in get_registry()
    )


def entry_start_readers(line: str) -> tuple[str, ...]:
    """Names of enabled readers that recognize a physical line as an entry start."""
    claimed = []
    for module in get_registry():
        starts_entry = getattr(module, "starts_entry", None)
        if callable(starts_entry) and starts_entry(line):
            claimed.append(getattr(module, "NAME", None) or getattr(
                module, "__name__", "unknown").rsplit(".", 1)[-1])
    return tuple(sorted(set(claimed)))


def _validated_record(entry: str, module, record: dict) -> dict:
    """Validate reader output against the exact entry before it can be trusted."""
    if not isinstance(record, dict):
        raise ValueError(f"reference reader {getattr(module, 'NAME', '?')} returned invalid record")
    module_name = getattr(module, "NAME", None) or getattr(module, "__name__", "unknown").rsplit(".", 1)[-1]
    record.setdefault("reader", module_name)
    record.setdefault("version", getattr(module, "VERSION", "1"))
    if not validate_parse(record, entry):
        raise ValueError(f"reference reader {record.get('reader', '?')} returned invalid record")
    return record


def _parse_structural(entry: str, *, readers=None) -> dict:
    """Parse all safe structural readings; conflicting claims abstain.

    ORDER controls deterministic presentation only. It never resolves a conflict.
    A recognized ambiguous result prevents a weaker generic reader from claiming
    the entry.
    """
    modules = [m for m in (get_registry() if readers is None else list(readers))
               if getattr(m, "STRUCTURAL", True)]
    claims = []
    ambiguities = []
    for module in modules:
        parse = getattr(module, "parse_fields", None)
        if not callable(parse):
            continue
        record = parse(entry)
        if record is None:
            continue
        record = _validated_record(entry, module, record)
        status = record.get("status")
        if status == "ambiguous":
            ambiguities.append(record)
        elif status == "parsed":
            claims.append(record)
        else:
            raise ValueError(f"reference reader returned unsupported status: {status!r}")
    if ambiguities:
        return {"reader": "+".join(r["reader"] for r in ambiguities),
                "version": "1", "status": "ambiguous", "fields": {},
                "conflicts": [c for r in ambiguities for c in r.get("conflicts", [])]}
    if not claims:
        return {"reader": "none", "version": "1", "status": "unrecognized",
                "fields": {}, "conflicts": []}
    merged_fields = {}
    for claim in claims:
        for name, item in claim["fields"].items():
            previous = merged_fields.get(name)
            if previous is None:
                merged_fields[name] = dict(item)
            elif previous["value"] != item["value"]:
                return {"reader": "+".join(sorted(c["reader"] for c in claims)),
                        "version": "1", "status": "ambiguous", "fields": {},
                        "conflicts": [{"field": name, "reason": "conflicting_values",
                                       "readers": sorted(c["reader"] for c in claims)}]}
            else:
                previous_spans = {(span["start"], span["end"]) for span in previous["spans"]}
                next_spans = {(span["start"], span["end"]) for span in item["spans"]}
                if previous_spans != next_spans:
                    return {"reader": "+".join(sorted(c["reader"] for c in claims)),
                            "version": "1", "status": "ambiguous", "fields": {},
                            "conflicts": [{"field": name, "reason": "competing_source_spans",
                                           "readers": sorted(c["reader"] for c in claims)}]}
                previous["spans"] += [span for span in item["spans"] if span not in previous["spans"]]
    classifications = [c["classification"] for c in claims if c.get("classification")]
    if classifications and any(value != classifications[0] for value in classifications[1:]):
        return {"reader": "+".join(sorted(c["reader"] for c in claims)),
                "version": "1", "status": "ambiguous", "fields": {},
                "conflicts": [{"field": "classification", "reason": "conflicting_values"}]}
    result = {"reader": "+".join(sorted(c["reader"] for c in claims)), "version": "1",
              "status": "parsed", "fields": merged_fields,
              "conflicts": [conflict for claim in claims
                            for conflict in claim.get("conflicts", [])]}
    if classifications:
        result["classification"] = classifications[0]
    return result


def parse_entry(entry: str, *, chicago_hanging: bool = False,
                include_generic: bool = True) -> dict:
    """Return the unique safe structural result, or an explicit generic reading."""
    entry = unicodedata.normalize("NFC", entry)
    record = _parse_structural(entry)
    if record["status"] != "unrecognized" or not include_generic:
        return record
    for module in get_registry():
        if not getattr(module, "GENERIC", False):
            continue
        parsed = module.parse_fields(entry, chicago_hanging=chicago_hanging)
        if parsed is not None:
            return _validated_record(entry, module, parsed)
    return record


def validate_parse(record: dict, raw_entry: str) -> bool:
    """Strict public validation for parse payloads before storage or matching."""
    raw_entry = unicodedata.normalize("NFC", raw_entry)
    if not isinstance(record, dict) or record.get("status") not in {"parsed", "ambiguous", "unrecognized"}:
        return False
    if not isinstance(record.get("reader"), str) or not record["reader"]:
        return False
    if not isinstance(record.get("version"), str) or not record["version"]:
        return False
    if not isinstance(record.get("fields"), dict) or not isinstance(record.get("conflicts", []), list):
        return False
    if record["status"] != "parsed":
        return (not record["fields"] and isinstance(record.get("conflicts", []), list)
                and (record["status"] != "ambiguous" or bool(record.get("conflicts"))))
    types = {"authors": list, "title": str, "journal": str, "year": int,
             "volume": str, "issue": str, "pages": str, "publisher": str,
             "place": str, "editors": list, "doi": str, "url": str, "first_author": str}
    try:
        for name, item in record["fields"].items():
            if name not in types or not isinstance(item, dict):
                return False
            value = item.get("value")
            if not isinstance(value, types[name]) or (name == "year" and isinstance(value, bool)):
                return False
            if isinstance(value, str) and not value.strip():
                return False
            if name == "authors" and not all(isinstance(author, str) for author in value):
                return False
            if name == "authors" and (not value or not all(author.strip() for author in value)):
                return False
            if name == "editors" and (not value or not all(isinstance(editor, str) and editor.strip() for editor in value)):
                return False
            rule = item.get("rule")
            spans = item.get("spans")
            if not isinstance(rule, str) or not rule.strip() or not isinstance(spans, list) or not spans:
                return False
            if name in {"authors", "editors"} and len(spans) != len(value):
                return False
            for span in spans:
                if not isinstance(span, dict):
                    return False
                start, end, raw_value = span.get("start"), span.get("end"), span.get("raw_value")
                if (not isinstance(start, int) or isinstance(start, bool)
                        or not isinstance(end, int) or isinstance(end, bool)
                        or not isinstance(raw_value, str)
                        or not 0 <= start < end <= len(raw_entry)
                        or raw_entry[start:end] != raw_value):
                    return False
        if record.get("classification") is not None and not isinstance(record["classification"], dict):
            return False
    except (TypeError, ValueError):
        return False
    return True


def reset_for_tests() -> None:
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None
