#!/usr/bin/env python3
# core/fetch/extraction/fetch_html.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""HTML parsing helpers for deterministic source fetches."""

from __future__ import annotations

import html as _html
import json
import re
import urllib.parse
from html.parser import HTMLParser

HTML_FULLTEXT_MIN_CHARS = 1200
ABSTRACT_MIN_CHARS = 120
CINII_METADATA_MARKERS = (
    "ui search",
    "bibliographic information",
    "doi",
    "publisher",
    "journal",
    "citations",
    "export",
)
PAYWALL_MARKERS = (
    "purchase access",
    "buy article",
    "rent or buy",
    "subscribe to this journal",
    "get full access",
    "sign in to access",
    "login to access",
    "institutional access",
    "access through your institution",
    "subscription required",
)
CHALLENGE_MARKERS = (
    "just a moment",
    "cloudflare",
    "verifying your browser",
    "checking if the site connection is secure",
    "security verification",
    "verifica di sicurezza",
    "this site needs to review the security of your connection",
    "enable javascript and cookies to continue",
    "enable javascript to continue",
    "please enable cookies",
    "verify you are human",
    "prove you are human",
    "attention required",
    "awswafcookiedomainlist",
    "press and hold",
    "cookieabsent",
    "action/cookieabsent",
    "making sure you're not a bot",
    ".within.website",
    "anubis",
)
WEAK_CHALLENGE_MARKERS = (
    "cloudflare",
    # "anubis" is a common word (Egyptology, the malware-analysis sandbox); on its
    # own it must not flag a page. Listing it here (as with "cloudflare") requires a
    # corroborating challenge title before it counts. Real Anubis challenges also
    # carry a strong marker ("making sure you're not a bot") or ".within.website".
    "anubis",
)
CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "verifying your browser",
    "validate user",
    "attention required",
    "verify you are human",
    "prove you are human",
    "captcha",
    "cookieabsent",
    "making sure you're not a bot",
)
CAPTCHA_MARKER = "captcha"

_SUSTAINED_PARAGRAPH_MIN_CHARS = 80
_SUSTAINED_PARAGRAPHS_MIN = 5
_SUSTAINED_PARAGRAPHS_200_MIN = 3
_SUSTAINED_PARAGRAPHS_400_MIN = 1
_SHORT_ARTICLE_MIN_CHARS = 800
_SHORT_ARTICLE_PARAGRAPHS_200_MIN = 2
_SHORT_ARTICLE_PARAGRAPHS_400_MIN = 2

_PARAGRAPH_EXCLUDED_TAGS = {
    "aside",
    "button",
    "canvas",
    "dialog",
    "footer",
    "form",
    "header",
    "nav",
    "noscript",
    "script",
    "style",
    "svg",
    "template",
}
_PARAGRAPH_EXCLUDED_ROLES = {
    "banner",
    "complementary",
    "contentinfo",
    "dialog",
    "navigation",
    "search",
}
_PARAGRAPH_LAYOUT_ROOT_TAGS = {"html", "body", "main", "article"}
_PARAGRAPH_CHROME_TOKENS = {
    "advert",
    "advertisement",
    "breadcrumb",
    "comments",
    "consent",
    "cookie",
    "footer",
    "header",
    "menu",
    "modal",
    "navbar",
    "navigation",
    "popup",
    "promo",
    "related",
    "share",
    "sidebar",
    "social",
    "toolbar",
}


def html_fulltext_ok(text: str) -> bool:
    if len(text) < HTML_FULLTEXT_MIN_CHARS:
        return False
    alpha = sum(c.isalpha() for c in text)
    return alpha / len(text) >= 0.35


