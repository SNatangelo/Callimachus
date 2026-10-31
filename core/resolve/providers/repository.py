#!/usr/bin/env python3
# core/resolve/providers/repository.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Conservative PDF expansion for corroborated DSpace repository records.

The provider recognises DSpace 7 item UUIDs and legacy handle landings.  It
does not scan arbitrary reference URLs, follow search pages, or treat a file
named ``.pdf`` as evidence that it is the cited work.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import re
import unicodedata
import urllib.parse

NAME = "repository"
MANIFEST = {"origin": NAME}

_PDF_META_NAMES = {"citation_pdf_url", "pdf_url", "dc.identifier.pdf", "dc.relation", "dc.relation.isformatof", "dc.relation.hasformat"}
_TITLE_META_NAMES = {"citation_title", "dc.title", "dc.title[]", "bepress_citation_title"}
_DOI_META_NAMES = {"citation_doi", "dc.identifier.doi", "dc.identifier"}
_PDFISH_PATH = re.compile(r"(?:\.pdf(?:$|[?#])|/(?:bitstream|bitstreams)/|/(?:download|downloads)/|/content(?:$|[?#]))", re.I)
_SUPPLEMENT = re.compile(r"\b(?:supplement(?:ary|al)?|appendix|appendices|supporting[ _-]?information|dataset|data[ _-]?set)\b|-supp\.pdf(?:$|[?#\s])", re.I)
_DSPACE_ITEM = re.compile(r"/items/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})(?:/)?$", re.I)
_LEGACY_HANDLE = re.compile(r"/(?:xmlui/)?handle/\d+/[^/?#]+/?$", re.I)
_BARE_HANDLE = re.compile(r"^/\d+/[^/?#]+/?$")
_BEPRESS_RECORD_PATH = re.compile(
    r"^/(?:[a-z0-9-]+_[a-z0-9_-]+/\d+|[a-z0-9_-]+/vol\d+/iss\d+/\d+)/?$",
    re.I,
)
_OJS_DOWNLOAD_PATH = re.compile(
    r"^(?P<prefix>/.*/index\.php/[^/]+/article)/download/(?P<article>\d+)(?:/[^/]+)?/?$",
    re.I,
)
_BEPRESS_COVER_PAGE = "bepress_is_article_cover_page"
_BEPRESS_TITLE = "bepress_citation_title"
_BEPRESS_AUTHOR = "bepress_citation_author"
_BEPRESS_DATE = "bepress_citation_date"
_BEPRESS_JOURNAL = "bepress_citation_journal_title"
_BEPRESS_VOLUME = "bepress_citation_volume"
_BEPRESS_ISSUE = "bepress_citation_issue"
_BEPRESS_DOI = "bepress_citation_doi"
_BEPRESS_PDF = "bepress_citation_pdf_url"


def _http_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    parsed = urllib.parse.urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _pdfish(url: str, mime_type: str | None = None) -> bool:
    return "pdf" in str(mime_type or "").lower() or bool(_PDFISH_PATH.search(url))


def is_bare_handle_url(value: str | None) -> bool:
    """Recognise only the public Handle resolver's bare numeric landing form."""
    url = _http_url(value)
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.casefold() == "hdl.handle.net" and bool(
        _BARE_HANDLE.fullmatch(parsed.path)
    )


def is_bepress_article_landing_url(value: str | None) -> bool:
    """Recognise only stable BePress article-record path shapes before probing."""
    url = _http_url(value)
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    return bool(_BEPRESS_RECORD_PATH.fullmatch(parsed.path))


def is_ojs_article_download_url(value: str | None) -> bool:
    url = _http_url(value)
    return bool(url and _OJS_DOWNLOAD_PATH.fullmatch(urllib.parse.urlparse(url).path))


def _supplementary(url: str, label: str = "") -> bool:
    return bool(_SUPPLEMENT.search(urllib.parse.unquote(url) + " " + label))


