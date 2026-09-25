# core/parse/pdf_links.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""PDF hyperlink-layer citation resolver (read-only PoC).

Many PDFs produced by LaTeX ``hyperref`` (and several publisher toolchains)
embed *named* internal link annotations that connect each in-text citation to
its entry in the reference list.  When present, this layer is ground truth: it
resolves ``citation -> reference`` without any surname/year or numeric text
matching, and it marks exact reference boundaries.

The naming scheme varies by producer but the structure is universal:

  * ViT / arXiv LaTeX : ``cite.vaswani2017``      (BibTeX key in the name)
  * Nature            : ``bm_CR45``               (numbered citation reference)
  * Emerald / others  : ``ref051``                (numbered anchor)

An in-text link and its target share the *same* name, so the citation->reference
map is simply a group-by on the link name; coordinates are only needed to slice
the reference entry text out of the bibliography.

This module is intentionally side-effect free and pipeline-independent: it reads
a PDF and returns a :class:`LinkLayer`, or ``None`` when the PDF carries no
usable citation link layer.  Wiring it into the parse pipeline is a separate
step; nothing here imports the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import bisect
import json
import os
import re

# Named-destination patterns that denote a reference anchor, loaded from an
# external JSON (editable without touching code).  These are only a fast
# accelerator: a destination that lands in the bibliography region is treated as
# a reference anchor regardless of its name (see _ref_anchor_names), so an
# unknown publisher's scheme still works without being listed.
_ANCHORS_JSON = os.path.join(
    os.path.dirname(__file__), "config", "link_anchors.json")


@lru_cache(maxsize=1)
def _ref_name_res() -> tuple[re.Pattern, ...]:
    pats: list[re.Pattern] = []
    try:
        with open(_ANCHORS_JSON, encoding="utf-8") as f:
            raw = json.load(f).get("ref_name_patterns", [])
    except (OSError, ValueError):
        raw = []
    for p in raw:
        try:
            pats.append(re.compile(r"^(?:" + p + r")$", re.IGNORECASE))
        except re.error:
            continue
    return tuple(pats)


def _name_looks_like_ref(name: str) -> bool:
    return any(r.match(name) for r in _ref_name_res())


try:
    from core.parse import parser_text as _parser_text
except ImportError:  # pragma: no cover - direct-execution import path
    import parser_text as _parser_text
_BIB_HEADING_RE = _parser_text.bibliography_heading_re()


@dataclass
class Citation:
    """One in-text citation hotspot backed by a link annotation."""

    page: int
    rect: tuple[float, float, float, float]
    text: str          # the in-text text under the link rect (may be a fragment)
    target: str        # destination name it points to (== a Reference.name)
    years: set = field(default_factory=set)  # year(s) carried by / adjacent to it


@dataclass
class Reference:
    """One reference-list entry located via a named destination."""

    name: str          # destination name, e.g. "cite.vaswani2017" / "ref051"
    page: int
    y: float           # top-down y of the entry start on its page
    text: str = ""     # entry text, sliced between consecutive anchors
    key: str | None = None   # citation key parsed from the name, when encoded


@dataclass
class LinkLayer:
    references: dict[str, Reference] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)

    def resolved(self) -> list[tuple[Citation, Reference]]:
        """Citations whose target is a known reference anchor."""
        return [(c, self.references[c.target])
                for c in self.citations if c.target in self.references]

    def __bool__(self) -> bool:  # truthy only when it actually links things
        return bool(self.references) and bool(self.citations)