class _SustainedParagraphCollector(HTMLParser):
    """Collect visible ``<p>`` prose without consulting publisher selectors.

    This deliberately does less than the shared HTML parser. It is a bounded
    recovery probe for an explicitly cited webpage after the normal parser has
    already returned ``landing_page``; page chrome and executable payloads are
    excluded so navigation text cannot satisfy the prose thresholds.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._frames: list[tuple[str, bool]] = []
        self._blocked_depth = 0
        self._paragraph_chunks: list[str] | None = None

    @staticmethod
    def _is_excluded(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _PARAGRAPH_EXCLUDED_TAGS:
            return True
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        roles = set(re.findall(r"[a-z0-9]+", attr_map.get("role", "").lower()))
        if roles & _PARAGRAPH_EXCLUDED_ROLES:
            return True
        chrome_tokens = set(re.findall(
            r"[a-z0-9]+",
            " ".join((attr_map.get("id", ""), attr_map.get("class", ""))).lower(),
        ))
        return bool(
            tag not in _PARAGRAPH_LAYOUT_ROOT_TAGS
            and chrome_tokens & _PARAGRAPH_CHROME_TOKENS
        )

    def _finish_paragraph(self) -> None:
        if self._paragraph_chunks is None:
            return
        text = re.sub(r"\s+", " ", " ".join(self._paragraph_chunks)).strip()
        if text:
            self.paragraphs.append(text)
        self._paragraph_chunks = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        excluded = self._is_excluded(tag, attrs)
        self._frames.append((tag, excluded))
        if excluded:
            self._blocked_depth += 1
        if tag == "p" and self._blocked_depth == 0:
            self._finish_paragraph()
            self._paragraph_chunks = []
        elif tag == "br" and self._paragraph_chunks is not None and self._blocked_depth == 0:
            self._paragraph_chunks.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br" and self._paragraph_chunks is not None and self._blocked_depth == 0:
            self._paragraph_chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "p":
            self._finish_paragraph()
        for index in range(len(self._frames) - 1, -1, -1):
            if self._frames[index][0] != tag:
                continue
            removed = self._frames[index:]
            del self._frames[index:]
            self._blocked_depth = max(
                0,
                self._blocked_depth - sum(1 for _name, excluded in removed if excluded),
            )
            break

    def handle_data(self, data: str) -> None:
        if self._paragraph_chunks is not None and self._blocked_depth == 0:
            self._paragraph_chunks.append(data)

    def close(self) -> None:
        super().close()
        self._finish_paragraph()


def _sustained_paragraph_profile(html_text: str) -> dict:
    """Return a fail-closed prose candidate from distinct visible paragraphs."""
    collector = _SustainedParagraphCollector()
    try:
        collector.feed(html_text or "")
        collector.close()
    except Exception:
        # HTMLParser is intentionally tolerant, but a malformed input must never
        # turn a partial recovery probe into a silent full-text promotion.
        return {
            "eligible": False,
            "text": "",
            "candidate_text": "",
            "short_article_eligible": False,
            "chars": 0,
            "paragraphs_80": 0,
            "paragraphs_200": 0,
            "paragraphs_400": 0,
        }

    distinct = []
    seen = set()
    for paragraph in collector.paragraphs:
        if paragraph in seen:
            continue
        seen.add(paragraph)
        if len(paragraph) >= _SUSTAINED_PARAGRAPH_MIN_CHARS:
            distinct.append(paragraph)
    text = "\n\n".join(distinct)
    paragraphs_200 = sum(1 for paragraph in distinct if len(paragraph) >= 200)
    paragraphs_400 = sum(1 for paragraph in distinct if len(paragraph) >= 400)
    eligible = bool(
        len(distinct) >= _SUSTAINED_PARAGRAPHS_MIN
        and paragraphs_200 >= _SUSTAINED_PARAGRAPHS_200_MIN
        and paragraphs_400 >= _SUSTAINED_PARAGRAPHS_400_MIN
        and html_fulltext_ok(text)
    )
    short_article_eligible = bool(
        len(text) >= _SHORT_ARTICLE_MIN_CHARS
        and paragraphs_200 >= _SHORT_ARTICLE_PARAGRAPHS_200_MIN
        and paragraphs_400 >= _SHORT_ARTICLE_PARAGRAPHS_400_MIN
    )
    return {
        "eligible": eligible,
        "text": text if eligible else "",
        "candidate_text": text,
        "short_article_eligible": short_article_eligible,
        "chars": len(text),
        "paragraphs_80": len(distinct),
        "paragraphs_200": paragraphs_200,
        "paragraphs_400": paragraphs_400,
    }


def is_cinii_metadata_shell(base_url: str, page_text: str, html_text: str = "") -> bool:
    """Return whether a CiNii record is metadata chrome rather than article prose.

    CiNii record pages can be long enough to pass the generic HTML threshold even
    though their visible content is only navigation and bibliographic metadata.
    Keep this host-scoped and require several independent labels; a real article
    body with sustained prose is deliberately not rejected.
    """
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower().rstrip(".")
    if host not in {"cir.nii.ac.jp", "ci.nii.ac.jp"}:
        return False
    normalized = re.sub(r"\s+", " ", page_text or "").strip().lower()
    hits = [marker for marker in CINII_METADATA_MARKERS if marker in normalized]
    if len(hits) < 4 or len(normalized) >= 6000:
        return False

    body_sections = sum(
        bool(re.search(rf"\b{re.escape(marker)}\b", normalized))
        for marker in (
            "introduction",
            "background",
            "methods",
            "results",
            "discussion",
            "conclusion",
            "references",
        )
    )
    return body_sections <= 2


def abstract_ok(text: str) -> bool:
    if len(text) < ABSTRACT_MIN_CHARS:
        return False
    alpha = sum(c.isalpha() for c in text)
    return alpha / len(text) >= 0.30


def decode_html(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def decode_textual_body(body: bytes, content_type: str | None = None) -> str | None:
    ctype = (content_type or "").lower()
    head = (body or b"")[:512].lstrip().lower()
    if (
        "html" in ctype
        or "xml" in ctype
        or ctype.startswith("text/")
        or head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or head.startswith(b"<?xml")
        or b"<head" in head
        or b"<body" in head
    ):
        return decode_html(body)
    for marker in PAYWALL_MARKERS + CHALLENGE_MARKERS:
        if marker.encode("utf-8") in head:
            return decode_html(body)
    return None


def attrs(fragment: str) -> dict:
    out = {}
    for key, value in re.findall(
        r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(".*?"|\'.*?\'|[^\s>]+)',
        fragment,
        flags=re.DOTALL,
    ):
        value = value.strip().strip("\"'")
        out[key.lower()] = _html.unescape(value)
    return out


def meta_map(html_text: str) -> dict[str, list[str]]:
    meta = {}
    for tag in re.findall(r"<meta\b[^>]*>", html_text, flags=re.IGNORECASE | re.DOTALL):
        a = attrs(tag)
        key = (
            a.get("name")
            or a.get("property")
            or a.get("http-equiv")
            or a.get("itemprop")
            or ""
        ).strip().lower()
        content = a.get("content")
        if key and content:
            meta.setdefault(key, []).append(content.strip())
    return meta


def first_meta(meta: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        vals = meta.get(key.lower()) or []
        for val in vals:
            if val:
                return val
    return None


def page_title(html_text: str) -> str | None:
    m = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", html_text)
    if not m:
        return None
    title = strip_tags(m.group(1))
    return title or None


# Not every ``href`` names something fetchable. Publisher pages carry JavaScript
# handlers and un-substituted template placeholders in that attribute, and joining
# them to the base URL yields a plausible-looking address that is always a 404:
#
#   .../doi/pdf/10.1177/\"javascript:new ...
#   .../doi/pdf/10.1177/\"{$url}\
#
# Left in, each one costs a request against a host that is usually already
# rate-limited, and buries the real failures in the trace.
_NON_FETCHABLE_SCHEME_RE = re.compile(
    r"^\s*(?:javascript|mailto|tel|data|blob|about|file)\s*:", re.IGNORECASE)
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"[{}]|\$\{|<%|%>|\\\"|\\'")
_ANCILLARY_DOCUMENT_RE = re.compile(
    r"\b(?:supplement(?:ary|al)?|appendix|appendices|supporting[ _-]?information|"
    r"dataset|data[ _-]?set|suppl(?:ement)?[ _-]?file|si[ _-]?\d+)\b|"
    r"-supp\.pdf(?:$|[?#\s])",
    re.IGNORECASE,
)


def _is_fetchable_href(href: str) -> bool:
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return False
    if _NON_FETCHABLE_SCHEME_RE.match(href):
        return False
    # A quote or brace inside an href is markup that leaked through, not a path:
    # real URLs percent-encode those characters.
    if _TEMPLATE_PLACEHOLDER_RE.search(href):
        return False
    # A leading or trailing + indicates a JavaScript string-concatenation fragment,
    # not a real path. The + character is legal inside query strings but never at
    # the boundaries.
    if href.startswith("+") or href.endswith("+"):
        return False
    return True


def is_ancillary_document_url(url: str) -> bool:
    """Reject supplements and datasets before they enter the fetch queue."""
    return bool(_ANCILLARY_DOCUMENT_RE.search(urllib.parse.unquote(url or "")))


def wayback_original_url(url: str) -> str | None:
    """Return the original URL embedded in a Wayback replay URL, if present."""
    parsed = urllib.parse.urlsplit(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    replay = re.match(r"^/web/[^/]+/(https?://.+)$", parsed.path)
    if host != "web.archive.org" or not replay:
        return None
    original = replay.group(1)
    if parsed.query:
        original += "?" + parsed.query
    if parsed.fragment:
        original += "#" + parsed.fragment
    return original


def metadata_record_kind(url: str) -> str | None:
    """Classify deterministic record-only routes, including Wayback replays."""
    effective_url = wayback_original_url(url) or url
    parsed = urllib.parse.urlparse(effective_url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower()
    query = urllib.parse.parse_qs(parsed.query or "")
    if host in {"arxiv.org", "export.arxiv.org"} and path.startswith("/abs/"):
        return "arxiv abstract page"
    if (
        host == "pubmed.ncbi.nlm.nih.gov" and re.fullmatch(r"/\d+/?", path)
        or host in {"ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}
        and re.match(r"^/pubmed/\d+/?$", path)
    ):
        return "pubmed metadata record"
    if (
        (host == "ssrn.com" or host.endswith(".ssrn.com"))
        and (
            "/abstract=" in path
            or (
                path.endswith("/sol3/papers.cfm")
                and any(key.lower() in {"abstract_id", "abstractid"} for key in query)
            )
        )
    ):
        return "ssrn metadata record"
    return None


def _join_discovered_url(base_url: str, href: str) -> str:
    """Resolve links inside a raw Wayback replay against its original URL."""
    base = urllib.parse.urlsplit(base_url)
    host = (base.hostname or "").lower().rstrip(".")
    replay = re.match(r"^(/web/[^/]+/)(https?://.+)$", base.path)
    if host != "web.archive.org" or not replay:
        return urllib.parse.urljoin(base_url, href)

    href_parts = urllib.parse.urlsplit(href)
    if href_parts.scheme or href_parts.netloc or href.startswith("/web/"):
        return urllib.parse.urljoin(base_url, href)

    original_base = wayback_original_url(base_url) or replay.group(2)
    original_url = urllib.parse.urljoin(original_base, href)
    replay_prefix = urllib.parse.urlunsplit(
        (base.scheme, base.netloc, replay.group(1), "", "")
    )
    return replay_prefix + original_url


def extract_links(base_url: str, html_text: str) -> list[str]:
    urls = []
    seen = set()
    for tag in re.findall(r"<a\b[^>]*>", html_text, flags=re.IGNORECASE | re.DOTALL):
        href = attrs(tag).get("href")
        if not href or not _is_fetchable_href(href):
            continue
        url = _join_discovered_url(base_url, href)
        # Fragments identify a location in the same resource, not a distinct
        # fetch candidate.  Keep query strings significant (e.g. version or
        # access-token parameters), and preserve the first spelling returned.
        key = urllib.parse.urlunsplit(urllib.parse.urlsplit(url)._replace(fragment=""))
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def strip_tags(html_text: str) -> str:
    body = re.sub(r"(?is)<script\b.*?</script>", " ", html_text)
    body = re.sub(r"(?is)<style\b.*?</style>", " ", body)
    body = re.sub(r"(?is)<!--.*?-->", " ", body)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</(p|div|section|article|h[1-6]|li|tr|td)>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = _html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n\n", body)
    return body.strip()


def extract_page_text(html_text: str) -> str:
    # Delegates to the same DOM-based, publisher-selector-aware converter used for
    # user-provided manuscript HTML (core.parse.html_text), in "source mode"
    # (preserve_markers=False): plain prose, no literal <sup> tags, no injected
    # [n] rebracketing, no appended References block — a fetched page's body is
    # read by the verifier and string-matched by the passage guard, so citation
    # markers must NOT be introduced into it.
    #
    # Imported lazily: core.parse.html_text imports core.fetch.extraction.fetch_html at
    # module load time, so a module-level import here would be circular.
    try:
        from core.parse import html_text as _html_text_mod
    except ImportError:  # pragma: no cover - standalone execution
        import html_text as _html_text_mod  # type: ignore
    return _html_text_mod.to_canonical_text(html_text, preserve_markers=False)["text"]


def extract_abstract_with_source(
    html_text: str, meta: dict[str, list[str]]
) -> tuple[str | None, str | None]:
    """Extract an explicitly labelled abstract with its source type.

    Generic ``description``/``og:description`` metadata is frequently navigation,
    login, or catalogue copy, so it is never an abstract source. This keeps a
    page's opening chrome from becoming a made-up source tier.
    """
    meta_abstract = first_meta(
        meta,
        "citation_abstract",
        "dc.description.abstract",
        "dcterms.abstract",
    )
    if meta_abstract:
        text = strip_tags(meta_abstract)
        if abstract_ok(text):
            return text, "metadata"
    for script in re.findall(
        r"(?is)<script\b[^>]*type=[\"']application/ld\+json(?:\s*;[^\"']*)?[\"'][^>]*>(.*?)</script>",
        html_text,
    ):
        try:
            payload = json.loads(_html.unescape(script))
        except (TypeError, ValueError):
            continue
        records = payload if isinstance(payload, list) else [payload]
        stack = [record for record in records if isinstance(record, dict)]
        while stack:
            record = stack.pop(0)
            kind = record.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            is_article = any(
                str(value or "").lower() in {"article", "scholarlyarticle", "medicalscholarlyarticle"}
                for value in kinds
            )
            abstract = record.get("abstract")
            if is_article and isinstance(abstract, str):
                text = strip_tags(abstract)
                if abstract_ok(text):
                    return text, "metadata"
            for value in record.values():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(item for item in value if isinstance(item, dict))
    m = re.search(
        r'(?is)<(?:section|div)[^>]+(?:id|class)=["\'][^"\']*abstract[^"\']*["\'][^>]*>(.*?)</(?:section|div)>',
        html_text,
    )
    if m:
        text = strip_tags(m.group(1))
        if abstract_ok(text):
            return text, "visible"

    # Some publisher/catalogue pages keep the abstract in a plain paragraph
    # following an explicit heading rather than in citation metadata.
    heading = re.search(
        r"(?is)<h[1-6][^>]*>\s*abstract\s*</h[1-6]>\s*<(?:p|div)[^>]*>(.*?)</(?:p|div)>",
        html_text,
    )
    if heading:
        text = strip_tags(heading.group(1))
        if abstract_ok(text):
            return text, "visible"
    return None, None


def extract_abstract_text(html_text: str, meta: dict[str, list[str]]) -> str | None:
    return extract_abstract_with_source(html_text, meta)[0]


def _normalize_doi(value: object) -> str | None:
    text = _html.unescape(str(value or "")).strip().lower()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text).rstrip(".,;)")
    return text if text.startswith("10.") and "/" in text else None


def _identifier_values(meta: dict[str, list[str]], scheme: str) -> set[str]:
    keys = {
        "doi": ("citation_doi", "dc.identifier", "prism.doi", "doi"),
        "pmid": ("citation_pmid", "pmid", "dc.identifier"),
        "pmcid": ("citation_pmcid", "pmcid", "dc.identifier"),
    }[scheme]
    out = set()
    for key in keys:
        for value in meta.get(key, []):
            if scheme == "doi":
                doi = _normalize_doi(value)
                if doi:
                    out.add(doi)
            elif scheme == "pmcid":
                out.update(match.upper() for match in re.findall(r"PMC\d+", str(value or ""), re.I))
            elif key == "dc.identifier":
                out.update(re.findall(r"\bpmid\s*[:=]?\s*(\d{5,9})\b", str(value or ""), re.I))
            else:
                out.update(re.findall(r"\d{5,9}", str(value or "")))
    return out


def _first_author_key(ref: dict) -> str | None:
    value = str(ref.get("ay_surname") or "").strip().lower()
    if value:
        return value
    raw = str(ref.get("raw_entry") or "")
    match = re.match(r"\s*([A-Za-zÀ-ÿ][\w'’.-]+)", raw)
    return match.group(1).lower() if match else None


def _metadata_year(meta: dict[str, list[str]]) -> str | None:
    for key in ("citation_publication_date", "citation_date", "dc.date", "prism.publicationdate"):
        value = first_meta(meta, key)
        match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
        if match:
            return match.group(0)
    return None


def _title_match(left: str | None, right: str | None) -> bool:
    left_tokens = set(re.findall(r"[a-z0-9]+", str(left or "").lower()))
    right_tokens = set(re.findall(r"[a-z0-9]+", str(right or "").lower()))
    return bool(len(left_tokens) >= 3 and left_tokens == right_tokens)


def _doi_from_url(url: str | None) -> str | None:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"doi.org", "dx.doi.org"}:
        return None
    return _normalize_doi(urllib.parse.unquote(parsed.path.lstrip("/")))


def _canonical_route(url: str | None) -> str | None:
    """Canonicalize a cited HTTP route without broadening it to a host match."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = urllib.parse.unquote(parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _cited_route_match(ref: dict, requested_url: str | None, final_url: str | None) -> bool:
    """Return a strict route match; sharing a publisher host is not enough."""
    cited_route = _canonical_route(ref.get("url"))
    if cited_route and cited_route in {
        _canonical_route(requested_url),
        _canonical_route(final_url),
    }:
        return True
    return bool(
        _normalize_doi(ref.get("doi"))
        and _doi_from_url(requested_url) == _normalize_doi(ref.get("doi"))
    )