def _text_key(value: object) -> str:
    """Strict, punctuation-insensitive comparison key for record fields."""
    text = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _author_surname(value: object) -> str:
    text = html.unescape(str(value or "")).strip()
    if "," in text:
        text = text.split(",", 1)[0]
    tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
    return _text_key(tokens[-1]) if tokens else ""


def _year(value: object) -> int | None:
    match = re.search(r"\b(?:18|19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _coordinate(ref: dict, kind: str) -> str:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value")
            if value not in (None, ""):
                return str(value).strip()
    for field in ({"container": ("journal", "venue"), "volume": ("volume", "journal_volume"),
                   "issue": ("issue", "journal_issue")}.get(kind, ())):
        value = ref.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _journal_abbreviation_compatible(declared: str, observed: str) -> bool:
    """Recognise an unambiguous, ordered journal initialism conservatively."""
    left = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", html.unescape(declared))
    right = re.findall(r"[A-Za-z]+", html.unescape(observed))
    if len(left) != len(right) or len(left) < 3:
        return False
    for abbreviated, expanded in zip(left, right):
        short = _text_key(abbreviated)
        long = _text_key(expanded)
        if short == long or (len(short) == 1 and short == long[:1]):
            continue
        if len(short) >= 3 and long.startswith(short):
            continue
        # A printed contraction such as ``Commc'ns`` is an ordered deletion
        # from ``Communications``; accepting only a four-character-or-longer
        # subsequence keeps this narrower than generic fuzzy venue matching.
        remaining = iter(long)
        if len(short) >= 4 and all(char in remaining for char in short):
            continue
        return False
    return True


def _journal_matches(ref: dict, observed: str) -> bool:
    declared = _coordinate(ref, "container")
    if not declared:
        return True
    if _text_key(declared) == _text_key(observed):
        return True
    try:
        from core.resolve.journal_authority import assess_local_journal
        observed_ref = dict(ref, cited_coordinates=[
            {"kind": "container", "normalized_value": observed},
        ])
        left, right = assess_local_journal(ref), assess_local_journal(observed_ref)
        if left and right and left.get("record_id") == right.get("record_id"):
            return True
    except Exception:
        # A missing or unusable optional authority registry never becomes an
        # identity grant.  The strict abbreviation relation below remains local.
        pass
    return _journal_abbreviation_compatible(declared, observed)


def _record_metadata_match(
    resolve_mod, ref: dict, *, title: str, author: str, year: int,
    journal: str, volume: str | None, issue: str | None,
) -> dict:
    """Build a profile consistent with the record's attested journal identity."""
    profile = resolve_mod._metadata_match_profile(ref, {
        "author": [{"family": _author_surname(author)}],
        "published": {"date-parts": [[year]]},
        "container-title": [journal],
        "volume": volume,
        "issue": issue,
    }, title)
    # The caller has already required ``_journal_matches``.  Preserve the
    # observed journal string, but do not let the generic token overlap turn a
    # deterministically recognised abbreviation into a false refutation.
    comparisons = profile.get("coordinate_comparisons") or []
    if any(item.get("kind") == "container" for item in comparisons):
        profile["venue_overlap"] = 1.0
        for item in comparisons:
            if item.get("kind") == "container":
                item["status"] = "match"
    return profile


class _LandingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta: list[tuple[str, str]] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        row = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "meta":
            name = (row.get("name") or row.get("property") or "").strip().lower()
            value = row.get("content", "").strip()
            if name and value:
                self.meta.append((name, html.unescape(value)))
        elif tag.lower() in {"a", "link"}:
            row["_tag"] = tag.lower()
            self.links.append(row)


