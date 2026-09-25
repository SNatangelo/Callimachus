# core/resolve/publisher_issue_attestation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic official issue-TOC attestation for The GW International Law Review."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from .journal_authority import _text_key
    from .providers._issue_common import target_status as _target_status
except ImportError:  # direct execution
    from resolve.journal_authority import _text_key
    from resolve.providers._issue_common import target_status as _target_status


NAME = "gwilr_official_issue"
RULE_VERSION = "publisher-issue-attestation/v1"
PUBLISHER_HOST = "www.thegwilr.org"
CANONICAL_ORIGIN = f"https://{PUBLISHER_HOST}"
_ALIASES = frozenset({
    _text_key("The George Washington International Law Review"),
    _text_key("Geo. Wash. Int’l L. Rev."),
})
_SECTIONS = frozenset({"Articles", "Notes", "Note", "Book Notes"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_HEADINGS = frozenset({f"h{number}" for number in range(1, 7)})


def _resolve_module():
    try:
        from core.resolve import service as resolve_mod
    except ImportError:  # direct execution
        from resolve import service as resolve_mod
    return resolve_mod


def _enabled(*, environ=None) -> bool:
    try:
        from core.resolve import provider_config
    except ImportError:  # direct execution
        from resolve import provider_config

    config = provider_config.load(environ=environ)
    return bool((config.get("providers", {}).get(NAME) or {}).get("enabled", True))


def _coordinate(ref: dict, kind: str) -> str | None:
    for item in ref.get("cited_coordinates") or ():
        if isinstance(item, dict) and item.get("kind") == kind:
            value = item.get("normalized_value") or item.get("raw_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _empty(ref: dict) -> dict:
    return {
        "rule_version": RULE_VERSION,
        "provider": NAME,
        "status": "not_applicable",
        "reason": "citation is outside the GWILR official issue-TOC scope",
        "scope": None,
        "target_status": "inconclusive",
        "target_member_order": None,
        "cited_container": _coordinate(ref, "container"),
        "cited_volume": _coordinate(ref, "volume"),
        "cited_issue": _coordinate(ref, "issue"),
        "journal_title": None,
        "publisher_host": PUBLISHER_HOST,
        "issue_url": None,
        "response_sha256": None,
        "attestation_basis": None,
        "section_count": None,
        "members": [],
    }


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[object] = field(default_factory=list)


class _Tree(HTMLParser):
    """A deliberately strict, small DOM sufficient for the static TOC format."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.error: str | None = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.casefold()
        node = _Node(tag, {str(key).casefold(): str(value or "") for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str):
        tag = tag.casefold()
        if tag in _VOID:
            return
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            self.error = self.error or f"malformed HTML end tag {tag!r}"
            return
        self.stack.pop()

    def handle_data(self, data: str):
        self.stack[-1].children.append(data)

    def close(self):
        super().close()
        if len(self.stack) != 1:
            self.error = self.error or "malformed HTML has unclosed elements"


def _text(value: _Node | str) -> str:
    if isinstance(value, str):
        return value
    return "".join(_text(child) for child in value.children)


def _clean(value: str) -> str:
    return " ".join(value.split())


def _walk(node: _Node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _content_blocks(root: _Node) -> list[_Node]:
    blocks = []
    for node in _walk(root):
        if node.tag != "div" or "sqs-html-content" not in node.attrs.get("class", "").split():
            continue
        direct = [child for child in node.children if isinstance(child, _Node)]
        has_known_section = any(
            child.tag in _HEADINGS and _clean(_text(child)) in _SECTIONS
            for child in direct
        )
        has_issue_item = any(
            child.tag == "p"
            and "(pdf)" in _clean(_text(child)).casefold()
            and any(
                descendant.tag == "em"
                and _clean(_text(descendant)).startswith("by ")
                for descendant in _walk(child)
            )
            for child in direct
        )
        if has_known_section or has_issue_item:
            blocks.append(node)
    return blocks


def _canonical_url(href: str) -> str:
    resolved = urljoin(CANONICAL_ORIGIN + "/", href)
    parts = urlsplit(resolved)
    if parts.scheme != "https" or parts.netloc != PUBLISHER_HOST:
        raise ValueError("issue member PDF link leaves the canonical publisher host")
    return urlunsplit(("https", PUBLISHER_HOST, parts.path, parts.query, ""))


def _member(paragraph: _Node, *, section: str, ref: dict) -> dict:
    nodes = paragraph.children
    br_positions = [index for index, node in enumerate(nodes) if isinstance(node, _Node) and node.tag == "br"]
    em_positions = [index for index, node in enumerate(nodes) if isinstance(node, _Node) and node.tag == "em"]
    if len(br_positions) != 1 or len(em_positions) != 1 or em_positions[0] <= br_positions[0]:
        raise ValueError("issue paragraph lacks one title/PDF/br/author structure")
    br_index, em_index = br_positions[0], em_positions[0]
    if any(_clean(_text(node)) for node in nodes[br_index + 1:em_index]):
        raise ValueError("issue paragraph has content between br and author line")
    if any(_clean(_text(node)) for node in nodes[em_index + 1:]):
        raise ValueError("issue paragraph has content after author line")
    prefix = nodes[:br_index]
    allowed = {"a"}
    if any(isinstance(node, _Node) and node.tag not in allowed for node in prefix):
        raise ValueError("issue paragraph has an unparsed title element")
    pdf_links = [node for node in prefix if isinstance(node, _Node) and node.tag == "a"]
    if len(pdf_links) > 1 or any(any(isinstance(item, _Node) for item in link.children) for link in pdf_links):
        raise ValueError("issue paragraph has an ambiguous PDF link")
    prefix_text = _clean("".join(_text(node) for node in prefix))
    if not re.search(r"\(\s*PDF\s*\)", prefix_text):
        raise ValueError("issue paragraph lacks its PDF marker")
    title = _clean(re.sub(r"\(\s*PDF\s*\)", "", prefix_text))
    author_line = _clean(_text(nodes[em_index]))
    if not title or not author_line.startswith("by ") or not author_line[3:].strip():
        raise ValueError("issue paragraph lacks a nonempty by-author line")
    author = author_line[3:].strip()
    first_author = re.split(r"\s+(?:and|&)\s*|\s*,\s*", author, maxsplit=1)[0].strip()
    if not first_author:
        raise ValueError("issue paragraph has no first author")
    url = None
    if pdf_links:
        href = pdf_links[0].attrs.get("href", "").strip()
        if not href:
            raise ValueError("issue PDF link lacks href")
        url = _canonical_url(href)
    record_key = "\x1f".join(_text_key(value) for value in (section, title, author))
    return {
        "record_id": "gwilr:" + hashlib.sha256(record_key.encode("utf-8")).hexdigest(),
        "section": section,
        "title": title,
        "first_author": first_author,
        "author_line": author_line,
        # The issue page does not state a publication year.  Never turn the
        # citation's own claim into publisher-attested metadata.
        "year": None,
        "journal": "The George Washington International Law Review",
        "volume": _coordinate(ref, "volume"),
        "issue": _coordinate(ref, "issue"),
        "locator": None,
        "url": url,
    }


def _parse(body: str, *, ref: dict, volume: str, issue: str) -> tuple[int, list[dict]]:
    parser = _Tree()
    parser.feed(body)
    parser.close()
    if parser.error:
        raise ValueError(parser.error)
    titles = [_clean(_text(node)) for node in _walk(parser.root) if node.tag == "title"]
    if titles != [f"Vol. {volume} Issue {issue} — The George Washington International Law Review"]:
        raise ValueError("official page title does not exactly attest the cited issue")
    headings = [
        _clean(_text(node)) for node in _walk(parser.root)
        if node.tag in _HEADINGS and _clean(_text(node)) == f"Volume {volume}, Issue {issue}"
    ]
    if len(headings) != 1:
        raise ValueError("official issue heading is missing or ambiguous")
    blocks = _content_blocks(parser.root)
    if len(blocks) != 1:
        raise ValueError("official page has no unique issue content block beginning with Articles")
    block = blocks[0]
    direct = [child for child in block.children if isinstance(child, _Node)]
    if (
        not direct
        or direct[0].tag not in _HEADINGS
        or _clean(_text(direct[0])) != "Articles"
    ):
        raise ValueError("official issue content does not begin with Articles")
    block_text = _text(block).casefold()
    block_attrs = " ".join(
        f"{key}={value}"
        for node in (block, *_walk(block))
        for key, value in node.attrs.items()
    ).casefold()
    if any(marker in block_text or marker in block_attrs for marker in ("pagination", "lazy", "load more", "next page")):
        raise ValueError("issue content block declares pagination or lazy loading")
    section = None
    section_count = 0
    seen_sections: set[str] = set()
    members: list[dict] = []
    for child in block.children:
        if isinstance(child, str):
            if _clean(child):
                raise ValueError("issue content block has unparsed text")
            continue
        if child.tag in _HEADINGS:
            section = _clean(_text(child))
            if section not in _SECTIONS:
                raise ValueError("issue content block has an unknown section")
            if section in seen_sections:
                raise ValueError("issue content block repeats a section")
            seen_sections.add(section)
            section_count += 1
            continue
        if child.tag != "p" or section is None:
            raise ValueError("issue content block has an unparsed element")
        members.append(_member(child, section=section, ref=ref))
    if not members:
        raise ValueError("issue content block has no members")
    ids = [member["record_id"] for member in members]
    if len(ids) != len(set(ids)):
        raise ValueError("issue content block contains duplicate members")
    return section_count, members


def attest_issue(ref: dict) -> dict:
    """Attest one GWILR issue TOC, or return a fail-closed fixed-shape result."""
    out = _empty(ref)
    if not _enabled():
        out["reason"] = "GWILR official issue attestation is disabled"
        return out
    container = out["cited_container"]
    volume = out["cited_volume"]
    issue = out["cited_issue"]
    if _text_key(container) not in _ALIASES or not volume or not issue or not volume.isdigit() or not issue.isdigit():
        return out
    out["scope"] = "issue"
    out["journal_title"] = "The George Washington International Law Review"
    out["issue_url"] = f"{CANONICAL_ORIGIN}/volume-{int(volume)}-issue-{int(issue)}"
    try:
        status, body, final_url = _resolve_module()._get_with_final_url(
            out["issue_url"], accept="text/html",
        )
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"official issue page returned HTTP {status}")
        if final_url != out["issue_url"]:
            raise ValueError("official issue response left the exact canonical issue URL")
        if not isinstance(body, str):
            raise ValueError("official issue page body is not text")
        out["response_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        section_count, members = _parse(body, ref=ref, volume=str(int(volume)), issue=str(int(issue)))
        # The official page attests issue membership but not its publication
        # year.  Identify the target from the fields the page actually exposes;
        # the original cited year remains available for a separate comparison.
        cited_title = ref.get("title")
        if not isinstance(cited_title, str) or not cited_title.strip():
            # A complete TOC can still be retained as evidence, but without a
            # parsed title this adapter cannot distinguish the cited work from
            # the other issue members and therefore cannot prove absence.
            target_status, target_member_order = "inconclusive", None
        else:
            target_ref = {**ref, "year": None, "ay_year": None}
            target_status, target_member_order = _target_status(target_ref, members)
        out.update({
            "status": "complete",
            "reason": "official GWILR issue TOC was parsed completely",
            "target_status": target_status,
            "target_member_order": target_member_order,
            "attestation_basis": (
                "official single-page issue TOC; all recognized sections and every "
                "item paragraph were parsed"
            ),
            "section_count": section_count,
            "members": members,
        })
        return out
    except Exception as exc:
        out.update({
            "status": "incomplete",
            "reason": f"{type(exc).__name__}: {exc}",
            "target_status": "inconclusive",
            "target_member_order": None,
            "attestation_basis": None,
            "section_count": None,
            "members": [],
        })
        return out