def _normalize_identifier(scheme: str, value: object) -> str | None:
    if scheme == "doi":
        return _normalize_doi(value)
    text = str(value or "").strip()
    if scheme == "pmid":
        return text if re.fullmatch(r"\d{5,9}", text) else None
    if scheme == "pmcid":
        text = text.upper()
        return text if re.fullmatch(r"PMC\d+", text) else None
    return None


def _resolved_identifier_value(resolved_identifier: dict, scheme: str) -> str | None:
    if str(resolved_identifier.get("type") or "").lower() != scheme:
        return None
    return _normalize_identifier(scheme, resolved_identifier.get("value"))


def _expected_identity_sets(
    ref: dict,
    resolve_result: dict,
    candidate_context: dict | None,
) -> tuple[dict[str, set[str]], dict[str, str], dict[str, set[str]]]:
    """Choose one defensible identity tier for each identifier scheme.

    Parsed citation identifiers are authoritative.  With no citation identifier,
    a resolver identifier is stronger than provider-local candidate metadata; a
    candidate is considered only when the resolver established no identifier at
    all.  Mixing these tiers would let a weak candidate override a known work.
    """
    schemes = ("doi", "pmid", "pmcid")
    resolved_identifier = resolve_result.get("resolved_identifier") or {}
    context_ids = (candidate_context or {}).get("identifiers") or {}
    reference = {
        scheme: _normalize_identifier(scheme, ref.get(scheme))
        for scheme in schemes
    }
    resolver = {
        scheme: (
            _resolved_identifier_value(resolved_identifier, scheme)
            or _normalize_identifier(scheme, resolve_result.get(scheme))
        )
        for scheme in schemes
    }
    resolver_has_identifier = any(resolver.values())
    expected: dict[str, set[str]] = {}
    sources: dict[str, str] = {}
    authoritative: dict[str, set[str]] = {}
    for scheme in schemes:
        if reference[scheme]:
            expected[scheme] = {reference[scheme]}
            authoritative[scheme] = {reference[scheme]}
            sources[scheme] = "reference"
        elif resolver[scheme]:
            expected[scheme] = {resolver[scheme]}
            sources[scheme] = "resolver"
        elif not resolver_has_identifier:
            candidate = _normalize_identifier(scheme, context_ids.get(scheme))
            expected[scheme] = {candidate} if candidate else set()
            if candidate:
                sources[scheme] = "candidate"
        else:
            expected[scheme] = set()
    return expected, sources, authoritative


