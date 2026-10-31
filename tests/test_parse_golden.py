#!/usr/bin/env python3
# tests/test_parse_golden.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Golden characterization tests for parse()."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import sys
import zipfile
from xml.sax.saxutils import escape as _xml_escape

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tests._bootstrap import *  # noqa: F401,F403


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "parse_golden"
_INPUT_DIR = _FIXTURE_DIR / "inputs"
_FIXTURE_PATH = _FIXTURE_DIR / "cases.json"

_MODULE_PREFIXES = (
    "core.parse.extractors",
    "core.parse.citation_schemes",
    "core.parse.format_handlers",
    "core.parse.parser_config",
)
_MODULE_EXACT = (
    "core.parse.extract",
    "core.parse.parse_manuscript",
    "core.parse.parsing_common",
)


_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""


_CASE_SPECS = [
    {
        "name": "md_numeric_basic",
        "filename": "md_numeric_basic.md",
        "kind": "text",
        "content": (
            "# Manuscript\n\n"
            "A numeric claim [1]. Another numeric claim [2].\n\n"
            "## References\n\n"
            "[1] Alpha A. Numeric source one. Journal. 2020.\n"
            "[2] Beta B. Numeric source two. Journal. 2021.\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "txt_numeric_standalone_identifiers",
        "filename": "txt_numeric_standalone_identifiers.txt",
        "kind": "text",
        "content": (
            "Standalone identifiers are supported [1] and [2].\n\n"
            "References\n\n"
            "[1] 10.1038/example.123\n"
            "[2] https://example.org/report\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "docx_numeric_superscript",
        "filename": "docx_numeric_superscript.docx",
        "kind": "docx",
        "paragraphs": [
            [("Docx superscript claim", False), ("1", True), (".", False)],
            "References",
            "[1] Gamma G. Superscript source. Journal. 2020.",
        ],
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "tex_numeric_thebibliography",
        "filename": "tex_numeric_thebibliography.tex",
        "kind": "text",
        "content": (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "A TeX citation appears here \\cite{alpha2020}.\n"
            "\\begin{thebibliography}{9}\n"
            "\\bibitem{alpha2020} Alpha A. TeX source. Journal. 2020.\n"
            "\\end{thebibliography}\n"
            "\\end{document}\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "pdf_numeric_superscript_restore",
        "filename": "pdf_numeric_superscript_restore.pdf",
        "kind": "pdf",
        "lines": [
            (
                "PDF superscript claim demonstrates the parser behavior across a longer body of text "
                "with enough alphabetic content to pass the quality gate before the citation marker.1"
            ),
            (
                "Additional prose keeps the extracted text comfortably above the minimum threshold so "
                "this case exercises superscript restoration rather than OCR fallback."
            ),
            "",
            "References",
            "[1] Delta D. PDF superscript source. Journal. 2020.",
        ],
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "md_author_year_basic",
        "filename": "md_author_year_basic.md",
        "kind": "text",
        "content": (
            "# Manuscript\n\n"
            "Author-year evidence appears here (Smith 2021).\n\n"
            "## References\n\n"
            "Smith, J. 2021. Author-year source. Journal.\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "txt_author_year_multiline_refs",
        "filename": "txt_author_year_multiline_refs.txt",
        "kind": "text",
        "content": (
            "This is established (Abebe 2021).\n\n"
            "References\n\n"
            "Abebe, T., & Tesfaye, M. (2021). Disability and institutional barriers. Journal\n"
            "of Ethiopian Studies, 45(2), 123-140.\n"
            "Baker, R., & McCafferty, D. (2020). The role of organizational culture: A systematic\n"
            "review. Journal of Business Ethics, 170(3), 567-582.\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "docx_author_year_basic",
        "filename": "docx_author_year_basic.docx",
        "kind": "docx",
        "paragraphs": [
            "Docx author-year claim (Jones 2019).",
            "References",
            "Jones, K. 2019. Docx author-year source. Journal.",
        ],
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "tex_author_year_basic",
        "filename": "tex_author_year_basic.tex",
        "kind": "text",
        "content": (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "This result follows from prior work (Brown 2018).\n"
            "\\begin{thebibliography}{9}\n"
            "\\bibitem{brown2018} Brown, B. 2018. TeX author-year source. Journal.\n"
            "\\end{thebibliography}\n"
            "\\end{document}\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "pdf_author_year_basic",
        "filename": "pdf_author_year_basic.pdf",
        "kind": "pdf",
        "lines": [
            (
                "Prior work supports this conclusion (Taylor 2017) and the surrounding paragraph is "
                "intentionally verbose so the generated PDF has a healthy amount of readable text."
            ),
            (
                "More explanatory prose follows to satisfy the extraction quality gate while keeping "
                "the case semantically simple and stable for the golden fixture."
            ),
            "",
            "References",
            "Taylor, T. 2017. PDF author-year source. Journal.",
        ],
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "md_inline_doi_auto",
        "filename": "md_inline_doi_auto.md",
        "kind": "text",
        "content": (
            "# Draft\n\n"
            "The treatment reduces mortality (10.1038/s41586-022-05543-x).\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
    {
        "name": "txt_inline_doi_forced",
        "filename": "txt_inline_doi_forced.txt",
        "kind": "text",
        "content": "Transformers reshaped NLP (arXiv:1706.03762).\n",
        "citations_mode": "inline-doi",
        "window": 1,
    },
    {
        "name": "pdf_scanned_ocr_stub",
        "filename": "pdf_scanned_ocr_stub.pdf",
        "kind": "pdf",
        "lines": ["x"],
        "citations_mode": "auto",
        "window": 1,
        "ocr_stub_text": (
            "OCR recovered claim [1].\n\n"
            "References\n\n"
            "[1] OCR Source. Journal. 2020.\n"
        ),
        "ocr_stub_method": "stubbed",
    },
    {
        "name": "md_bulleted_refs",
        "filename": "md_bulleted_refs.md",
        "kind": "text",
        "content": (
            "# Manuscript\n\n"
            "Important findings reported (WHO 2021) and also (Jones 2019).\n\n"
            "## References\n\n"
            "- World Health Organization. (2021). Global report on disease prevention. Geneva: WHO Press.\n"
            "- Jones, K. (2019). Another paper. Another Journal, 5(1), 45-60.\n"
        ),
        "citations_mode": "auto",
        "window": 1,
    },
]


def load_cases() -> list[dict]:
    with _FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _matches_module_scope(name: str) -> bool:
    if name in _MODULE_EXACT:
        return True
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _MODULE_PREFIXES)


def _snapshot_scoped_modules() -> dict[str, object]:
    return {
        name: module
        for name, module in list(sys.modules.items())
        if _matches_module_scope(name)
    }


def _pop_scoped_modules() -> None:
    for name in list(sys.modules):
        if _matches_module_scope(name):
            sys.modules.pop(name, None)


def _restore_scoped_modules(saved_modules: dict[str, object]) -> None:
    _pop_scoped_modules()
    sys.modules.update(saved_modules)
    core_pkg = sys.modules.get("core")
    if core_pkg is None:
        return
    parse_pkg = sys.modules.get("core.parse")
    if parse_pkg is None:
        return
    saved_extract = saved_modules.get("core.parse.extract")
    if saved_extract is None:
        if hasattr(parse_pkg, "extract"):
            delattr(parse_pkg, "extract")
    else:
        parse_pkg.extract = saved_extract
    saved_parse = saved_modules.get("core.parse.parse_manuscript")
    if saved_parse is None:
        if hasattr(parse_pkg, "parse_manuscript"):
            delattr(parse_pkg, "parse_manuscript")
    else:
        parse_pkg.parse_manuscript = saved_parse
    saved_extractors = saved_modules.get("core.parse.extractors")
    if saved_extractors is None:
        if hasattr(parse_pkg, "extractors"):
            delattr(parse_pkg, "extractors")
    else:
        parse_pkg.extractors = saved_extractors
    saved_schemes = saved_modules.get("core.parse.citation_schemes")
    if saved_schemes is None:
        if hasattr(parse_pkg, "citation_schemes"):
            delattr(parse_pkg, "citation_schemes")
    else:
        parse_pkg.citation_schemes = saved_schemes
    saved_parser_config = saved_modules.get("core.parse.parser_config")
    if saved_parser_config is None:
        if hasattr(parse_pkg, "parser_config"):
            delattr(parse_pkg, "parser_config")
    else:
        parse_pkg.parser_config = saved_parser_config


def _load_parse_modules():
    _pop_scoped_modules()
    extract_mod = importlib.import_module("core.parse.extract")
    parse_mod = importlib.import_module("core.parse.parse_manuscript")
    return extract_mod, parse_mod


def _docx_run_xml(text: str, superscript: bool) -> str:
    body = f"<w:t xml:space=\"preserve\">{_xml_escape(text)}</w:t>"
    if not superscript:
        return f"<w:r>{body}</w:r>"
    return (
        "<w:r>"
        "<w:rPr><w:vertAlign w:val=\"superscript\"/></w:rPr>"
        f"{body}"
        "</w:r>"
    )


def _write_docx(path: Path, paragraphs: list[object]) -> None:
    doc_paragraphs = []
    for paragraph in paragraphs:
        if isinstance(paragraph, str):
            runs_xml = _docx_run_xml(paragraph, False)
        else:
            runs_xml = "".join(_docx_run_xml(text, superscript) for text, superscript in paragraph)
        doc_paragraphs.append(f"<w:p>{runs_xml}</w:p>")
    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(doc_paragraphs)
        + "</w:body></w:document>"
    )
    def _add(zf, name, data):
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, data)

    with zipfile.ZipFile(path, "w") as zf:
        _add(zf, "[Content_Types].xml", _DOCX_CONTENT_TYPES)
        _add(zf, "_rels/.rels", _DOCX_RELS)
        _add(zf, "word/document.xml", document_xml)


def _pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _write_pdf(path: Path, lines: list[str]) -> None:
    stream_lines = ["BT", "/F1 12 Tf", "72 760 Td"]
    for idx, line in enumerate(lines):
        if idx:
            stream_lines.append("0 -18 Td")
        stream_lines.append(f"({_pdf_escape(line)}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream",
    ]

    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = []
    current = len(parts[0])
    for index, obj in enumerate(objects, 1):
        offsets.append(current)
        piece = f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
        parts.append(piece)
        current += len(piece)

    xref_start = current
    xref = [f"xref\n0 {len(objects) + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for offset in offsets:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    ).encode("ascii")
    parts.extend(xref)
    parts.append(trailer)
    path.write_bytes(b"".join(parts))


def _write_inputs() -> None:
    _INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for spec in _CASE_SPECS:
        path = _INPUT_DIR / spec["filename"]
        kind = spec["kind"]
        if kind == "text":
            path.write_text(spec["content"], encoding="utf-8", newline="\n")
        elif kind == "docx":
            _write_docx(path, spec["paragraphs"])
        elif kind == "pdf":
            _write_pdf(path, spec["lines"])
        else:
            raise ValueError(f"unsupported input kind: {kind}")


def _normalize_result(result: dict) -> dict:
    out = copy.deepcopy(result)
    manuscript_id = out["manuscript"]["id"]
    out["manuscript"].pop("full_text", None)
    # Dedicated manuscript-identity tests pin this additive evidence payload;
    # these historical parser goldens remain focused on citation semantics.
    out["manuscript"].pop("title_evidence", None)
    out["manuscript"]["id"] = "ms-PLACEHOLDER"
    out["manuscript"]["uploaded_at"] = "TIMESTAMP"

    ref_id_map = {}
    for idx, ref in enumerate(out.get("references") or [], 1):
        old_ref_id = ref.get("id")
        new_ref_id = f"ref-{idx:03d}"
        if old_ref_id:
            ref_id_map[old_ref_id] = new_ref_id
            ref["id"] = new_ref_id
        if ref.get("manuscript_id") == manuscript_id:
            ref["manuscript_id"] = "ms-PLACEHOLDER"

    claim_id_map = {}
    for idx, claim in enumerate(out.get("claims") or [], 1):
        # Structural lineage is observational provenance.  Dedicated parser
        # tests pin its contract; semantic golden fixtures intentionally keep
        # their historical payload snapshots stable.
        claim.pop("structural_provenance", None)
        # Verify-v3 parser topology has its own focused persistence tests.
        # These historical semantic fixtures deliberately stay additive-only.
        for field in (
            "parser_sentence_index", "marker_group_index", "marker_group_count",
            "marker_start", "marker_end",
        ):
            claim.pop(field, None)
        old_claim_id = claim.get("id")
        new_claim_id = f"claim-{idx:03d}"
        if old_claim_id:
            claim_id_map[old_claim_id] = new_claim_id
            claim["id"] = new_claim_id
        if claim.get("manuscript_id") == manuscript_id:
            claim["manuscript_id"] = "ms-PLACEHOLDER"

    for citation in out.get("citations") or []:
        claim_id = citation.get("claim_id")
        ref_id = citation.get("ref_id")
        if claim_id in claim_id_map:
            citation["claim_id"] = claim_id_map[claim_id]
        if ref_id in ref_id_map:
            citation["ref_id"] = ref_id_map[ref_id]

    for ambiguity in out.get("ambiguities") or []:
        claim_id = ambiguity.get("claim_id")
        if claim_id in claim_id_map:
            ambiguity["claim_id"] = claim_id_map[claim_id]
        candidates = ambiguity.get("candidate_ref_ids")
        if candidates:
            ambiguity["candidate_ref_ids"] = [ref_id_map.get(ref_id, ref_id) for ref_id in candidates]

    debug_payload = out.get("_debug") or {}
    # Backend arbitration metrics are observational diagnostics: character
    # counts and installed-backend errors legitimately vary by environment.
    # Dedicated extraction-quality tests pin their schema and selection logic;
    # parse golden cases continue to pin the semantic parser payload.
    (debug_payload.get("extract") or {}).pop("pdf_variant_audit", None)
    debug_ambiguities = debug_payload.get("ambiguities") or []
    for ambiguity in debug_ambiguities:
        claim_id = ambiguity.get("claim_id")
        if claim_id in claim_id_map:
            ambiguity["claim_id"] = claim_id_map[claim_id]
        candidates = ambiguity.get("candidate_ref_ids")
        if candidates:
            ambiguity["candidate_ref_ids"] = [ref_id_map.get(ref_id, ref_id) for ref_id in candidates]
    debug_orphans = debug_payload.get("orphans") or []
    for orphan in debug_orphans:
        claim_id = orphan.get("claim_id")
        if claim_id in claim_id_map:
            orphan["claim_id"] = claim_id_map[claim_id]
    return out


def _ocr_stub_for(spec: dict):
    stub_text = spec.get("ocr_stub_text")
    if not stub_text:
        return None

    def _stub(path: str, ocr_lang: str, meta: dict, ocr_notice):
        patched = dict(meta)
        patched["pdf_method"] = f"ocr:{spec.get('ocr_stub_method', 'stub')}"
        patched["ocr"] = True
        patched["ocr_lang"] = ocr_lang
        return stub_text, patched

    return _stub


def _run_case(spec: dict) -> tuple[dict, str]:
    saved_modules = _snapshot_scoped_modules()
    extract_mod, parse_mod = _load_parse_modules()
    input_rel = spec.get("input")
    input_path = _FIXTURE_DIR / input_rel if input_rel else (_INPUT_DIR / spec["filename"])
    saved_ocr = extract_mod._manuscript_ocr
    stub = _ocr_stub_for(spec)
    try:
        if stub is not None:
            extract_mod._manuscript_ocr = stub
        result, debug_md = parse_mod.parse(
            str(input_path),
            spec.get("window", 1),
            spec.get("citations_mode", "auto"),
        )
    finally:
        extract_mod._manuscript_ocr = saved_ocr
        _restore_scoped_modules(saved_modules)
    return _normalize_result(result), debug_md


def _generate_cases() -> list[dict]:
    _write_inputs()
    cases = []
    for spec in _CASE_SPECS:
        result, debug_md = _run_case(spec)
        cases.append(
            {
                "name": spec["name"],
                "input": f"inputs/{spec['filename']}",
                "window": spec.get("window", 1),
                "citations_mode": spec.get("citations_mode", "auto"),
                "ocr_stub_text": spec.get("ocr_stub_text"),
                "ocr_stub_method": spec.get("ocr_stub_method"),
                "expected": {
                    "result": result,
                    "debug_md": debug_md,
                },
            }
        )
    return cases


class TestParseGolden(unittest.TestCase):
    maxDiff = None

    def test_parse_golden(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                result, debug_md = _run_case(case)
                self.assertEqual(result, case["expected"]["result"])
                self.assertEqual(debug_md, case["expected"]["debug_md"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--generate"]:
        cases = _generate_cases()
        _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        _FIXTURE_PATH.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(cases)} cases to {_FIXTURE_PATH}")
    else:
        unittest.main()
