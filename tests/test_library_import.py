# tests/test_library_import.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import sqlite3
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from core.app.library import importer as library_import, metadata as library_metadata
from core.app.library.importer import LibraryImporter
from core.fetch.storage import content_store
from core.resolve import sources


fitz = pytest.importorskip("pymupdf")
_ORIGINAL_METADATA_LOOKUP = library_metadata.lookup_identity


def _pdf(
    path: Path,
    title: str,
    doi: str | list[str] | None = None,
    *,
    author_line: str = "Research Group 2024",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), title, fontsize=18)
    page.insert_text((72, 135), author_line, fontsize=11)
    dois = [doi] if isinstance(doi, str) else list(doi or ())
    for index, value in enumerate(dois):
        page.insert_text((72, 160 + index * 15), f"https://doi.org/{value}", fontsize=10)
    body = "This manuscript contains enough readable scholarly text for deterministic extraction and quality review. " * 12
    page.insert_textbox((72, 190, 520, 760), body, fontsize=10)
    document.set_metadata({"title": title})
    document.save(path)
    document.close()


def _text_document(path: Path, title: str, doi: str, *, markdown: bool = False) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    heading = f"# {title}" if markdown else title
    content = (
        f"{heading}\nJane Doe\n2024\nhttps://doi.org/{doi}\n\n"
        + "The article body contains enough readable text to confirm its identity. " * 12
    )
    encoded = content.encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def _builtin_non_pdf_document(path: Path, suffix: str, title: str, doi: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "The article body contains enough readable scholarly text for identity review. " * 12
    if suffix == ".docx":
        word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        paragraphs = (title, "Jane Doe 2024", f"https://doi.org/{doi}", body)
        paragraph_xml = "".join(
            f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"
            for text in paragraphs
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{word_ns}"><w:body>{paragraph_xml}'
            "<w:sectPr/></w:body></w:document>"
        )
        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            archive.writestr("word/document.xml", document_xml)
    elif suffix == ".tex":
        path.write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            f"\\title{{{title}}}\n\\author{{Jane Doe}}\n\\maketitle\n"
            "2024\n"
            f"https://doi.org/{doi}\n\n{body}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
    else:
        xhtml = suffix == ".xhtml"
        namespace = ' xmlns="http://www.w3.org/1999/xhtml"' if xhtml else ""
        declaration = '<?xml version="1.0" encoding="UTF-8"?>\n' if xhtml else "<!doctype html>\n"
        path.write_text(
            declaration
            + f"<html{namespace}><head><title>{escape(title)}</title></head><body>"
            + "<main><article>"
            + f"<h1>{escape(title)}</h1><p>Jane Doe 2024</p>"
            + f"<p>https://doi.org/{escape(doi)}</p><p>{escape(body)}</p>"
            + "</article></main></body></html>",
            encoding="utf-8",
        )
    return path.read_bytes()


def _conflicting_pdf(
    path: Path,
    title: str,
    doi: str | list[str],
    *,
    property_title: str = "Thomson",
    author_line: str = "Jane Doe 2024",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    page = document.new_page()
    title_rect = fitz.Rect(72, 100, 520, 170)
    assert page.insert_textbox(title_rect, title, fontsize=16) >= 0
    page.insert_text((72, 190), author_line, fontsize=11)
    dois = [doi] if isinstance(doi, str) else list(doi)
    for index, value in enumerate(dois):
        page.insert_text((72, 215 + index * 15), f"https://doi.org/{value}", fontsize=10)
    page.insert_textbox(
        (72, 260, 520, 760),
        "This manuscript contains enough readable scholarly text for deterministic extraction and quality review. " * 12,
        fontsize=10,
    )
    document.set_metadata({"title": property_title})
    document.save(path)
    document.close()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"CITATION_VERIFIER_STATE_DIR": str(tmp_path / "state")}


@pytest.fixture(autouse=True)
def _disable_external_library_metadata(monkeypatch):
    def unavailable(scheme, identifier):
        provenance = {
            "provider": "doi.org",
            "scheme": scheme,
            "requested_identifier": identifier,
        }
        raise library_metadata.LibraryMetadataError(
            "metadata provider unavailable in test", provenance=provenance,
        )

    monkeypatch.setattr(library_metadata, "lookup_identity", unavailable)


@pytest.mark.parametrize("metadata_location", ["front", "abstract", "introduction", "absent"])
def test_native_pdf_metadata_title_does_not_become_the_journal_masthead(
    tmp_path, metadata_location,
):
    original = tmp_path / "journal-masthead.pdf"
    title = "Combining liquid biopsy and radiomics for precision care"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 110), "Pharmacological Research", fontsize=26)
    if metadata_location == "front":
        page.insert_text((72, 175), "Combining liquid biopsy and radiomics", fontsize=16)
        page.insert_text((72, 197), "for precision care", fontsize=16)
    else:
        page.insert_text((72, 175), "A different visible article title", fontsize=16)
    page.insert_text((72, 225), "Jane Doe 2024", fontsize=11)
    page.insert_text((72, 245), "https://doi.org/10.1234/native-title", fontsize=10)
    page.insert_text(
        (72, 275), "Introduction" if metadata_location == "introduction" else "Abstract",
        fontsize=11,
    )
    if metadata_location in {"abstract", "introduction"}:
        page.insert_text((72, 295), title, fontsize=10)
    page.insert_textbox(
        (72, 320, 520, 760),
        "This readable paper studies clinical outcomes and deterministic document identity. " * 20,
        fontsize=10,
    )
    document.set_metadata({"title": title})
    document.save(original)
    document.close()
    original_bytes = original.read_bytes()
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))

    row = importer.inspect([str(original)])[0]

    if metadata_location == "front":
        assert row["title"] == title
        assert row["status"] == "identified", row
        assert row["method"].endswith("pdf_metadata_front_matter")
        reopened = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
        pending = reopened.pending_rows()[0]
        assert pending["title"] == title
        saved = reopened.save([pending])[0]
        assert saved["status"] == "stored", saved
    else:
        assert row["status"] == "needs_review", row
        assert row["reason_code"] == "title_conflict"
    assert original.read_bytes() == original_bytes


def test_inspect_save_catalogues_pdf_and_preserves_original_with_state_root(tmp_path):
    original = tmp_path / "input" / "paper.pdf"
    _pdf(original, "Local Deterministic Identity Paper", "10.1234/library.1")
    original_bytes = original.read_bytes()
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))

    rows = importer.inspect([str(original)])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "identified"
    assert row["title"] == "Local Deterministic Identity Paper"
    assert row["doi"] == "10.1234/library.1"
    assert row["reason_code"] is None
    assert row["reason_params"] == {}
    assert "_text" not in row

    save_events = []
    saved = importer.save([row], progress_callback=save_events.append)
    assert saved[0]["status"] == "stored", saved[0]
    assert saved[0]["reason_code"] is None
    assert saved[0]["reason_params"] == {}
    assert original.read_bytes() == original_bytes
    assert Path(saved[0]["stored_path"]).is_file()
    save_file_events = [event for event in save_events if "file" in event]
    assert [event["stage"] for event in save_file_events] == [
        "reading", "extracting", "checking", "checking", "storing", "processed",
    ]
    assert [event["completed"] for event in save_file_events] == [0, 0, 0, 0, 0, 1]
    assert save_file_events[-1]["total"] == 1
    assert save_events[-1] == {"stage": "finalizing", "completed": 1, "total": 1}
    cached = content_store.find_reusable_parsed_text(
        str(tmp_path / "runs" / "anchor"),
        {"doi": "10.1234/library.1", "title": "Local Deterministic Identity Paper"},
        tiers=("fulltext",),
        environ=_env(tmp_path),
    )
    assert cached is not None
    assert cached["supplied_via"] == "user_library_review"
    with sqlite3.connect(content_store.db_path(str(tmp_path / "runs" / "anchor"), _env(tmp_path))) as conn:
        row = conn.execute(
            "SELECT library_item_id FROM parsed_texts WHERE parsed_text_id=?",
            (cached["parsed_text_id"],),
        ).fetchone()
    assert row[0] == saved[0]["library_item_id"]


