# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib

import json

from pathlib import Path

from core.app.guided_fetch import build_source_inventory

from core.fetch.admission import provided_fulltext

from core.gui.guided_fetch_viewmodel import GuidedFetchViewModel

from core.infra.db.repository import RunRepository

from core.parse.parsing_common import _make_reference

from core.resolve.matching import _metadata_match_profile

from core.resolve.resolver_coverage import (
    bibliographic_concern,
    bibliographic_review_labels,
)

def _repo(tmp_path) -> RunRepository:
    return RunRepository.create(
        str(tmp_path), run_id="guided-inventory", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="fixture",
    )

def _evidence(profile: dict) -> dict:
    return {
        "has_identifier": False,
        "has_searchable_title": True,
        "has_author": True,
        "has_year": True,
        "has_venue": True,
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "source_type_evidence": [],
        "indexability": "high",
        "checks_completed": [],
        "minimum_checks_completed": True,
        "best_candidate": None,
        "resolution_basis": "metadata_search",
        "existence_confidence": "high",
        "title_overlap": 1.0,
        "metadata_match": profile,
        "identifier_fallback": None,
        "synthetic_reference_risk": {"score": 0, "band": "none", "signals": []},
        "journal_authority": {
            "status": "recognized",
            "registry": "fixture",
            "registry_version": "1",
            "record_id": "fixture:journal",
            "cited_venue": "Journal of Tests",
            "canonical_title": "Journal of Tests",
            "matched_alias": "Journal of Tests",
            "match_basis": "canonical_title",
            "snapshot_sha256": "0" * 64,
        },
        "bibliographic_adjudication": {
            "rule_version": "bibliographic-adjudication/v1",
            "outcome": "identified",
            "identity_status": "identified",
            "check_status": "complete",
            "correction_status": "not_needed",
            "refutations": [],
        },
    }

def _store_source(repo: RunRepository, run_dir, source_id: str, tier: str, text: str) -> None:
    path = run_dir / "sources" / f"{source_id}.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    repo.store_source_text(
        source_text_id=source_id,
        ref_id="r1",
        identity_key="fixture:r1",
        tier=tier,
        origin="fixture",
        stored_path=f"sources/{source_id}.txt",
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        char_count=len(text),
        source_ref="https://example.test/source",
        supplied_by="user" if tier == "fulltext" else None,
        supplied_via="guided_fetch" if tier == "fulltext" else None,
        extraction_method="fixture",
    )

def _write_guided_ocr_artifact(
    run_dir: Path,
    ref_id: str,
    ref_number: int,
    *,
    status: str,
    index: int = 0,
    discarded: bool = False,
) -> Path | None:
    artifact_dir = run_dir / "sources" / "guided_ocr"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    scan_bytes = f"%PDF-1.4 fixture {ref_id} {index}".encode("utf-8")
    scan_sha256 = hashlib.sha256(scan_bytes).hexdigest()
    scan_token = f"fixture:{ref_id}:{index}"
    scan_id = provided_fulltext.ocr_scan_id(ref_id, scan_token, scan_sha256)
    (artifact_dir / f"{scan_id}.pdf").write_bytes(scan_bytes)
    text = f"Validated OCR text for {ref_id}, candidate {index}." if status == "done" else None
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    text_path = (
        artifact_dir / f"{scan_id}-{text_sha256}.txt" if text_sha256 else None
    )
    if text_path is not None:
        text_path.write_text(text, encoding="utf-8")
    metadata = {
        "scan_id": scan_id,
        "ref_id": ref_id,
        "ref_number": ref_number,
        "scan_token": scan_token,
        "scan_sha256": scan_sha256,
        "status": status,
        "display_name": f"{ref_id}-{index}.pdf",
        "source_ref": provided_fulltext.ocr_scan_source_ref(scan_sha256),
        "reason": None if status == "done" else "OCR has not completed.",
        "discarded": discarded,
        "identity_attested": False,
        "text_path": None if text_path is None else text_path.relative_to(run_dir).as_posix(),
        "text_sha256": text_sha256,
        "ocr_method": "fixture" if text_path is not None else None,
    }
    (artifact_dir / f"{scan_id}.json").write_text(
        json.dumps(metadata), encoding="utf-8",
    )
    return text_path

def test_source_inventory_previews_only_one_valid_undiscarded_guided_ocr_sidecar(
    tmp_path,
):
    repo = _repo(tmp_path)
    try:
        references = []
        for number, ref_id in enumerate(
            ("r1", "r2", "r3", "r4", "r5", "r6"), start=1,
        ):
            reference = _make_reference(
                number, f"Smith, J. Scanned article {number}. Journal of Tests (2020).",
            )
            reference["id"] = ref_id
            references.append(reference)
        repo.replace_parse_payload(claims=[], references=references, citations=[])
    finally:
        repo.close()

    done_path = _write_guided_ocr_artifact(
        tmp_path, "r1", 1, status="done",
    )
    _write_guided_ocr_artifact(tmp_path, "r2", 2, status="pending")
    _write_guided_ocr_artifact(tmp_path, "r3", 3, status="failed")
    _write_guided_ocr_artifact(
        tmp_path, "r4", 4, status="done", discarded=True,
    )
    _write_guided_ocr_artifact(tmp_path, "r5", 5, status="done", index=0)
    _write_guided_ocr_artifact(tmp_path, "r5", 5, status="done", index=1)
    tampered_path = _write_guided_ocr_artifact(
        tmp_path, "r6", 6, status="done",
    )
    assert done_path is not None and tampered_path is not None
    tampered_path.write_text("changed after OCR validation", encoding="utf-8")

    inventory = build_source_inventory(str(tmp_path))
    by_ref = {item["ref_id"]: item for item in inventory["references"]}

    assert by_ref["r1"]["fetch"]["preview_path"] == str(done_path)
    assert by_ref["r1"]["fetch"]["preview_kind"] == "guided_ocr_staged"
    assert by_ref["r1"]["fetch"]["sources"] == []
    assert by_ref["r1"]["fetch"]["best_source"] is None
    assert by_ref["r1"]["fetch"]["tier"] is None
    for ref_id in ("r2", "r3", "r4", "r5", "r6"):
        assert by_ref[ref_id]["fetch"]["preview_path"] is None
        assert by_ref[ref_id]["fetch"]["preview_kind"] is None
    assert all(
        "path" not in item and "text_file_path" not in item
        for item in inventory["ocr_queue"]
    )
