#!/usr/bin/env python3
# core/resolve/providers/curated_copies.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Small, auditable catalogue of public author/institutional copies.

Entries are data, not fetch-pipeline branches.  A candidate is emitted only on
an exact title plus compatible first-author/year match; the normal downloaded
document identity gate remains authoritative.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

NAME = "curated_copies"
OFFICIAL_LAYOUT_SPLIT_RELATION = "official_curated_exact_layout_split_document"
MANIFEST = {
    "canonical_hosts": [
        "yann.lecun.com",
        "meyn.ece.ufl.edu",
        "www.cs.toronto.edu",
        "www.image-net.org",
        "clarivate.com",
    ],
}

CATALOGUE = (
    {
        "title": "Backpropagation applied to handwritten zip code recognition",
        "first_author": "lecun",
        "year": 1989,
        "url": "http://yann.lecun.com/exdb/publis/pdf/lecun-89e.pdf",
        "copy_kind": "author_copy",
    },
    {
        "title": "Learning multiple layers of features from tiny images",
        "first_author": "krizhevsky",
        "year": 2009,
        "url": "https://www.cs.toronto.edu/~kriz/learning-features-2009-TR.pdf",
        "copy_kind": "author_institutional_report",
    },
    {
        "title": "ImageNet: A large-scale hierarchical image database",
        "first_author": "deng",
        "year": 2009,
        "doi": "10.1109/CVPR.2009.5206848",
        "url": "https://www.image-net.org/static_files/papers/imagenet_cvpr09.pdf",
        "copy_kind": "official_conference_copy",
        "require_author_year": True,
    },
    {
        "title": "Journal Citation Reports Reference Guide, v3",
        "first_author": "clarivate",
        "year": 2025,
        "url": (
            "https://clarivate.com/academia-government/wp-content/uploads/sites/3/"
            "dlm_uploads/2025/12/JCR-Reference-Guide-2025-V3.pdf"
        ),
        "content_type": "application/pdf",
        "copy_kind": "official_institutional_guide",
        "require_author_year": True,
        # This narrowly permits fetch_store's exact official-document fallback
        # when PDF column ordering defeats the ordinary identity probe.
        "official": True,
        "official_identity_fallback": True,
        "institutional_signals": ("clarivate",),
    },
    {
        "title": "Squad: 100,000+ questions for machine comprehension of text",
        "first_author": "rajpurkar",
        "year": 2016,
        "doi": "10.18653/v1/D16-1264",
        "url": "https://aclanthology.org/D16-1264.pdf",
        "copy_kind": "official_published_copy",
        "require_author_year": True,
        "official": True,
        "institutional_signals": ("aclanthology",),
    },
    {
        "title": "The Geography of Principal Internships in North Carolina",
        "first_author": "drake",
        "year": 2024,
        "doi": "10.1177/23328584231219994",
        "url": "https://files.eric.ed.gov/fulltext/EJ1455224.pdf",
        "copy_kind": "official_repository_copy",
        "require_author_year": True,
        "require_doi": True,
    },
    {
        "title": "Commentary: Measuring the success of blinding in RCTs: don't, must, can't or needn't?",
        "first_author": "sackett",
        "year": 2007,
        "doi": "10.1093/ije/dym088",
        "url": "https://oup.silverchair-cdn.com/article-minimal/656558",
        "kind": "landing",
        "content_type": "text/html",
        "copy_kind": "public_publisher_body",
        "require_author_year": True,
        "require_doi": True,
    },
    {
        "title": "Spinal manipulation, medication, or home exercise with advice for acute and subacute neck pain: a randomized trial",
        "first_author": "bronfort",
        "year": 2012,
        "doi": "10.7326/0003-4819-156-1-201201030-00002",
        "url": "https://www.manuellterapi.net/intern/images/stories/spinal_manipulation_medication_exercise_jan2012.pdf",
        "copy_kind": "professional_public_mirror",
        "require_author_year": True,
        "require_doi": True,
    },
    {
        "title": "Community Ecology Package",
        "first_author": "oksanen",
        "year": 2018,
        "url": "https://cran.r-project.org/src/contrib/Archive/vegan/vegan_2.5-3.tar.gz",
        "kind": "cran_archive",
        "content_type": "application/gzip",
        "copy_kind": "official_versioned_source_archive",
        "require_author_year": True,
        "require_raw_markers": ("vegan", "2.5-3"),
        "require_raw_version": "2.5-3",
    },
    {
        "title": "World Investment Report 2020: International Production Beyond the Pandemic",
        "first_author": "unctad",
        "year": 2020,
        "url": "https://unctad.org/system/files/official-document/wir2020_en.pdf",
        "copy_kind": "official_institutional_report",
        "require_author_year": True,
        "official": True,
    },
)