@pytest.mark.parametrize(("suffix", "fmt", "markdown"), [
    (".TXT", "txt", False),
    (".markdown", "markdown", True),
])
def test_non_pdf_import_review_save_preserves_format_bytes_and_provenance(
    tmp_path, suffix, fmt, markdown,
):
    title = f"Registry Based {fmt.title()} Identity Paper"
    doi = f"10.1234/library-{fmt}"
    original = tmp_path / "input" / f"paper{suffix}"
    original_bytes = _text_document(original, title, doi, markdown=markdown)
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)
    importer = LibraryImporter(anchor, environ)

    inspected = importer.inspect([str(original)])
    assert len(inspected) == 1
    row = inspected[0]
    assert row["status"] == "identified", row
    assert row["source_extension"] == suffix
    assert row["content_family"] == "text"
    assert row["extraction_format"] == fmt
    assert row["extraction_method"] == f"extractor:{fmt}"
    staged = (
        tmp_path / "state" / "library_imports" / row["_session_id"]
        / "staged" / f"{row['sha256']}{suffix}"
    )
    assert staged.read_bytes() == original_bytes

    reopened = LibraryImporter(anchor, environ)
    pending = reopened.pending_rows()
    assert len(pending) == 1
    assert pending[0]["source_extension"] == suffix
    assert pending[0]["extraction_format"] == fmt
    reviewed = reopened.review_identity(pending[0])
    assert reviewed["status"] == "identified", reviewed
    saved = reopened.save([reviewed])[0]
    assert saved["status"] == "stored", f"{saved.get('reason_code')}: {saved.get('reason')}"
    assert Path(saved["stored_path"]).read_bytes() == original_bytes
    assert original.read_bytes() == original_bytes

    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["initial_evidence"]["extraction"] == {
        "content_family": "text", "format": fmt, "method": f"extractor:{fmt}",
    }
    assert record["revisions"][-1]["evidence"]["extraction_format"] == fmt
    assert record["revisions"][-1]["evidence"]["extraction_method"] == f"extractor:{fmt}"

    with sqlite3.connect(content_store.db_path(anchor, environ)) as conn:
        library_file = conn.execute(
            "SELECT original_filename, file_format FROM library_items WHERE library_item_id=?",
            (saved["library_item_id"],),
        ).fetchone()
        parsed_file = conn.execute(
            "SELECT file_format, source_ref, sha256, extraction_method, library_item_id "
            "FROM parsed_texts WHERE parsed_text_id=?",
            (saved["parsed_text_id"],),
        ).fetchone()
    assert library_file[1] == fmt
    assert library_file[0].endswith(suffix)
    assert parsed_file[0:2] == (fmt, str(original.resolve()))
    assert len(parsed_file[2]) == 64
    assert parsed_file[3:] == (f"extractor:{fmt}", saved["library_item_id"])


@pytest.mark.parametrize(("suffix", "fmt", "family"), [
    (".docx", "docx", "docx"),
    (".tex", "latex", "text"),
    (".html", "html", "html"),
    (".htm", "html", "html"),
    (".xhtml", "html", "html"),
])
def test_builtin_non_pdf_import_review_save_preserves_format_and_source_bytes(
    tmp_path, suffix, fmt, family,
):
    title = f"Registry Based {fmt.title()} Identity Paper"
    doi = f"10.1234/library-{fmt}-{suffix.lstrip('.')}"
    original = tmp_path / "input" / f"paper{suffix}"
    original_bytes = _builtin_non_pdf_document(original, suffix, title, doi)
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)
    importer = LibraryImporter(anchor, environ)

    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row
    assert row["source_extension"] == suffix
    assert row["content_family"] == family
    assert row["extraction_format"] == fmt
    assert row["extraction_method"] == f"extractor:{fmt}"
    staged = (
        tmp_path / "state" / "library_imports" / row["_session_id"]
        / "staged" / f"{row['sha256']}{suffix}"
    )
    assert staged.read_bytes() == original_bytes

    reopened = LibraryImporter(anchor, environ)
    pending = reopened.pending_rows()
    assert len(pending) == 1
    assert pending[0]["source_extension"] == suffix
    assert pending[0]["extraction_format"] == fmt
    reviewed = reopened.review_identity(pending[0])
    assert reviewed["status"] == "identified", reviewed
    saved = reopened.save([reviewed])[0]
    assert saved["status"] == "stored", saved
    assert Path(saved["stored_path"]).read_bytes() == original_bytes
    assert original.read_bytes() == original_bytes

    with sqlite3.connect(content_store.db_path(anchor, environ)) as conn:
        formats = conn.execute(
            "SELECT li.file_format, pt.file_format "
            "FROM library_items li JOIN parsed_texts pt USING (library_item_id) "
            "WHERE li.library_item_id=?",
            (saved["library_item_id"],),
        ).fetchone()
    assert formats == (fmt, fmt)


@pytest.mark.parametrize("field", ["content_family", "extraction_format", "extraction_method"])
@pytest.mark.parametrize("invalid_state", ["missing", "empty", "wrong_type"])
def test_review_rejects_missing_or_empty_initial_format_metadata(
    tmp_path, field, invalid_state,
):
    original = tmp_path / "paper.txt"
    _text_document(original, "Required Format Metadata Paper", "10.1234/required-format")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row

    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    record = payload["files"][row["sha256"]]
    if invalid_state == "missing":
        record.pop(field)
    elif invalid_state == "wrong_type":
        record[field] = {"invalid": True}
    else:
        record[field] = ""
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    reviewed = importer.review_identity(row)
    assert reviewed["status"] == "needs_review"
    assert reviewed["reason_code"] == "invalid_review_record"


def test_library_discovery_uses_enabled_extractor_registry_for_files_and_folders(
    monkeypatch, tmp_path,
):
    supported = tuple(library_import.parse_extract.supported_extensions())
    future_extension = ".future"
    monkeypatch.setattr(
        library_import.parse_extract,
        "supported_extensions",
        lambda: (*supported, future_extension),
    )
    folder = tmp_path / "incoming"
    text_file = folder / "paper.TXT"
    future_file = folder / "paper.FUTURE"
    unsupported_file = folder / "ignore.csv"
    for path in (text_file, future_file, unsupported_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")

    assert LibraryImporter._files([str(future_file)]) == [future_file.resolve()]
    assert LibraryImporter._files([str(folder)]) == [
        future_file.resolve(), text_file.resolve(),
    ]


def test_library_discovery_excludes_disabled_registry_extensions(monkeypatch, tmp_path):
    text_file = tmp_path / "paper.txt"
    text_file.write_text("fixture", encoding="utf-8")
    supported = tuple(
        extension for extension in library_import.parse_extract.supported_extensions()
        if extension.casefold() != ".txt"
    )
    monkeypatch.setattr(library_import.parse_extract, "supported_extensions", lambda: supported)

    assert LibraryImporter._files([str(text_file)]) == []


def test_identical_bytes_with_different_extractor_extensions_fail_explicitly(tmp_path):
    folder = tmp_path / "incoming"
    first = folder / "a.txt"
    second = folder / "b.markdown"
    content = (
        "Shared Registry Identity Paper\nJane Doe\n2024\n"
        "https://doi.org/10.1234/shared-format-bytes\n\n"
        + "The same bytes can select different extractors by extension. " * 12
    ).encode("utf-8")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(content)
    second.write_bytes(content)

    rows = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).inspect([
        str(folder)
    ])

    assert len(rows) == 2
    assert rows[0]["status"] == "identified", rows[0]
    assert rows[1]["status"] == "unreadable"
    assert rows[1]["reason_code"] == "source_format_conflict"


