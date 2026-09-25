#!/usr/bin/env python3
# core/resolve/providers/acl.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""ACL Anthology resolver + fetch provider."""

from __future__ import annotations

import html
import json
import re
import threading
import urllib.error
import urllib.parse
from xml.etree import ElementTree as ET

NAME = "acl"
RESOLVE_NAME = "acl_search"
OPTIONAL_STAGE = True
CANONICAL_COMPANION = True
MANIFEST = {
    "doi_prefixes": ["10.3115/", "10.18653/"],
    "canonical_hosts": ["aclanthology.org"],
}
API_BASE = "https://aclanthology.org"
_ACL_DOI_PREFIXES = ("10.3115/", "10.18653/")
_ACL_ANTHOLOGY_CACHE: dict[str, dict[str, dict]] = {}
_ACL_XML_TREE_CACHE: tuple[str, ...] | None = None
_ACL_CACHE_LOCK = threading.Lock()
_ACL_TREE_INFLIGHT = None
_ACL_XML_INFLIGHT: dict[str, object] = {}
_ACL_GITHUB_TREE_URL = "https://api.github.com/repos/acl-org/acl-anthology/git/trees/master?recursive=1"
_ACL_GITHUB_RAW_BASE = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/"


def _is_acl_doi(doi: str | None) -> bool:
    if not doi:
        return False
    doi_lower = doi.lower()
    return any(doi_lower.startswith(p) for p in _ACL_DOI_PREFIXES)


def _extract_paper_ids(doi: str) -> list[str]:
    """Extract candidate ACL Anthology paper IDs from newer-style DOIs.

    ACL Anthology paper IDs are case-sensitive in the filesystem: the PDF
    at ``https://aclanthology.org/D16-1244.pdf`` is NOT reachable as
    ``d16-1244.pdf``.  The canonical form embeds the venue prefix in the
    DOI with mixed case (e.g. ``D16``, ``W19``, ``P14``), so we produce
    both the raw extracted form AND an uppercase variant as fallbacks.

    Examples:
        10.18653/v1/2020.acl-main.10  -> 2020.acl-main.10
        10.3115/v1/W19-4001           -> W19-4001
        10.18653/v2/2022.emnlp-main.1 -> 2022.emnlp-main.1
        10.18653/v1/d16-1244          -> d16-1244, D16-1244
    """
    m = re.match(
        r"10\.(?:3115|18653)/v([12])/(.+)$",
        doi.strip(),
        re.IGNORECASE,
    )
    if m:
        raw = m.group(2)
        upper = raw.upper()
        if raw != upper:
            return [raw, upper]
        return [raw]
    return []


def _resolve_doi_via_api(doi: str, *, get_fn) -> str | None:
    """Search the ACL Anthology API for a paper by DOI; return its paper_id.

    Downloads the full ACL Anthology listing (~8 MB JSON dict keyed by
    paper_id) and scans it for the matching DOI.  This is only called for
    older-style DOIs (10.3115/NNNNN.NNNNN) that don't embed a paper_id.
    """
    try:
        url = f"{API_BASE}/api/"
        _status, body = get_fn(url, accept="application/json", profile="api")
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    doi_lower = doi.strip().lower()
    for paper_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        paper_doi = (entry.get("doi") or "").lower()
        if paper_doi == doi_lower or paper_doi.endswith(doi_lower):
            return paper_id

    return None


def _acl_search_candidates(ref: dict) -> list[dict]:
    """Fallback through the Anthology search logic for legacy ACL DOIs.

    Older 10.3115/... DOIs often do not round-trip cleanly through a direct DOI
    lookup, but the Anthology title/year search can still identify the canonical
    paper_id and OA PDF.
    """
    try:
        try:
            from core.resolve import service as _resolve
        except ImportError:
            import resolve as _resolve
    except Exception:
        return []

    try:
        resolver = _resolve._resolver_registry().get("acl_search")
        discover = getattr(resolver, "discover", None) if resolver is not None else None
        if not callable(discover):
            return []
        result = discover(ref)
    except Exception:
        return []
    if not isinstance(result, dict) or result.get("status") != "resolved":
        return []
    seen = set()
    out = []
    for link in result.get("fulltext_links") or []:
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        content_type = str(link.get("content_type") or "").lower()
        kind = "pdf" if "pdf" in content_type else "landing"
        out.append({"method": NAME, "url": url, "kind": kind})
    return out


