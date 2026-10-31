#!/usr/bin/env python3
# core/parse/extractors/docx.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""DOCX manuscript extractor."""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

try:
    from core.parse import parser_text
    from core.parse import docx_limits
    from core.parse.parsing_common import RANGE_DASHES, _select_biblio_heading_index
except ImportError:  # standalone use, as the other extractors allow
    import parser_text
    import docx_limits
    from parsing_common import RANGE_DASHES, _select_biblio_heading_index


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
EXTENSIONS = (".docx",)

_LITERAL_REFERENCE_NUMBER_RE = re.compile(
    r"^\s*(?:\[(?P<bracket>\d+)\](?:\.)?\s+|"
    r"\((?P<paren>\d+)\)\s+|"
    r"(?P<punct>\d+)[.)]\s+|(?P<glued>\d+)(?=[A-Z]\.))"
)


def _first_bibliography_heading(paragraphs: list[str]) -> int | None:
    heading_re = parser_text.bibliography_heading_re()
    for index, paragraph in enumerate(paragraphs):
        stripped = re.sub(r"^[\s■▪●•◆□▶*·]+", "", paragraph).strip().lower().rstrip(":.")
        if heading_re.fullmatch(stripped):
            return index
    return None


def _decimal_numbering_starts(numbering_xml: bytes | None, num_ids: set[str]) -> dict[str, int]:
    """Resolve supported decimal level-zero list starts for active references."""
    if numbering_xml is None:
        raise ValueError("DOCX bibliography numbering is active but word/numbering.xml is missing")
    try:
        root = ET.fromstring(numbering_xml)
    except ET.ParseError as exc:
        raise ValueError("DOCX bibliography numbering definition is malformed") from exc

    nums: dict[str, list[ET.Element]] = {}
    for num in root.findall(f"{W}num"):
        num_id = num.get(f"{W}numId")
        if num_id is not None:
            nums.setdefault(num_id, []).append(num)

    abstract_nums: dict[str, list[ET.Element]] = {}
    for abstract in root.findall(f"{W}abstractNum"):
        abstract_id = abstract.get(f"{W}abstractNumId")
        if abstract_id is not None:
            abstract_nums.setdefault(abstract_id, []).append(abstract)

    starts = {}
    for num_id in num_ids:
        matching_nums = nums.get(num_id, [])
        if not matching_nums:
            raise ValueError(f"DOCX bibliography numbering definition for id {num_id!r} is missing")
        if len(matching_nums) > 1:
            raise ValueError(f"DOCX bibliography has duplicate numbering id {num_id!r}")
        num = matching_nums[0]
        abstract_id_el = num.find(f"{W}abstractNumId")
        if abstract_id_el is None or abstract_id_el.get(f"{W}val") is None:
            raise ValueError(f"DOCX bibliography numbering id {num_id!r} has no abstract definition")
        abstract_id = abstract_id_el.get(f"{W}val")
        matching_abstracts = abstract_nums.get(abstract_id, [])
        if not matching_abstracts:
            raise ValueError(f"DOCX bibliography abstract numbering for id {num_id!r} is missing")
        if len(matching_abstracts) > 1:
            raise ValueError(f"DOCX bibliography has duplicate abstract numbering for id {num_id!r}")
        abstract = matching_abstracts[0]
        overrides = [
            item for item in num.findall(f"{W}lvlOverride")
            if item.get(f"{W}ilvl") == "0"
        ]
        if len(overrides) > 1:
            raise ValueError(f"DOCX bibliography has duplicate level-zero overrides for id {num_id!r}")
        if overrides and overrides[0].find(f"{W}lvl") is not None:
            # A full level override replaces the abstract definition. Its label
            # is outside this extractor's deliberately narrow numbering support.
            continue
        level = next(
            (item for item in abstract.findall(f"{W}lvl")
             if item.get(f"{W}ilvl") == "0"),
            None,
        )
        if level is None:
            raise ValueError(f"DOCX bibliography level-zero definition for id {num_id!r} is missing")
        format_el = level.find(f"{W}numFmt")
        if format_el is None or format_el.get(f"{W}val") is None:
            raise ValueError(f"DOCX bibliography numbering format for id {num_id!r} is missing")
        if format_el.get(f"{W}val") != "decimal":
            continue
        label_el = level.find(f"{W}lvlText")
        if label_el is None or label_el.get(f"{W}val") is None:
            raise ValueError(f"DOCX decimal bibliography label for id {num_id!r} is missing")
        if label_el.get(f"{W}val") != "%1.":
            continue

        start_el = level.find(f"{W}start")
        start_text = start_el.get(f"{W}val") if start_el is not None else None
        if overrides:
            override_el = overrides[0].find(f"{W}startOverride")
            if override_el is not None:
                start_text = override_el.get(f"{W}val")
        if start_text is None:
            raise ValueError(f"DOCX bibliography start value for id {num_id!r} is missing")
        try:
            start = int(start_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"DOCX bibliography start value for id {num_id!r} is malformed") from exc
        starts[num_id] = start
    return starts


