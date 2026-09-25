#!/usr/bin/env python3
# core/parse/citation_schemes/numeric.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Numeric citation scheme."""

from __future__ import annotations

import importlib
import re

try:
    from core.parse import format_handlers
    from core.parse.parsing_common import (
        RANGE_DASHES, NUMBER_RUN, STRUCTURAL_TABLE_SENTINEL,
        STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL,
    )
except ImportError:
    import format_handlers  # type: ignore[no-redef]
    from parsing_common import (
        RANGE_DASHES, NUMBER_RUN, STRUCTURAL_TABLE_SENTINEL,
        STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL,
    )

NAME = "numeric"

# Corresponding-author asterisk right after a marker (⟦SUP:1⟧* or [1]*).  A
# citation is never followed by "*", so its presence marks an author/affiliation
# block: within such a sentence, markers glued to an author surname are
# affiliation markers, not citations.
_AFFIL_STAR_RE = re.compile(
    rf"(?:⟦SUP:[\d,{RANGE_DASHES}]+⟧|\[\d[\d,{RANGE_DASHES}]*\])\*")
# A capitalized word immediately preceding the marker (the surname it annotates).
_TRAILING_SURNAME_RE = re.compile(r"([A-Za-z][\w'’-]*)$")
# First body section heading; the leading author block ends here.
_BODY_START_RE = re.compile(r"\b(?:Introduction|Abstract|Background|Summary)\b")
_EMAIL_LOCAL_TAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_DOMAIN_HEAD_RE = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}")
_GLYCOPROTEIN_RECEPTOR_PREFIX_RE = re.compile(r"(?i)\bgp$")
_GLYCOPROTEIN_RECEPTOR_TAIL_RE = re.compile(r"(?i)^\s+receptor\b")


def _is_email_marker(sentence: str, match: re.Match) -> bool:
    """Whether a PDF superscript/bracket token sits inside an email address."""
    return bool(
        _EMAIL_LOCAL_TAIL_RE.search(sentence[:match.start()])
        and _EMAIL_DOMAIN_HEAD_RE.match(sentence[match.end():])
    )


def _surname_glued_marker_count(sentence: str) -> int:
    """Count numeric markers shaped like author-affiliation annotations."""
    count = 0
    for match in MARKER_RE.finditer(sentence):
        if match.group("par"):
            continue
        surname = _TRAILING_SURNAME_RE.search(sentence[:match.start()])
        if surname and surname.group(1)[0].isupper():
            count += 1
    return count


def _resolve_parse_module():
    try:
        return importlib.import_module("core.parse.parse_manuscript")
    except ImportError:
        return importlib.import_module("parse_manuscript")


MARKER_RE = _resolve_parse_module().MARKER_RE