def pdf_url(doi: str | None) -> str | None:
    """Map an ACL DOI to an ACL Anthology PDF URL (newer-style DOIs only).

    Returns the UPPERCASE variant when the DOI has mixed case (e.g.
    10.18653/v1/d16-1244 → D16-1244), because ACL Anthology file names are
    case-sensitive and always use the canonical uppercase form.
    """
    if not _is_acl_doi(doi):
        return None
    for paper_id in _extract_paper_ids(doi):
        return f"{API_BASE}/{paper_id}.pdf"
    return None


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    """Return PDF candidate items for a reference with an ACL DOI.

    For newer DOIs that embed a paper ID (10.18653/v1/...), produces
    multiple URL variants when the ID has mixed case, because ACL Anthology
    file names are case-sensitive (D16-1244.pdf works, d16-1244.pdf 404s).

    For older DOIs (10.3115/NNNNN.NNNNN), resolves the paper ID via the
    ACL Anthology API listing.
    """
    normalize_doi = kwargs.get("normalize_doi") or (lambda x: x)
    doi = normalize_doi(ref.get("doi"))
    if not _is_acl_doi(doi):
        return []

    # Newer DOIs (10.18653/v1/...) — paper_id is embedded in the DOI.
    # Produce candidates for ALL variants; the pipeline tries them in order
    # and the uppercase one will succeed.
    paper_ids = _extract_paper_ids(doi)
    if paper_ids:
        return [
            {"method": NAME, "url": f"{API_BASE}/{pid}.pdf", "kind": "pdf"}
            for pid in paper_ids
        ]

    # Older DOIs (10.3115/NNNNN.NNNNN) — resolve via the ACL Anthology API
    get_fn = kwargs.get("get_fn")
    if get_fn:
        try:
            paper_id = _resolve_doi_via_api(doi, get_fn=get_fn)
            if paper_id:
                return [
                    {"method": NAME, "url": f"{API_BASE}/{paper_id}.pdf", "kind": "pdf"}
                ]
        except Exception:
            pass

    # Legacy DOI fallback: search Anthology by title/year and return its OA links.
    items = _acl_search_candidates(ref)
    if items:
        return items

    return []


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod

    venue_text = " ".join(str(ref.get(key) or "") for key in ("venue", "journal", "raw_entry")).lower()
    venue_match = bool(re.search(
        # "natural language" alone is too generic a trigger; require the field phrase.
        # The identity gate in discover() still prevents a wrong attachment, but this
        # avoids firing discover() (and its network calls) for unrelated references.
        r"\b(?:acl|naacl|emnlp|eacl|conll|ijcnlp|nlp|natural language processing|"
        r"computational linguistics|paraphras(?:e|es|ing))\b",
        venue_text,
    ))
    return bool(
        _is_acl_anthology_url(ref.get("url"))
        or _acl_paper_id_from_doi(ref.get("doi"))
        or (
            resolve_mod._article_like_resolution_candidate(ref)
            and venue_match
        )
    )


def _acl_xml_paths(resolve_mod) -> tuple[str, ...]:
    global _ACL_XML_TREE_CACHE, _ACL_TREE_INFLIGHT
    with _ACL_CACHE_LOCK:
        if _ACL_XML_TREE_CACHE is not None:
            return _ACL_XML_TREE_CACHE
        flight = _ACL_TREE_INFLIGHT
        if flight is None:
            flight = {"event": threading.Event(), "error": None}
            _ACL_TREE_INFLIGHT = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight["event"].wait()
        if flight["error"] is not None:
            raise flight["error"]
        with _ACL_CACHE_LOCK:
            if _ACL_XML_TREE_CACHE is not None:
                return _ACL_XML_TREE_CACHE
        raise RuntimeError("ACL Anthology XML tree single-flight did not publish a result")
    try:
        _status, body = resolve_mod._get(
            _ACL_GITHUB_TREE_URL, accept="application/vnd.github+json"
        )
        payload = json.loads(body)
        tree = payload.get("tree") or []
        paths = sorted(
            item.get("path")
            for item in tree
            if isinstance(item, dict)
            and str(item.get("path") or "").startswith("data/xml/")
            and str(item.get("path") or "").endswith(".xml")
        )
        if not paths:
            raise ValueError("ACL Anthology XML tree is empty")
        result = tuple(str(path) for path in paths)
        with _ACL_CACHE_LOCK:
            _ACL_XML_TREE_CACHE = result
        return result
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        with _ACL_CACHE_LOCK:
            if _ACL_TREE_INFLIGHT is flight:
                _ACL_TREE_INFLIGHT = None
            flight["event"].set()


