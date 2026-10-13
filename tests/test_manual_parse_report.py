# tests/test_manual_parse_report.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Manual Parse adjudication stays separate from Resolve/Fetch reporting."""

from __future__ import annotations

from types import SimpleNamespace

from core.app.parse_review import ParseReviewController
from core.infra.db.repository import RunRepository
from core.fetch.diagnostics import gaps
from core.report import render
from core.report.io import _db_projection_sha256, _load_run_projection


def _answer_and_apply(repo, task_id, payload):
    repo.submit_task_answer(task_id=task_id, actor_type="user", raw_payload=payload)
    repo.apply_manual_parse_review(task_id)


def test_report_renders_db_roundtrip_author_year_ambiguity(tmp_path):
    repo = RunRepository.create(str(tmp_path), run_id="ambiguity-report", input_path="paper.pdf",
        input_sha256="a" * 64, accuracy="standard", style=None, model_id=None,
        http_profile="default", challenge_mode="off", fixture_fingerprint="fixture")
    try:
        repo.replace_parse_payload(
            claims=[{"claim_id": "c1", "sentence": "Claim", "marker_numbers": []}],
            references=[{"ref_id": "r1", "ref_number": 1, "raw_entry": "Smith (2020)"}],
            citations=[{"claim_id": "c1", "marker_raw": "(Smith 2020)", "candidate_ref_ids": ["r1"]}],
        )
    finally:
        repo.close()
    markdown, summary = render(str(tmp_path))
    assert "Ambiguous citations" in markdown
    assert "[1] Smith (2020)" in markdown
    assert summary["ambiguous_citations"] == 1


def test_manual_parse_projection_uses_effective_denominator_and_not_http(tmp_path):
    repo = RunRepository.create(str(tmp_path), run_id="manual-report", input_path="paper.pdf",
        input_sha256="a" * 64, accuracy="standard", style=None, model_id=None,
        http_profile="default", challenge_mode="off", fixture_fingerprint="fixture")
    raw = "Alpha, ‘One’ (2020). Beta, ‘Two’ (2021)."
    repo.replace_parse_payload(
        claims=[{"claim_id": "claim-1", "sentence": "Claim", "marker_numbers": [1, 2, 3]}],
        references=[
            {"ref_id": "parent", "ref_number": 1, "raw_entry": raw},
            {"ref_id": "none-parent", "ref_number": 2, "raw_entry": "Editorial note."},
            {"ref_id": "network", "ref_number": 3, "raw_entry": "Gamma (2022)."},
        ],
        citations=[
            {"claim_id": "claim-1", "ref_id": "parent", "ref_number": 1},
            {"claim_id": "claim-1", "ref_id": "none-parent", "ref_number": 2},
            {"claim_id": "claim-1", "ref_id": "network", "ref_number": 3},
        ],
        footnote_notes=[
            {"note_id": "split", "manuscript_id": "m", "note_number": 1,
             "raw_note": raw, "extraction_status": "ambiguous"},
            {"note_id": "none", "manuscript_id": "m", "note_number": 2,
             "raw_note": "Editorial note.", "extraction_status": "no_sources"},
        ],
        footnote_note_sources=[],
        claim_footnotes=[
            {"claim_id": "claim-1", "note_id": "split"},
            {"claim_id": "claim-1", "note_id": "none"},
        ],
        footnote_note_parents=[
            {"note_id": "split", "ref_id": "parent"},
            {"note_id": "none", "ref_id": "none-parent"},
        ],
    )
    repo.create_manual_parse_review(task_id="split-review", review_kind="footnote_source_review",
                                    target_id="split", instructions="split")
    split_hash = repo.get_task("split-review").task_payload["target_sha256"]
    _answer_and_apply(repo, "split-review", {
        "action": "split_sources", "target_sha256": split_hash, "reason": "two works",
        "source_texts": ["Alpha, ‘One’ (2020).", "Beta, ‘Two’ (2021)."],
    })
    repo.create_manual_parse_review(task_id="none-review", review_kind="footnote_source_review",
                                    target_id="none", instructions="confirm")
    none_hash = repo.get_task("none-review").task_payload["target_sha256"]
    _answer_and_apply(repo, "none-review", {
        "action": "no_sources", "target_sha256": none_hash, "reason": "editorial",
    })
    repo.create_manual_parse_review(task_id="identity-review", review_kind="reference_identity_review",
                                    target_id="network", instructions="correct")
    identity_hash = repo.get_task("identity-review").task_payload["target_sha256"]
    before_identity = _db_projection_sha256(str(tmp_path))
    _answer_and_apply(repo, "identity-review", {
        "action": "correct_identity", "target_sha256": identity_hash,
        "reason": "publisher record", "title": "Canonical Gamma", "doi": "10.1/gamma",
    })
    after_identity = _db_projection_sha256(str(tmp_path))
    repo.upsert_resolve_result("network", {"ref_id": "network", "status": "unresolved",
                                            "reason": "network_error"})
    raw_parse = repo.parse_payload()
    repo.close()

    projection = _load_run_projection(str(tmp_path))
    assert projection["parse_raw"] == raw_parse
    assert [ref["id"] for ref in projection["parse_raw"]["references"]] == [
        "parent", "none-parent", "network",
    ]
    assert len(projection["parse_effective"]["references"]) == 3
    assert before_identity != after_identity
    assert {row["action"] for row in projection["manual_parse_adjudication"]} == {
        "split_sources", "no_sources", "correct_identity"
    }

    gap_report = gaps.build_gap_report(str(tmp_path), accuracy="standard")
    assert gap_report["summary"]["references"] == 3
    assert gap_report["summary"]["unresolved_network"] == 1
    assert gap_report["summary"]["manual_no_sources"] == 1
    assert gap_report["summary"]["manual_ambiguous"] == 0
    assert gap_report["summary"]["manual_split_sources"] == 1
    assert gap_report["summary"]["manual_identity_overrides"] == 1

    markdown, _summary = render(str(tmp_path))
    assert "## Manual Parse adjudication" in markdown
    assert "distinct from Resolve/Fetch and HTTP failures" in markdown
    assert "network_error" not in markdown.split("## Manual Parse adjudication", 1)[1].split("## 1.", 1)[0]