def _response_body_and_url(get_fn, url: str, *, accept: str) -> tuple[bytes | None, str]:
    """Return a successful response body and its effective URL when supplied."""
    if not callable(get_fn):
        return None, url
    try:
        # Opt in to the provider callback's private response carrier so a
        # Handle redirect resolves relative links against the repository page.
        kwargs = {"accept": accept, "profile": "document", "timeout": 20}
        if getattr(get_fn, "supports_effective_url", False):
            kwargs["include_effective_url"] = True
        response = get_fn(url, **kwargs)
        response_url = url
        if isinstance(response, tuple) and len(response) >= 2:
            status, body = response[0], response[1]
        elif isinstance(response, dict):
            status, body = response.get("status"), response.get("body")
            response_url = _http_url(response.get("url")) or url
        else:
            return None, url
        if status is not None and not (200 <= int(status) < 300):
            return None, response_url
        return (body if isinstance(body, bytes) else str(body or "").encode("utf-8")), response_url
    except Exception:
        return None, url


def _response_body(get_fn, url: str, *, accept: str) -> bytes | None:
    return _response_body_and_url(get_fn, url, accept=accept)[0]


def _dedupe(urls: list[str]) -> list[str]:
    seen = set()
    return [url for url in urls if url and not (url in seen or seen.add(url))]


def _landing_parser(body: bytes) -> _LandingParser | None:
    parser = _LandingParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return parser


def _meta_value(
    parser: _LandingParser, name: str, *, first_repeated: bool = False,
) -> str | None:
    values = [value for key, value in parser.meta if key == name]
    if first_repeated:
        return values[0].strip() if values and values[0].strip() else None
    return values[0].strip() if len(values) == 1 and values[0].strip() else None


def _bepress_record(
    ref: dict, landing_url: str, body: bytes, effective_url: str,
) -> dict | None:
    """Return a narrowly attested BePress article-cover record, or nothing.

    The cover-page marker is the authority boundary.  Ordinary pages hosted by
    a BePress customer are deliberately not treated as records.
    """
    parser = _landing_parser(body)
    if parser is None or _meta_value(parser, _BEPRESS_COVER_PAGE) != "1":
        return None
    title = _meta_value(parser, _BEPRESS_TITLE)
    author = _meta_value(parser, _BEPRESS_AUTHOR, first_repeated=True)
    year = _year(_meta_value(parser, _BEPRESS_DATE))
    journal = _meta_value(parser, _BEPRESS_JOURNAL)
    volume = _meta_value(parser, _BEPRESS_VOLUME)
    issue = _meta_value(parser, _BEPRESS_ISSUE)
    pdf = _http_url(urllib.parse.urljoin(effective_url, _meta_value(parser, _BEPRESS_PDF) or ""))
    if not all((title, author, year, journal, pdf)) or _supplementary(pdf):
        return None

    try:
        from core.resolve import service as resolve_mod
        cited_title = resolve_mod._article_title_candidate(ref) or ref.get("title")
        cited_author = resolve_mod._first_author_key(ref.get("raw_entry")) or ref.get("ay_surname")
    except Exception:
        return None
    if not cited_title or _text_key(cited_title) != _text_key(title):
        return None
    if not cited_author or _text_key(cited_author) != _author_surname(author):
        return None
    if not _journal_matches(ref, journal):
        return None
    for kind, observed in (("volume", volume), ("issue", issue)):
        declared = _coordinate(ref, kind)
        if declared and (not observed or _text_key(declared) != _text_key(observed)):
            return None
    declared_doi = _normalized_doi(ref.get("doi"))
    observed_doi = _normalized_doi(_meta_value(parser, _BEPRESS_DOI))
    if declared_doi and observed_doi and declared_doi != observed_doi:
        return None

    try:
        metadata_match = _record_metadata_match(
            resolve_mod, ref, title=title, author=author, year=year,
            journal=journal, volume=volume, issue=issue,
        )
    except Exception:
        return None
    context = {
        "provider": NAME,
        "official": True,
        "provider_record_id": "bepress_article_cover_page",
        "official_document_relation": "official_article_cover_page_links_exact_document",
        "landing_page_url": effective_url,
        "canonical_url": pdf,
        "title": title,
        "first_author": _author_surname(author),
        "year": year,
    }
    if observed_doi:
        context["identifiers"] = {"doi": observed_doi}
    return {
        "status": "resolved",
        "via": "repository_bepress",
        "matched_title": title,
        "matched_authors": [author],
        "matched_year": year,
        "identifiers": {"doi": observed_doi} if observed_doi else {},
        "metadata_match": metadata_match,
        "reason": "BePress article cover page exact bibliographic attestation",
        "resolution_basis": "canonical_source",
        "existence_confidence": "high",
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "article",
        "fulltext_links": [{
            "url": pdf,
            "content_type": "pdf",
            "identity_context": context,
        }],
    }