def _acl_ref_year(ref: dict) -> int | None:
    year = ref.get("year")
    try:
        if year:
            return int(year)
    except Exception:
        pass
    raw = str(ref.get("raw_entry") or "")
    match = re.search(r"\b(19|20)\d{2}\b", raw)
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


def _acl_collection_path_hints(paper_id: str | None) -> list[str]:
    if not paper_id:
        return []
    pid = str(paper_id).strip()
    if not pid:
        return []
    stems: list[str] = []
    if re.match(r"^[A-Za-z]\d{2}-", pid):
        stems.append(pid.split("-", 1)[0].upper())
    if "." in pid:
        base = pid.rsplit(".", 1)[0]
        stems.append(base)
        parts = base.split(".")
        if len(parts) >= 2:
            stems.append(".".join(parts[:2]))
            venue_root = parts[1].split("-", 1)[0]
            stems.append(f"{parts[0]}.{venue_root}")
    out = []
    seen = set()
    for stem in stems:
        path = f"data/xml/{stem}.xml"
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _acl_paper_id_from_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    match = re.search(r"10\.18653/v1/([^/\s]+)$", str(doi).strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def _acl_candidate_collection_paths(resolve_mod, ref: dict, paper_id: str | None = None) -> list[str]:
    xml_paths = _acl_xml_paths(resolve_mod)
    xml_path_set = set(xml_paths)
    out: list[str] = []
    seen = set()

    def add(path: str | None):
        if not path or path in seen or path not in xml_path_set:
            return
        seen.add(path)
        out.append(path)

    for hint in _acl_collection_path_hints(paper_id):
        add(hint)

    year = _acl_ref_year(ref)
    if year is None:
        return out

    year_prefix = f"data/xml/{year}."
    for path in xml_paths:
        if path.startswith(year_prefix):
            add(path)

    yy = f"{year % 100:02d}"
    legacy_pat = re.compile(rf"data/xml/[A-Z]{yy}\.xml$")
    for path in xml_paths:
        if legacy_pat.fullmatch(path):
            add(path)
    return out


def _acl_parse_meta(meta_node) -> dict:
    if meta_node is None:
        return {}
    out = {}
    for key in (
        "booktitle",
        "journal",
        "year",
        "venue",
        "publisher",
        "journal-volume",
        "journal-issue",
    ):
        value = _acl_xml_text(meta_node.find(key))
        if value:
            out[key] = html.unescape(value)
    return out


def _acl_xml_text(node) -> str | None:
    if node is None:
        return None
    text = "".join(part for part in node.itertext() if part)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _acl_author_name(author_node) -> str | None:
    if author_node is None:
        return None
    first = _acl_xml_text(author_node.find("first"))
    last = _acl_xml_text(author_node.find("last"))
    if first or last:
        return " ".join(part for part in (first, last) if part).strip() or None
    return _acl_xml_text(author_node)


_ACL_COMMENT_URL = re.compile(
    r"https://aclanthology\.org/((?:[A-Z]\d{2}-\d{4})|(?:19|20)\d{2}\.[a-z0-9-]+\.\d+)/"
)
_ACL_SHORT_TITLE_VENUE_ALIASES = {
    "emnlp": ("empirical methods in natural language processing",),
}


def _acl_paper_id_from_comment(paper_node) -> str | None:
    """Return an official ACL ID only from a comment directly in this paper."""
    for child in list(paper_node):
        if child.tag is not ET.Comment:
            continue
        comment = (child.text or "").strip()
        match = _ACL_COMMENT_URL.fullmatch(comment)
        if match:
            return match.group(1)
    return None


def _acl_parse_record(paper_node, inherited_meta: dict) -> dict | None:
    paper_id = _acl_xml_text(paper_node.find("url"))
    if not paper_id:
        paper_id = _acl_paper_id_from_comment(paper_node)
    # Keep anonymous/numeric records available for metadata matching, but never
    # promote the hierarchy-local ``id`` to a canonical ACL paper identity.
    record_key = paper_id or _acl_xml_text(paper_node.find("bibkey")) or paper_node.get("id")
    title = _acl_xml_text(paper_node.find("title"))
    if not record_key or not title:
        return None
    authors = []
    for author in paper_node.findall("author"):
        name = _acl_author_name(author)
        if name:
            authors.append(html.unescape(name))
    rec = dict(inherited_meta)
    rec.update(
        {
            "record_key": record_key,
            "paper_id": paper_id,
            "title": html.unescape(title),
            "authors": authors,
            "abstract": html.unescape(_acl_xml_text(paper_node.find("abstract")) or "") or None,
            "doi": _acl_xml_text(paper_node.find("doi")),
            "pages": _acl_xml_text(paper_node.find("pages")),
        }
    )
    if not rec.get("journal") and (
        rec.get("journal-volume") or (paper_id and re.match(r"^[JQ]\d{2}$", paper_id))
    ):
        rec["journal"] = rec.get("booktitle")
    return rec


def _acl_collect_records(node, inherited_meta: dict, out: dict[str, dict]) -> None:
    current_meta = dict(inherited_meta)
    tag = str(node.tag).split("}", 1)[-1]
    if tag != "paper":
        current_meta.update(_acl_parse_meta(node.find("meta")))
    if tag == "paper":
        rec = _acl_parse_record(node, current_meta)
        if rec is not None:
            out[rec["record_key"]] = rec
        return
    for child in list(node):
        if isinstance(child.tag, str):
            _acl_collect_records(child, current_meta, out)


def _acl_collection_records(resolve_mod, path: str) -> dict[str, dict]:
    with _ACL_CACHE_LOCK:
        cached = _ACL_ANTHOLOGY_CACHE.get(path)
        if cached is not None:
            return cached
        flight = _ACL_XML_INFLIGHT.get(path)
        if flight is None:
            flight = {"event": threading.Event(), "error": None}
            _ACL_XML_INFLIGHT[path] = flight
            owner = True
        else:
            owner = False
    if not owner:
        flight["event"].wait()
        if flight["error"] is not None:
            raise flight["error"]
        with _ACL_CACHE_LOCK:
            cached = _ACL_ANTHOLOGY_CACHE.get(path)
            if cached is not None:
                return cached
        raise RuntimeError("ACL Anthology XML single-flight did not publish a result")
    try:
        _status, body = resolve_mod._get(
            _ACL_GITHUB_RAW_BASE + path,
            accept="application/xml,text/xml;q=0.9,*/*;q=0.8",
        )
        root = ET.fromstring(body, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        records: dict[str, dict] = {}
        _acl_collect_records(root, {}, records)
        with _ACL_CACHE_LOCK:
            _ACL_ANTHOLOGY_CACHE[path] = records
        return records
    except Exception as exc:
        flight["error"] = exc
        raise
    finally:
        with _ACL_CACHE_LOCK:
            if _ACL_XML_INFLIGHT.get(path) is flight:
                _ACL_XML_INFLIGHT.pop(path, None)
            flight["event"].set()


def _acl_paper_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = (urllib.parse.urlparse(url).path or "").strip("/")
    if not path:
        return None
    tail = path.split("/")[-1]
    if tail.lower().endswith(".pdf"):
        tail = tail[:-4]
    return tail or None


def _is_acl_anthology_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urllib.parse.urlparse(url).netloc or "").lower()
    return host == "aclanthology.org" or host.endswith(".aclanthology.org")


def _acl_record_authors(rec: dict) -> list[str]:
    out = []
    for author in rec.get("authors") or []:
        if isinstance(author, dict):
            name = author.get("name") or author.get("full_name")
            if not name:
                given = str(author.get("first_name") or author.get("given") or "").strip()
                family = str(author.get("last_name") or author.get("family") or "").strip()
                name = " ".join(part for part in (given, family) if part).strip()
        else:
            name = str(author or "").strip()
        if name:
            out.append(name)
    return out


def _acl_record_venue(rec: dict) -> str | None:
    return (
        rec.get("venue")
        or rec.get("booktitle")
        or rec.get("journal")
        or rec.get("collection")
        or rec.get("source")
    )


def _acl_record_doi(rec: dict) -> str | None:
    doi = rec.get("doi")
    if not doi:
        return None
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(doi).strip(), flags=re.IGNORECASE)


