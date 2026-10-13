# tests/test_guided_fetch_inventory.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.app.guided_fetch import _load_guided_ocr_artifacts, build_source_inventory
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


@pytest.mark.parametrize("view", ["all", "paged", "focused"])
def test_source_inventory_projects_structural_fields_into_parsed_panel(tmp_path, view):
    reference = _make_reference(
        1, "Smith J, Roe A (2020) A complete article title. Example Journal 12(3): 30-40.",
    )
    reference["id"] = "r1"
    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[reference], citations=[])
    finally:
        repo.close()
    kwargs = {"offset": 0, "limit": 1} if view == "paged" else {}
    if view == "focused":
        kwargs["focus_ref_id"] = "r1"
    inventory = build_source_inventory(str(tmp_path), **kwargs)
    parsed = inventory["references"][0]["parsed"]
    assert parsed["raw_entry"] == reference["raw_entry"]
    assert parsed["bibliographic_parse"] == reference["bibliographic_parse"]
    rows = dict(GuidedFetchViewModel(inventory).detail_panels("r1")["Parsed"]["rows"])
    assert rows["Authors"] == "Smith J; Roe A"
    assert rows["Journal"] == "Example Journal"
    assert rows["Volume"] == "12"
    assert rows["Issue"] == "3"
    assert rows["Pages"] == "30-40"
    assert rows["Publication year"] == "2020"
    parsed["authors"].append("Changed display value")
    assert parsed["bibliographic_parse"]["fields"]["authors"]["value"] == ["Smith J", "Roe A"]


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