def official_canonical_hosts() -> tuple[str, ...]:
    """Hosts belonging to catalogue records explicitly declared official."""
    return tuple(sorted({
        parsed.hostname.lower()
        for entry in CATALOGUE if entry.get("official") is True
        for parsed in (urllib.parse.urlsplit(entry["url"]),)
        if parsed.hostname
    }))


def official_layout_split_catalogue_record(url: str) -> dict | None:
    """Return the sole opt-in record, only for its literal canonical URL.

    The Fetch fallback receives frozen candidate context, so it must recover
    these catalogue-only facts from an exact route rather than trusting
    provider-private candidate fields.
    """
    for entry in CATALOGUE:
        if (
            entry.get("official_identity_fallback") is True
            and url == entry["url"]
        ):
            return entry
    return None


def _norm(value: object) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    )
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _first_author(ref: dict) -> str | None:
    # Keep author parsing consistent with the resolver.  The import is local
    # because sources imports the provider registry during module setup.
    from core.resolve.sources import _first_author_surname

    raw = str(ref.get("raw_entry") or "")
    title = str(ref.get("title") or "")
    if title:
        match = re.search(re.escape(title), raw, re.IGNORECASE)
        if match:
            raw = raw[:match.start()]
    legacy_first = re.split(r",|\s+(?:and|&)\s+", raw, maxsplit=1)[0]
    tokens = _norm(legacy_first).split()
    legacy_surname = tokens[-1] if tokens else None
    surname = _first_author_surname(ref)
    # Preserve the catalogue's established initial-first and institutional
    # forms, which the consolidated parser cannot always disambiguate.
    if re.match(r"\s*[A-Z]\.\s+", raw) or not surname:
        return legacy_surname
    return surname


def _normalized_doi(value: object) -> str:
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^https?://(?:www\.)?(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    return text.casefold().rstrip(".,;")


def _match(ref: dict) -> dict | None:
    title = _norm(ref.get("title"))
    author = _first_author(ref)
    try:
        year = int(ref.get("year")) if ref.get("year") is not None else None
    except (TypeError, ValueError):
        year = None
    for entry in CATALOGUE:
        if title != _norm(entry["title"]):
            continue
        entry_doi = _normalized_doi(entry.get("doi"))
        ref_doi = _normalized_doi(ref.get("doi"))
        if entry.get("require_doi") and not ref_doi:
            continue
        if entry_doi and ref_doi and entry_doi != ref_doi:
            continue
        if entry.get("require_author_year"):
            if author != entry["first_author"] or year != entry["year"]:
                continue
        else:
            # Preserve the catalogue's original tolerant behaviour for legacy
            # author copies whose citations omit an author or year.  Records
            # marked ``require_author_year`` deliberately require both.
            if author and author != entry["first_author"]:
                continue
            if year is not None and year != entry["year"]:
                continue
        raw = _norm(ref.get("raw_entry"))
        if any(_norm(marker) not in raw for marker in entry.get("require_raw_markers", ())):
            continue
        version = entry.get("require_raw_version")
        if version and not re.search(
            rf"(?<![0-9A-Za-z.-]){re.escape(str(version))}(?![0-9A-Za-z.-])",
            str(ref.get("raw_entry") or ""),
            re.IGNORECASE,
        ):
            continue
        return entry
    return None


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    entry = _match(ref) if isinstance(ref, dict) else None
    if entry is None:
        return []
    identifiers = {"doi": entry["doi"]} if entry.get("doi") else {}
    identity_context = {
        "provider": NAME,
        "title": entry["title"],
        "year": entry["year"],
        "first_author": entry["first_author"],
        "identifiers": identifiers,
        "source_confidence": 0.98,
    }
    if entry.get("official") is True:
        identity_context.update({
            "official": True,
            "canonical_host": True,
            "canonical_url": entry["url"],
        })
    if entry.get("official_identity_fallback") is True:
        identity_context["official_document_relation"] = OFFICIAL_LAYOUT_SPLIT_RELATION
    return [{
        "method": NAME,
        "url": entry["url"],
        "kind": entry.get("kind", "pdf"),
        "content_type": entry.get("content_type", "application/pdf"),
        "content_version": "published",
        "discovered_via": NAME,
        "provenance": [NAME, "catalogue"],
        "discovery_reason": f"exact {entry['copy_kind']} catalogue match",
        "identity_context": identity_context,
    }]