def _acl_words(value: str | None) -> list[str]:
    return re.findall(r"[^\W_]+", str(value or "").casefold())


def _acl_authoritative_short_title_prefix_match(
    ref: dict,
    rec: dict,
    metadata_match: dict | None,
    *,
    cited_title: str | None,
) -> bool:
    """Allow an ACL-only abbreviated title after independent ACL evidence agrees.

    This is deliberately narrower than generic metadata matching: the cited title
    must be a four-or-more-token prefix on word boundaries, and the authoritative
    ACL record must independently agree on first author, exact year, printed venue
    and a canonical Anthology paper identifier.
    """
    if not isinstance(metadata_match, dict):
        return False
    cited_words = _acl_words(cited_title)
    matched_words = _acl_words(rec.get("title"))
    if (
        len(cited_words) < 4
        or matched_words[:len(cited_words)] != cited_words
    ):
        return False

    cited_author = str(metadata_match.get("cited_first_author") or "").strip().casefold()
    matched_author = str(metadata_match.get("matched_first_author") or "").strip().casefold()
    if (
        metadata_match.get("author_match") is not True
        or not cited_author
        or cited_author != matched_author
        or metadata_match.get("year_match") is not True
    ):
        return False

    paper_id = rec.get("paper_id")
    if not isinstance(paper_id, str) or not _ACL_COMMENT_URL.fullmatch(
        f"https://aclanthology.org/{paper_id}/"
    ):
        return False

    raw_words = " ".join(_acl_words(ref.get("raw_entry")))
    venue = " ".join(_acl_words(_acl_record_venue(rec)))
    if venue and venue in raw_words:
        return True
    return any(
        alias in raw_words
        for alias in _ACL_SHORT_TITLE_VENUE_ALIASES.get(venue, ())
    )