def test_source_inventory_projects_auditable_fetch_state(tmp_path):
    repo = _repo(tmp_path)
    try:
        ref_one = _make_reference(
            1, "Smith, J. A cited article. Journal of Tests, 12(3), 10-20 (2020).",
        )
        ref_two = _make_reference(2, "Doe, J. Missing article. Journal of Tests, 1, 1-2 (2021).")
        ref_one["id"] = "r1"
        ref_two["id"] = "r2"
        repo.replace_parse_payload(claims=[], references=[ref_one, ref_two], citations=[])

        record = {
            "title": ["A cited article"],
            "author": [{"family": "Smith"}],
            "published": {"date-parts": [[2020]]},
            "container-title": ["Journal of Tests"],
            "volume": "12",
            "issue": "3",
            "page": "10-20",
        }
        profile = _metadata_match_profile(ref_one, record, "A cited article")
        evidence = _evidence(profile)
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "via": "fixture", "matched_title": "A cited article",
            "reference_status_tag": "verified", "fabrication_risk": "low",
            "attempts": [{"status": "resolved", "via": "fixture", "metadata_match": profile}],
            "evidence_profile": evidence,
        })
        repo.upsert_resolve_result("r2", {
            "status": "unverified", "via": "fixture",
            "reference_status_tag": "suspected_fabricated", "fabrication_risk": "high",
            "tag_reason": "fixture suspicion",
            "abstract": "Metadata-only abstract",
            "attempts": [{"status": "unverified", "via": "fixture"}],
            "evidence_profile": evidence,
        })
        _store_source(repo, tmp_path, "abstract", "abstract", "Abstract source")
        _store_source(repo, tmp_path, "fulltext", "fulltext", "Full text source")
        repo.store_source_text(
            source_text_id="escaped", ref_id="r1", identity_key="fixture:r1",
            tier="fulltext", origin="other", stored_path="sources/../escape.txt",
            sha256=hashlib.sha256(b"escape").hexdigest(), char_count=6,
            extraction_method="fixture",
        )
        repo.append_fetch_attempt(
            "r1", method="GET", url="https://example.test/source", kind="fulltext",
            final_url="https://example.test/source", status_code=200,
            content_type="text/html", outcome="stored", origin="fixture",
        )

        result = repo.get_resolve_result("r1")
        reference = repo.get_reference("r1")
        repo.create_task(
            task_id="fetch-r1", slot="fetch", ref_id="r1", task_payload={
                "kind": "fetch", "status": "pending", "answer": None,
                "ref_id": "r1", "ref_number": 1,
                "reference": {
                    key: getattr(reference, key)
                    for key in ("ref_number", "raw_entry", "title", "doi", "pmid", "url", "isbn", "year", "source_type", "source_kind", "indexability", "source_type_confidence")
                },
                "source_identity": {
                    "reference_status_tag": result.reference_status_tag,
                    "fabrication_risk": result.fabrication_risk,
                    "tag_reason": result.tag_reason,
                    "matched_title": result.matched_title,
                    "metadata_match": result.evidence_profile["metadata_match"],
                },
                "instructions": "Supply a source.",
            },
        )
        repo.create_task(
            task_id="browser-group", slot="fetch", task_payload={
                "kind": "browser_challenge", "status": "pending", "answer": None,
                "domain": "example.test", "instructions": "Open the browser.",
                "candidate_urls": ["https://example.test/source"],
                "references": [
                    {"ref_id": ref_id, "ref_number": number, "raw_entry": raw, "doi": None,
                     "url": None, "candidate_urls": ["https://example.test/source"]}
                    for ref_id, number, raw in (("r1", 1, ref_one["raw_entry"]), ("r2", 2, ref_two["raw_entry"]))
                ],
            },
        )
        repo.create_source_identity_attestation(
            task_id="identity-r1",
            ref_id="r1",
            source_text_id="fulltext",
            slot="fetch",
            instructions="Confirm this exact source identity.",
        )

        inventory = build_source_inventory(str(tmp_path))
    finally:
        repo.close()

    by_ref = {item["ref_id"]: item for item in inventory["references"]}
    first = by_ref["r1"]
    resolved_tab = GuidedFetchViewModel(inventory).detail_tabs("r1")["Resolved"]
    assert inventory["run"]["run_id"] == "guided-inventory"
    assert first["parsed"]["cited_coordinates"] == ref_one["cited_coordinates"]
    assert first["resolve"]["journal_authority"] == evidence["journal_authority"]
    assert first["resolve"]["coordinate_comparisons"] == profile["coordinate_comparisons"]
    assert (
        resolved_tab["evidence_profile"]["bibliographic_adjudication"]
        == evidence["bibliographic_adjudication"]
    )
    assert first["fetch"]["tier"] == "fulltext"
    assert next(source for source in first["fetch"]["sources"] if source["source_text_id"] == "fulltext")["preview_path"] == str(tmp_path / "sources" / "fulltext.txt")
    escaped = next(source for source in first["fetch"]["sources"] if source["source_text_id"] == "escaped")
    assert escaped["available"] is False and escaped["preview_path"] is None
    assert len(first["fetch"]["attempts"]) == 1
    assert {task["task_id"] for task in first["fetch"]["pending_tasks"]} == {
        "fetch-r1", "browser-group", "identity-r1",
    }
    identity = next(
        task for task in first["fetch"]["pending_tasks"]
        if task["task_kind"] == "source_identity_attestation"
    )
    assert identity["target_sha256"]
    assert identity["source_text_id"] == "fulltext"
    assert "stored_path" not in identity
    assert "file_path" not in identity
    assert by_ref["r2"]["fetch"]["sources"] == []
    assert by_ref["r2"]["fetch"]["tier"] == "abstract"
    assert by_ref["r2"]["fabrication_suspicion"]["suspected"] is True
    second_resolve = by_ref["r2"]["resolve"]
    second_profile = second_resolve["evidence_profile"]
    assert by_ref["r2"]["bibliographic_concern"] == bibliographic_concern(second_resolve)
    assert by_ref["r2"]["bibliographic_review_labels"] == bibliographic_review_labels(
        reference=by_ref["r2"]["parsed"],
        status=second_resolve["status"],
        attempts=second_resolve["attempts"],
        adjudication=second_profile["bibliographic_adjudication"],
        coverage=second_profile.get("resolver_coverage", {}),
    )
    assert [task["task_id"] for task in by_ref["r2"]["fetch"]["pending_tasks"]] == ["browser-group"]


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


def test_guided_ocr_loader_rejects_escaped_and_symlinked_artifacts(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    text_path = _write_guided_ocr_artifact(
        run_dir, "r1", 1, status="done",
    )
    assert text_path is not None
    artifact_dir = run_dir / "sources" / "guided_ocr"
    metadata_path = next(artifact_dir.glob("*.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    scan_path = artifact_dir / f"{metadata['scan_id']}.pdf"

    metadata["text_path"] = f"sources/guided_ocr/../{text_path.name}"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert _load_guided_ocr_artifacts(str(run_dir)) == {}

    metadata["text_path"] = text_path.relative_to(run_dir).as_posix()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(scan_path.read_bytes())
    scan_path.unlink()
    try:
        scan_path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert _load_guided_ocr_artifacts(str(run_dir)) == {}