def landing_identity_profile(
    ref: dict,
    resolve_result: dict | None,
    meta: dict[str, list[str]],
    candidate_context: dict | None = None,
    requested_url: str | None = None,
    final_url: str | None = None,
) -> dict:
    """Compare landing-page citation identifiers to the work being fetched.

    A declared citation DOI/PMID/PMCID is stronger than a title spelling.  A
    conflicting declared identifier is therefore terminal for that landing and
    must not lend its abstract or PDF links to the requested reference.
    """
    resolve_result = resolve_result or {}
    expected, expected_sources, authoritative = _expected_identity_sets(
        ref, resolve_result, candidate_context
    )
    observed = {scheme: _identifier_values(meta, scheme) for scheme in expected}
    title = first_meta(meta, "citation_title", "dc.title", "og:title", "twitter:title")
    title = strip_tags(title) if title else None
    title_match = any(
        _title_match(title, expected_title)
        for expected_title in (ref.get("title"), resolve_result.get("matched_title"))
        if expected_title
    )
    matched_scheme = next(
        (scheme for scheme in ("doi", "pmid", "pmcid") if expected[scheme] & observed[scheme]),
        None,
    )
    requested_doi = _doi_from_url(requested_url)
    redirect_doi_alias = bool(
        not matched_scheme
        and requested_doi
        and requested_doi in authoritative.get("doi", set())
        and len(observed["doi"]) == 1
        and title_match
        and str(final_url or "") != str(requested_url or "")
        and _doi_from_url(final_url) is None
    )
    if redirect_doi_alias:
        matched_scheme = "doi_redirect_alias"
    authoritative_conflicts = [
        scheme for scheme in ("doi", "pmid", "pmcid")
        if authoritative.get(scheme) and observed[scheme]
        and not (authoritative[scheme] & observed[scheme])
    ]
    conflicts = [
        scheme for scheme in ("doi", "pmid", "pmcid")
        if expected[scheme] and observed[scheme] and not (expected[scheme] & observed[scheme])
    ]
    if redirect_doi_alias:
        authoritative_conflicts = [scheme for scheme in authoritative_conflicts if scheme != "doi"]
        conflicts = [scheme for scheme in conflicts if scheme != "doi"]
    conflict_scheme = (authoritative_conflicts or conflicts or [None])[0]
    cited_author = _first_author_key(ref)
    landing_authors = " ".join(
        value.lower() for key in ("citation_author", "dc.creator", "author")
        for value in meta.get(key, [])
    )
    expected_year = str(ref.get("year") or resolve_result.get("year") or "").strip()
    author_year_match = bool(
        cited_author
        and re.search(rf"(?<![A-Za-zÀ-ÿ]){re.escape(cited_author)}(?![A-Za-zÀ-ÿ])", landing_authors)
        and expected_year
        and expected_year == _metadata_year(meta)
    )
    return {
        "status": "conflict" if conflict_scheme else "matched" if matched_scheme else "unconfirmed",
        "matched_scheme": None if conflict_scheme else matched_scheme,
        "conflict_scheme": conflict_scheme,
        "canonical_doi": (
            next(iter(observed["doi"]), None)
            if redirect_doi_alias else next(iter(expected["doi"] & observed["doi"]), None)
        ),
        "title": title,
        "title_match": title_match,
        "author_year_match": author_year_match,
        "cited_route_match": _cited_route_match(ref, requested_url, final_url),
        "article_id": first_meta(meta, "citation_article_id", "citation_id", "article_id"),
        "pdf_url": (
            urllib.parse.urljoin(
                final_url or requested_url or "",
                first_meta(meta, "citation_pdf_url", "pdf_url") or "",
            )
            if first_meta(meta, "citation_pdf_url", "pdf_url") else None
        ),
        "expected_identifiers": any(expected.values()),
        "expected_identifier_sources": expected_sources,
        "observed_identifiers": {key: sorted(values) for key, values in observed.items() if values},
    }