def _acl_to_crossref_like(rec: dict) -> dict:
    authors = _acl_record_authors(rec)
    author_list = []
    if authors:
        author_list.append({"family": authors[0].split()[-1]})
    year = rec.get("year")
    title = rec.get("title")
    venue = _acl_record_venue(rec)
    return {
        "title": [title] if title else None,
        "author": author_list,
        "published-print": {"date-parts": [[year]]} if year else {},
        "container-title": [venue] if venue else [],
    }


def _acl_fulltext_meta(paper_id: str | None, rec: dict) -> dict:
    doi = _acl_record_doi(rec)
    landing_url = f"https://aclanthology.org/{paper_id}/" if paper_id else None
    pdf_url = f"https://aclanthology.org/{paper_id}.pdf" if paper_id else None
    links = []
    seen = set()

    def add(url: str | None, content_type: str):
        if not url or url in seen:
            return
        seen.add(url)
        links.append({"url": url, "content_type": content_type})

    if doi:
        add(f"https://doi.org/{doi}", "doi")
    add(landing_url, "html")
    add(pdf_url, "application/pdf")
    work_type = "journal article" if rec.get("journal") else "conference paper"
    has_acl_fulltext = bool(landing_url or pdf_url)
    return {
        "doi": doi,
        "fulltext_exists": True if has_acl_fulltext else "unknown",
        "oa_status": "open" if has_acl_fulltext else "unknown",
        "work_type": work_type,
        "fulltext_links": links,
    }