def test_identity_keep_ambiguous_is_audited_without_an_overlay(tmp_path):
    repo = RunRepository.create(str(tmp_path), run_id="identity-ambiguous", input_path="paper.pdf",
        input_sha256="a" * 64, accuracy="standard", style=None, model_id=None,
        http_profile="default", challenge_mode="off", fixture_fingerprint="fixture")
    repo.replace_parse_payload(
        claims=[], references=[{"ref_id": "ref", "ref_number": 1, "raw_entry": "Unknown work."}],
        citations=[],
    )
    repo.create_manual_parse_review(task_id="identity-keep", review_kind="reference_identity_review",
                                    target_id="ref", instructions="retain uncertainty")
    target_hash = repo.get_task("identity-keep").task_payload["target_sha256"]
    _answer_and_apply(repo, "identity-keep", {
        "action": "keep_ambiguous", "target_sha256": target_hash, "reason": "not | enough evidence",
    })
    row = repo.manual_parse_review_projection()[0]
    assert row["status"] == "identity_ambiguous"
    assert row["action"] == "keep_ambiguous"
    assert row["original_title"] == row["effective_title"]
    assert row["producer_identity"] is None
    repo.close()

    gap_report = gaps.build_gap_report(str(tmp_path), accuracy="standard")
    assert gap_report["summary"]["manual_identity_overrides"] == 0
    markdown, _summary = render(str(tmp_path))
    assert "not &#124; enough evidence" in markdown


def test_report_renders_applied_citation_attribution_without_reference_number_key(tmp_path):
    repo = RunRepository.create(str(tmp_path), run_id="attribution-report",
        input_path="paper.pdf", input_sha256="a" * 64, accuracy="standard",
        style=None, model_id=None, http_profile="default", challenge_mode="off",
        fixture_fingerprint="fixture")
    try:
        repo.replace_parse_payload(
            claims=[{"claim_id": "c1", "sentence": "A claim"}],
            references=[{"ref_id": "r1", "ref_number": 2,
                         "raw_entry": "Hochreiter and Schmidhuber (1997)"}],
            citations=[{"claim_id": "c1", "marker_raw": "(Hochrieter and Schmidhuber, 1997)",
                        "surname": "hochrieter", "year": 1997,
                        "candidate_ref_ids": ["r1"]}],
        )
        occurrence = repo.parse_payload()["ambiguities"][0]["occurrence_id"]
        repo.create_manual_parse_review(task_id="citation-review",
            review_kind="citation_reference_review", target_id=occurrence,
            candidates=[{"id": "r1", "origin": "parser", "score": 0.9}],
            instructions="Review the reference")
        target_hash = repo.get_task("citation-review").task_payload["target_sha256"]
        answer_id = "citation-answer"
        repo.submit_task_answer_with_provenance(
            task_id="citation-review", actor_type="user", answer_id=answer_id,
            raw_payload={"action": "select_reference", "target_sha256": target_hash,
                         "ref_id": "r1"},
            provenance={"answer_id": answer_id, "producer_class": "operator",
                        "producer_identity": "local-operator", "authenticated_uid": 1000,
                        "ingress_kind": "controlled_metadata",
                        "admitted_at": "2026-09-06T12:00:00+00:00", "file_count": 0,
                        "authority_id": "local-unattested"},
            files=[], expected_assurance=repo.get_execution_assurance(),
        )
        repo.apply_manual_parse_review("citation-review")
        row = next(row for row in repo.manual_parse_review_projection()
                   if row["subject_type"] == "citation_attribution")
        assert "ref_number" not in row
    finally:
        repo.close()

    markdown, _summary = render(str(tmp_path))
    assert "citation · claim c1 · reference [2]" in markdown
    assert "attribution_overridden / select_reference" in markdown


