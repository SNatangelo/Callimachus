# core/parse/manuscript_identity.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Local, deterministic evidence about the manuscript's own identity.

This module deliberately records only what the uploaded manuscript itself can
show.  It does not resolve titles, use a filename, or inspect bibliography
entries.  A missing local signal remains a typed ``unknown`` result.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"\barXiv\s*:\s*(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.IGNORECASE)
_PMID_RE = re.compile(r"\bPMID\s*:\s*(\d+)\b", re.IGNORECASE)
_INVALID_TITLES = frozenset({
    "untitled", "untitled document", "document", "unknown", "none",
    "no title", "null", "microsoft word", "microsoft powerpoint", "new document",
})
_LEGAL_NOTICE_RE = re.compile(
    r"\b(?:all rights reserved|copyright|©|creative commons|licensed under|"
    r"permission to (?:make|reproduce)|provided proper attribution|no part of this)\b",
    re.IGNORECASE,
)


def _empty_evidence() -> dict[str, Any]:
    return {
        "title": None,
        "title_status": "unknown",
        "title_method": "none",
        "metadata_title": None,
        "layout_title": None,
        "identifiers": [],
    }


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    value = " ".join(value.split())
    return value or None


def _is_title_like(value: str | None) -> bool:
    if not value or not 4 <= len(value) <= 300:
        return False
    folded = value.casefold().strip(" .:-_")
    canonical = " ".join(re.findall(r"[a-z0-9]+", folded))
    letters = sum(char.isalpha() for char in value)
    if canonical in _INVALID_TITLES or letters < 4 or letters / len(value) < 0.45:
        return False
    noisy = sum(
        unicodedata.category(char)[0] in {"C", "P", "S"}
        for char in value
    )
    if noisy / len(value) > 0.18:
        return False
    if "//" in value or _DOI_RE.search(value) or _LEGAL_NOTICE_RE.search(value):
        return False
    return True


def _title_key(value: str) -> str:
    """A conservative local comparison key for layout/metadata repair only."""
    return "".join(
        character for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def bounded_front_matter(text: str) -> str:
    """Return only the manuscript-owned portion before its first major section."""
    # Some extractors merge the heading and its first sentence.  Keep the
    # manuscript-identity scan bounded even when no standalone section heading
    # survives extraction, so identifiers in the bibliography can never be
    # adopted as identifiers of the uploaded paper.
    inline_boundary = re.search(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)]?\s+)?"
        r"(?:abstract|references|bibliography)\b",
        text,
    )
    end = inline_boundary.start() if inline_boundary else len(text)
    return text[:min(end, 12000)]


def _front_matter_identifiers(text: str) -> list[dict[str, str]]:
    """Extract declared identifiers without ever treating bibliography rows as own IDs."""
    front_matter = bounded_front_matter(text)
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        normalised = value.strip().rstrip(".,;)").lower()
        key = (kind, normalised)
        if normalised and key not in seen:
            seen.add(key)
            found.append({"scheme": kind, "value": normalised,
                          "source": "front_matter_text"})

    for match in _DOI_RE.finditer(front_matter):
        add("doi", match.group(0))
    for match in _ARXIV_RE.finditer(front_matter):
        add("arxiv_id", match.group(1))
    for match in _PMID_RE.finditer(front_matter):
        add("pmid", match.group(1))
    return found


def _pdf_metadata_title(path: str) -> str | None:
    try:
        import pymupdf as fitz
        document = fitz.open(path)
    except Exception:
        return None
    try:
        return _clean_text((document.metadata or {}).get("title"))
    except Exception:
        return None
    finally:
        document.close()


def _horizontal_line_text(line: dict) -> tuple[str, float, float] | None:
    direction = line.get("dir") or (1.0, 0.0)
    if not isinstance(direction, (tuple, list)) or len(direction) < 2:
        return None
    if abs(float(direction[0])) < 0.98 or abs(float(direction[1])) > 0.15:
        return None
    spans = line.get("spans") or []
    pieces: list[str] = []
    weighted_size = 0.0
    letters = 0
    for span in spans:
        raw_value = span.get("text")
        if not isinstance(raw_value, str) or not raw_value:
            continue
        # PyMuPDF splits mixed-font title words into separate spans.  Preserve
        # each span's source whitespace: adding a separator here turns "AN" or
        # "IMAGE" into a sequence of artificial single-character words.
        pieces.append(raw_value)
        alpha = sum(character.isalpha() for character in raw_value)
        if not alpha:
            continue
        size = span.get("size")
        if not isinstance(size, (int, float)) or size <= 0:
            continue
        letters += alpha
        weighted_size += float(size) * alpha
    value = _clean_text("".join(pieces))
    if not value or not letters:
        return None
    return value, weighted_size / letters, float(letters)