def discover(ref: dict) -> dict | None:
    from core.resolve import service as resolve_mod

    via = RESOLVE_NAME
    direct_paper_id = _acl_paper_id_from_url(ref.get("url"))
    doi = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        str(ref.get("doi") or "").strip(),
        flags=re.IGNORECASE,
    ) or None
    direct_paper_id = direct_paper_id or _acl_paper_id_from_doi(doi)
    title = resolve_mod._article_title_candidate(ref)
    if not (direct_paper_id or doi or title):
        return {
            "status": "unverified",
            "via": via,
            "reason": "no usable title, DOI, or ACL Anthology URL for ACL search",
        }
    try:
        candidate_paths = _acl_candidate_collection_paths(resolve_mod, ref, direct_paper_id)
        if not candidate_paths:
            return {
                "status": "unverified",
                "via": via,
                "reason": "no plausible ACL Anthology collection for this reference",
            }
        records = {}
        for path in candidate_paths:
            records.update(_acl_collection_records(resolve_mod, path))
        if not records:
            return {
                "status": "unverified",
                "via": via,
                "reason": "no ACL Anthology paper records found in plausible collections",
            }
        matched_paper_id = None
        matched_rec = None
        confidence = "medium"
        reason = "ACL Anthology title search match"
        metadata_match = None
        authoritative_short_title = False

        if direct_paper_id and direct_paper_id in records:
            matched_paper_id = direct_paper_id
            matched_rec = records[direct_paper_id]
            confidence = "high"
            reason = "ACL Anthology URL resolved to a paper record"
        elif doi:
            doi_lower = doi.lower()
            for paper_id, rec in records.items():
                rec_doi = (_acl_record_doi(rec) or "").lower()
                if rec_doi == doi_lower:
                    matched_paper_id = paper_id
                    matched_rec = rec
                    confidence = "high"
                    reason = "ACL Anthology DOI match"
                    break

        if matched_rec is None:
            best_rec = None
            best_paper_id = None
            best_profile = None
            best_score = -1.0
            for paper_id, rec in records.items():
                if not isinstance(rec, dict):
                    continue
                profile = resolve_mod._metadata_match_profile(
                    ref, _acl_to_crossref_like(rec), rec.get("title")
                )
                if profile["score"] > best_score:
                    best_score = profile["score"]
                    best_profile = profile
                    best_rec = rec
                    best_paper_id = paper_id
            matched_rec = best_rec
            matched_paper_id = best_paper_id
            metadata_match = best_profile
            matched_title = (matched_rec or {}).get("title")
            overlap = best_profile["title_overlap"] if best_profile else None
            authoritative_short_title = _acl_authoritative_short_title_prefix_match(
                ref,
                matched_rec or {},
                best_profile,
                cited_title=title,
            )
            if authoritative_short_title:
                confidence = "high"
                reason = "ACL Anthology authoritative short-title prefix identity match"
            if (
                overlap is not None
                and overlap < resolve_mod.TITLE_MISMATCH_MAX
                and not authoritative_short_title
            ):
                return {
                    "status": "unverified",
                    "via": via,
                    "matched_title": matched_title,
                    "reason": (
                        f"title too dissimilar (overlap {overlap:.3f} < {resolve_mod.TITLE_MISMATCH_MAX}); "
                        "ACL Anthology candidate does not identify the cited work"
                    ),
                    "resolution_basis": "metadata_search",
                    "existence_confidence": "low",
                    "metadata_match": best_profile,
                }
            if matched_rec is None or best_score < 0.30:
                return {
                    "status": "unverified",
                    "via": via,
                    "matched_title": matched_title,
                    "reason": (
                        "best ACL Anthology match below confidence threshold "
                        f"(score {best_score:.3f}, overlap {overlap})"
                    ),
                    "resolution_basis": "metadata_search",
                    "existence_confidence": "low",
                    "metadata_match": best_profile,
                }

        matched_title = matched_rec.get("title") if isinstance(matched_rec, dict) else None
        identifier_basis = "metadata_search"
        resolved_identifier = None
        if direct_paper_id and matched_paper_id == direct_paper_id:
            identifier_basis = "acl_id"
            resolved_identifier = {
                "type": "acl_id",
                "value": matched_paper_id,
                "validated_via": via,
            }
        elif doi and matched_paper_id is not None and reason == "ACL Anthology DOI match":
            identifier_basis = "doi"
            resolved_identifier = {
                "type": "doi",
                "value": doi,
                "validated_via": via,
            }
        out = {
            "status": "resolved",
            "via": via,
            "matched_title": matched_title,
            "matched_authors": _acl_record_authors(matched_rec or {}),
            "abstract": html.unescape((matched_rec or {}).get("abstract") or "") or None,
            "retracted": False,
            "reason": reason,
            "resolution_basis": identifier_basis,
            "existence_confidence": confidence,
            "paper_id": (matched_rec or {}).get("paper_id"),
        }
        out.update(_acl_fulltext_meta((matched_rec or {}).get("paper_id"), matched_rec or {}))
        if metadata_match is not None:
            out["metadata_match"] = metadata_match
        if authoritative_short_title:
            out["identity_basis"] = "authoritative_acl_short_title_prefix"
        if resolved_identifier is not None:
            out["resolved_identifier"] = resolved_identifier
        return out
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return {
                "status": "unresolved",
                "via": via,
                "reason": "rate_limited (HTTP 429) - NOT fabrication",
            }
        return {"status": "unresolved", "via": via, "reason": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "unresolved", "via": via, "reason": f"network: {type(exc).__name__}"}
