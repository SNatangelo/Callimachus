# tests/infra/test_current_run_schema.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3

import pytest

from core.infra.db.repository import RunRepository
from core.infra.db.schema import SCHEMA_VERSION


_REMOVED = (
    "citation_projections", "verdict_attempts", "jury_log",
    "llm_dispatch_jury1_answers", "llm_dispatch_jury1_answer_evidence",
)


def _repo(tmp_path) -> RunRepository:
    return RunRepository.create(
        str(tmp_path), run_id="run-current", input_path="input.pdf",
        input_sha256="input-sha", accuracy="standard", style=None,
        model_id=None, http_profile="default", challenge_mode="off",
        fixture_fingerprint="fixture",
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def test_fresh_current_schema_omits_removed_verification_objects(tmp_path):
    repo = _repo(tmp_path)
    try:
        assert SCHEMA_VERSION == 109
        assert "identity_attested" in {
            row[1] for row in repo._conn.execute("PRAGMA table_info(task_fetch_answers)")
        }
        assert "identity_attested" in {
            row[1] for row in repo._conn.execute("PRAGMA table_info(task_browser_answer_items)")
        }
        assert _table_exists(repo._conn, "cited_bibliographic_coordinates")
        assert _table_exists(repo._conn, "cited_bibliographic_coordinate_spans")
        assert _table_exists(repo._conn, "resolve_attempt_metadata_coordinate_comparisons")
        assert _table_exists(repo._conn, "resolve_evidence_metadata_coordinate_comparisons")
        assert _table_exists(repo._conn, "resolve_evidence_issue_attestations")
        assert _table_exists(repo._conn, "resolve_evidence_issue_attestation_members")
        assert _table_exists(repo._conn, "resolve_evidence_issue_attestation_sources")
        assert _table_exists(repo._conn, "resolve_evidence_issue_attestation_observations")
        evidence_columns = {
            row[1] for row in repo._conn.execute(
                "PRAGMA table_info(resolve_evidence_profiles)"
            )
        }
        assert "issue_attestations_present" in evidence_columns
        assert "issue_attestations_count" in evidence_columns
        assert "journal_alias_assessment_present" in evidence_columns
        assert "bibliographic_suspicion_present" in evidence_columns
        assert _table_exists(repo._conn, "resolve_evidence_journal_alias_assessments")
        assert _table_exists(repo._conn, "resolve_evidence_journal_alias_candidates")
        assert _table_exists(repo._conn, "resolve_evidence_bibliographic_suspicions")
        assert _table_exists(repo._conn, "resolve_evidence_bibliographic_suspicion_providers")
        assert _table_exists(repo._conn, "resolve_attempt_identity_searches")
        assert _table_exists(repo._conn, "source_text_materialization_intents")
        assert _table_exists(repo._conn, "source_text_materialization_intent_flag_states")
        assert _table_exists(repo._conn, "source_text_materialization_intent_flags")
        assert _table_exists(repo._conn, "source_text_materialization_outcomes")
        assert _table_exists(repo._conn, "source_text_materialization_invalidations")
        assert _table_exists(repo._conn, "credential_inventory_snapshots")
        assert _table_exists(repo._conn, "credential_inventory_entries")
        assert _table_exists(repo._conn, "credential_transport_observations")
        assert _table_exists(repo._conn, "completed_remediation_provenance")
        triggers = {
            row[0] for row in repo._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "completed_remediation_provenance_no_update",
            "completed_remediation_provenance_no_delete",
            "source_text_materialization_intents_no_update",
            "source_text_materialization_outcomes_no_update",
            "source_text_materialization_invalidations_no_update",
            "source_text_materialization_invalidations_no_delete",
            "institutional_search_provenance_no_update",
            "institutional_search_provenance_no_delete",
            "institutional_search_backend_attempts_no_update",
            "institutional_search_backend_attempts_no_delete",
            "institutional_search_candidates_no_update",
            "institutional_search_candidates_no_delete",
        } <= triggers
        assert _table_exists(repo._conn, "manuscript_identity")
        assert _table_exists(repo._conn, "manuscript_identity_identifiers")
        assert _table_exists(repo._conn, "footnote_notes")
        assert _table_exists(repo._conn, "footnote_note_sources")
        assert _table_exists(repo._conn, "footnote_note_parents")
        assert _table_exists(repo._conn, "operational_references")
        assert _table_exists(repo._conn, "claim_footnotes")
        assert _table_exists(repo._conn, "task_manual_parse_review_details")
        assert _table_exists(repo._conn, "task_manual_parse_review_answers")
        assert _table_exists(repo._conn, "task_manual_parse_review_split_sources")
        assert _table_exists(repo._conn, "manual_footnote_source_overrides")
        assert _table_exists(repo._conn, "manual_footnote_source_override_sources")
        assert _table_exists(repo._conn, "manual_reference_identity_overrides")
        assert _table_exists(repo._conn, "manual_parse_review_applications")
        assert _table_exists(repo._conn, "unresolved_citations")
        assert _table_exists(repo._conn, "manual_citation_attribution_overrides")
        assert _table_exists(repo._conn, "task_source_identity_attestation_details")
        assert _table_exists(repo._conn, "task_source_identity_attestation_answers")
        assert _table_exists(repo._conn, "source_identity_attestation_decisions")
        assert _table_exists(repo._conn, "artifact_checkpoints")
        assert _table_exists(repo._conn, "artifact_crash_recoveries")
        assert _table_exists(repo._conn, "artifact_crash_recovery_entries")
        assert _table_exists(repo._conn, "artifact_integrity_violations")
        assert _table_exists(repo._conn, "artifact_integrity_overrides")
        assert _table_exists(repo._conn, "task_answer_provenance")
        assert _table_exists(repo._conn, "task_answer_provenance_files")
        assert _table_exists(repo._conn, "integrity_unit_completions")
        assert _table_exists(repo._conn, "llm_dispatch_protocol_errors")
        assert _table_exists(repo._conn, "resolve_transport_operations")
        assert _table_exists(repo._conn, "resolve_transport_operation_refs")
        assert _table_exists(repo._conn, "resolve_transport_operation_manuscript_targets")
        assert _table_exists(repo._conn, "resolve_transport_http_attempts")
        assert _table_exists(repo._conn, "resolve_transport_failures")
        assert _table_exists(repo._conn, "institutional_search_provenance")
        assert _table_exists(repo._conn, "institutional_search_backend_attempts")
        assert _table_exists(repo._conn, "institutional_search_candidates")
        assert _table_exists(repo._conn, "fetch_transport_requests")
        assert _table_exists(repo._conn, "fetch_transport_http_attempts")
        assert _table_exists(repo._conn, "fetch_transport_failures")
        assert _table_exists(repo._conn, "performance_snapshot")
        assert _table_exists(repo._conn, "performance_spans")
        assert all(not _table_exists(repo._conn, name) for name in _REMOVED)
        columns = {
            row[1]
            for row in repo._conn.execute(
                "PRAGMA table_info(verify_claim_evidence_config)"
            )
        }
        assert "invalid_key_env_sync" not in columns
        assert "typesafe_min_confidence" not in columns
        assert {
            "max_tokens",
            "reasoning",
            "reasoning_effort",
            "provider_confidence_threshold",
            "confidence_policy_id",
        } <= columns
        claim_columns = {
            row[1]: row for row in repo._conn.execute("PRAGMA table_info(claims)")
        }
        assert "structural_provenance_json" in claim_columns
        assert claim_columns["structural_provenance_json"][3] == 0
        max_tokens = next(
            row
            for row in repo._conn.execute(
                "PRAGMA table_info(verify_claim_evidence_config)"
            )
            if row[1] == "max_tokens"
        )
        assert max_tokens[3] == 0
    finally:
        repo.close()


def test_current_schema_rejects_reintroduced_removed_object(tmp_path):
    repo = _repo(tmp_path)
    repo.close()
    with sqlite3.connect(tmp_path / "run.sqlite") as conn:
        conn.execute("CREATE TABLE verdict_attempts(id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="contains removed objects"):
        RunRepository.open(str(tmp_path))