def test_future_registered_extractor_can_be_reviewed_and_saved_without_importer_switches(
    monkeypatch, tmp_path,
):
    extension = ".future"
    title = "Future Registry Extractor Identity Paper"
    doi = "10.1234/future-registry"
    text = (
        f"{title}\nJane Doe\n2024\nhttps://doi.org/{doi}\n\n"
        + "A future extractor supplies deterministic text for identity review. " * 12
    )
    supported = tuple(library_import.parse_extract.supported_extensions())
    monkeypatch.setattr(
        library_import.parse_extract,
        "supported_extensions",
        lambda: (*supported, extension),
    )
    monkeypatch.setattr(
        library_import.parse_extract,
        "probe_file",
        lambda _path: {"declared_format": "futuretext", "detected_format": "futuretext"},
    )
    monkeypatch.setattr(
        library_import.parse_extract,
        "extract_text",
        lambda _path: (text, "futuretext", {"method": "future-reader-v1"}),
    )
    original = tmp_path / f"paper{extension}"
    original.write_text("source bytes are preserved", encoding="utf-8")
    original_bytes = original.read_bytes()
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)

    importer = LibraryImporter(anchor, environ)
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row
    assert row["extraction_format"] == "futuretext"
    assert row["extraction_method"] == "future-reader-v1"
    assert row["source_extension"] == extension
    reopened = LibraryImporter(anchor, environ)
    pending = reopened.pending_rows()[0]
    reviewed = reopened.review_identity(pending)
    assert reviewed["status"] == "identified", reviewed
    saved = reopened.save([reviewed])[0]
    assert saved["status"] == "stored", saved
    assert Path(saved["stored_path"]).read_bytes() == original_bytes
    with sqlite3.connect(content_store.db_path(anchor, environ)) as conn:
        stored_format = conn.execute(
            "SELECT file_format FROM parsed_texts WHERE parsed_text_id=?",
            (saved["parsed_text_id"],),
        ).fetchone()[0]
    assert stored_format == "futuretext"


@pytest.mark.parametrize("content_kind", ["empty", "mislabeled_pdf"])
def test_non_pdf_empty_or_mislabeled_content_fails_closed(tmp_path, content_kind):
    original = tmp_path / f"{content_kind}.txt"
    if content_kind == "mislabeled_pdf":
        pdf_path = tmp_path / "source.pdf"
        _pdf(pdf_path, "Mislabeled Registry File Identity")
        original.write_bytes(pdf_path.read_bytes())
    else:
        original.write_bytes(b"")

    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "unreadable"
    assert row["reason_code"] == "unreadable_file"
    assert importer.save([row])[0]["reason_code"] == "identity_review_required"


def test_non_pdf_staged_tampering_fails_review_and_save(tmp_path):
    original = tmp_path / "paper.txt"
    _text_document(original, "Tamper Guard Plain Text Paper", "10.1234/tamper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row
    staged = (
        tmp_path / "state" / "library_imports" / row["_session_id"]
        / "staged" / f"{row['sha256']}.txt"
    )
    staged.write_text("tampered bytes", encoding="utf-8")

    saved = importer.save([row])[0]
    assert saved["status"] == "needs_review"
    assert saved["reason_code"] == "staged_file_changed"
    reviewed = importer.review_identity(row)
    assert reviewed["status"] == "needs_review"
    assert reviewed["reason_code"] == "staged_file_changed"
    assert original.read_text(encoding="utf-8").startswith("Tamper Guard Plain Text Paper")


def test_invalid_staged_extension_record_fails_closed_before_path_access(tmp_path):
    original = tmp_path / "paper.txt"
    _text_document(original, "Extension Path Guard Paper", "10.1234/path-guard")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row
    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["files"][row["sha256"]]["source_extension"] = "../outside"
    review_path.write_text(json.dumps(payload), encoding="utf-8")

    failed = importer.save([row])[0]
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "invalid_review_record"


@pytest.mark.parametrize("select_folder", [False, True])
def test_inspect_preserves_distinct_pdf_paths_differing_only_by_case(tmp_path, select_folder):
    incoming = tmp_path / "incoming"
    upper = incoming / "Paper.pdf"
    lower = incoming / "paper.pdf"
    _pdf(upper, "First distinct research paper")
    _pdf(lower, "Second distinct research paper")
    if upper.samefile(lower):
        pytest.skip("filesystem does not support distinct case-sensitive paths")

    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    paths = [str(incoming)] if select_folder else [str(upper), str(lower), str(upper)]
    result = importer.inspect(paths)
    assert len(result) == 2
    assert {row["file"] for row in result} == {
        str(upper.resolve()), str(lower.resolve()),
    }


def test_inspect_recurses_deduplicates_bytes_and_preserves_same_named_distinct_pdfs(tmp_path):
    first = tmp_path / "in" / "a" / "paper.pdf"
    second = tmp_path / "in" / "b" / "paper.pdf"
    duplicate = tmp_path / "in" / "copy.pdf"
    _pdf(first, "Paper Alpha Identity", "10.1234/alpha")
    _pdf(second, "Paper Beta Identity", "10.1234/beta")
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(first, duplicate)

    rows = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).inspect([
        str(tmp_path / "in")
    ])
    assert len(rows) == 2
    assert {row["doi"] for row in rows} == {"10.1234/alpha", "10.1234/beta"}
    assert len({row["sha256"] for row in rows}) == 2


def test_inspect_reports_final_file_manifest_and_per_file_progress(tmp_path):
    folder = tmp_path / "library"
    original = folder / "a" / "paper.pdf"
    duplicate = folder / "b" / "copy.pdf"
    corrupt = folder / "c" / "bad.pdf"
    _pdf(original, "Progress Identity Paper", "10.1234/progress")
    duplicate.parent.mkdir(parents=True)
    shutil.copyfile(original, duplicate)
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not actually a PDF")

    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    events = []
    rows = importer.inspect([str(folder)], progress_callback=events.append)

    queued = events[1]
    assert queued["stage"] == "queued"
    assert queued["files"] == sorted([str(original), str(duplicate), str(corrupt)])
    assert queued["total"] == 3
    assert len(rows) == 2
    assert [row["status"] for row in rows].count("identified") == 1
    assert [row["status"] for row in rows].count("unreadable") == 1

    per_file = {}
    for event in events:
        if "file" in event:
            per_file.setdefault(event["file"], []).append(event)
    assert set(per_file) == set(queued["files"])
    for file_events in per_file.values():
        terminal = [event for event in file_events if event["stage"] == "processed"]
        assert len(terminal) == 1
        assert terminal[0]["completed"] == terminal[0]["index"] + 1
        assert terminal[0]["total"] == 3

    original_events = per_file[str(original)]
    assert [event["stage"] for event in original_events] == [
        "reading", "staging", "extracting", "checking", "identifying", "processed",
    ]
    assert [event["stage"] for event in per_file[str(duplicate)]] == [
        "reading", "processed",
    ]

    repeated_events = []
    repeated = importer.inspect([str(original)], progress_callback=repeated_events.append)
    assert repeated[0]["sha256"] == rows[0]["sha256"]
    assert [event["stage"] for event in repeated_events if "file" in event] == [
        "reading", "processed",
    ]