def is_paywalled_html(html_text: str) -> bool:
    low = html_text.lower()
    return any(marker in low for marker in PAYWALL_MARKERS)


def _challenge_shell_markers(html_text: str) -> list[str]:
    """Return only compound fingerprints for known otherwise-empty shells."""
    meta = meta_map(html_text)
    titles = {
        re.sub(r"\s+", " ", str(bit or "")).strip().lower()
        for bit in (
            page_title(html_text),
            first_meta(meta, "og:title", "twitter:title", "citation_title", "dc.title"),
        )
        if bit
    }
    low = html_text.lower()
    markers = []
    if "client challenge" in titles and "/_fs-ch-" in low:
        markers.append("fastly client challenge")
    if "project muse -- verification required!" in titles:
        markers.append("project muse verification required")
    return markers


def is_challenge_html(html_text: str) -> bool:
    markers = challenge_markers_in_html(html_text)
    if any(marker not in WEAK_CHALLENGE_MARKERS for marker in markers):
        return True
    meta = meta_map(html_text)
    title_bits = [
        page_title(html_text),
        first_meta(meta, "og:title", "twitter:title", "citation_title", "dc.title"),
    ]
    low_titles = " \n".join(bit.lower() for bit in title_bits if bit)
    if not low_titles:
        return False
    return any(marker in low_titles for marker in CHALLENGE_TITLE_MARKERS)


