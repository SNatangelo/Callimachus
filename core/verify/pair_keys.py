# core/verify/pair_keys.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Content-addressed V3 verification-pair keys."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata


def normalize_raw(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def manuscript_identity(value: object) -> str:
    """Validate the sole manuscript identity accepted by V3 pair keys."""
    normalized = normalize_raw(value)
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("manuscript SHA must be 64 lowercase hexadecimal characters")
    return normalized


def reference_identity(reference: dict) -> str | None:
    raw_entry = normalize_raw(reference.get("raw_entry")).lower()
    if raw_entry:
        return f"entry:{raw_entry}"
    parts = [
        normalize_raw(reference.get(name)).lower()
        for name in ("ay_surname", "ay_year", "ay_suffix", "title")
    ]
    if any(parts):
        return "identity:" + "|".join(parts)
    for field in ("doi", "pmid", "isbn"):
        value = normalize_raw(reference.get(field)).lower()
        if value:
            return f"{field}:{value}"
    return None


def marker_context(
    context: object, marker: object, sentence: object = "", *, left_tokens: int = 10,
    right_tokens: int = 30,
) -> str:
    raw_sentence = unicodedata.normalize(
        "NFC", str(sentence if sentence not in (None, "") else context or "")
    )
    raw_marker = unicodedata.normalize("NFC", str(marker or "")).strip()
    at = raw_sentence.find(raw_marker) if raw_marker else -1
    if at < 0 and ";" in raw_marker:
        for part in raw_marker.strip("()").split(";"):
            part = part.strip().strip("()")
            if part and (at := raw_sentence.find(part)) >= 0:
                raw_marker = part
                break
    if at >= 0:
        left = normalize_raw(raw_sentence[:at]).split()
        right = normalize_raw(raw_sentence[at + len(raw_marker):]).split()
        return " ".join(left[-left_tokens:] + [normalize_raw(raw_marker)] + right[:right_tokens])
    return normalize_raw(raw_sentence)


def stable_pair_digest_v3(
    *, manuscript: str, reference: str, marker: str,
    normalized_claim_sentence: str, normalized_marker_context: str,
) -> str:
    payload = "\x1f".join((
        manuscript, reference, marker, normalized_claim_sentence,
        normalized_marker_context,
    )).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PairKey:
    key: str
    reason: str


def key_for_row(row: dict, manuscript: str | None) -> PairKey:
    """Derive the sole supported pair identity or reject incomplete provenance."""
    manuscript_value = manuscript_identity(manuscript)
    reference = reference_identity(row)
    if not reference:
        raise ValueError("missing stable reference identity")
    marker = normalize_raw(row.get("marker_raw"))
    if not marker:
        raise ValueError("missing raw citation marker")
    sentence = normalize_raw(row.get("sentence"))
    if not sentence:
        raise ValueError("missing canonical claim sentence")
    context = marker_context(row.get("context_window"), marker, sentence)
    if not context:
        raise ValueError("missing canonical marker context")
    key = "S" + stable_pair_digest_v3(
        manuscript=manuscript_value,
        reference=reference,
        marker=marker,
        normalized_claim_sentence=sentence,
        normalized_marker_context=context,
    )[:24]
    return PairKey(
        key,
        "marker_context_sha256=" + hashlib.sha256(context.encode("utf-8")).hexdigest()[:12],
    )


def assign_pair_keys(rows: list[dict], manuscript: str | None) -> list[dict]:
    """Annotate V3 rows and disambiguate repeated exact identities locally."""
    prepared = []
    for row in rows:
        item = dict(row)
        result = key_for_row(item, manuscript)
        item.update(key=result.key, key_reason=result.reason)
        prepared.append(item)
    groups: dict[str, list[dict]] = {}
    for row in prepared:
        groups.setdefault(row["key"], []).append(row)
    for key, group in groups.items():
        if len(group) > 1:
            for occurrence, row in enumerate(group, 1):
                row["key"] = f"{key}@{occurrence}"
                row["key_reason"] = (
                    f"repeated source/marker occurrence {occurrence}/{len(group)}; "
                    + row["key_reason"]
                )
    return prepared