def test_progress_callback_error_is_not_reported_as_unreadable_pdf(tmp_path):
    original = tmp_path / "progress-error.pdf"
    _pdf(original, "Progress Callback Error Paper", "10.1234/progress-error")

    def fail_progress(event):
        if event["stage"] == "extracting":
            raise RuntimeError("progress sink failed")

    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    with pytest.raises(Exception, match="progress callback failed"):
        importer.inspect([str(original)], progress_callback=fail_progress)


def test_save_progress_callback_error_is_not_reported_as_pdf_failure(tmp_path):
    original = tmp_path / "save-progress-error.pdf"
    _pdf(original, "Save Progress Callback Error Paper", "10.1234/save-progress-error")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]

    def fail_progress(event):
        if event["stage"] == "extracting":
            raise RuntimeError("progress sink failed")

    with pytest.raises(Exception, match="progress callback failed"):
        importer.save([row], progress_callback=fail_progress)
    assert importer.pending_rows()[0]["sha256"] == row["sha256"]


def test_pending_rows_survive_a_new_importer_and_conflicting_edit_fails_closed(tmp_path):
    original = tmp_path / "input.pdf"
    _pdf(original, "Pending Identity Paper", "10.1234/pending")
    first = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = first.inspect([str(original)])[0]
    row["doi"] = "10.1234/other"

    pending = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).pending_rows()
    assert len(pending) == 1
    assert pending[0]["sha256"] == row["sha256"]
    failed = first.save([row])[0]
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "identity_review_required"
    assert failed["reason_params"] == {}
    assert failed["_session_id"] == row["_session_id"]
    assert failed["_file_id"] == row["_file_id"]
    assert "reviewed against the PDF" in failed["reason"]
    assert original.exists()


def test_title_only_import_requires_manual_author_and_year_and_saves_review_history(tmp_path):
    original = tmp_path / "input.pdf"
    _pdf(original, "Title Only Research Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "missing_author_year"
    failed = importer.review_identity(row)
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "missing_author_year"
    assert importer.save([failed])[0]["reason_code"] == "identity_review_required"

    row["authors"] = "Research Group"
    row["year"] = 2024
    row = importer.review_identity(row)
    assert row["status"] == "identified", row
    saved = importer.save([row])[0]
    assert saved["status"] == "stored", saved
    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["reason_code"] is None
    assert record["reason_params"] == {}
    assert record["initial_evidence"]["title"] == "Title Only Research Paper"
    assert record["revisions"][-1]["after"]["authors"] == "Research Group"
    cached = content_store.find_reusable_parsed_text(
        str(tmp_path / "runs" / "anchor"),
        {"title": "Title Only Research Paper", "ay_surname": "Group", "year": 2024},
        tiers=("fulltext",), environ=_env(tmp_path),
    )
    assert cached is not None


@pytest.mark.parametrize(("authors", "expected_surname"), [
    ("Jane Doe", "Doe"),
    ("Doe, Jane", "Doe"),
    ("Doe J", "Doe"),
])
def test_manual_author_formats_save_and_reuse_the_correct_title_author_year_alias(
    tmp_path, authors, expected_surname,
):
    original = tmp_path / "manual-author.pdf"
    title = "Manual Author Identity Paper"
    _pdf(original, title, author_line="Jane Doe 2024")
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)
    importer = LibraryImporter(anchor, environ)
    row = importer.inspect([str(original)])[0]
    assert row["doi"] is None
    assert row["pmid"] is None

    row["authors"] = authors
    row["year"] = 2024
    row = importer.review_identity(row)
    assert row["status"] == "identified", row
    saved = importer.save([row])[0]
    assert saved["status"] == "stored", saved
    assert saved["authors"] == authors

    record_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["authors"] == authors
    assert record["revisions"][-1]["after"]["authors"] == authors

    cached = content_store.find_reusable_parsed_text(
        anchor,
        {"title": title, "ay_surname": expected_surname, "year": 2024},
        tiers=("fulltext",),
        environ=environ,
    )
    assert cached is not None
    assert content_store.find_reusable_parsed_text(
        anchor,
        {"title": title, "ay_surname": "Jane", "year": 2024},
        tiers=("fulltext",),
        environ=environ,
    ) is None


def test_ambiguous_initials_do_not_create_a_title_author_year_alias(tmp_path):
    original = tmp_path / "ambiguous-author.pdf"
    title = "Ambiguous Manual Author Paper"
    _pdf(original, title, author_line="Jane Doe 2024")
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)
    importer = LibraryImporter(anchor, environ)
    row = importer.inspect([str(original)])[0]
    row["authors"] = "J. D."
    row["year"] = 2024

    failed = importer.review_identity(row)
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "missing_author_year"
    assert failed["authors"] == "J. D."
    assert importer.save([failed])[0]["reason_code"] == "identity_review_required"
    assert content_store.find_reusable_parsed_text(
        anchor,
        {"title": title, "ay_surname": "D", "year": 2024},
        tiers=("fulltext",),
        environ=environ,
    ) is None


def test_wrong_manual_author_cannot_make_a_title_only_pdf_reusable(tmp_path):
    original = tmp_path / "wrong-author.pdf"
    _pdf(original, "Title Only Research Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    row["authors"] = "Ada Researcher"
    row["year"] = 2024
    failed = importer.review_identity(row)
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "author_mismatch"
    assert "front-matter author" in failed["reason"]
    assert importer.save([failed])[0]["reason_code"] == "identity_review_required"


def test_conflicting_pdf_metadata_and_layout_titles_need_review(tmp_path):
    original = tmp_path / "conflicting-titles.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 90), "Visible Front Page Title", fontsize=18)
    page.insert_text((72, 130), "Research Group 2024", fontsize=11)
    page.insert_textbox(
        (72, 190, 520, 760),
        "This manuscript contains enough readable scholarly text for deterministic extraction and quality review. " * 12,
        fontsize=10,
    )
    document.set_metadata({"title": "Different Metadata Title"})
    document.save(original)
    document.close()

    row = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path)
    ).inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"
    assert row["reason_params"] == {
        "metadata_title": "Different Metadata Title",
        "layout_title": "Visible Front Page Title",
    }
    assert "Different Metadata Title" in row["reason"]
    assert "Visible Front Page Title" in row["reason"]
    pending = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path)
    ).pending_rows()
    assert len(pending) == 1
    assert pending[0]["reason_code"] == "title_conflict"
    assert pending[0]["reason_params"] == row["reason_params"]


def test_corrupt_pdf_is_reported_per_file_and_manual_identity_cannot_save_it(tmp_path):
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"not actually a PDF")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(corrupt)])[0]
    assert row["status"] == "unreadable"
    assert row["reason_code"] == "unreadable_pdf"
    assert row["reason_params"] == {}
    row.update(title="Forged Title", doi="10.1234/forged")
    failed = importer.review_identity(row)
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "unreadable_pdf"
    assert failed["reason_params"] == {}
    assert "integrity" in failed["reason"] or "content" in failed["reason"]
    assert importer.save([failed])[0]["reason_code"] == "identity_review_required"


def test_short_readable_pdf_fails_quality_gate_before_identity_can_be_saved(tmp_path):
    short_pdf = tmp_path / "short-readable.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), "Short readable quality probe.", fontsize=12)
    document.save(short_pdf)
    document.close()

    with fitz.open(short_pdf) as source:
        text = "\n".join(page.get_text() for page in source)
    assert 0 < len(text.strip()) < 200

    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(short_pdf)])[0]
    assert row["status"] == "unreadable"
    assert row["reason_code"] == "unreadable_pdf"
    row.update(title="Short Readable Quality Probe", authors="Jane Doe", year=2024)
    rejected = importer.review_identity(row)
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "unreadable_pdf"
    assert importer.save([rejected])[0]["reason_code"] == "identity_review_required"
    attested = importer.review_identity(row, operator_attested=True)
    assert attested["status"] == "needs_review"
    assert attested["reason_code"] == "unreadable_pdf"
    pending = importer.pending_rows()
    assert len(pending) == 1
    assert pending[0]["reason_code"] == "unreadable_pdf"