def is_challenge_url(url: str | None) -> bool:
    """Recognize challenge redirects whose HTML body has no useful marker."""
    low = str(url or "").lower()
    try:
        parsed = urllib.parse.urlsplit(low)
    except ValueError:
        parsed = None
    if (
        parsed is not None
        and (parsed.hostname or "") == "opil.ouplaw.com"
        and parsed.path.rstrip("/") == "/oidc_callback"
    ):
        return True
    return any(marker in low for marker in (
        "/action/cookieabsent", "cookieabsent", "/captcha", "/challenge",
        # ".within.website" is Anubis's distinctive challenge domain; a bare
        # "anubis" substring in a URL slug is not a challenge signal.
        "/.within.website/", ".within.website",
    ))


# Cookie-gate markers: the recoverable subset of a challenge. An Atypon/Literatum
# publisher (SIAM, SagePub, many others) bounces a cookie-less request to
# `/action/cookieAbsent` instead of serving the page. Unlike a Cloudflare JS
# challenge or a CAPTCHA, this only wants a session cookie the server itself
# issues, so replaying the request with a warmed-up cookie jar can clear it.
COOKIE_GATE_MARKERS = (
    "cookieabsent",
    "action/cookieabsent",
)
_COOKIE_GATE_NEUTRAL_MARKERS = frozenset(
    (
        *COOKIE_GATE_MARKERS,
        "please enable cookies",
        "enable javascript and cookies to continue",
    )
)