def find_markers(sentence: str, suppressed_out: list | None = None,
                 sentence_index: int = 0, force_affil_block: bool = False) -> list[object]:
    """Every plausible citation marker in the sentence, with formula guards:
    * '[n]' attached to a single-letter identifier or a digit is array/matrix
      indexing (x[1]), not a citation;
    * '[n]' attached to a multi-letter identifier IN formula context
      (head[1], h_i[1], W_O[1]) is suppressed -- AND gate requires BOTH
      adjacency AND mathematical operators / function names / subscripts
      in the preceding window;
    * '(n)' after a structure word (Eq., Fig., Table ...), after an equation
      ('y = ax + b (4)'), at the start of the sentence or right after ':'
      (list enumeration) is not a citation;
    * a marker containing 0 is interval/tuple notation ([0, 1]) -- reference
      numbering starts at 1.

    When *suppressed_out* is provided, markers suppressed by the formula-context
    guard are appended as dicts with ``reason: "formula_context"`` so they can
    be recorded in the parse debug output."""
    parse_mod = _resolve_parse_module()
    found = []
    affil_block = force_affil_block or bool(_AFFIL_STAR_RE.search(sentence))
    for m in MARKER_RE.finditer(sentence):
        raw_n = m.group("brk_n") or m.group("par_n") or m.group("sup_n")
        nums = parse_mod._expand_numbers(raw_n)
        if not nums or 0 in nums:
            continue
        kind = "brk" if m.group("brk") else ("par" if m.group("par") else "sup")
        start = m.start()
        if kind in ("brk", "sup") and _is_email_marker(sentence, m):
            if suppressed_out is not None:
                suppressed_out.append({
                    "marker_raw": parse_mod._display(m.group(0)),
                    "sentence_index": sentence_index,
                    "reason": "email_address",
                })
            continue
        # Biomedical names such as ``gp130 receptor`` can be extracted from a
        # PDF as ``gp[130] receptor`` or ``gp⟦SUP:130⟧ receptor``.  The closed
        # prefix+tail shape keeps ordinary word-glued Vancouver citations
        # outside this guard.
        if (
            kind in ("brk", "sup")
            and nums == [130]
            and _GLYCOPROTEIN_RECEPTOR_PREFIX_RE.search(sentence[:start])
            and _GLYCOPROTEIN_RECEPTOR_TAIL_RE.match(sentence[m.end():])
        ):
            if suppressed_out is not None:
                suppressed_out.append({
                    "marker_raw": parse_mod._display(m.group(0)),
                    "sentence_index": sentence_index,
                    "reason": "biomedical_symbol",
                })
            continue
        if kind in ("brk", "sup") and start > 0:
            # sup markers (⟦SUP:N⟧) come from PDF superscript numbers; the
            # sentinel is always adjacent to the preceding character, so the
            # same formula-context guard applies: single-letter (x¹) → skip,
            # multi-letter + formula context (head¹) → suppress.
            if kind == "brk" and sentence[start - 1].isdigit():
                continue
            mb = re.search(r"([A-Za-z_]\w*)$", sentence[:start])
            if mb and len(mb.group(1)) == 1:
                continue
            # Affiliation / corresponding-author marker: in an author block (a
            # sentence carrying a "*" corresponding-author marker), a number glued
            # to a capitalized surname annotates an affiliation, not a citation.
            if affil_block:
                surname = _TRAILING_SURNAME_RE.search(sentence[:start])
                if surname and surname.group(1)[0].isupper():
                    if suppressed_out is not None:
                        suppressed_out.append({
                            "marker_raw": parse_mod._display(m.group(0)),
                            "sentence_index": sentence_index,
                            "reason": "affiliation_marker",
                        })
                    continue
            # AND gate: multi-letter / underscore identifier adjacent to
            # bracket / superscript + formula context in the preceding window.
            if parse_mod._brk_in_formula_context(sentence, start):
                if suppressed_out is not None:
                    suppressed_out.append({
                        "marker_raw": parse_mod._display(m.group(0)),
                        "sentence_index": sentence_index,
                        "reason": "formula_context",
                    })
                continue
        if kind == "par":
            lead = sentence[:start].rstrip()
            tail = sentence[m.end():]
            # Journal coordinates such as ``Cureus 18(3): r223`` use the
            # parenthesis for an issue number, not a citation.  Requiring the
            # volume immediately before it and a locator after the colon keeps
            # ordinary parenthetical citation styles outside this guard.
            if (
                re.search(r"\b\d{1,4}\s*$", lead)
                and re.match(r"\s*:\s*[A-Za-z]?\d", tail)
            ):
                if suppressed_out is not None:
                    suppressed_out.append({
                        "marker_raw": parse_mod._display(m.group(0)),
                        "sentence_index": sentence_index,
                        "reason": "journal_coordinate",
                    })
                continue
            if not lead or lead.endswith(":"):
                continue
            if parse_mod._PAR_STRUCTURE_RE.search(lead):
                continue
            if parse_mod._par_in_equation_context(lead):
                continue
        found.append(parse_mod.Marker(m.group(0), nums, m.span(), kind))
    return found


def detect(body: str, sentences: list[str], references: list[dict]) -> float:
    del body, references
    return float(sum(len(find_markers(sentence)) for sentence in sentences))


_TABLE_ROW_PRIMARY_RE = re.compile(r"\[(\d{1,3})\]\s*\d{1,3}\.\d+")
_DECIMAL_RE = re.compile(r"\b\d+\.\d+\b")