def test_skipped_parse_reviews_render_without_effective_or_remediation_overrides(
    tmp_path, monkeypatch,
):
    repo = RunRepository.create(
        str(tmp_path), run_id="skipped-parse-review", input_path="paper.pdf",
        input_sha256="a" * 64, accuracy="standard", style=None, model_id=None,
        http_profile="default", challenge_mode="off", fixture_fingerprint="fixture",
    )
    repo.replace_parse_payload(
        claims=[{"claim_id": "c1", "sentence": "A concrete claim.",
                  "marker_numbers": [1]}],
        references=[{"ref_id": "r1", "ref_number": 1,
                     "raw_entry": "Smith (2020). Source title."}],
        citations=[{"claim_id": "c1", "marker_raw": "(Smyth 2020)",
                    "surname": "smyth", "year": 2020,
                    "candidate_ref_ids": ["r1"]}],
        footnote_notes=[{"note_id": "n1", "manuscript_id": "m", "note_number": 1,
                         "raw_note": "Smith (2020). Source title.",
                         "extraction_status": "sources_extracted"}],
        footnote_note_sources=[{
            "note_id": "n1", "ref_id": "r1", "source_order": 0,
            "raw_start": 0, "raw_end": len("Smith (2020). Source title."),
        }],
        claim_footnotes=[{"claim_id": "c1", "note_id": "n1"}],
        footnote_note_parents=[{"note_id": "n1", "ref_id": "r1"}],
    )
    occurrence = repo.parse_payload()["ambiguities"][0]["occurrence_id"]
    repo.create_manual_parse_review(
        task_id="citation-review", review_kind="citation_reference_review",
        target_id=occurrence,
        candidates=[{"id": "r1", "origin": "parser", "score": 0.9}],
        instructions="Review the citation.",
    )
    repo.create_manual_parse_review(
        task_id="note-review", review_kind="footnote_source_review",
        target_id="n1", instructions="Review the note.",
    )
    repo.create_manual_parse_review(
        task_id="identity-review", review_kind="reference_identity_review",
        target_id="r1", instructions="Review the identity.",
    )
    effective_before = repo.effective_parse_payload()
    assurance = repo.get_execution_assurance()
    repo.close()

    monkeypatch.setattr(
        "core.app.parse_review.resolve_existing",
        lambda *_args, **_kwargs: SimpleNamespace(gate=None, assurance=assurance),
    )
    answers = ParseReviewController(str(tmp_path)).skip_remaining_reviews()
    assert len(answers) == 3
    assert len({answer["answer_id"] for answer in answers}) == 3

    repo = RunRepository.open(str(tmp_path))
    try:
        tasks = repo.list_tasks(slot="parse_review")
        assert {task.status for task in tasks} == {"answered"}
        assert {
            repo.get_latest_task_answer(task.task_id).raw_payload["action"]
            for task in tasks
        } == {"skip_review"}
        assert {
            repo.get_latest_task_answer(task.task_id).raw_payload["target_sha256"]
            for task in tasks
        } == {task.task_payload["target_sha256"] for task in tasks}
        for task in tasks:
            repo.apply_manual_parse_review(task.task_id)
        assert repo.effective_parse_payload() == effective_before
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_footnote_source_overrides"
        ).fetchone()[0] == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_reference_identity_overrides"
        ).fetchone()[0] == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_citation_attribution_overrides"
        ).fetchone()[0] == 0
    finally:
        repo.close()

    projection = _load_run_projection(str(tmp_path))
    rows = projection["manual_parse_adjudication"]
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"skipped"}
    assert {row["action"] for row in rows} == {"skip_review"}
    assert projection["parse_effective"] == effective_before
    assert projection["remediation"]["interventions"] == []
    assert projection["remediation"]["footnote_split_required"] == []

    markdown, _summary = render(str(tmp_path))
    assert markdown.count("skipped / skip_review") == 3