def is_cookie_gate(url: str | None = None, *, markers=(), html_text: str = "") -> bool:
    """Whether a blocked response is specifically a cookie gate (retry-able via a
    cookie handshake), as opposed to a JS/Cloudflare challenge or CAPTCHA."""
    hay = {str(m).strip().casefold() for m in markers}
    if html_text:
        hay.update(
            str(marker).strip().casefold()
            for marker in challenge_markers_in_html(html_text)
        )
    if hay - _COOKIE_GATE_NEUTRAL_MARKERS:
        return False
    if hay.intersection(COOKIE_GATE_MARKERS):
        return True
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.path.casefold() == "/action/cookieabsent"


def challenge_markers_in_html(html_text: str) -> list[str]:
    low = html_text.lower()
    markers = [marker for marker in CHALLENGE_MARKERS if marker in low]
    markers.extend(_challenge_shell_markers(html_text))
    if _captcha_is_contextual(html_text):
        markers.append(CAPTCHA_MARKER)
    return markers


def _captcha_is_contextual(html_text: str) -> bool:
    """Treat generic captcha mentions as challenges only in challenge context.

    Uses the raw-html ``strip_tags`` extraction rather than ``extract_page_text``:
    this function sits underneath the challenge/login-page determination
    (``challenge_markers_in_html`` -> ``is_challenge_html``), and ``extract_page_text``
    now delegates to ``html_text.to_canonical_text``, which itself calls back into
    ``is_challenge_html`` to decide whether to withhold the body — calling
    ``extract_page_text`` from here would recurse into that check indefinitely.
    ``strip_tags`` is also arguably more correct for this purpose: a captcha widget
    can sit in page chrome that a main-content selector would exclude.
    """
    meta = meta_map(html_text)
    title_bits = [
        page_title(html_text),
        first_meta(meta, "og:title", "twitter:title", "citation_title", "dc.title"),
    ]
    if any(CAPTCHA_MARKER in str(bit or "").lower() for bit in title_bits):
        return True
    visible = strip_tags(html_text).lower()
    if re.search(rf"\b{CAPTCHA_MARKER}\b", visible):
        return True
    return bool(
        re.search(
            rf"<form\b[^>]*\b(?:action|class|id|name)\s*=\s*['\"][^'\"]*{CAPTCHA_MARKER}",
            html_text,
            re.I,
        )
    )