def _read_numpr(numpr: ET.Element | None, *, context: str) -> tuple[str | None, str | None]:
    if numpr is None:
        return None, None
    num_ids = numpr.findall(f"{W}numId")
    levels = numpr.findall(f"{W}ilvl")
    if len(num_ids) > 1 or len(levels) > 1:
        raise ValueError(f"DOCX {context} has ambiguous numbering properties")
    num_id = None
    level = None
    if num_ids:
        num_id = num_ids[0].get(f"{W}val")
        if num_id is None:
            raise ValueError(f"DOCX {context} has a malformed numbering id")
    if levels:
        level = levels[0].get(f"{W}val")
        if level is None:
            raise ValueError(f"DOCX {context} has a malformed numbering level")
    return num_id, level


def _numbering_properties(ppr: ET.Element | None, *, context: str):
    if ppr is None:
        return None, None, None
    styles = ppr.findall(f"{W}pStyle")
    if len(styles) > 1:
        raise ValueError(f"DOCX {context} has ambiguous paragraph styles")
    style_id = styles[0].get(f"{W}val") if styles else None
    if styles and style_id is None:
        raise ValueError(f"DOCX {context} has a malformed paragraph style")
    numprs = ppr.findall(f"{W}numPr")
    if len(numprs) > 1:
        raise ValueError(f"DOCX {context} has ambiguous numbering properties")
    num_id, level = _read_numpr(numprs[0] if numprs else None, context=context)
    return style_id, num_id, level


def _style_definitions(styles_xml: bytes | None) -> dict[str, list[ET.Element]]:
    if styles_xml is None:
        return {}
    try:
        root = ET.fromstring(styles_xml)
    except ET.ParseError as exc:
        raise ValueError("DOCX paragraph style definitions are malformed") from exc
    styles: dict[str, list[ET.Element]] = {}
    for style in root.findall(f"{W}style"):
        style_id = style.get(f"{W}styleId")
        if style_id is not None:
            styles.setdefault(style_id, []).append(style)
    return styles