def _pdf_layout_title(path: str) -> str | None:
    """Read title-sized, horizontal first-page lines from the title region only."""
    try:
        import pymupdf as fitz
        document = fitz.open(path)
    except Exception:
        return None
    try:
        if not document.page_count:
            return None
        page = document[0]
        height = float(page.rect.height)
        if height <= 0:
            return None
        lines: list[tuple[float, str, float, float]] = []
        for block in page.get_text("dict").get("blocks") or []:
            for line in block.get("lines") or []:
                bbox = line.get("bbox") or ()
                if len(bbox) < 2:
                    continue
                y = float(bbox[1])
                if not 0.075 * height <= y <= 0.38 * height:
                    continue
                extracted = _horizontal_line_text(line)
                if extracted is None:
                    continue
                value, size, letters = extracted
                if _LEGAL_NOTICE_RE.search(value):
                    continue
                lines.append((y, value, size, letters))
    except Exception:
        return None
    finally:
        document.close()

    if not lines:
        return None
    lines.sort(key=lambda item: item[0])
    max_size = max(item[2] for item in lines)
    prominent = [item for item in lines if item[2] >= max_size * 0.90]
    if not prominent:
        return None

    candidates: list[tuple[float, float, float, str]] = []
    current: list[tuple[float, str, float, float]] = []
    for line in prominent:
        if current and line[0] - current[-1][0] > max(line[2], current[-1][2]) * 2.2:
            value = _clean_text(" ".join(item[1] for item in current))
            if _is_title_like(value):
                total_weight = sum(item[2] * item[3] for item in current)
                candidates.append((
                    total_weight / sum(item[3] for item in current),
                    total_weight,
                    current[0][0],
                    value,
                ))
            current = []
        current.append(line)
    if current:
        value = _clean_text(" ".join(item[1] for item in current))
        if _is_title_like(value):
            total_weight = sum(item[2] * item[3] for item in current)
            candidates.append((
                total_weight / sum(item[3] for item in current),
                total_weight,
                current[0][0],
                value,
            ))
    if not candidates:
        return None
    # Prefer the group with the largest alphabetic-weighted mean font.  Total
    # weight only breaks an equal-font tie, preventing a long author block from
    # outscoring the title merely because it contains more characters.
    return max(candidates, key=lambda candidate: (
        candidate[0], candidate[1], -candidate[2]
    ))[3]


def _non_pdf_front_matter_title(text: str, fmt: str) -> tuple[str, str] | None:
    """Return a clear text-format title without guessing from body prose."""
    front_matter = bounded_front_matter(text)
    lines = [line.strip() for line in front_matter.splitlines() if line.strip()]
    if not lines:
        return None
    if fmt in {"md", "markdown"}:
        for line in lines[:4]:
            heading = re.fullmatch(r"#{1,6}\s+(.+?)(?:\s+#+)?", line)
            if heading:
                title = _clean_text(heading.group(1))
                if _is_title_like(title):
                    return title, "markdown_heading"
    first = _clean_text(lines[0])
    if first:
        explicit = re.fullmatch(r"title\s*:\s*(.+)", first, re.IGNORECASE)
        candidate = _clean_text(explicit.group(1)) if explicit else first
        if _is_title_like(candidate):
            return candidate, (
                "text_front_matter_title_field" if explicit
                else "text_front_matter_first_line"
            )
    return None


def collect_local_evidence(path: str, fmt: str, text: str,
                           extraction_meta: dict | None) -> dict[str, Any]:
    """Collect title and identifier evidence contained in one uploaded manuscript.

    ``validated_local`` means a PDF metadata title passed the strict local validity
    checks.  ``inferred_local`` is a visible, prominent first-page PDF title.  No
    source outside the uploaded file is queried, and unavailable evidence stays
    explicitly unknown.
    """
    evidence = _empty_evidence()
    evidence["identifiers"] = _front_matter_identifiers(text if isinstance(text, str) else "")
    if fmt != "pdf":
        text_title = _non_pdf_front_matter_title(
            text if isinstance(text, str) else "", fmt
        )
        if text_title is not None:
            title, method = text_title
            evidence.update({
                "title": title,
                "title_status": "inferred_local",
                "title_method": method,
            })
        return evidence

    metadata_title = _pdf_metadata_title(path)
    layout_title = _pdf_layout_title(path)
    evidence["metadata_title"] = (
        metadata_title
        if _is_title_like(metadata_title) and "_" not in metadata_title
        else None
    )
    evidence["layout_title"] = layout_title if _is_title_like(layout_title) else None
    metadata_key = _title_key(evidence["metadata_title"] or "")
    layout_key = _title_key(evidence["layout_title"] or "")
    if (evidence["metadata_title"] and evidence["layout_title"]
            and metadata_key and metadata_key == layout_key):
        evidence.update({
            "title": evidence["metadata_title"],
            "title_status": "validated_local",
            "title_method": "pdf_metadata_normalized_layout",
        })
    elif evidence["layout_title"]:
        evidence.update({
            "title": evidence["layout_title"],
            "title_status": "inferred_local",
            "title_method": "pdf_layout_prominent_lines",
        })
    elif evidence["metadata_title"]:
        evidence.update({
            "title": evidence["metadata_title"],
            "title_status": "validated_local",
            "title_method": "pdf_metadata",
        })
    return evidence