def extract_external_links(path: str) -> list[dict]:
    """Return visible text attached to external HTTP(S) annotations.

    Bibliographies often hide a source URL behind the printed title.  Text
    extraction cannot see that URL, but the annotation rectangle still carries
    both the URI and the exact visible title.  Rectangles sharing a URI on one
    page are joined in reading order so wrapped titles remain matchable.
    """
    try:
        import pymupdf as fitz  # PyMuPDF
        doc = fitz.open(path)
    except Exception:
        return []
    grouped: dict[tuple[int, str], list[tuple[tuple[float, ...], str]]] = {}
    try:
        for pno, page in enumerate(doc):
            for link in page.get_links():
                uri = str(link.get("uri") or "").strip()
                if not re.match(r"^https?://", uri, re.I):
                    continue
                rect = link.get("from")
                if rect is None:
                    continue
                try:
                    text = page.get_textbox(rect).replace("\n", " ").strip()
                    coords = (float(rect.y0), float(rect.x0), float(rect.y1), float(rect.x1))
                except Exception:
                    continue
                if text:
                    grouped.setdefault((pno, uri), []).append((coords, text))
    finally:
        doc.close()
    out = []
    for (page, url), fragments in grouped.items():
        text = " ".join(fragment for _coords, fragment in sorted(fragments)).strip()
        if text:
            out.append({"page": page, "url": url, "text": text})
    return out



def _blocks_reader(doc):
    """How to read this PDF's blocks, phantom spaces repaired when it has them.

    The reference text this module slices out of the page is what the resolver
    then tries to identify, so reading it raw from a producer that breaks its own
    words is what turns an entry into "coho rt Oto mo rpha" - matching no work,
    and dragging a correct DOI down with it.  Falls back to the plain reader when
    the fetch module is unavailable.
    """
    try:
        from core.fetch.extraction.pdf import page_blocks_reader
    except ImportError:  # pragma: no cover - direct-execution import path
        try:
            from fetch.pdf import page_blocks_reader
        except ImportError:
            return lambda page: page.get_text("blocks")
    return page_blocks_reader(doc)

def _key_from_name(name: str) -> str | None:
    """Extract a citation key from a destination name when one is encoded.

    ``cite.vaswani2017`` -> ``vaswani2017``; numbered anchors (``ref051``,
    ``bm_CR45``) carry no semantic key -> ``None``.
    """
    if name.lower().startswith("cite."):
        return name[5:]
    return None


def _name_order(name: str) -> tuple:
    """Bibliography order of a destination name: ``bib9`` before ``bib10``.

    A tie-break, and it has to be a TOTAL one.  Two anchors can resolve to the very
    same point — a producer that anchors an entry on the line where the previous one
    ends leaves "bib39" and "bib56" at the identical (page, column, y) — and a stable
    sort cannot separate them: whichever the caller happened to append first stays
    first.  That caller iterated a SET, so the winner changed with the process's hash
    seed, and with it which reference got the text and which came out empty: the same
    PDF parsed twice reported a different bibliography.  Ordering by the number in the
    name settles it the way the bibliography itself does.
    """
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"(\d+)", name) if part)


def _dest_point(dest: dict) -> tuple[float, float] | None:
    """(x, y) of a resolved destination in raw PDF (bottom-up) coordinates.

    PyMuPDF returns either ``{'to': Point(x, y), ...}`` (``/XYZ`` form) or a raw
    ``{'dest': '/FitR x0 y0 x1 y1'}`` string.  Both carry bottom-up PDF y; the
    caller flips to top-down with the page height.  Returns ``None`` when no
    usable point can be read.
    """
    to = dest.get("to")
    if to is not None:
        try:
            return float(to[0]), float(to[1])
        except (TypeError, IndexError):
            try:
                return float(to.x), float(to.y)
            except Exception:
                return None
    nums = re.findall(r"[-\d.]+", dest.get("dest") or "")
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    return None


_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_YEAR_TOKEN_RE = re.compile(r"^\(?(?:19|20)\d{2}[a-z]?\)?[;,.]?$")