# A sentence splitter can leave a *bare* terminal ``[N]`` in its own piece.
# That marker is unambiguously postfix to the preceding sentence.  A marker
# followed by prose is ambiguous: ``[5] Further ...`` may be a layout seam,
# but ``[5] Smith et al. ...`` is also a normal citation-led sentence.  Never
# reassign the latter without physical-layout provenance.
_LEADING_NUMERIC_MARKER_RE = re.compile(
    rf"^\s*(?P<marker>\[(?:{NUMBER_RUN})\]|⟦SUP:[\d,{RANGE_DASHES}]+⟧)"
    r"\s*(?P<trailer>[.!?])?\s*$")
_LEADING_NUMERIC_MARKER_WITH_PROSE_RE = re.compile(
    rf"^\s*(?P<marker>\[(?:{NUMBER_RUN})\]|⟦SUP:[\d,{RANGE_DASHES}]+⟧)"
    r"\s+(?P<prose>\S.*)$")
_CITATION_LED_PROSE_RE = re.compile(
    r"^(?:"
    r"[A-Z][A-Za-zÀ-ÿ'’.-]+\s+(?:(?:et\s+al\.?\s+)?"
    r"(?i:report(?:ed|s)?|show(?:ed|s)?|find(?:s|ings)?|found|"
    r"demonstrat(?:ed|es)?|describ(?:ed|es)?|argu(?:ed|es)?|"
    r"propos(?:ed|es)?|observ(?:ed|es)?)\b|and\b|&)"
    r"|(?i:report(?:ed|s)?|show(?:ed|s)?|find(?:s|ings)?|found|"
    r"demonstrat(?:ed|es)?|describ(?:ed|es)?|argu(?:ed|es)?|"
    r"propos(?:ed|es)?|observ(?:ed|es)?)\b"
    r")",
)


def _merge_segment_lineage(previous, marker):
    """Conservatively merge layout lineage when a bare marker is reattached."""
    if not isinstance(previous, dict):
        return marker if isinstance(marker, dict) else None
    if not isinstance(marker, dict):
        return dict(previous)
    merged = dict(previous)
    kinds = list(dict.fromkeys(
        list(previous.get("segment_kinds") or [])
        + list(marker.get("segment_kinds") or [])
    ))
    boundaries = list(dict.fromkeys(
        list(previous.get("boundary_kinds") or [])
        + list(marker.get("boundary_kinds") or [])
        + ["marker_glue"]
    ))
    structurally_ambiguous = bool(
        previous.get("crosses_incompatible_segments")
        or marker.get("crosses_incompatible_segments")
        or previous.get("status") == "ambiguous"
        or marker.get("status") == "ambiguous"
    )
    merged.update(
        segment_kinds=kinds,
        boundary_kinds=boundaries,
        crosses_incompatible_segments=structurally_ambiguous,
        status="ambiguous" if structurally_ambiguous else "recovered",
        recovered=not structurally_ambiguous,
    )
    for key, fn in (
        ("line_start", min),
        ("raw_start", min),
        ("line_end", max),
        ("raw_end", max),
    ):
        values = [
            value for value in (previous.get(key), marker.get(key))
            if value is not None
        ]
        if values:
            merged[key] = fn(values)
    return merged