def test_unexpected_extraction_error_uses_unreadable_pdf_code(monkeypatch, tmp_path):
    original = tmp_path / "extraction-error.pdf"
    _pdf(original, "Extraction Error Paper", "10.1234/extraction-error")

    def fail_extraction(_path):
        raise RuntimeError("temporary extractor failure")

    monkeypatch.setattr("core.app.library.importer.parse_extract.extract_text", fail_extraction)
    row = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).inspect([
        str(original)
    ])[0]

    assert row["status"] == "unreadable"
    assert row["reason_code"] == "unreadable_pdf"
    assert row["reason_params"] == {}
    assert "temporary extractor failure" in row["reason"]


def test_unexpected_storage_error_uses_save_failed_code(monkeypatch, tmp_path):
    original = tmp_path / "storage-error.pdf"
    _pdf(original, "Storage Error Paper", "10.1234/storage-error")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]

    def fail_storage(*_args, **_kwargs):
        raise RuntimeError("temporary storage failure")

    monkeypatch.setattr("core.app.library.importer.content_store.archive_user_original", fail_storage)
    failed = importer.save([row])[0]

    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "save_failed"
    assert failed["reason_params"] == {}
    assert failed["_session_id"] == row["_session_id"]
    assert failed["_file_id"] == row["_file_id"]
    assert "temporary storage failure" in failed["reason"]


def test_multiple_observed_dois_allow_selecting_one_but_reject_external_value(tmp_path):
    original = tmp_path / "two-dois.pdf"
    _pdf(original, "Multiple Identifier Research Paper", [
        "10.1234/first", "10.1234/second",
    ])
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "multiple_identifiers"
    assert row["_identifier_choices"]["doi"] == ["10.1234/first", "10.1234/second"]

    invalid = dict(row, doi="10.1234/external")
    rejected = importer.review_identity(invalid)
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "choose_doi"
    assert rejected["reason_params"] == {}
    assert rejected["_session_id"] == row["_session_id"]
    assert rejected["_file_id"] == row["_file_id"]
    assert "choose one DOI" in rejected["reason"]

    retry = dict(rejected, doi="10.1234/second")
    retry = importer.review_identity(retry)
    assert retry["status"] == "identified", retry
    saved = importer.save([retry])[0]
    assert saved["status"] == "stored", saved
    assert saved["reason_code"] is None
    assert saved["reason_params"] == {}
    assert saved["doi"] == "10.1234/second"


def test_duplicate_references_for_same_work_are_collapsed_but_distinct_tie_is_reviewed(tmp_path):
    original = tmp_path / "duplicate-ref.pdf"
    title = "Duplicate Reference Matching Paper"
    _pdf(original, title, "10.1234/shared")
    duplicate_work = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path), references=[
            {"id": "cached-ref", "doi": "10.1234/shared", "title": title},
            {"id": "run-ref", "doi": "10.1234/shared", "title": title, "year": 2024},
        ]
    )
    row = duplicate_work.inspect([str(original)])[0]
    assert row["status"] == "identified"
    assert row["ref_id"] in {"cached-ref", "run-ref"}

    no_id = tmp_path / "distinct-tie.pdf"
    tie_title = "Shared Title Without Stable Disambiguator"
    _pdf(no_id, tie_title)
    distinct_work = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path), references=[
            {"id": "work-a", "doi": "10.1234/work-a", "title": tie_title},
            {"id": "work-b", "doi": "10.1234/work-b", "title": tie_title},
        ]
    )
    tied = distinct_work.inspect([str(no_id)])[0]
    assert tied["status"] == "needs_review"
    assert tied["reason_code"] == "multiple_matches"
    assert "more than one reference" in tied["reason"]


def test_missing_title_and_identifiers_is_unidentified(tmp_path):
    original = tmp_path / "no-identity.pdf"
    document = fitz.open()
    page = document.new_page()
    body = "A body section with enough readable words to pass the PDF quality gate. " * 18
    page.insert_textbox((72, 400, 520, 700), body, fontsize=10)
    document.save(original)
    document.close()

    row = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path)
    ).inspect([str(original)])[0]
    assert row["status"] == "unidentified", row
    assert row["reason_code"] == "no_identity"
    assert "no reliable title or identifier" in row["reason"]


@pytest.mark.parametrize("first_ids, second_ids", [
    ({"doi": "10.1234/one"}, {"doi": "10.1234/two"}),
    ({"doi": "10.1234/shared", "pmid": "111"}, {"doi": "10.1234/shared", "pmid": "222"}),
])
def test_conflicting_identifiers_are_not_collapsed_by_shared_aliases(tmp_path, first_ids, second_ids):
    original = tmp_path / "same-title-different-work.pdf"
    title = "Shared Title With Conflicting Stable Identifiers"
    _pdf(original, title)
    common = {"title": title, "ay_surname": "Group", "year": 2024}
    importer = LibraryImporter(
        str(tmp_path / "runs" / "anchor"), _env(tmp_path), references=[
            {**common, "id": "one", **first_ids},
            {**common, "id": "two", **second_ids},
        ],
    )
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert "more than one reference" in row["reason"]


def test_new_importer_can_save_pending_item_and_changed_stage_is_rejected(tmp_path):
    original = tmp_path / "recoverable.pdf"
    _pdf(original, "Recovery Across Importer Instances", "10.1234/recovery")
    anchor = str(tmp_path / "runs" / "anchor")
    first = LibraryImporter(anchor, _env(tmp_path))
    inspected = first.inspect([str(original)])[0]

    recovered = LibraryImporter(anchor, _env(tmp_path))
    pending = recovered.pending_rows()
    assert len(pending) == 1
    assert pending[0]["_file_id"] == inspected["_file_id"]
    assert recovered.save(pending)[0]["status"] == "stored"

    second = LibraryImporter(anchor, _env(tmp_path))
    changed = second.inspect([str(original)])[0]
    staged = (
        tmp_path / "state" / "library_imports" / changed["_session_id"]
        / "staged" / f"{changed['sha256']}.pdf"
    )
    staged.write_bytes(b"modified after review")
    failed = second.save([changed])[0]
    assert failed["status"] == "needs_review"
    assert "integrity" in failed["reason"]


def test_corrupted_pending_review_fails_explicitly_without_discarding_file(tmp_path):
    original = tmp_path / "corrupt-review.pdf"
    _pdf(original, "Corrupted Review Recovery", "10.1234/review")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    review = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    review.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot read pending library review") as exc_info:
        LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).pending_rows()
    assert exc_info.value.reason_code == "pending_review_unreadable"
    assert exc_info.value.reason_params == {}
    assert review.read_text(encoding="utf-8") == "{invalid json"


