# core/resolve/providers/issue_stanford_law_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Official single-page issue attestation for the Stanford Law Review."""

from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from ._issue_common import coordinate, target_status, text_key


ISSUE_ATTESTATION = {
    "provider": "stanford_law_review_official_issue",
    "rule_version": "issue-attestation/v1",
}
_JOURNAL = "Stanford Law Review"
_ISSN = "0038-9765"
_ALIASES = frozenset({text_key(_JOURNAL), text_key("Stan. L. Rev.")})
_ISSUE_HOST = "www.stanfordlawreview.org"
_PDF_HOST = "review.law.stanford.edu"


class _IssueHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_article = False
        self.article_count = 0
        self.in_jsonld = False
        self.jsonld_parts: list[str] = []
        self.jsonld: list[str] = []
        self.heading_depth = 0
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.has_pager = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {str(key).casefold(): str(value or "") for key, value in attrs}
        classes = set(attrs.get("class", "").split())
        if "facetwp-pager" in classes:
            self.has_pager = True
        if tag == "article" and {"post-type-print_issue", "loop-item"} <= classes:
            if self.in_article:
                raise ValueError("Stanford issue page nests article records")
            self.article_count += 1
            self.in_article = True
        if tag == "h2" and "hsw-m-title" in classes:
            self.heading_depth = 1
            self.heading_parts = []
        elif self.heading_depth:
            self.heading_depth += 1
        if (
            tag == "script"
            and self.in_article
            and attrs.get("type", "").casefold() == "application/ld+json"
        ):
            self.in_jsonld = True
            self.jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_jsonld:
            self.jsonld.append("".join(self.jsonld_parts))
            self.in_jsonld = False
        if self.heading_depth:
            self.heading_depth -= 1
            if self.heading_depth == 0:
                self.headings.append(" ".join("".join(self.heading_parts).split()))
        if tag == "article" and self.in_article:
            self.in_article = False

    def handle_data(self, data: str) -> None:
        if self.in_jsonld:
            self.jsonld_parts.append(data)
        if self.heading_depth:
            self.heading_parts.append(data)


def _canonical_url(value: object, host: str) -> str:
    url = html.unescape(str(value or "")).strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.casefold() != host or not parts.path:
        raise ValueError("Stanford issue member URL is not canonical")
    return url


def _year(value: object) -> int | None:
    match = re.search(r"\b(?:18|19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def supports_issue_attestation(ref: dict) -> bool:
    source_kind = ref.get("source_kind") or ref.get("source_type")
    return bool(
        source_kind in {"article_like", "article"}
        and text_key(coordinate(ref, "container")) in _ALIASES
        and str(coordinate(ref, "volume") or "").isdigit()
        and str(coordinate(ref, "issue") or "").isdigit()
    )


def _parse(body: str, *, volume: str, issue: str) -> list[dict]:
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 5_000_000:
        raise ValueError("Stanford issue response is missing or oversized")
    parser = _IssueHTML()
    parser.feed(body)
    parser.close()
    expected_heading = f"Volume {volume}, Issue {issue}"
    if parser.headings != [expected_heading] or parser.has_pager:
        raise ValueError("Stanford issue page is not a closed single-page TOC")
    if parser.article_count == 0 or len(parser.jsonld) != parser.article_count:
        raise ValueError("Stanford issue page has incomplete article metadata")
    members: list[dict] = []
    seen_urls: set[str] = set()
    for raw in parser.jsonld:
        payload = json.loads(raw)
        graph = payload.get("@graph") if isinstance(payload, dict) else None
        if not isinstance(graph, list):
            raise ValueError("Stanford issue JSON-LD graph is missing")
        issue_nodes = [item for item in graph if isinstance(item, dict) and item.get("@type") == "PublicationIssue"]
        article_nodes = [item for item in graph if isinstance(item, dict) and item.get("@type") == "ScholarlyArticle"]
        if len(issue_nodes) != 1 or len(article_nodes) != 1:
            raise ValueError("Stanford issue JSON-LD item is ambiguous")
        issue_node, article = issue_nodes[0], article_nodes[0]
        periodical = issue_node.get("isPartOf")
        if not isinstance(periodical, dict) or (
            issue_node.get("issueNumber") != f"Issue {issue}"
            or periodical.get("volumeNumber") != f"Volume {volume}"
            or periodical.get("name") != _JOURNAL
            or periodical.get("issn") != _ISSN
        ):
            raise ValueError("Stanford JSON-LD does not attest the requested issue")
        title = " ".join(str(article.get("name") or "").split())
        authors = article.get("author")
        first_author = None
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            first_author = " ".join(str(authors[0].get("name") or "").split()) or None
        landing = _canonical_url(article.get("url"), _ISSUE_HOST)
        pdf = _canonical_url(article.get("workExample"), _PDF_HOST)
        locator = str(article.get("pageStart") or "").strip() or None
        if not title or not first_author or not locator or landing in seen_urls:
            raise ValueError("Stanford issue article metadata is incomplete or duplicated")
        seen_urls.add(landing)
        record_key = "\x1f".join((volume, issue, landing))
        members.append({
            "record_id": "stanford-law-review:" + hashlib.sha256(record_key.encode()).hexdigest(),
            "title": title,
            "first_author": first_author,
            "year": _year(article.get("datePublished")),
            "journal": _JOURNAL,
            "volume": volume,
            "issue": issue,
            "locator": locator,
            "pmid": None,
            "pmcid": None,
            "doi": None,
            "url": pdf,
        })
    return members


def attest_issue(ref: dict) -> dict:
    volume = coordinate(ref, "volume")
    issue = coordinate(ref, "issue")
    if not volume or not issue:
        raise ValueError("Stanford issue coordinates are missing")
    url = f"https://{_ISSUE_HOST}/print/volume-{volume}/issue-{issue}/"
    from core.resolve import service as resolve_mod

    status, body, final_url = resolve_mod._get_with_final_url(url, accept="text/html")
    if not 200 <= int(status) < 300:
        raise RuntimeError(f"Stanford issue TOC returned HTTP {status}")
    if final_url != url:
        raise ValueError("Stanford issue TOC redirected away from its canonical URL")
    members = _parse(body, volume=volume, issue=issue)
    target, order = target_status(ref, members)
    return {
        "provider": ISSUE_ATTESTATION["provider"],
        "rule_version": ISSUE_ATTESTATION["rule_version"],
        "status": "complete",
        "reason": "official publisher page exposes one validated JSON-LD record per TOC article",
        "scope": "issue",
        "target_status": target,
        "target_member_order": order,
        "cited_container": coordinate(ref, "container"),
        "cited_volume": volume,
        "cited_issue": issue,
        "journal_title": _JOURNAL,
        "completeness_basis": "official_single_page_toc_exact_jsonld",
        "members": members,
        "sources": [{
            "role": "issue_toc",
            "url": url,
            "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }],
        "observations": [
            {"key": "issn", "value_type": "text", "text_value": _ISSN, "integer_value": None},
            {"key": "article_count", "value_type": "integer", "text_value": None, "integer_value": len(members)},
        ],
    }
