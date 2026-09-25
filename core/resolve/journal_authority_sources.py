# core/resolve/journal_authority_sources.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic source parsers for local journal-authority snapshots."""

from __future__ import annotations

import csv
from itertools import chain
from pathlib import Path
import xml.etree.ElementTree as ET

from .journal_authority import normalize_issn


def nlm_records(text: str) -> list[dict]:
    records = []
    for block in text.split("--------------------------------------------------------"):
        values = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
        record_id = values.get("NlmId")
        title = values.get("JournalTitle")
        if not record_id or not title:
            continue
        aliases = [
            value for key in ("MedAbbr", "IsoAbbr")
            if (value := values.get(key))
        ]
        issns = [
            value for key in ("ISSN (Print)", "ISSN (Online)")
            if (value := values.get(key))
        ]
        if issns:
            records.append({
                "record_id": "nlm:" + record_id,
                "canonical_title": title,
                "aliases": aliases,
                "issns": issns,
            })
    return records


def csv_records(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"record_id", "canonical_title", "aliases", "issns"}
        if set(reader.fieldnames or ()) != required:
            raise ValueError(
                "canonical CSV header must be record_id,canonical_title,aliases,issns"
            )
        for row in reader:
            yield {
                "record_id": row["record_id"],
                "canonical_title": row["canonical_title"],
                "aliases": row["aliases"].split("|") if row["aliases"] else [],
                "issns": row["issns"].split("|") if row["issns"] else [],
            }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _datafields(record: ET.Element, tag: str):
    for field in record:
        if _local_name(field.tag) == "datafield" and field.get("tag") == tag:
            yield field


def _subfields(field: ET.Element, codes: tuple[str, ...]) -> list[str]:
    return [
        (child.text or "").strip()
        for child in field
        if (
            _local_name(child.tag) == "subfield"
            and child.get("code") in codes
            and (child.text or "").strip()
        )
    ]


def _titles(record: ET.Element, tag: str) -> list[str]:
    values = []
    for field in _datafields(record, tag):
        parts = _subfields(field, ("a", "b", "n", "p"))
        if parts:
            values.append(" ".join(parts).strip(" /:;,="))
    return values


def _normalized_issns(values: list[str], field_name: str) -> list[str]:
    normalized = [normalize_issn(value) for value in values]
    if any(value is None for value in normalized):
        raise ValueError(f"ISSN MARCXML contains an invalid {field_name}")
    return sorted(set(normalized))


def _marcxml_record(record: ET.Element) -> dict | None:
    primary_issns = _normalized_issns([
        value
        for field in _datafields(record, "022")
        for value in _subfields(field, ("a",))
    ], "ISSN")
    if not primary_issns:
        return None

    linking_issns = _normalized_issns([
        value
        for field in _datafields(record, "023")
        if (field.get("ind1") or " ") == "0"
        for value in _subfields(field, ("a",))
    ] + [
        value
        for field in _datafields(record, "022")
        for value in _subfields(field, ("l",))
    ], "ISSN-L")
    if len(linking_issns) > 1:
        raise ValueError("ISSN MARCXML record contains conflicting ISSN-L values")

    key_titles = _titles(record, "222")
    proper_titles = _titles(record, "245")
    if not key_titles and not proper_titles:
        return None
    aliases = []
    for tag in ("210", "222", "245", "246"):
        aliases.extend(_titles(record, tag))

    grouping_issn = linking_issns[0] if linking_issns else primary_issns[0]
    return {
        "record_id": ("issnl:" if linking_issns else "issn:") + grouping_issn,
        "canonical_title": (key_titles or proper_titles)[0],
        "aliases": aliases,
        "issns": primary_issns,
    }


def marcxml_records(path: Path):
    """Stream ISSN MARCXML records while releasing parsed XML elements."""
    root = None
    try:
        for event, element in ET.iterparse(path, events=("start", "end")):
            if root is None and event == "start":
                root = element
            if event != "end" or _local_name(element.tag) != "record":
                continue
            parsed = _marcxml_record(element)
            if parsed is not None:
                yield parsed
            element.clear()
            if root is not element:
                root.clear()
    except ET.ParseError as exc:
        raise ValueError("ISSN MARCXML input is malformed") from exc


def require_first(records):
    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration as exc:
        raise ValueError(
            "input contains no journal records with a valid identity shape"
        ) from exc
    return chain((first,), iterator)