def test_forged_identified_status_cannot_save_unreviewed_manual_identity(monkeypatch, tmp_path):
    original = tmp_path / "forged-status.pdf"
    _pdf(original, "Forged Status Identity Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    row.update(authors="Research Group", year=2024, status="identified")

    def archive_must_not_run(*_args, **_kwargs):
        pytest.fail("save must reject unreviewed identity before archiving")

    monkeypatch.setattr("core.app.library.importer.content_store.archive_user_original", archive_must_not_run)
    failed = importer.save([row])[0]
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "identity_review_required"
    assert failed["_session_id"] == row["_session_id"]
    assert failed["_file_id"] == row["_file_id"]


def test_manual_identity_approval_is_bound_to_canonical_snapshot(tmp_path):
    original = tmp_path / "snapshot-bound.pdf"
    _pdf(original, "Snapshot Bound Manual Identity")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    row.update(authors="Research Group", year=2024)

    reviewed = importer.review_identity(row)
    assert reviewed["status"] == "identified", reviewed
    edited = dict(reviewed, authors="Different Author")
    rejected = importer.save([edited])[0]
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "identity_review_required"
    assert importer.save([reviewed])[0]["status"] == "stored"


def test_operator_attestation_is_audited_and_saved_without_repeating_identity_probe(
    monkeypatch, tmp_path,
):
    original = tmp_path / "operator-attested.pdf"
    _pdf(original, "Operator Attested Research Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    row.update(authors="Research Group", year=2024)
    probe_calls = []
    monkeypatch.setattr(
        sources,
        "document_identity_probe",
        lambda *_args, **_kwargs: probe_calls.append("called")
        or {"ok": False, "decision": "inconclusive"},
    )

    original_row = dict(row)
    lookup_calls = []
    monkeypatch.setattr(
        library_metadata, "lookup_identity",
        lambda *_args, **_kwargs: lookup_calls.append("unexpected"),
    )
    reviewed = importer.review_identity(row, operator_attested=True)
    assert reviewed["status"] == "identified", reviewed
    assert probe_calls == [] and lookup_calls == []
    assert row == original_row
    assert reviewed["authors"] == "Research Group" and str(reviewed["year"]) == "2024"

    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    accepted = record["accepted_identity"]
    identity_review = record["identity_review"]
    assert accepted["source"] == "operator_attested"
    assert identity_review["accepted"] is True
    assert identity_review["source"] == "operator_attested"
    assert identity_review["staged_sha256"] == row["sha256"]
    assert identity_review["identity_snapshot"] == accepted["fields"]
    assert identity_review["document_identity"] is None
    decision = record["revisions"][-1]
    assert decision["decision"] == "identity_review_accepted"
    assert decision["evidence"]["source"] == "operator_attested"
    assert decision["evidence"]["staged_sha256"] == row["sha256"]
    assert decision["evidence"]["identity_snapshot"] == accepted["fields"]

    tampered = dict(reviewed, authors="A different author")
    rejected = importer.save([tampered])[0]
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "identity_review_required"

    saved = importer.save([reviewed])[0]
    assert saved["status"] == "stored", saved
    assert probe_calls == [] and lookup_calls == []
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["revisions"][-1]["evidence"]["identity_source"] == "operator_attested"


def test_operator_attestation_requires_exact_boolean_and_rejects_lookup_combination(
    monkeypatch, tmp_path,
):
    original = tmp_path / "operator-attestation-flags.pdf"
    _pdf(original, "Operator Attestation Flag Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    lookups = []
    monkeypatch.setattr(
        library_metadata,
        "lookup_identity",
        lambda *args, **kwargs: lookups.append((args, kwargs)),
    )

    invalid_bool = importer.review_identity(row, operator_attested=1)
    assert invalid_bool["status"] == "needs_review"
    assert invalid_bool["reason_code"] == "invalid_operator_attestation"
    combined = importer.review_identity(
        row, "doi", "10.1234/operator-attestation", operator_attested=True,
    )
    assert combined["status"] == "needs_review"
    assert combined["reason_code"] == "invalid_identity_decision"
    assert lookups == []
    assert importer.pending_rows()[0]["status"] == row["status"]


def test_operator_attestation_still_rejects_changed_staged_file(tmp_path):
    original = tmp_path / "operator-attestation-staged-hash.pdf"
    _pdf(original, "Operator Attestation Staged Hash Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    staged = (
        tmp_path / "state" / "library_imports" / row["_session_id"]
        / "staged" / f"{row['sha256']}.pdf"
    )
    staged.write_bytes(b"changed after inspection")

    rejected = importer.review_identity(row, operator_attested=True)
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "staged_pdf_changed"


@pytest.mark.parametrize(
    "dois,selected_doi,reason_code",
    [
        (["10.1234/attested-one"], "10.1234/unobserved", "doi_conflict"),
        (["10.1234/ambiguous-one", "10.1234/ambiguous-two"], "", "choose_doi"),
    ],
)
def test_operator_attestation_keeps_front_matter_identifier_conflicts_fail_closed(
    monkeypatch, tmp_path, dois, selected_doi, reason_code,
):
    original = tmp_path / "operator-attestation-identifier-conflict.pdf"
    title = "Operator Attestation Identifier Conflict Work"
    _conflicting_pdf(original, title, dois, author_line="Jane Doe 2024")

    def unavailable_lookup(*_args, **_kwargs):
        raise library_metadata.LibraryMetadataError(
            "fixture lookup unavailable", code="identity_metadata_unavailable",
        )

    monkeypatch.setattr(library_metadata, "lookup_identity", unavailable_lookup)
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    candidate = dict(row, title=title, authors="Jane Doe", year=2024, doi=selected_doi)

    rejected = importer.review_identity(candidate, operator_attested=True)
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == reason_code


def test_exact_doi_metadata_can_resolve_conflicting_pdf_property_title(monkeypatch, tmp_path):
    original = tmp_path / "metadata-title-conflict.pdf"
    visible_title = (
        "Beyond tissue biopsy: a diagnostic framework to address tumor "
        "heterogeneity in lung cancer"
    )
    doi = "10.1097/cco.0000000000000598"
    _conflicting_pdf(
        original, visible_title, doi, author_line="Wieland Voigt 2020",
    )
    original_bytes = original.read_bytes()

    def exact_lookup(scheme, identifier):
        assert scheme == "doi"
        assert identifier == doi
        return {
            "fields": {
                "title": visible_title, "authors": "Voigt", "year": 2020,
                "doi": doi,
            },
            "provenance": {
                "provider": "doi.org", "scheme": "doi",
                "requested_identifier": doi, "returned_identifier": doi,
                "response_sha256": "a" * 64,
            },
        }

    monkeypatch.setattr("core.app.library.importer.library_metadata.lookup_identity", exact_lookup)
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "identified", row
    assert row["reason_code"] is None
    assert row["title"] == visible_title
    assert row["doi"] == doi
    assert row["method"] == "doi+front_matter_identifier"

    review_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["initial_evidence"]["metadata_title"] == "Thomson"
    assert record["initial_evidence"]["title"]
    assert record["accepted_identity"]["fields"]["title"] == visible_title.casefold()
    assert record["accepted_identity"]["source"] == "automatic_identifier_metadata"
    assert record["identity_review"]["returned_title_identity"]["ok"] is True
    assert record["revisions"][-1]["decision"] == "automatic_identity_accepted"
    assert record["revisions"][-1]["evidence"]["metadata_lookup"]["provider"] == "doi.org"

    reopened = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    pending = reopened.pending_rows()
    assert len(pending) == 1
    assert pending[0]["status"] == "identified"
    saved = reopened.save(pending)[0]
    assert saved["status"] == "stored", saved

    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["identity_review"]["metadata_lookup"]["provider"] == "doi.org"
    assert record["revisions"][-2]["decision"] == "automatic_identity_accepted"
    assert record["revisions"][-1]["evidence"]["identity_source"] == "automatic_identifier_metadata"
    assert record["revisions"][-1]["evidence"]["returned_title_identity"]["ok"] is True
    assert original.read_bytes() == original_bytes


def test_title_conflict_can_still_use_explicit_manual_doi_review(monkeypatch, tmp_path):
    original = tmp_path / "manual-metadata-title-conflict.pdf"
    title = "Manual DOI Review Front Matter Work"
    doi = "10.1234/manual-title-conflict"
    _conflicting_pdf(original, title, doi)
    def unavailable_lookup(*_args, **_kwargs):
        raise library_metadata.LibraryMetadataError(
            "fixture lookup unavailable", code="identity_metadata_unavailable",
        )
    monkeypatch.setattr(library_metadata, "lookup_identity", unavailable_lookup)
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))

    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"

    def exact_lookup(scheme, identifier):
        assert scheme == "doi"
        assert identifier == doi
        return {
            "fields": {
                "title": title, "authors": "Jane Doe", "year": 2024,
                "doi": doi,
            },
            "provenance": {
                "provider": "doi.org", "scheme": "doi",
                "requested_identifier": doi, "returned_identifier": doi,
                "response_sha256": "e" * 64,
            },
        }

    monkeypatch.setattr(library_metadata, "lookup_identity", exact_lookup)
    probe_calls = []
    original_probe = sources.document_identity_probe

    def capture_probe(ref, text):
        probe_calls.append(dict(ref))
        return original_probe(ref, text)

    monkeypatch.setattr(sources, "document_identity_probe", capture_probe)
    reviewed = importer.review_identity(row, "doi", doi)
    assert reviewed["status"] == "identified", reviewed
    assert len(probe_calls) == 1
    assert reviewed["title"] == title
    assert reviewed["doi"] == doi

    review_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["accepted_identity"]["source"] == "identifier_metadata"
    assert record["identity_review"]["staged_sha256"] == row["sha256"]
    assert record["identity_review"]["metadata_lookup"]["response_sha256"] == "e" * 64
    assert record["revisions"][0]["decision"] == "automatic_identity_rejected"
    assert record["revisions"][-1]["decision"] == "identity_review_accepted"
    saved = importer.save([reviewed])[0]
    assert saved["status"] == "stored", saved
    assert len(probe_calls) == 1


def test_exact_isbn_review_preserves_other_observed_ids_after_reopen_and_save(
    monkeypatch, tmp_path,
):
    title = "ISBN Lookup with Observed DOI"
    doi = "10.1234/isbn-lookup-observed-doi"
    isbn = "9780306406157"
    original = tmp_path / "isbn-and-doi.pdf"
    _conflicting_pdf(original, title, doi, author_line="Jane Doe 2024")

    collect_local_evidence = library_import.manuscript_identity.collect_local_evidence

    def evidence_with_isbn(*args, **kwargs):
        evidence = collect_local_evidence(*args, **kwargs)
        evidence["identifiers"].append({"scheme": "isbn", "value": isbn})
        return evidence

    def exact_lookup(scheme, identifier):
        if scheme == "doi":
            raise library_metadata.LibraryMetadataError(
                "automatic DOI lookup unavailable",
                provenance={"provider": "doi.org", "scheme": scheme,
                            "requested_identifier": identifier},
            )
        assert scheme == "isbn"
        assert identifier == isbn
        return {
            "fields": {
                "title": title, "authors": "Jane Doe", "year": 2024,
                "isbn": isbn,
            },
            "provenance": {
                "provider": "openlibrary", "scheme": "isbn",
                "requested_identifier": isbn, "returned_identifier": isbn,
                "response_sha256": "b" * 64,
            },
        }

    monkeypatch.setattr(
        library_import.manuscript_identity, "collect_local_evidence", evidence_with_isbn,
    )
    monkeypatch.setattr(library_metadata, "lookup_identity", exact_lookup)
    anchor = str(tmp_path / "runs" / "anchor")
    environ = _env(tmp_path)
    importer = LibraryImporter(anchor, environ)
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"

    reviewed = importer.review_identity(row, "isbn", isbn)
    assert reviewed["status"] == "identified", reviewed
    assert reviewed["doi"] == doi
    assert reviewed["isbn"] == isbn

    review_path = (
        tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    )
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    accepted_fields = record["accepted_identity"]["fields"]
    assert accepted_fields["doi"] == doi
    assert accepted_fields["isbn"] == isbn

    tampered = dict(reviewed, doi="10.1234/other-work")
    rejected = importer.save([tampered])[0]
    assert rejected["status"] == "needs_review"
    assert rejected["reason_code"] == "identity_review_required"

    reopened = LibraryImporter(anchor, environ)
    pending = reopened.pending_rows()
    assert len(pending) == 1
    assert pending[0]["doi"] == doi
    assert pending[0]["isbn"] == isbn
    saved = reopened.save(pending)[0]
    assert saved["status"] == "stored", saved

    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["saved"] is True
    assert record["identity_review"]["identity_snapshot"]["doi"] == doi
    assert record["identity_review"]["identity_snapshot"]["isbn"] == isbn
    reusable = content_store.find_reusable_parsed_text(
        anchor, {"doi": doi, "isbn": isbn}, tiers=("fulltext",), environ=environ,
    )
    assert reusable is not None
    assert reusable["parsed_text_id"] == saved["parsed_text_id"]


def test_auto_doi_wrong_returned_title_preserves_title_conflict(monkeypatch, tmp_path):
    original = tmp_path / "automatic-wrong-title.pdf"
    title = "Correct Work in PDF Front Matter"
    doi = "10.1234/automatic-wrong-title"
    _conflicting_pdf(original, title, doi)
    provenance = {
        "provider": "doi.org", "scheme": "doi",
        "requested_identifier": doi, "returned_identifier": doi,
        "response_sha256": "c" * 64,
    }
    monkeypatch.setattr(
        library_metadata,
        "lookup_identity",
        lambda *_args: {
            "fields": {
                "title": "Different Remote Work", "authors": "Jane Doe",
                "year": 2024, "doi": doi,
            },
            "provenance": provenance,
        },
    )
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"
    assert row["title"] == title

    review_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["status"] == "needs_review"
    assert record["reason_code"] == "title_conflict"
    assert record["identity_review"]["metadata_lookup"] == provenance
    assert record["identity_review"]["returned_title_identity"]["ok"] is False
    assert record["revisions"][-1]["decision"] == "automatic_identity_rejected"
    assert record["revisions"][-1]["before"] == record["revisions"][-1]["after"]
    assert record["revisions"][-1]["after"]["title"] == row["title"]
    assert importer.save([row])[0]["reason_code"] == "identity_review_required"


@pytest.mark.parametrize(
    "error_code", ["identity_metadata_mismatch", "identity_metadata_unavailable"]
)
def test_auto_doi_lookup_failure_preserves_conflict_and_provenance(
    monkeypatch, tmp_path, error_code,
):
    original = tmp_path / f"automatic-{error_code}.pdf"
    doi = "10.1234/automatic-lookup-failure"
    _conflicting_pdf(original, "Visible Exact DOI Work", doi)
    provenance = {
        "provider": "doi.org", "scheme": "doi",
        "requested_identifier": doi, "returned_identifier": "10.1234/other",
        "response_sha256": "d" * 64,
    }

    def lookup_failure(*_args):
        raise library_metadata.LibraryMetadataError(
            "metadata response did not attest the requested DOI",
            code=error_code,
            provenance=provenance,
        )

    monkeypatch.setattr(library_metadata, "lookup_identity", lookup_failure)
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"

    review_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["title"] == row["title"]
    assert record["doi"] == doi
    assert record["reason_code"] == "title_conflict"
    assert record["identity_review"]["metadata_lookup"] == provenance
    assert record["revisions"][-1]["decision"] == "automatic_identity_rejected"
    assert record["revisions"][-1]["evidence"]["reason_code"] == error_code


def test_auto_doi_uses_existing_exact_metadata_doi_validation(monkeypatch, tmp_path):
    original = tmp_path / "automatic-provider-doi-mismatch.pdf"
    requested = "10.1234/requested-auto"
    _conflicting_pdf(original, "Visible Exact DOI Work", requested)
    body = json.dumps({"DOI": "10.1234/other", "title": ["Different Work"]}).encode()
    monkeypatch.setattr(
        "core.resolve.service._get_with_final_url",
        lambda *_args, **_kwargs: (200, body, "https://doi.org/10.1234/requested-auto"),
    )
    monkeypatch.setattr(
        library_metadata,
        "lookup_identity",
        _ORIGINAL_METADATA_LOOKUP,
    )
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"

    review_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(review_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["identity_review"]["metadata_lookup"]["requested_identifier"] == requested
    assert record["identity_review"]["metadata_lookup"]["response_sha256"]
    assert record["revisions"][-1]["evidence"]["reason_code"] == "identity_metadata_mismatch"


def test_ambiguous_front_matter_dois_skip_automatic_lookup(monkeypatch, tmp_path):
    original = tmp_path / "automatic-ambiguous-doi.pdf"
    _conflicting_pdf(
        original,
        "Visible Ambiguous DOI Work",
        ["10.1234/ambiguous-one", "10.1234/ambiguous-two"],
    )
    calls = []
    monkeypatch.setattr(library_metadata, "lookup_identity", lambda *args: calls.append(args))
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] == "needs_review"
    assert row["reason_code"] == "title_conflict"
    assert calls == []
    assert importer.pending_rows()[0]["reason_code"] == "title_conflict"


def test_identified_pdf_does_not_trigger_automatic_doi_lookup(monkeypatch, tmp_path):
    original = tmp_path / "already-identified.pdf"
    _pdf(original, "Already Identified PDF", "10.1234/already-identified")
    calls = []
    monkeypatch.setattr(library_metadata, "lookup_identity", lambda *args: calls.append(args))
    row = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path)).inspect([
        str(original)
    ])[0]
    assert row["status"] == "identified"
    assert calls == []