def _effective_paragraph_numbering(
    paragraph_nodes: list[ET.Element],
    styles_xml: bytes | None,
    first_heading_idx: int,
) -> list[tuple[str | None, str]]:
    properties = []
    for paragraph in paragraph_nodes:
        pprs = paragraph.findall(f"{W}pPr")
        if len(pprs) > 1:
            raise ValueError("DOCX paragraph has ambiguous properties")
        properties.append(
            _numbering_properties(
                pprs[0] if pprs else None,
                context="paragraph",
            )
        )

    needs_styles = any(style_id and direct_num_id is None for style_id, direct_num_id, _ in properties)
    styles = _style_definitions(styles_xml) if needs_styles else {}
    style_cache: dict[str, tuple[str | None, str, frozenset[str], str | None]] = {}

    def resolve_style(style_id: str, visiting: frozenset[str] = frozenset()):
        if style_id in visiting:
            return None, "0", frozenset(), "cyclic style inheritance"
        if not visiting and style_id in style_cache:
            return style_cache[style_id]
        matches = styles.get(style_id, [])
        if not matches:
            result = (None, "0", frozenset(), None)
        elif len(matches) > 1:
            branches = [resolve_definition(style, style_id, visiting) for style in matches]
            possible_ids = frozenset().union(*(branch[2] for branch in branches))
            result = (None, "0", possible_ids, "ambiguous style definition")
        else:
            result = resolve_definition(matches[0], style_id, visiting)
        if not visiting:
            style_cache[style_id] = result
        return result

    def resolve_definition(style: ET.Element, style_id: str, visiting: frozenset[str]):
        if style.get(f"{W}type") not in (None, "paragraph"):
            return None, "0", frozenset(), "style is not a paragraph style"
        pprs = style.findall(f"{W}pPr")
        numprs = [numpr for item in pprs for numpr in item.findall(f"{W}numPr")]
        based_on = style.findall(f"{W}basedOn")
        if len(based_on) > 1:
            branches = []
            for base in based_on:
                base_id = base.get(f"{W}val")
                if base_id is not None:
                    branches.append(resolve_style(base_id, visiting | {style_id}))
            possible_ids = frozenset().union(*(branch[2] for branch in branches))
            inherited = (None, "0", possible_ids, "ambiguous basedOn definitions")
        elif based_on and based_on[0].get(f"{W}val") is not None:
            inherited = resolve_style(
                based_on[0].get(f"{W}val"), visiting | {style_id}
            )
        else:
            inherited = (None, "0", frozenset(), None)

        if len(pprs) > 1 or len(numprs) > 1:
            possible_ids = set(inherited[2])
            for numpr in numprs:
                possible_ids.update(
                    value
                    for num_id_el in numpr.findall(f"{W}numId")
                    if (value := num_id_el.get(f"{W}val")) not in (None, "0")
                )
            return None, "0", frozenset(possible_ids), "ambiguous style numbering"
        try:
            own_num_id, own_level = _read_numpr(
                numprs[0] if numprs else None, context=f"style {style_id!r}"
            )
        except ValueError as exc:
            possible_ids = set(inherited[2])
            possible_ids.update(
                value
                for num_id_el in (numprs[0].findall(f"{W}numId") if numprs else [])
                if (value := num_id_el.get(f"{W}val")) not in (None, "0")
            )
            return None, "0", frozenset(possible_ids), str(exc)
        if own_num_id is not None:
            possible_ids = set(inherited[2])
            if own_num_id != "0":
                possible_ids.add(own_num_id)
            return own_num_id, own_level or "0", frozenset(possible_ids), inherited[3]

        inherited_num_id, inherited_level, possible_ids, issue = inherited
        if own_level is not None:
            inherited_level = own_level
        if based_on and based_on[0].get(f"{W}val") is None:
            issue = "malformed basedOn definition"
        return inherited_num_id, inherited_level, possible_ids, issue

    effective: list[tuple[str | None, str]] = [(None, "0")] * len(properties)

    def paragraph_value(index: int, *, relevant_num_ids: set[str] | None):
        style_id, direct_num_id, direct_level = properties[index]
        if direct_num_id is not None:
            return direct_num_id, direct_level or "0"

        inherited_num_id, inherited_level = None, "0"
        if style_id:
            inherited_num_id, inherited_level, possible_ids, issue = resolve_style(style_id)
            relevant = index > first_heading_idx or (
                relevant_num_ids is not None and possible_ids & relevant_num_ids
            )
            if issue and relevant:
                raise ValueError(f"DOCX bibliography style {style_id!r} has {issue}")
            if issue:
                inherited_num_id = None
                inherited_level = "0"
        if direct_level is not None:
            inherited_level = direct_level
        return inherited_num_id, inherited_level

    for index in range(first_heading_idx + 1, len(properties)):
        effective[index] = paragraph_value(index, relevant_num_ids=None)

    active_num_ids = {
        num_id
        for num_id, level in effective[first_heading_idx + 1:]
        if num_id not in (None, "0") and level == "0"
    }
    for index in range(first_heading_idx + 1):
        effective[index] = paragraph_value(index, relevant_num_ids=active_num_ids)
    return effective


