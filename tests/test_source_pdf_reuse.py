# tests/test_source_pdf_reuse.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.fetch.extraction import source_pdf
from core.parse.pdf_links import Citation, LinkLayer, Reference


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "repo" / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    return run_dir


def _pdf(path: Path, data: bytes = b"%PDF-1.7\nsource bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _rich_result(text: str = "native extracted text"):
    links = LinkLayer(
        references={"cite.example": Reference("cite.example", 2, 12.5, "Example source", "example")},
        citations=[Citation(1, (1.0, 2.0, 3.0, 4.0), "[1]", "cite.example", {2020, 2022})],
    )
    return (
        text,
        "pdf",
        {
            "pdf_method": "pymupdf",
            "pdf_links": links,
            "footnote_references": {1: "First footnote"},
            "pdf_variant_audit": {"candidates": [{"method": "pymupdf", "quality_ok": True}]},
        },
    )


def test_reuses_complete_result_across_renamed_identical_pdfs(monkeypatch, tmp_path):
    run_dir = _run_dir(tmp_path)
    first = _pdf(tmp_path / "incoming" / "first.pdf")
    second = _pdf(tmp_path / "incoming" / "renamed.pdf", first.read_bytes())
    calls = []
    result = _rich_result()

    def extract(path, **kwargs):
        calls.append((Path(path).name, kwargs))
        return result

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    initial = source_pdf.extract_text(str(first), run_dir=run_dir)
    reused = source_pdf.extract_text(str(second), run_dir=run_dir)

    assert len(calls) == 1
    assert initial == reused
    assert isinstance(reused[2]["pdf_links"], LinkLayer)
    assert isinstance(reused[2]["pdf_links"].references["cite.example"], Reference)
    assert isinstance(reused[2]["pdf_links"].citations[0], Citation)
    assert reused[2]["pdf_links"].citations[0].rect == (1.0, 2.0, 3.0, 4.0)
    assert reused[2]["pdf_links"].citations[0].years == {2020, 2022}
    assert reused[2]["footnote_references"] == {1: "First footnote"}


def test_persisted_result_is_reused_in_a_separate_process(tmp_path):
    run_dir = _run_dir(tmp_path)
    first = _pdf(tmp_path / "incoming" / "first.pdf")
    second = _pdf(tmp_path / "incoming" / "second.pdf", first.read_bytes())
    counter = tmp_path / "extract-count.json"
    repo_root = Path(__file__).resolve().parents[1]
    script = r"""
import json, pathlib, sys
from core.fetch.extraction import source_pdf
counter = pathlib.Path(sys.argv[3])
def extract(path, **kwargs):
    count = int(counter.read_text() if counter.exists() else "0") + 1
    counter.write_text(str(count))
    return ("same extracted text", "pdf", {"pdf_method": "test"})
source_pdf.parse_extract.extract_text = extract
source_pdf.extract_text(sys.argv[1], run_dir=sys.argv[2])
"""

    for candidate in (first, second):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(candidate), str(run_dir), str(counter)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    assert counter.read_text() == "1"


def test_changed_bytes_invalidate_even_when_size_and_mtime_match(monkeypatch, tmp_path):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "same.pdf", b"%PDF-1.7\nAAAA")
    original_stat = path.stat()
    calls = []

    def extract(candidate, **kwargs):
        calls.append(Path(candidate).read_bytes())
        return _rich_result(calls[-1].decode("ascii"))

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    first = source_pdf.extract_text(str(path), run_dir=run_dir)
    path.write_bytes(b"%PDF-1.7\nBBBB")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = source_pdf.extract_text(str(path), run_dir=run_dir)

    assert first[0] != second[0]
    assert len(calls) == 2


def test_extractor_setting_change_invalidates_cache(monkeypatch, tmp_path):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "settings.pdf")
    calls = []

    def extract(candidate, **kwargs):
        calls.append(candidate)
        return _rich_result()

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    monkeypatch.setenv("CITATION_VERIFIER_PDF_SUPERSCRIPTS", "1")
    source_pdf.extract_text(str(path), run_dir=run_dir)
    monkeypatch.setenv("CITATION_VERIFIER_PDF_SUPERSCRIPTS", "0")
    source_pdf.extract_text(str(path), run_dir=run_dir)

    assert len(calls) == 2


def test_tampered_cache_is_rejected_and_reextracted(monkeypatch, tmp_path, caplog):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "tampered.pdf")
    calls = []

    def extract(candidate, **kwargs):
        calls.append(candidate)
        return _rich_result(f"extraction {len(calls)}")

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    source_pdf.extract_text(str(path), run_dir=run_dir)
    cache_files = list(source_pdf._cache_path(run_dir, "", "").parent.glob("*.json"))
    assert len(cache_files) == 1
    envelope = json.loads(cache_files[0].read_text(encoding="utf-8"))
    envelope["payload"]["items"][0] = "tampered"
    cache_files[0].write_text(json.dumps(envelope), encoding="utf-8")

    result = source_pdf.extract_text(str(path), run_dir=run_dir)

    assert result[0] == "extraction 2"
    assert len(calls) == 2
    assert "payload hash" in caplog.text


def test_cache_storage_inside_run_is_refused(monkeypatch, tmp_path, caplog):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "inside-run.pdf")
    calls = []

    def extract(candidate, **kwargs):
        calls.append(candidate)
        return _rich_result()

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    from core.fetch.storage import content_store
    monkeypatch.setattr(content_store, "storage_root", lambda _run: str(run_dir / "storage"))

    source_pdf.extract_text(str(path), run_dir=run_dir)
    source_pdf.extract_text(str(path), run_dir=run_dir)

    assert len(calls) == 2
    assert not (run_dir / "storage").exists()
    assert "inside the run directory" in caplog.text


def test_extraction_failures_are_retried_and_not_cached(monkeypatch, tmp_path):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "retry.pdf")
    calls = []

    def extract(candidate, **kwargs):
        calls.append(candidate)
        if len(calls) == 1:
            raise ValueError(
                "PDF extraction insufficient (sparse text or binary output): method tried: stdlib"
            )
        return _rich_result()

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    with pytest.raises(ValueError, match="PDF extraction insufficient"):
        source_pdf.extract_text(str(path), run_dir=run_dir)
    result = source_pdf.extract_text(str(path), run_dir=run_dir)

    assert result[0] == "native extracted text"
    assert len(calls) == 2


def test_source_mutation_during_extraction_fails_closed(monkeypatch, tmp_path):
    run_dir = _run_dir(tmp_path)
    path = _pdf(tmp_path / "incoming" / "mutating.pdf", b"%PDF-1.7\noriginal")

    def extract(candidate, **kwargs):
        Path(candidate).write_bytes(b"%PDF-1.7\nreplacement")
        return _rich_result()

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    with pytest.raises(source_pdf.PdfSourceChangedError):
        source_pdf.extract_text(str(path), run_dir=run_dir)


def test_non_pdf_and_pdf_without_run_directory_delegate_directly(monkeypatch, tmp_path):
    calls = []

    def extract(path, **kwargs):
        calls.append((path, kwargs))
        return _rich_result()

    monkeypatch.setattr(source_pdf.parse_extract, "extract_text", extract)
    text_path = tmp_path / "source.txt"
    pdf_path = tmp_path / "source.pdf"

    assert source_pdf.extract_text(str(text_path), run_dir=tmp_path) == _rich_result()
    assert source_pdf.extract_text(str(pdf_path)) == _rich_result()
    assert len(calls) == 2