def repair_leading_marker_glue(sentences: list[str]) -> list[str]:
    """Keep a numeric citation adjacent to the sentence it closes.

    This is a segmentation repair, not marker synthesis: the exact marker is
    moved from a marker-only split piece to the preceding terminal prose.  A
    marker followed by prose is moved only when physical-layout provenance says
    that it begins on the same source line where the previous sentence ended.
    Citation-led author/reporting constructions remain untouched.
    """
    source_lineage = getattr(sentences, "lineage", None)
    carry_lineage = (
        isinstance(source_lineage, list)
        and len(source_lineage) == len(sentences)
    )
    repaired: list[str] = []
    repaired_lineage: list[dict | None] = []
    for index, sentence in enumerate(sentences):
        match = _LEADING_NUMERIC_MARKER_RE.match(sentence)
        prose_match = _LEADING_NUMERIC_MARKER_WITH_PROSE_RE.match(sentence)
        previous = repaired[-1] if repaired else ""
        if (
            match
            and previous.rstrip().endswith((".", "!", "?"))
            and len(re.findall(r"[A-Za-zÀ-ÿ]+", previous)) >= 4
        ):
            repaired[-1] = (
                f"{previous.rstrip()} {match.group('marker')}"
                f"{match.group('trailer') or ''}"
            )
            if carry_lineage:
                repaired_lineage[-1] = _merge_segment_lineage(
                    repaired_lineage[-1],
                    source_lineage[index],
                )
            continue
        previous_lineage = repaired_lineage[-1] if repaired_lineage else None
        current_lineage = source_lineage[index] if carry_lineage else None
        same_physical_line = bool(
            isinstance(previous_lineage, dict)
            and isinstance(current_lineage, dict)
            and previous_lineage.get("line_end") is not None
            and previous_lineage.get("line_end") == current_lineage.get("line_start")
            and not previous_lineage.get("crosses_incompatible_segments")
            and not current_lineage.get("crosses_incompatible_segments")
        )
        if (
            prose_match
            and same_physical_line
            and previous.rstrip().endswith((".", "!", "?"))
            and len(re.findall(r"[A-Za-zÀ-ÿ]+", previous)) >= 4
            and not _CITATION_LED_PROSE_RE.match(prose_match.group("prose"))
        ):
            repaired[-1] = f"{previous.rstrip()} {prose_match.group('marker')}"
            repaired.append(prose_match.group("prose"))
            if carry_lineage:
                prior = dict(previous_lineage)
                prior["boundary_kinds"] = list(dict.fromkeys(
                    list(prior.get("boundary_kinds") or [])
                    + ["leading_marker_reassigned"]
                ))
                prior["recovered"] = True
                prior["status"] = "recovered"
                repaired_lineage[-1] = prior
                repaired_lineage.append(current_lineage)
            continue
        if sentence:
            repaired.append(sentence)
            if carry_lineage:
                repaired_lineage.append(source_lineage[index])
    if carry_lineage:
        try:
            return type(sentences)(repaired, lineage=repaired_lineage)
        except TypeError:  # pragma: no cover - unknown list-compatible caller
            pass
    return repaired


