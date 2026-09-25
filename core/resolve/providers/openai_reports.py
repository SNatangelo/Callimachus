#!/usr/bin/env python3
# core/resolve/providers/openai_reports.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official OpenAI research-report adapter.

This is a provider-family adapter, rather than a fetch-pipeline exception: the
registry discovers it like every other provider and future official report URLs
can use the same narrow matching rules.
"""

from __future__ import annotations

from html.parser import HTMLParser
import html
import re
import urllib.parse

NAME = "openai_reports"
RESOLVE_NAME = "openai_reports_search"
# It is an intentionally narrow fallback after general metadata sources.
OPTIONAL_STAGE = True
MANIFEST = {
    "origin": NAME,
    "canonical_hosts": ["openai.com", "cdn.openai.com"],
    "via_aliases": [RESOLVE_NAME],
}

_OFFICIAL_HOSTS = ("openai.com", "cdn.openai.com")
_SUPPLEMENT = re.compile(r"\b(?:supplement(?:ary)?|appendi[xc]es?|dataset|data[ _-]?set)\b", re.I)
_INDEX_URL = "https://openai.com/research/"
# This intentionally closed list tolerates bibliographic elision only for
# English function words.  Content-word order remains part of the key.
_TITLE_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "with",
})
_KNOWN_REPORTS = (
    {
        "title": "Improving Language Understanding with Unsupervised Learning",
        "year": 2018,
        "first_author": "radford",
        "url": "https://cdn.openai.com/research-covers/language-unsupervised/language_understanding_paper.pdf",
        # The landing page uses the citation title but links directly to a PDF
        # whose report-cover title differs. This is an explicit catalogue fact,
        # not an inference from the common official host.
        "landing_page_url": "https://openai.com/index/language-unsupervised/",
        "expected_document_title": (
            "Improving Language Understanding by Generative Pre-Training"
        ),
        "official_document_relation": "official_landing_page_links_exact_document",
    },
    {
        "title": "Language Models are Unsupervised Multitask Learners",
        "year": 2019,
        "first_author": "radford",
        "url": "https://cdn.openai.com/better-language-models/language-models.pdf",
    },
)


def _official_url(value: str | None) -> str | None:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.scheme not in {"http", "https"} or not any(host == item or host.endswith("." + item) for item in _OFFICIAL_HOSTS):
        return None
    return url


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def ordered_content_title_key(value: object) -> tuple[str, ...]:
    """Return a conservative, order-preserving bibliographic title key."""
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if token not in _TITLE_FUNCTION_WORDS
    )


def is_function_word_elision(cited_title: object, catalogue_title: object) -> bool:
    """Return whether the cited title deletes only catalogue function words."""
    cited_tokens = _norm(cited_title).split()
    catalogue_tokens = _norm(catalogue_title).split()
    if not cited_tokens or not catalogue_tokens:
        return False
    cited_index = 0
    for token in catalogue_tokens:
        if cited_index < len(cited_tokens) and cited_tokens[cited_index] == token:
            cited_index += 1
        elif token not in _TITLE_FUNCTION_WORDS:
            return False
    return cited_index == len(cited_tokens)


def _strong_title(value: str | None) -> bool:
    return len(_norm(value).split()) >= 4


def _first_author(ref: dict, catalogue_title: str | None = None) -> str | None:
    raw = str(ref.get("raw_entry") or "")
    titles = (str(ref.get("title") or ""), str(catalogue_title or ""))
    matches = [
        match
        for title in titles
        if title
        if (match := re.search(re.escape(title), raw, re.IGNORECASE))
    ]
    if matches:
        raw = raw[:min(match.start() for match in matches)]
    first = re.split(r",|\s+(?:and|&)\s+", raw, maxsplit=1)[0]
    tokens = _norm(first).split()
    if tokens[-2:] == ["et", "al"]:
        tokens = tokens[:-2]
    return tokens[-1] if tokens else None


def _known_report(ref: dict) -> dict | None:
    title_key = ordered_content_title_key(ref.get("title"))
    if not title_key:
        return None
    try:
        year = int(ref.get("year")) if ref.get("year") is not None else None
    except (TypeError, ValueError):
        year = None
    matches = [
        report
        for report in _KNOWN_REPORTS
        if ordered_content_title_key(report["title"]) == title_key
    ]
    # A content-title collision is never resolved by weaker metadata: the
    # authoritative catalogue rule must be unique before it can be admitted.
    if len(matches) != 1:
        return None
    report = matches[0]
    if not is_function_word_elision(ref.get("title"), report["title"]):
        return None
    author = _first_author(ref, report["title"])
    if year is not None and year != report["year"]:
        return None
    if author and author != report["first_author"]:
        return None
    return report


def known_document_identity(
    ref: dict,
    source_ref: str | None,
    text: str,
) -> bool:
    """Confirm cached bytes through the closed official report catalogue.

    This is the context-free counterpart of Fetch's landing-page relation
    check.  It is intentionally available only for catalogue entries that
    declare an exact landing-to-document relation and rechecks both the exact
    official route and the PDF front matter.
    """
    report = _known_report(ref)
    if not report or not report.get("official_document_relation"):
        return False
    source = _official_url(source_ref)
    if not source or _norm(source) != _norm(report.get("url")):
        return False
    expected_tokens = set(_norm(report.get("expected_document_title")).split())
    if len(expected_tokens) < 3:
        return False
    head = str(text or "")[:6000]
    front = re.split(r"\babstract\b", head, maxsplit=1, flags=re.IGNORECASE)[0]
    front_norm = _norm(front)
    front_tokens = set(front_norm.split())
    if len(expected_tokens & front_tokens) / len(expected_tokens) < 0.9:
        return False
    return report["first_author"] in front_tokens


def _likely_openai_report(ref: dict) -> bool:
    blob = " ".join(str(ref.get(key) or "") for key in ("title", "raw_entry", "url", "venue", "publisher")).casefold()
    return bool(
        _known_report(ref)
        or "openai" in blob
        or "technical report" in blob
        or ref.get("source_kind") == "report_like"
        or _official_url(ref.get("url"))
    )


def supports(ref: dict) -> bool:
    from core.resolve import service as resolve_mod
    return bool(not ref.get("url") and resolve_mod._article_like_resolution_candidate(ref)
                and _likely_openai_report(ref) and _strong_title(resolve_mod._article_title_candidate(ref)))


class _IndexParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attrs = dict(attrs)
        self._href = attrs.get("href")
        self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._parts)))
            self._href, self._parts = None, []


def _index_candidates(body: bytes) -> list[tuple[str, str]]:
    parser = _IndexParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        return []
    out = []
    for href, label in parser.links:
        url = _official_url(urllib.parse.urljoin(_INDEX_URL, html.unescape(href)))
        if url and not _SUPPLEMENT.search(url + " " + label):
            out.append((url, re.sub(r"\s+", " ", html.unescape(label)).strip()))
    return out


def _report_pdf_url(landing: str) -> str | None:
    # The official index can link directly to a report PDF.  A research landing
    # is retained as a landing candidate; its normal fetch path parses standard
    # PDF metadata, so this adapter never guesses a file path.
    return landing if urllib.parse.urlparse(landing).path.lower().endswith(".pdf") else None


def discover(ref: dict) -> dict:
    from core.resolve import service as resolve_mod
    title = resolve_mod._article_title_candidate(ref)
    if not (_likely_openai_report(ref) and _strong_title(title)):
        return {"status": "unverified", "via": RESOLVE_NAME, "reason": "not a sufficiently specific OpenAI report citation"}
    known = _known_report(ref)
    if known:
        context = {
            "provider": NAME,
            "title": known["title"],
            "year": known["year"],
            "first_author": known["first_author"],
            "source_confidence": 1.0,
            "canonical_host": True,
        }
        if known.get("official_document_relation"):
            context.update({
                "official": True,
                "official_document_relation": known["official_document_relation"],
                "landing_page_url": known["landing_page_url"],
                "canonical_url": known["url"],
                "expected_document_title": known["expected_document_title"],
            })
        return {
            "status": "resolved", "via": RESOLVE_NAME,
            "matched_title": known["title"], "matched_year": known["year"],
            "retracted": False, "fulltext_exists": True, "oa_status": "open",
            "reason": "exact match in the official OpenAI report catalogue",
            "resolution_basis": "canonical_source", "existence_confidence": "high",
            "fulltext_links": [{"url": known["url"], "content_type": "pdf",
                                "identity_context": context}],
        }
    try:
        _status, body = resolve_mod._get(_INDEX_URL, accept="text/html")
    except Exception as exc:
        return {"status": "unresolved", "via": RESOLVE_NAME, "reason": f"network: {type(exc).__name__}"}
    expected = _norm(title)
    matches = [(url, label) for url, label in _index_candidates(body) if _norm(label) == expected]
    if len(matches) != 1:
        return {"status": "unverified", "via": RESOLVE_NAME,
                "reason": "no unique exact title match on the official OpenAI research index",
                "resolution_basis": "metadata_search", "existence_confidence": "low"}
    landing, matched_title = matches[0]
    context = {"provider": RESOLVE_NAME, "title": matched_title, "source_confidence": 0.98, "canonical_host": True}
    pdf = _report_pdf_url(landing)
    links = [{"url": pdf or landing, "content_type": "pdf" if pdf else "html", "identity_context": context}]
    return {"status": "resolved", "via": RESOLVE_NAME, "matched_title": matched_title,
            "retracted": False, "fulltext_exists": bool(pdf), "oa_status": "open",
            "reason": "unique exact title match on official OpenAI research index",
            "resolution_basis": "canonical_source", "existence_confidence": "high", "fulltext_links": links}


def candidate_items(ref: dict, **kwargs) -> list[dict]:
    """Use an explicitly cited official PDF; no guessed report filenames."""
    known = _known_report(ref) if isinstance(ref, dict) else None
    url = known["url"] if known else _official_url(ref.get("url") if isinstance(ref, dict) else None)
    if not url or not urllib.parse.urlparse(url).path.lower().endswith(".pdf") or _SUPPLEMENT.search(url):
        return []
    context = {
        "provider": NAME,
        "title": known["title"] if known else ref.get("title"),
        "year": known["year"] if known else ref.get("year"),
        "source_confidence": 0.98,
        "canonical_host": True,
    }
    if known and known.get("official_document_relation"):
        context.update({
            "official": True,
            "first_author": known["first_author"],
            "official_document_relation": known["official_document_relation"],
            "landing_page_url": known["landing_page_url"],
            "canonical_url": known["url"],
            "expected_document_title": known["expected_document_title"],
        })
    return [{"method": NAME, "url": url, "kind": "pdf",
             "discovery_reason": "exact official report catalogue match" if known else "explicit official report URL",
             "identity_context": context}]