def test_wrong_pdf_keeps_lookup_candidate_but_cannot_approve_or_save(monkeypatch, tmp_path):
    original = tmp_path / "wrong-pdf-match.pdf"
    doi = "10.1234/wrong-pdf"
    _pdf(original, "Unrelated Visible PDF Work", doi, author_line="Jane Doe 2024")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    monkeypatch.setattr(
        "core.app.library.importer.library_metadata.lookup_identity",
        lambda *_args: {
            "fields": {
                "title": "Different Remote Work", "authors": "Jane Doe",
                "year": 2024, "doi": doi,
            },
            "provenance": {"provider": "doi.org", "requested_identifier": doi},
        },
    )

    reviewed = importer.review_identity(row, "doi", doi)
    assert reviewed["status"] == "needs_review"
    assert reviewed["reason_code"] == "identity_pdf_mismatch"
    assert reviewed["title"] == "Different Remote Work"
    assert importer.save([reviewed])[0]["reason_code"] == "identity_review_required"


def test_identifier_mismatch_is_audited_without_filling_false_metadata(monkeypatch, tmp_path):
    original = tmp_path / "provider-mismatch.pdf"
    _pdf(original, "Provider Mismatch Review Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    provenance = {
        "provider": "doi.org", "scheme": "doi",
        "requested_identifier": "10.1234/requested", "returned_identifier": "10.1234/other",
    }

    def mismatch(*_args):
        raise library_metadata.LibraryMetadataError(
            "metadata response did not attest the requested DOI",
            code="identity_metadata_mismatch", provenance=provenance,
        )

    monkeypatch.setattr("core.app.library.importer.library_metadata.lookup_identity", mismatch)
    reviewed = importer.review_identity(row, "doi", "10.1234/requested")
    assert reviewed["status"] == "needs_review"
    assert reviewed["reason_code"] == "identity_metadata_mismatch"
    assert reviewed["title"] == row["title"]
    assert reviewed["doi"] == row["doi"]
    record_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["identity_review"]["metadata_lookup"] == provenance
    assert importer.save([reviewed])[0]["reason_code"] == "identity_review_required"