def attest_bepress_record(ref: dict, landing_url: str) -> dict | None:
    """Attest one BePress cover page without treating host ownership as proof."""
    if not isinstance(ref, dict) or not _http_url(landing_url) or _pdfish(landing_url):
        return None
    try:
        from core.resolve import service as resolve_mod
        status, body = resolve_mod._get(
            landing_url, accept="text/html,application/xhtml+xml",
        )
    except Exception:
        return None
    if not (200 <= int(status) < 300):
        return None
    payload = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    return _bepress_record(ref, landing_url, payload, landing_url)


def attest_ojs_download_record(ref: dict, download_url: str) -> dict | None:
    """Attest an OJS article view derived only from its canonical download URL."""
    url = _http_url(download_url)
    match = _OJS_DOWNLOAD_PATH.fullmatch(urllib.parse.urlparse(url or "").path)
    if not isinstance(ref, dict) or not url or match is None:
        return None
    parsed = urllib.parse.urlparse(url)
    view_url = urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc,
        f"{match.group('prefix')}/view/{match.group('article')}", "", "", "",
    ))
    try:
        from core.resolve import service as resolve_mod
        status, body = resolve_mod._get(view_url, accept="text/html,application/xhtml+xml")
    except Exception:
        return None
    if not (200 <= int(status) < 300):
        return None
    parser = _landing_parser(body if isinstance(body, bytes) else str(body or "").encode("utf-8"))
    if parser is None:
        return None
    title = _meta_value(parser, "citation_title")
    author = _meta_value(parser, "citation_author", first_repeated=True)
    # OJS installations often retain a stale ``citation_date`` from a prior
    # issue.  Prefer the explicit publication field, then the displayed record
    # date, and never silently choose the ambiguous fallback.
    year = _year(
        _meta_value(parser, "citation_publication_date")
        or _meta_value(parser, "dc.date.created")
    )
    journal = _meta_value(parser, "citation_journal_title")
    volume = _meta_value(parser, "citation_volume")
    issue = _meta_value(parser, "citation_issue")
    pdf = _http_url(urllib.parse.urljoin(view_url, _meta_value(parser, "citation_pdf_url") or ""))
    if not all((title, author, year, journal, pdf)) or _supplementary(pdf):
        return None
    cited_title = resolve_mod._article_title_candidate(ref) or ref.get("title")
    cited_author = resolve_mod._first_author_key(ref.get("raw_entry")) or ref.get("ay_surname")
    if (not cited_title or _text_key(cited_title) != _text_key(title)
            or not cited_author or _text_key(cited_author) != _author_surname(author)
            or not _journal_matches(ref, journal)):
        return None
    for kind, observed in (("volume", volume), ("issue", issue)):
        declared = _coordinate(ref, kind)
        if declared and (not observed or _text_key(declared) != _text_key(observed)):
            return None
    declared_doi = _normalized_doi(ref.get("doi"))
    observed_doi = _normalized_doi(_meta_value(parser, "citation_doi"))
    if declared_doi and observed_doi and declared_doi != observed_doi:
        return None
    metadata_match = _record_metadata_match(
        resolve_mod, ref, title=title, author=author, year=year,
        journal=journal, volume=volume, issue=issue,
    )
    context = {
        "provider": NAME, "official": True, "provider_record_id": "ojs_article_view",
        "official_document_relation": "official_article_view_links_exact_document",
        "landing_page_url": view_url, "canonical_url": pdf, "title": title,
        "first_author": _author_surname(author), "year": year,
    }
    if observed_doi:
        context["identifiers"] = {"doi": observed_doi}
    return {
        "status": "resolved", "via": "repository_ojs", "matched_title": title,
        "matched_authors": [author], "matched_year": year,
        "identifiers": {"doi": observed_doi} if observed_doi else {},
        "metadata_match": metadata_match,
        "reason": "OJS article view exact bibliographic attestation",
        "resolution_basis": "canonical_source", "existence_confidence": "high",
        "retracted": False, "fulltext_exists": True, "oa_status": "open", "work_type": "article",
        "fulltext_links": [{
            "url": pdf, "content_type": "pdf", "identity_context": context,
        }],
    }


