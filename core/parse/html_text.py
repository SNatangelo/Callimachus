#!/usr/bin/env python3
# core/parse/html_text.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical HTML -> text conversion for the citation parser (Phase 1).

A publisher HTML page (an article's own full-text page, or a landing page) is not
prose: it is chrome (nav/header/footer/cookie banners), metadata, and — somewhere
inside it — the article.  Turning that into text a citation parser can read means
finding the article, dropping the chrome, and NOT throwing away the two things a
citation marker can be made of in HTML: a raised ``<sup>`` and a link to a
bibliography entry (``<a href="#ref-3">3</a>``).  Both survive here verbatim (the
``<sup>`` tag literally, the numeric link as ``[3]``) so the existing extractors
(:mod:`core.parse.extractors.superscript`, the numeric scheme) find them exactly
as they would in a hand-written Markdown or DOCX manuscript.

Deliberately stdlib-only (``html.parser``, ``re``, ``json``, ``pathlib``), matching
the rest of ``core/``.  No third-party HTML/DOM library, and no JavaScript execution
— a JS-only single-page app is not silently returned as an empty parse, it is
reported as the explicit ``empty_body`` outcome.

The module is importable both as ``core.parse.html_text`` and standalone (the
``try/except ImportError`` fallbacks below mirror the pattern used across
``core/parse/extractors``).
"""

from __future__ import annotations

import codecs
from copy import deepcopy
import html as html_mod
from html.parser import HTMLParser
import json
from pathlib import Path
import re

try:
    from core.fetch.extraction import fetch_html
except ImportError:  # pragma: no cover - standalone execution
    import fetch_html  # type: ignore

try:
    from core.parse import boilerplate
except ImportError:  # pragma: no cover - standalone execution
    import boilerplate  # type: ignore

try:
    from core.parse.parsing_common import NUMBER_RUN
except ImportError:  # pragma: no cover - standalone execution
    from parsing_common import NUMBER_RUN  # type: ignore


# ---------------------------------------------------------------------------
# Publisher-family selector config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SELECTORS_PATH = _HERE / "config" / "html_selectors.json"

# Mirrors config/html_selectors.json; used when the file is missing or invalid so
# the converter never crashes, and by anyone importing this module without the repo
# layout around it.  Order matters: publisher families are tried most-specific
# first, "generic" last (see _select_container).
DEFAULT_SELECTOR_FAMILIES: list[dict] = [
    {"family": "springer_nature", "selectors": ["c-article-body", "main-content"]},
    {"family": "elsevier", "selectors": ["#body", ".Body"]},
    {"family": "wiley", "selectors": ["article__body"]},
    {"family": "mdpi", "selectors": ["html-body", "articlebody"]},
    {"family": "plos", "selectors": ["#artText"]},
    {"family": "oup_silverchair", "selectors": [".article-body", ".widget-ArticleFulltext"]},
    {"family": "taylor_francis", "selectors": [".hlFld-Fulltext"]},
    {"family": "arxiv_ar5iv", "selectors": ["ltx_document", "ltx_page_content"]},
    {"family": "wikipedia", "selectors": ["mw-content-text"]},
    {"family": "generic", "selectors": ["#content"]},
]

_SELECTORS_CACHE: list[dict] | None = None


def _parse_family_entry(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    family = entry.get("family")
    selectors = entry.get("selectors")
    if not isinstance(family, str) or not isinstance(selectors, list):
        return None
    clean = [s for s in selectors if isinstance(s, str) and s.strip()]
    return {"family": family, "selectors": clean} if clean else None


def load_selector_families(path: str | Path | None = None) -> list[dict]:
    """Load publisher-family selectors, falling back to defaults defensively.

    A missing file, invalid JSON, or a malformed entry never raises: the converter
    keeps working with :data:`DEFAULT_SELECTOR_FAMILIES` (or whatever prior valid
    entries were parsed) rather than failing the whole conversion over a config typo.
    """
    global _SELECTORS_CACHE
    target = Path(path) if path is not None else _SELECTORS_PATH
    if path is None and _SELECTORS_CACHE is not None:
        return deepcopy(_SELECTORS_CACHE)
    families = deepcopy(DEFAULT_SELECTOR_FAMILIES)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, list):
        parsed = [f for f in (_parse_family_entry(e) for e in raw) if f]
        if parsed:
            families = parsed
    if path is None:
        _SELECTORS_CACHE = deepcopy(families)
    return families


def reset_for_tests() -> None:
    global _SELECTORS_CACHE
    _SELECTORS_CACHE = None


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF8, "utf-8-sig"),
)
_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([a-zA-Z0-9_\-]+)', re.I)


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc, errors="replace")
            except (LookupError, UnicodeError):
                break
    match = _CHARSET_RE.search(raw[:4096])
    if match:
        try:
            return raw.decode(match.group(1).decode("ascii", "ignore"), errors="replace")
        except (LookupError, UnicodeError):
            pass
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Challenge / paywall / login gate
# ---------------------------------------------------------------------------

# Mirrors core.resolve.user_sources._challenge_marker: strong access-gate language
# that fetch_html's marker tuples do not carry verbatim (they lean on a title check
# instead).  Checked against the RAW visible text (chrome included, script/style
# stripped) because a login gate printed in a header must still win.
_LOGIN_CHALLENGE_RE = re.compile(
    r"\b(?:access\s+denied|please\s+(?:log\s*in|sign\s+in)|"
    r"(?:log|sign)\s+in\s+to\s+(?:continue|access)|captcha|"
    r"verify\s+you\s+are\s+human|enable\s+javascript|checking\s+your\s+browser|"
    r"security\s+check|unusual\s+traffic|cloudflare)\b",
    re.I,
)


def _visible_raw_text(decoded: str) -> str:
    without_payload = re.sub(
        r"(?is)<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", " ", decoded)
    return re.sub(r"(?s)<[^>]*>", " ", without_payload)


def _is_challenge_or_login(decoded: str) -> bool:
    # A challenge/login WALL (Cloudflare, a captcha, a "log in to continue"
    # interstitial) is withheld: there is no article behind it to extract. A
    # PAYWALL marker is deliberately NOT treated the same way. Full-text pages on
    # subscription platforms routinely carry "Subscribe" / "Access through your
    # institution" chrome in a header or footer even when the article body is
    # present, so is_paywalled_html would reject the very publisher pages this
    # converter exists to read. Paywall handling for a fetched page stays with the
    # fetch classifier (classify_html_content), which weighs it against the body.
    visible = re.sub(r"\s+", " ", html_mod.unescape(_visible_raw_text(decoded))).strip()
    if _LOGIN_CHALLENGE_RE.search(visible):
        return True
    return fetch_html.is_challenge_html(decoded)


# ---------------------------------------------------------------------------
# JSON-LD metadata
# ---------------------------------------------------------------------------

_JSONLD_SCRIPT_RE = re.compile(
    r"(?is)<script\b[^>]*type\s*=\s*[\"']application/ld\+json(?:\s*;[^\"']*)?[\"'][^>]*>(.*?)</script>")


def _jsonld_meta(decoded: str) -> dict:
    values: dict = {}
    for script in _JSONLD_SCRIPT_RE.findall(decoded):
        try:
            payload = json.loads(html_mod.unescape(script.strip()))
        except (TypeError, ValueError):
            continue
        items = payload if isinstance(payload, list) else [payload]
        stack = [item for item in items if isinstance(item, dict)]
        while stack:
            obj = stack.pop(0)
            for key in ("abstract", "name", "headline", "datePublished", "author", "identifier"):
                if key in obj and key not in values:
                    values[key] = obj[key]
            for value in obj.values():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(v for v in value if isinstance(v, dict))
    return values


# ---------------------------------------------------------------------------
# A minimal DOM: just enough tree to select a container and serialize it
# ---------------------------------------------------------------------------

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# Same chrome vocabulary as core.resolve.user_sources._HTML: page furniture that is
# never article prose.  MathML is dropped for a different reason (see _ser): its
# rendering leaves spurious digits behind that look like citation markers.
_SKIP_TAGS = {"script", "style", "nav", "footer", "form", "aside", "header"}
# A "with-sidebar" layout wraps article prose; only the sidebar itself is chrome.
_CHROME_RE = re.compile(
    r"(?:cookie|consent|paywall|login|toolbar|(?<!with[-_])sidebar|chrome|banner)",
    re.I,
)

_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _local(tag: str) -> str:
    """Strip an XML namespace prefix (``mml:math`` -> ``math``)."""
    return tag.rsplit(":", 1)[-1].lower()


class _TreeBuilder(HTMLParser):
    """Builds a minimal node tree: {tag, attrs, children, skip}.

    Not a spec-compliant DOM (no implied tags, no void-element auto-closing rules
    beyond the fixed list) — just enough structure to find a container by
    id/class/tag and serialize it back to text.  Mismatched or missing close tags
    are tolerated: an end tag closes the nearest matching open ancestor, or is
    ignored if none is open, so a somewhat malformed real-world page still parses
    instead of raising.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: dict = {"tag": "#root", "attrs": {}, "children": [], "skip": False}
        self._stack: list[dict] = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_d = {str(k).lower(): (str(v) if v is not None else "") for k, v in attrs}
        chrome = (attrs_d.get("class", "") + " " + attrs_d.get("id", "")).lower()
        skip = tag in _SKIP_TAGS or _local(tag) == "math" or bool(_CHROME_RE.search(chrome))
        node = {"tag": tag, "attrs": attrs_d, "children": [], "skip": skip}
        self._stack[-1]["children"].append(node)
        if tag not in _VOID_ELEMENTS:
            self._stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                return
        # unmatched close tag: ignore rather than corrupt the open stack

    def handle_data(self, data):
        if data:
            self._stack[-1]["children"].append(data)


def _iter_nonskip(node: dict):
    """Yield every element descendant in document order, never descending into
    a chrome/script/style/math subtree (those are excluded, root and all)."""
    for child in node.get("children", []):
        if isinstance(child, dict) and not child.get("skip"):
            yield child
            yield from _iter_nonskip(child)


def _plain_text_len(node) -> int:
    """Cheap richness score: total non-whitespace text under *node*, chrome
    and math excluded.  Used only to RANK candidates; the winner is re-rendered
    properly by :func:`_ser`."""
    total = 0
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            total += len(item.strip())
            continue
        if item.get("skip") or _local(item["tag"]) == "math":
            continue
        stack.extend(item.get("children", []))
    return total


def _link_text_len(node) -> int:
    total = 0
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            continue
        if item.get("skip"):
            continue
        if _local(item["tag"]) == "a":
            total += _plain_text_len(item)
            continue
        stack.extend(item.get("children", []))
    return total


# ---------------------------------------------------------------------------
# Serialization: node tree -> canonical text
# ---------------------------------------------------------------------------

_REF_HREF_RE = re.compile(r"#(?:ref|bib|b|cr|cite|r|reference|core-collateral)[-_]?\d", re.I)
_NUMBER_RUN_FULL_RE = re.compile(NUMBER_RUN)


def _ser_children(children: list, in_sup: bool, preserve_markers: bool = True) -> str:
    return "".join(_ser(c, in_sup, preserve_markers) for c in children)


def _ser(node, in_sup: bool = False, preserve_markers: bool = True) -> str:
    """Render a node (or a text leaf) back to text.

    Two rules exist only to keep a citation marker alive across the HTML->text
    boundary: a ``<sup>`` is re-emitted literally (so
    ``core.parse.extractors.superscript.convert`` still finds it downstream), and a
    numeric link to a bibliography anchor is re-bracketed as ``[n]`` (so the numeric
    scheme still finds it).  Neither applies to a link already INSIDE a ``<sup>``:
    ``<sup><a href="#B3">3</a></sup>`` must come out as ``<sup>3</sup>``, not
    ``<sup>[3]</sup>`` — the bracket is not a digit, and the sup regex feeding
    ``superscript.convert`` would then simply not match it.

    When *preserve_markers* is False (source mode: the text is a fetched source's
    body, read by the verifier and string-matched by the passage guard, not a
    manuscript to be re-parsed for citation markers) both rules are suppressed: a
    ``<sup>`` renders as its inner text with no tags, and a bibliography anchor
    renders as its visible text with no ``[n]`` rebracketing.
    """
    if isinstance(node, str):
        return node
    tag = _local(node["tag"])
    if node.get("skip") or tag == "math":
        return ""
    if tag == "br":
        return "\n"
    if tag == "sup":
        inner = _ser_children(node.get("children", []), True, preserve_markers)
        if not preserve_markers:
            return inner
        stripped = re.sub(r"\s+", " ", inner).strip()
        if stripped and len(stripped) <= 40 and "\n" not in inner:
            return f"<sup>{stripped}</sup>"
        return inner
    if tag == "a":
        text = _ser_children(node.get("children", []), in_sup, preserve_markers).strip()
        if in_sup:
            return text
        href = node.get("attrs", {}).get("href", "")
        if preserve_markers and text and _REF_HREF_RE.search(href) and _NUMBER_RUN_FULL_RE.fullmatch(text):
            return f"[{text}]"
        return text
    body = _ser_children(node.get("children", []), in_sup, preserve_markers)
    if tag in ("td", "th"):
        return body + " "
    if tag in _BLOCK_TAGS:
        prefix = "\n" if tag in _HEADING_TAGS else ""
        return prefix + body + "\n"
    return body


def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u00ad", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Container selection: fallback chain, richest wins at each level
# ---------------------------------------------------------------------------

def _parse_selector(sel: str) -> tuple[str, str]:
    sel = sel.strip()
    if sel.startswith("#"):
        return "id", sel[1:].strip().lower()
    if sel.startswith("."):
        return "class", sel[1:].strip().lower()
    return "auto", sel.lower()


def _selector_matches(node: dict, kind: str, token: str) -> bool:
    if not token:
        return False
    attrs = node.get("attrs", {})
    id_ = attrs.get("id", "").lower()
    cls = attrs.get("class", "").lower()
    if kind == "id":
        return token in id_
    if kind == "class":
        return token in cls
    # A publisher family is a SPECIFIC id/class signal, never a bare tag name.
    # Matching a generic tag ("article") here let a family short-circuit the
    # whole level on any page built from <article> teaser cards, hiding the real
    # <main> content behind a 30-char card. Selecting a bare <main>/<article> is
    # the job of the main_or_article fallback level, which ranks them globally.
    return token in id_ or token in cls


def _select_by_family(root: dict, families: list[dict]) -> tuple[str | None, dict | None]:
    """Level (a): try each publisher family in order; within a family, richest wins.

    The first family with ANY match wins the whole level — a later, more generic
    family (e.g. "generic" / "#content") never overrides an earlier, more specific
    publisher match just because it happens to enclose more text.
    """
    for family in families:
        name = str(family.get("family") or "family")
        parsed = [(k, t) for k, t in (_parse_selector(s) for s in family.get("selectors") or []) if t]
        if not parsed:
            continue
        best_node, best_len = None, 0
        for node in _iter_nonskip(root):
            if any(_selector_matches(node, kind, token) for kind, token in parsed):
                length = _plain_text_len(node)
                if length > best_len:
                    best_node, best_len = node, length
        if best_node is not None:
            return name, best_node
    return None, None


def _select_main_or_article(root: dict) -> dict | None:
    """Level (b): the richest <main>/<article>, preferring <main> on a tie
    (ports the same richest-wins idea as fetch_html.extract_page_text)."""
    best, best_key = None, (-1, -1)
    for node in _iter_nonskip(root):
        tag = _local(node["tag"])
        if tag not in ("main", "article"):
            continue
        key = (_plain_text_len(node), 1 if tag == "main" else 0)
        if key > best_key:
            best, best_key = node, key
    return best


def _select_text_density(root: dict) -> dict | None:
    """Level (c): the div/section with the most text and a low link density —
    a crude but stdlib-only stand-in for a readability algorithm."""
    best, best_key = None, (-1.0, -1.0)
    for node in _iter_nonskip(root):
        if _local(node["tag"]) not in ("div", "section"):
            continue
        length = _plain_text_len(node)
        if length < 200:
            continue
        density = _link_text_len(node) / length
        if density > 0.5:
            continue
        key = (float(length), -density)
        if key > best_key:
            best, best_key = node, key
    return best


def _select_body(root: dict) -> dict:
    """Level (d): the whole <body>, or the document root if there is none."""
    for node in _iter_nonskip(root):
        if _local(node["tag"]) == "body":
            return node
    return root


def _select_container(root: dict, families: list[dict]) -> tuple[str, dict]:
    name, node = _select_by_family(root, families)
    if node is not None:
        return f"family:{name}", node
    node = _select_main_or_article(root)
    if node is not None:
        return "main_or_article", node
    node = _select_text_density(root)
    if node is not None:
        return "text_density", node
    return "body_boilerplate_trim", _select_body(root)


# ---------------------------------------------------------------------------
# Structured bibliography
# ---------------------------------------------------------------------------

_REF_CONTAINER_TOKEN_RE = re.compile(r"(?:^|[\s_-])(?:ref-list|references|reference-list)(?:$|[\s_-])", re.I)
_REF_ITEM_ID_RE = re.compile(r"^(?:b|cr)\d", re.I)
_REF_ITEM_CLASS_RE = re.compile(r"c-article-references__item", re.I)
_LEADING_NUMBER_RE = re.compile(r"^(?:\[\d{1,4}\]|\d{1,4}[.)])\s*")


def _is_ref_container(node: dict) -> bool:
    if _local(node["tag"]) not in ("ol", "ul", "div", "section"):
        return False
    attrs = node.get("attrs", {})
    id_ = attrs.get("id", "").lower()
    cls = attrs.get("class", "").lower()
    if id_ == "references":
        return True
    return bool(_REF_CONTAINER_TOKEN_RE.search(f" {cls} ") or _REF_CONTAINER_TOKEN_RE.search(f" {id_} "))


def _collect_ref_items(root: dict) -> list[str]:
    """Find a marked-up reference list (``ol.references``, ``.ref-list``,
    ``li[id^=B]``/``li[id^=CR]``, ``.c-article-references__item``, ``#references
    li``) and return each entry's text in document order.

    Each collected ``<li>`` node is also marked ``skip`` so it is NOT rendered a
    second time in the inline body: the entries are emitted once, as the
    structured ``References`` block appended by :func:`to_canonical_text`.
    Without this the natural rendering of the list would remain in the body and
    its volume/issue numbers (``2018;12(3)``) would be misread as citation
    markers, producing spurious claims.
    """
    items: list[str] = []
    seen: set[int] = set()

    def walk(node: dict, in_container: bool) -> None:
        tag = _local(node["tag"])
        nested = in_container or _is_ref_container(node)
        if tag == "li" and id(node) not in seen:
            attrs = node.get("attrs", {})
            if nested or _REF_ITEM_ID_RE.match(attrs.get("id", "")) or _REF_ITEM_CLASS_RE.search(attrs.get("class", "")):
                text = re.sub(r"\s+", " ", _ser(node)).strip()
                if text:
                    seen.add(id(node))
                    items.append(text)
                    node["skip"] = True
                    return
        for child in node.get("children", []):
            if isinstance(child, dict) and not child.get("skip"):
                walk(child, nested)

    walk(root, False)
    return items


def _bibliography_block(items: list[str]) -> str | None:
    if not items:
        return None
    lines = ["References"]
    for i, raw_item in enumerate(items, start=1):
        cleaned = _LEADING_NUMBER_RE.sub("", raw_item).strip()
        if cleaned:
            lines.append(f"{i}. {cleaned}")
    return "\n".join(lines) if len(lines) > 1 else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def to_canonical_text(
    raw: bytes | str, *, base_url: str | None = None, preserve_markers: bool = True
) -> dict:
    """Convert a publisher HTML page to canonical text for the citation parser.

    Returns ``{"text", "meta", "outcome", "container"}``.  ``outcome`` is one of
    ``"ok"``, ``"challenge_or_login_page"``, ``"empty_body"``, ``"abstract_only"`` —
    always explicit, never a silently empty parse.  ``container`` records which
    level of the fallback chain produced the text (``"family:<name>"``,
    ``"main_or_article"``, ``"text_density"``, or ``"body_boilerplate_trim"``), or
    ``None`` when the page was rejected at the entry gate.

    *base_url* is accepted for callers that already have it (e.g. to resolve a
    ``citation_pdf_url``); this module does not need it to preserve citation
    markers, since it only ever inspects anchors' fragment identifiers.

    *preserve_markers* (default ``True``) controls whether citation markers survive
    the HTML->text boundary.  A MANUSCRIPT (the default) needs them preserved so the
    downstream citation extractors can still find them: a ``<sup>`` is re-emitted
    literally and a numeric bibliography anchor is rebracketed as ``[n]``, and a
    structured ``References`` block is appended.  A fetched SOURCE's body is read by
    the verifier and string-matched by the passage guard, so ``preserve_markers=False``
    renders ``<sup>`` as plain inner text, a bibliography anchor as its plain visible
    text (no ``[n]``), and does NOT append the ``References`` block — though a
    marked-up reference list is still detached from the inline flow so it is not
    rendered twice.
    """
    del base_url
    decoded = _decode(raw)

    meta = fetch_html.meta_map(decoded)
    jsonld = _jsonld_meta(decoded)
    if jsonld:
        meta = dict(meta)
        meta["jsonld"] = jsonld

    if _is_challenge_or_login(decoded):
        return {"text": "", "meta": meta, "outcome": "challenge_or_login_page", "container": None}

    builder = _TreeBuilder()
    try:
        builder.feed(decoded)
        builder.close()
    except Exception:
        pass  # HTMLParser is lenient; keep whatever was built from a truncated page

    families = load_selector_families()
    container_label, container_node = _select_container(builder.root, families)
    # Detach a marked-up reference list from the inline flow BEFORE serializing,
    # so it is emitted exactly once (as the structured block below) rather than
    # also being rendered here as prose the citation parser would misread.
    ref_items = _collect_ref_items(builder.root)
    text = _ser(container_node, False, preserve_markers) if container_node is not None else ""
    if container_label == "body_boilerplate_trim":
        watermark_lines = boilerplate.boilerplate_lines(text)
        if watermark_lines:
            text = boilerplate.strip_boilerplate(text, watermark_lines)
    text = _clean_text(text)

    alpha_len = sum(1 for c in text if c.isalpha())
    has_abstract_signal = bool(
        meta.get("citation_abstract") or meta.get("dc.description.abstract")
        or meta.get("dcterms.abstract") or jsonld.get("abstract")
    )
    if alpha_len < 200:
        outcome = "empty_body"
    elif not fetch_html.html_fulltext_ok(text) and (
        has_abstract_signal or re.search(r"(?im)^\s*abstract\s*[:\-]?\s*$", text)
    ):
        outcome = "abstract_only"
    else:
        outcome = "ok"

    biblio = _bibliography_block(ref_items) if preserve_markers else None
    if biblio:
        text = (text.rstrip() + "\n\n" + biblio).strip()

    return {"text": text, "meta": meta, "outcome": outcome, "container": container_label}