def _augment_citations(doc, citations: list["Citation"]) -> list["Citation"]:
    """Merge adjacent same-target rects and tag each citation with its year(s).

    Two publisher conventions place the year differently: some anchor the link on
    the whole ``(Author, 2020)`` (year inside the rect), others only on the author
    name with the year just to the right on the same line.  We read both: years
    found inside the rect text, plus the first year token immediately to its right
    (stopping at the next citation's ``;``/``(``).  Adjacent rects that share a
    target — a citation split across two link boxes, e.g. ``Stone-Johnson and`` +
    ``Wright, 2020`` — are merged first so the year is not orphaned from the name.
    """
    if not citations:
        return citations
    ordered = sorted(citations, key=lambda c: (c.page, round(c.rect[1], 1), c.rect[0]))
    merged: list[Citation] = []
    for c in ordered:
        if merged:
            m = merged[-1]
            if m.target == c.target and m.page == c.page:
                dy = c.rect[1] - m.rect[1]
                same_line = abs(dy) < 4.0 and 0 <= (c.rect[0] - m.rect[2]) < 12.0
                # A citation that wrapped across a line break: the head fragment
                # carries no year yet, and the tail starts on the next line.
                next_line = 4.0 <= dy < 16.0 and not _YEAR_RE.search(m.text or "")
                if same_line or next_line:
                    m.rect = (min(m.rect[0], c.rect[0]), min(m.rect[1], c.rect[1]),
                              max(m.rect[2], c.rect[2]), max(m.rect[3], c.rect[3]))
                    m.text = (m.text + " " + c.text).strip()
                    continue
        merged.append(Citation(page=c.page, rect=c.rect, text=c.text, target=c.target))

    words_by_page: dict[int, list] = {}
    for c in merged:
        years = {int(y) for y in _YEAR_RE.findall(c.text or "")}
        # Only look to the right when the box carries no year of its own; a box
        # that already holds its year must not borrow a neighbour's (which would
        # make two distinct works look like one ambiguous (surname, year)).
        if not years:
            x1, yc = c.rect[2], (c.rect[1] + c.rect[3]) / 2.0
            words = words_by_page.get(c.page)
            if words is None:
                try:
                    words = doc[c.page].get_text("words")
                except Exception:
                    words = []
                words_by_page[c.page] = words
            right = sorted((t for t in words
                            if abs((t[1] + t[3]) / 2.0 - yc) < 4.0 and x1 - 1 <= t[0] < x1 + 45),
                           key=lambda t: t[0])
            for t in right:
                tok = t[4]
                if _YEAR_TOKEN_RE.match(tok):
                    years.add(int(_YEAR_RE.search(tok).group(0)))
                    break
                if tok[:1] in (";", "("):
                    break  # next citation begins — do not borrow its year
        c.years = years
    return merged


def _column(x_left: float, page_width: float) -> int:
    """Reading-order column index (two-column max) from a block's LEFT edge.

    Keyed on the left edge, not the centre: a full-width single-column entry and
    its short continuation lines all start at the left margin (column 0), while a
    genuine right-column entry starts past the page midpoint (column 1).  Using
    the centre would split a full-width line (centre ≈ midpoint) from its own
    short wrapped lines.
    """
    return 0 if x_left < page_width / 2.0 else 1


def _bibliography_region(doc) -> tuple[int, float] | None:
    """(page, top-down y) of the References/Bibliography heading, or None.

    Any named destination at or after this point is a reference anchor,
    whatever its name — so a publisher scheme we have never seen still works."""
    read_blocks = _blocks_reader(doc)
    for pno in range(doc.page_count):
        try:
            blocks = read_blocks(doc[pno])
        except Exception:
            continue
        for b in blocks:
            for ln in (b[4] or "").splitlines():
                if _BIB_HEADING_RE.match(ln):
                    return (pno, b[1])
    return None


def _ref_anchor_names(doc, dests: dict) -> set[str]:
    """Destination names that denote a reference anchor: either the name matches
    a known pattern (fast path), or the destination lands in the bibliography
    region (generic — covers any producer's naming)."""
    region = _bibliography_region(doc)
    names: set[str] = set()
    for name, dest in dests.items():
        if _name_looks_like_ref(name):
            names.add(name)
            continue
        if region is None:
            continue
        page_no = dest.get("page")
        pt = _dest_point(dest)
        if page_no is None or pt is None:
            continue
        try:
            y_top = doc[page_no].rect.height - pt[1]
        except Exception:
            continue
        if (page_no, y_top) >= region:  # at or after the References heading
            names.add(name)
    return names