def _landing_pdf_urls(landing_url: str, body: bytes) -> list[str]:
    parser = _landing_parser(body)
    if parser is None:
        return []
    out: list[str] = []
    for name, value in parser.meta:
        url = _http_url(urllib.parse.urljoin(landing_url, value))
        if name in _PDF_META_NAMES and url and _pdfish(url) and not _supplementary(url):
            out.append(url)
    for row in parser.links:
        rel = {part.lower() for part in row.get("rel", "").split()}
        url = _http_url(urllib.parse.urljoin(landing_url, html.unescape(row.get("href", ""))))
        standard_rel = bool(rel & {"download", "alternate"})
        direct_pdf_anchor = row.get("_tag") == "a" and _pdfish(url)
        if url and not _supplementary(url, row.get("title", "") + " " + row.get("aria-label", "")) and (
            (standard_rel and _pdfish(url, row.get("type"))) or direct_pdf_anchor
        ):
            out.append(url)
    return _dedupe(out)


def _json_body(get_fn, url: str) -> dict:
    body = _response_body(get_fn, url, accept="application/hal+json,application/json")
    try:
        value = json.loads((body or b"").decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _href(value) -> str | None:
    return _http_url(value.get("href")) if isinstance(value, dict) else _http_url(value)


def _embedded_rows(payload: dict, key: str) -> list[dict]:
    rows = (payload.get("_embedded") or {}).get(key) or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _normalized_doi(value: object) -> str:
    text = urllib.parse.unquote(str(value or "")).strip()
    text = re.sub(r"^https?://(?:www\.)?(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    return text.lower()


def _ref_identity(ref: dict | None) -> dict | None:
    if not isinstance(ref, dict):
        return None
    title = str(ref.get("title") or "").strip()
    doi = _normalized_doi(ref.get("doi"))
    if not title and not doi:
        return None
    return {"title": title, "doi": doi, "year": ref.get("year")}


def _identity_matches(ref: dict | None, *, title: str | None = None, doi: str | None = None) -> bool:
    expected = _ref_identity(ref)
    if expected is None:
        return False
    got_doi = _normalized_doi(doi)
    if expected["doi"] and got_doi:
        # A declared DOI is stronger than a matching landing title.  Do not
        # expand a repository record that explicitly identifies another work.
        return expected["doi"] == got_doi
    if not expected["title"] or not title:
        return False
    try:
        from core.resolve import service as resolve_mod
        score = resolve_mod._title_match_score(expected["title"], title)
    except Exception:
        score = 0.0
    return score is not None and score >= 0.90


def _metadata_values(metadata: dict, *keys: str) -> list[str]:
    out = []
    for key in keys:
        for row in metadata.get(key) or []:
            if isinstance(row, dict) and row.get("value"):
                out.append(str(row["value"]))
    return out


def _dspace7_pdf_urls(landing_url: str, get_fn, ref: dict | None) -> list[str]:
    match = _DSPACE_ITEM.search(urllib.parse.urlparse(landing_url).path)
    if not match:
        return []
    parsed = urllib.parse.urlparse(landing_url)
    root, item_id = f"{parsed.scheme}://{parsed.netloc}", match.group(1)
    item = _json_body(get_fn, f"{root}/server/api/core/items/{item_id}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    titles = _metadata_values(metadata, "dc.title")
    dois = _metadata_values(metadata, "dc.identifier.doi", "dc.identifier")
    if not any(_identity_matches(ref, title=title, doi=doi) for title in titles or [None] for doi in dois or [None]):
        return []
    bundles_url = _href((item.get("_links") or {}).get("bundles")) or f"{root}/server/api/core/items/{item_id}/bundles"
    urls: list[str] = []
    for bundle in _embedded_rows(_json_body(get_fn, bundles_url), "bundles"):
        if str(bundle.get("name") or "").upper() != "ORIGINAL":
            continue
        bits_url = _href((bundle.get("_links") or {}).get("bitstreams"))
        for bitstream in _embedded_rows(_json_body(get_fn, bits_url or ""), "bitstreams"):
            content = _href((bitstream.get("_links") or {}).get("content"))
            mime = bitstream.get("mimeType") or bitstream.get("mime_type")
            name = str(bitstream.get("name") or "")
            if content and not _supplementary(content, name) and (("pdf" in str(mime).lower()) if mime else _pdfish(content)):
                urls.append(content)
    return _dedupe(urls)


def _legacy_pdf_urls(landing_url: str, get_fn, ref: dict | None) -> list[str]:
    body, effective_url = _response_body_and_url(
        get_fn, landing_url, accept="text/html,application/xhtml+xml"
    )
    parser = _landing_parser(body or b"")
    if parser is None:
        return []
    titles = [value for name, value in parser.meta if name in _TITLE_META_NAMES]
    dois = [value for name, value in parser.meta if name in _DOI_META_NAMES]
    if not any(_identity_matches(ref, title=title, doi=doi) for title in titles or [None] for doi in dois or [None]):
        return []
    return _landing_pdf_urls(effective_url, body or b"")


def discover_landing(landing_url: str | None, *, get_fn, ref: dict | None = None) -> list[str]:
    """Expand only a recognised DSpace record after metadata corroboration."""
    url = _http_url(landing_url)
    if not url or _pdfish(url):
        return []
    path = urllib.parse.urlparse(url).path
    if _DSPACE_ITEM.search(path):
        return _dspace7_pdf_urls(url, get_fn, ref)
    if _LEGACY_HANDLE.search(path) or is_bare_handle_url(url):
        return _legacy_pdf_urls(url, get_fn, ref)
    return []


def _context(ref: dict) -> dict:
    identifiers = {"doi": ref["doi"]} if ref.get("doi") else {}
    return {key: value for key, value in {"provider": NAME, "title": ref.get("title"),
            "year": ref.get("year"), "identifiers": identifiers, "source_confidence": 0.9}.items()
            if value not in (None, "", {})}


def landing_items(url: str | None, *, get_fn, ref: dict | None = None, **kwargs) -> list[dict]:
    # A ref is required: no identity means a repository file is merely a PDF,
    # not evidence it belongs to the cited work.
    if not isinstance(ref, dict):
        return []
    return [{
        "method": NAME,
        "url": pdf_url,
        "kind": "pdf",
        "discovered_via": NAME,
        "provenance": [NAME, "landing_metadata"],
        "discovery_reason": "identity-matched repository landing PDF",
        "identity_context": _context(ref),
    }
            for pdf_url in discover_landing(url, get_fn=get_fn, ref=ref)]


def landing_to_pdf(url: str | None, *, get_fn, ref: dict | None = None, **kwargs) -> str | None:
    items = landing_items(url, get_fn=get_fn, ref=ref)
    return items[0]["url"] if items else None


def candidate_items(ref: dict, *, get_fn, **kwargs) -> list[dict]:
    return landing_items(ref.get("url") if isinstance(ref, dict) else None, get_fn=get_fn, ref=ref)