def test_review_persistence_failure_is_reported_and_cannot_approve(monkeypatch, tmp_path):
    original = tmp_path / "review-persist-failure.pdf"
    _pdf(original, "Review Persist Failure Paper")
    importer = LibraryImporter(str(tmp_path / "runs" / "anchor"), _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    row.update(authors="Research Group", year=2024)

    def fail_write(*_args, **_kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr("core.app.library.importer._json_write", fail_write)
    failed = importer.review_identity(row)
    assert failed["status"] == "needs_review"
    assert failed["reason_code"] == "identity_review_persist_failed"
    assert importer.save([failed])[0]["reason_code"] == "identity_review_required"


@pytest.mark.parametrize("scheme", ["doi", "pmid", "pmcid", "arxiv", "isbn"])
def test_exact_identifier_review_save_roundtrip_keeps_supported_store_alias(
    monkeypatch, tmp_path, scheme,
):
    title = f"Library Roundtrip {scheme.upper()} Identity"
    original = tmp_path / f"roundtrip-{scheme}.pdf"
    _pdf(original, title, author_line="Jane Doe 2024")
    anchor = str(tmp_path / "runs" / "anchor")
    importer = LibraryImporter(anchor, _env(tmp_path))
    row = importer.inspect([str(original)])[0]
    assert row["status"] != "identified"

    identifiers = {
        "doi": "10.1234/library-roundtrip",
        "pmid": "12345678",
        "pmcid": "PMC1234567",
        "arxiv": "2401.12345v2",
        "isbn": "9780306406157",
    }
    fields_by_scheme = {
        "doi": {"doi": identifiers["doi"]},
        "pmid": {"pmid": identifiers["pmid"]},
        "pmcid": {"pmcid": identifiers["pmcid"], "doi": "10.1234/pmcid-roundtrip"},
        "arxiv": {
            "arxiv_id": identifiers["arxiv"],
            "doi": "10.48550/arxiv.2401.12345",
        },
        "isbn": {"isbn": identifiers["isbn"]},
    }
    lookup_provenance = {
        "provider": f"{scheme}-fixture", "scheme": scheme,
        "requested_identifier": identifiers[scheme],
        "returned_identifier": identifiers[scheme], "response_sha256": "b" * 64,
    }

    def exact_lookup(requested_scheme, identifier):
        assert requested_scheme == scheme
        assert identifier == identifiers[scheme]
        return {
            "fields": {
                "title": title, "authors": "Jane Doe", "year": 2024,
                **fields_by_scheme[scheme],
            },
            "provenance": lookup_provenance,
        }

    observed_refs = []
    original_probe = sources.document_identity_probe

    def capture_probe(ref, text):
        observed_refs.append(dict(ref))
        return original_probe(ref, text)

    monkeypatch.setattr("core.app.library.importer.library_metadata.lookup_identity", exact_lookup)
    monkeypatch.setattr("core.app.library.importer.sources.document_identity_probe", capture_probe)
    reviewed = importer.review_identity(row, scheme, identifiers[scheme])
    assert reviewed["status"] == "identified", reviewed
    assert reviewed["_session_id"] == row["_session_id"]
    assert reviewed["_file_id"] == row["_file_id"]
    assert observed_refs and "pmcid" not in observed_refs[-1]

    saved = importer.save([reviewed])[0]
    assert saved["status"] == "stored", saved
    record_path = tmp_path / "state" / "library_imports" / row["_session_id"] / "review.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))["files"][row["sha256"]]
    assert record["identity_review"]["metadata_lookup"] == lookup_provenance
    assert record["revisions"][-1]["evidence"]["metadata_lookup"] == lookup_provenance

    stable_identity = {
        "doi": fields_by_scheme[scheme].get("doi"),
        "pmid": fields_by_scheme[scheme].get("pmid"),
        "isbn": fields_by_scheme[scheme].get("isbn"),
    }
    assert content_store.find_reusable_parsed_text(
        anchor, stable_identity, tiers=("fulltext",), environ=_env(tmp_path),
    ) is not None