def _split_table_markers(sentence: str, markers: list) -> tuple[list, list]:
    """Within a table_row sentence, separate table markers from prose markers.

    PDF extraction can concatenate table rows with adjacent prose sentences.
    When that happens the marker cluster for the table is tightly packed
    while the prose marker sits across a much larger gap.  We detect that
    gap and assign each side to table / prose by counting decimals.

    Returns (table_markers, prose_markers).
    """
    if len(markers) <= 2:
        return list(markers), []

    # Sort by start position
    sorted_mk = sorted(markers, key=lambda mk: mk.span[0])

    # Gaps between consecutive markers (character count)
    gaps = []
    for i in range(len(sorted_mk) - 1):
        gaps.append(sorted_mk[i + 1].span[0] - sorted_mk[i].span[1])

    # Find largest gap
    max_i = max(range(len(gaps)), key=lambda i: gaps[i])
    max_gap = gaps[max_i]

    # Median of the other gaps
    other = sorted(g for i, g in enumerate(gaps) if i != max_i)
    if not other:
        return list(markers), []
    median_other = other[len(other) // 2]

    # Boundary: largest gap > 2.5x median → split here
    if max_gap <= 2.5 * median_other:
        return list(markers), []

    # Validate: the gap must be prose-like, not a table-internal gap.
    # Table gaps have low word uniqueness from repeated labels
    # ("WSJ only, discriminative, WSJ only, discriminative, …").
    # Prose gaps have high uniqueness.
    gap_text = sentence[sorted_mk[max_i].span[1]:sorted_mk[max_i + 1].span[0]]
    gap_words = re.findall(r"[A-Za-z]{2,}", gap_text.lower())
    if len(gap_words) >= 6:
        unique_ratio = len(set(gap_words)) / len(gap_words)
        if unique_ratio < 0.55:
            return list(markers), []  # repetitive gap → still a table

    # Split markers at the gap
    left = sorted_mk[:max_i + 1]
    right = sorted_mk[max_i + 1:]

    # Which side is the table?  Count decimals in the full sentence on each
    # side of the gap midpoint.
    midpoint = (sorted_mk[max_i].span[1] + sorted_mk[max_i + 1].span[0]) // 2
    left_dec = len(_DECIMAL_RE.findall(sentence[:midpoint]))
    right_dec = len(_DECIMAL_RE.findall(sentence[midpoint:]))

    if left_dec >= right_dec:
        return left, right
    else:
        return right, left


def _is_table_row(sentence: str, markers: list, fmt: str | None = None) -> bool:
    """Detect table rows that contain citation-like ``[N]`` markers.

    Two layers of detection:

    **Signal A (format-agnostic)** -- ``[21] 91.4``, ``[35] 91.5``: a bracketed
    number immediately followed by a decimal.  This pattern exists in benchmark
    tables but never in prose.  Requires >=3 markers + <30% alphabetic.

    **Format-specific signals** -- dispatched to the ``format_handlers/``
    package based on the *fmt* label from the extractor.  Each format has
    its own table-artifact signature (e.g. LaTeX ``&``/``\\\\``, Markdown
    ``|`` pipes, plain-text decimal density).
    """
    # This tag is introduced only from a run of >=3 physical checklist labels
    # (``1a``, ``1b``, ``2a``...) before PDF line wraps are folded.  It catches
    # reporting checklists and similar tables that have no decimal score layout.
    if STRUCTURAL_TABLE_SENTINEL in sentence:
        return True

    alpha = sum(1 for c in sentence if c.isalpha())
    total = len(sentence) or 1

    # Signal A: "[N] XX.X" benchmark-table pattern (format-agnostic)
    if _TABLE_ROW_PRIMARY_RE.search(sentence):
        if len(markers) >= 3 and alpha / total < 0.30:
            return True
        # Fall through to format-specific dispatch
        # (e.g. a single [18] 23.75 in a PDF table row won't trip Signal A
        #  but should still be caught by the plain_text decimal-density signal)

    # Signal B (format-agnostic): LaTeX table artifacts — & column separators
    # with citation markers.  A single & is common in prose ("Smith & Jones"),
    # but >=2 strongly suggests LaTeX column separation.  The alphabetic-density
    # guard (< 30 %) prevents false positives on prose with multiple ampersands
    # (e.g. "Smith & Jones [1] and Brown & Davis [2]" ≈ 80 % alpha).
    # Format-specific dispatch (handles format artifacts: & separators,
    # | pipes, decimal density, etc.)
    if fmt:
        return format_handlers.is_table_row(sentence, markers, fmt)

    return False


def build(sentences, references, window, manuscript_id, fmt=None, link_layer=None,
          boilerplate_refs=None):
    # link_layer / boilerplate_refs are accepted for a uniform scheme interface;
    # numeric citations resolve by number and use neither.
    del link_layer, boilerplate_refs
    parse_mod = _resolve_parse_module()
    refnum_to_id = {r["ref_number"]: r["id"] for r in references}
    formula_suppressed: list = []
    # Leading author/affiliation block: present only when a "*" corresponding-author
    # marker sits near the top.  PDF sentence-splitting can break the author list
    # across sentences (e.g. at "Douglas G."), so the block is detected at the
    # document level and spans from the top through the first body heading; only
    # surname-glued markers there are suppressed, so glued body citations are safe.
    _leading = min(len(sentences), 4)
    _affil_doc = (
        any(_AFFIL_STAR_RE.search(sentences[i]) for i in range(_leading))
        or sum(_surname_glued_marker_count(sentences[i]) for i in range(_leading)) >= 2
    )
    _region_end = -1
    if _affil_doc:
        _region_end = _leading - 1
        for j, s in enumerate(sentences):
            if _BODY_START_RE.search(s):
                if j <= 6:
                    _region_end = j
                break
    sentences = repair_leading_marker_glue(sentences)
    per_sentence = [
        find_markers(s, formula_suppressed, i, force_affil_block=(i <= _region_end))
        for i, s in enumerate(sentences)
    ]
    # Document-level marker style: when bracket/superscript markers clearly
    # dominate, residual "(n)" matches are almost always equation numbers or
    # list items that slipped past the sentence-level guards -- a manuscript
    # does not mix [n] and (n) citation styles. Suppressed markers are
    # recorded as data, never silently dropped.
    n_par = sum(1 for ms in per_sentence for mk in ms if mk.kind == "par")
    n_other = sum(1 for ms in per_sentence for mk in ms if mk.kind != "par")
    suppress_paren = n_other >= 5 and n_par > 0 and n_other >= 3 * n_par
    suppressed = list(formula_suppressed)
    claims, citations, rows = [], [], []
    orphans = []
    for idx, sent in enumerate(sentences):
        markers = per_sentence[idx]
        structural_table = STRUCTURAL_TABLE_SENTINEL in sent
        if (
            suppress_paren
            and not structural_table
            and any(mk.kind == "par" for mk in markers)
        ):
            suppressed.extend(
                {"marker_raw": parse_mod._display(mk.raw), "sentence_index": idx + 1,
                 "reason": "paren_style_mismatch"}
                for mk in markers if mk.kind == "par")
            markers = [mk for mk in markers if mk.kind != "par"]
        # Table-row detection: format-aware dispatch via format_handlers/
        # With --verify-table-citations the row is treated as prose: the markers stay
        # and become claims like any other (see parse_mod.verify_table_citations).
        if markers and _is_table_row(sent, markers, fmt) \
                and not parse_mod.verify_table_citations():
            if structural_table:
                # Layout isolation already bounded this exact region before
                # newline folding. Running the generic fused-row splitter here
                # could promote one marker from a proven/ambiguous table region
                # without the explicit opt-in.
                table_mks, prose_mks = markers, []
            else:
                table_mks, prose_mks = _split_table_markers(sent, markers)
            suppressed.extend(
                {"marker_raw": parse_mod._display(mk.raw), "sentence_index": idx + 1,
                 "reason": "table_row",
                 "ref_numbers": [n for n in mk.nums if n in refnum_to_id],
                 "structural_ambiguity": (
                     STRUCTURAL_AMBIGUOUS_TABLE_SENTINEL in sent
                 )}
                for mk in table_mks)
            markers = prose_mks
        rows.append((idx + 1, len(markers), parse_mod._display(sent)))
        if not markers:
            continue
        groups = parse_mod._group_markers(sent, markers)
        group_spans = [(g[0].span[0], g[-1].span[1]) for g in groups]
        # Parenthetical numeric markers are structurally ambiguous with issue
        # and volume numbers in legal bibliography prose.  `_fragment_texts`
        # validates a sentence as a whole, so retain its coherent all-or-none
        # contract: one ambiguous parenthetical marker selects the conservative
        # whole-sentence path; otherwise brk/sup use exact token matching.
        has_parenthetical_marker = any(
            mk.kind == "par" for group in groups for mk in group)
        frags, split = parse_mod._fragment_texts(
            sent, group_spans, exact_openers=not has_parenthetical_marker)
        for group_index, (group, frag) in enumerate(zip(groups, frags)):
            nums = sorted({n for mk in group for n in mk.nums})
            marker_raw = " ".join(parse_mod._display(mk.raw) for mk in group)
            resolved = [n for n in nums if n in refnum_to_id]
            claim = parse_mod._new_claim(
                manuscript_id, sentences, idx, window, marker_raw,
                nums, len(resolved) > 1,
                sentence_text=frag if split else None,
                scope="sentence_fragment" if split else "sentence",
                marker_group_index=group_index,
                marker_group_count=len(groups))
            claims.append(claim)
            # Each reference gets its own (claim, ref) citation row -- a claim like
            # ``[12, 15, 18]`` produces three independent verification tasks
            # with distinct task_id values ``{cid}__{rid}__{scope}``.  This is
            # by design: every reference must be verified independently even
            # when cited together in one marker group.  Deduplication is
            # handled downstream by the task_id guard in run.py.
            for n in nums:
                ref_id = refnum_to_id.get(n)
                if ref_id is None:
                    orphans.append({"marker_raw": f"[{n}]", "ref_number": n,
                                    "claim_id": claim["id"]})
                else:
                    citations.append({"claim_id": claim["id"],
                                      "ref_id": ref_id, "ref_number": n})
    extra = {"orphans": orphans}
    if suppressed:
        extra["suppressed_markers"] = suppressed
    table_only = parse_mod._table_only_citations(suppressed, citations, references)
    if table_only:
        extra["table_only_citations"] = table_only
    return claims, citations, rows, extra