def identity_corroboration_text(text: str, meta: dict[str, list[str]]) -> str:
    pieces = [text]
    for key in ("citation_title", "dc.title", "og:title", "citation_doi", "dc.identifier"):
        val = first_meta(meta, key)
        if val:
            pieces.append(val)
    return "\n".join(pieces)


def identity_probe_text(ref: dict, text: str, meta: dict[str, list[str]], *, corroborate_fn):
    return corroborate_fn(ref, identity_corroboration_text(text, meta))


def _landing_anchor_is_correlated(
    base_url: str,
    target_url: str,
    meta: dict[str, list[str]],
) -> bool:
    """Keep generic PDF anchors only when they belong to the landing work.

    Publisher pages often link PDF references, reports, and navigation assets.
    Same-host links are retained conservatively; cross-host links need the DOI
    declared by the landing page in their URL.  A Wayback replay is a special
    cross-origin container, so its shared archive host is never corroboration.
    """
    base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower().rstrip(".")
    target_host = (urllib.parse.urlsplit(target_url).hostname or "").lower().rstrip(".")
    if base_host.startswith("www."):
        base_host = base_host[4:]
    if target_host.startswith("www."):
        target_host = target_host[4:]
    if base_host and base_host != "web.archive.org" and base_host == target_host:
        return True

    decoded_target = urllib.parse.unquote(target_url).lower()
    return any(doi in decoded_target for doi in _identifier_values(meta, "doi"))


def _same_origin(left_url: str, right_url: str) -> bool:
    """Return whether two URLs have the same scheme, host, and effective port."""
    try:
        left = urllib.parse.urlsplit(left_url)
        right = urllib.parse.urlsplit(right_url)
        left_port = left.port or (443 if left.scheme.lower() == "https" else 80)
        right_port = right.port or (443 if right.scheme.lower() == "https" else 80)
    except (TypeError, ValueError):
        return False
    return (
        bool(left.scheme and left.hostname and right.scheme and right.hostname)
        and left.scheme.lower() in {"http", "https"}
        and left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower().rstrip(".")
        == (right.hostname or "").lower().rstrip(".")
        and left_port == right_port
    )


def enqueue_landing_candidates(
    queue: list[dict],
    seen: set[str],
    base_url: str,
    html_text: str,
    meta: dict[str, list[str]],
    *,
    kind_from_url,
    looks_pdf_url,
):
    def without_fragment(url: str) -> str:
        return urllib.parse.urlunsplit(urllib.parse.urlsplit(url)._replace(fragment=""))

    # Callers may have seeded ``seen`` with URLs including fragments.
    seen_keys = {without_fragment(url) for url in seen}
    extras = []
    for key in (
        "citation_pdf_url",
        "pdf_url",
        "citation_fulltext_html_url",
        "citation_abstract_html_url",
        "citation_public_url",
    ):
        val = first_meta(meta, key)
        # Filter the raw meta value the same way extract_links filters raw hrefs:
        # reject template placeholders and other non-fetchable fragments before joining.
        if val and _is_fetchable_href(val):
            extras.append((_join_discovered_url(base_url, val), False))
    for url in extract_links(base_url, html_text):
        if looks_pdf_url(url) and _landing_anchor_is_correlated(base_url, url, meta):
            extras.append((url, _same_origin(base_url, url)))
    for url, same_origin_anchor_pdf in extras:
        if is_ancillary_document_url(url):
            continue
        key = without_fragment(url)
        if key in seen_keys:
            continue
        seen.add(key)
        seen_keys.add(key)
        item = {
            "method": "landing_link",
            "url": url,
            "kind": kind_from_url(url),
        }
        # This records an actual same-origin PDF anchor.  Metadata links and
        # host-only correlations deliberately do not receive this provenance.
        if same_origin_anchor_pdf and item["kind"] == "pdf":
            item.update({
                "cited_landing_pdf": True,
                "cited_landing_url": base_url,
            })
        queue.append(item)