def extract_link_layer(path: str) -> LinkLayer | None:
    """Build the citation link layer for *path*, or ``None`` if unusable.

    Returns ``None`` (rather than an empty layer) when the PDF has no named
    citation links, so callers can cleanly fall back to the text pipeline.
    """
    try:
        import pymupdf as fitz  # PyMuPDF
    except Exception:
        return None
    try:
        doc = fitz.open(path)
    except Exception:
        return None

    try:
        dests = doc.resolve_names() if hasattr(doc, "resolve_names") else {}
        # Names that are reference anchors: known naming pattern OR a destination
        # landing in the bibliography region (generic, any producer).
        anchor_names = _ref_anchor_names(doc, dests)

        # 1) Collect in-text citation hotspots (named links to ref anchors).
        citations: list[Citation] = []
        ref_names: set[str] = set()
        for pno, page in enumerate(doc):
            for l in page.get_links():
                if l.get("kind") != fitz.LINK_NAMED:
                    continue
                name = l.get("name") or l.get("nameddest") or ""
                if name not in anchor_names:
                    continue
                rect = l["from"]
                try:
                    text = page.get_textbox(rect).replace("\n", " ").strip()
                except Exception:
                    text = ""
                citations.append(Citation(
                    page=pno,
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    text=text,
                    target=name,
                ))
                ref_names.add(name)

        if not citations:
            return None

        # 2) Resolve each reference anchor to a column-aware reading position.
        # PDF destination y is bottom-up; flip to top-down and derive the column
        # from x so two-column bibliographies read down each column, not across.
        # A tiny upward nudge keeps an anchor just above its own first line.
        anchors: list[dict] = []
        for name in sorted(ref_names, key=_name_order):
            dest = dests.get(name)
            if not dest:
                continue
            page_no = dest.get("page")
            if page_no is None:
                continue
            pt = _dest_point(dest)
            if pt is None:
                continue
            x, y_pdf = pt
            page = doc[page_no]
            y_top = page.rect.height - y_pdf
            col = _column(x, page.rect.width)
            anchors.append({
                "name": name, "page": page_no, "y": y_top,
                "pos": (page_no, col, y_top - 3.0),
            })

        if not anchors:
            return None

        # 3) Assemble each reference from the text blocks between consecutive
        # anchors, in column-aware reading order.  PyMuPDF's "blocks" mode groups
        # words into layout blocks (a column-correct paragraph/line unit); each
        # block is assigned to the latest anchor at or before it in reading order,
        # so a reference collects exactly its own blocks — across line wraps,
        # column breaks and page breaks — without absorbing a neighbour.
        anchors.sort(key=lambda a: (a["pos"], _name_order(a["name"])))
        anchor_positions = [a["pos"] for a in anchors]
        anchor_names = [a["name"] for a in anchors]
        buckets: dict[str, list[tuple]] = {a["name"]: [] for a in anchors}
        read_blocks = _blocks_reader(doc)
        for pno in sorted({a["page"] for a in anchors}):
            page = doc[pno]
            pw = page.rect.width
            try:
                blocks = read_blocks(page)
            except Exception:
                blocks = []
            for b in blocks:
                x0, y0, x1, y1, btext = b[0], b[1], b[2], b[3], b[4]
                if not (btext or "").strip():
                    continue
                pos = (pno, _column(x0, pw), y0)
                i = bisect.bisect_right(anchor_positions, pos) - 1
                if i < 0:
                    continue  # block before the first anchor (e.g. "References")
                buckets[anchor_names[i]].append((pos, btext))

        references: dict[str, Reference] = {}
        for a in anchors:
            blks = sorted(buckets[a["name"]], key=lambda t: t[0])
            text = " ".join(t[1].replace("\n", " ").strip() for t in blks).strip()
            references[a["name"]] = Reference(
                name=a["name"], page=a["page"], y=a["y"],
                text=text, key=_key_from_name(a["name"]),
            )

        # Merge split citation boxes and tag each with the year(s) it carries,
        # so an orphan (surname, year) can be paired to the exact link target.
        citations = _augment_citations(doc, citations)

        layer = LinkLayer(references=references, citations=citations)
        return layer or None
    finally:
        doc.close()