def _docx_text(path: str) -> str:
    docx_limits.check_directory(path)
    with zipfile.ZipFile(path) as z:
        docx_limits.check_xml_parts(z)
        xml = docx_limits.read_xml(z, "word/document.xml")
        try:
            numbering_xml = docx_limits.read_xml(z, "word/numbering.xml")
        except KeyError:
            numbering_xml = None
        try:
            styles_xml = docx_limits.read_xml(z, "word/styles.xml")
        except KeyError:
            styles_xml = None
    root = ET.fromstring(xml)
    paragraphs = []
    paragraph_nodes = []
    for p in root.iter(f"{W}p"):
        paragraph_nodes.append(p)
        buf = []
        for r in p.iter(f"{W}r"):
            rpr = r.find(f"{W}rPr")
            is_sup = False
            if rpr is not None:
                va = rpr.find(f"{W}vertAlign")
                if va is not None and va.get(f"{W}val") == "superscript":
                    is_sup = True
            text = "".join(t.text or "" for t in r.iter(f"{W}t"))
            if not text:
                continue
            if is_sup and re.fullmatch(rf"[\d,{RANGE_DASHES}]+", text.strip()):
                buf.append(f"⟦SUP:{text.strip()}⟧")
            else:
                buf.append(text)
        paragraphs.append("".join(buf))

    first_heading_idx = _first_bibliography_heading(paragraphs)
    if first_heading_idx is not None:
        paragraph_numbering = _effective_paragraph_numbering(
            paragraph_nodes, styles_xml, first_heading_idx
        )
        active_num_ids = {
            num_id
            for index, (num_id, level) in enumerate(paragraph_numbering)
            if index > first_heading_idx and num_id not in (None, "0") and level == "0"
        }
        if active_num_ids:
            starts = _decimal_numbering_starts(numbering_xml, active_num_ids)
            counters = {}
            projected_paragraphs = list(paragraphs)
            number_by_index = {}
            for index, (num_id, level) in enumerate(paragraph_numbering):
                if num_id not in starts or level != "0":
                    continue
                number = counters.get(num_id, starts[num_id])
                counters[num_id] = number + 1
                number_by_index[index] = number
                if (
                    index > first_heading_idx
                    and paragraphs[index].strip()
                    and not _LITERAL_REFERENCE_NUMBER_RE.match(paragraphs[index])
                ):
                    projected_paragraphs[index] = f"{number}. {paragraphs[index]}"

            selected_heading_idx = _select_biblio_heading_index(projected_paragraphs)
            if selected_heading_idx is not None:
                for index, number in number_by_index.items():
                    if index <= selected_heading_idx or not paragraphs[index].strip():
                        continue
                    literal_match = _LITERAL_REFERENCE_NUMBER_RE.match(paragraphs[index])
                    if literal_match:
                        literal_number = next(
                            int(value)
                            for value in literal_match.groupdict().values()
                            if value is not None
                        )
                        if literal_number != number:
                            raise ValueError(
                                "DOCX bibliography's literal reference number "
                                f"{literal_number} conflicts with automatic number {number}"
                            )
                    else:
                        paragraphs[index] = f"{number}. {paragraphs[index]}"
    return "\n".join(paragraphs)


def extract(path: str, *, ocr_lang: str, ocr_notice, manuscript_ocr: bool):
    del ocr_lang, ocr_notice, manuscript_ocr
    # DOCX states which runs are raised (w:vertAlign), so superscript markers are
    # read, not inferred — unlike plain text, which cannot carry the information.
    return _docx_text(path), "docx", {"superscript_source": "markup"}
