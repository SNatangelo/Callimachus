# tests/infra/test_resolve_attempt_storage.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import urllib.parse
from copy import deepcopy

import pytest

from core.infra.db.repository import RunRepository
from core.infra.db.resolve_attempt_storage import validate_resolve_attempts
from core.parse.parsing_common import _make_reference
from core.resolve import service as resolve
from core.resolve.bibliographic_adjudication import _resolved_identity_is_locked
from core.resolve.journal_authority import CATALOG_ENV, build_catalog
from core.resolve.providers import crossref_metadata


def _repo(tmp_path) -> RunRepository:
    repo = RunRepository.create(str(tmp_path), run_id="attempt", input_path="in", input_sha256="x", accuracy="standard", style=None, model_id=None, http_profile=None, challenge_mode=None, fixture_fingerprint="f")
    repo.replace_parse_payload(claims=[], references=[{"id": "r1", "ref_number": 1, "raw_entry": "One"}], citations=[])
    return repo


def test_resolve_attempts_round_trip_and_fail_closed(tmp_path):
    repo = _repo(tmp_path)
    payload = {"status": "resolved", "via": "crossref", "attempts": [{"status": "resolved", "via": "crossref"}]}
    try:
        repo.upsert_resolve_result("r1", payload)
        assert repo.get_resolve_result("r1").attempts == payload["attempts"]
        with pytest.raises(ValueError, match="unknown resolve attempt field"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [{"status": "resolved", "via": "crossref", "unknown": True}]})
        repo._conn.execute("DROP TRIGGER resolve_attempt_state_no_update")
        repo._conn.execute("UPDATE resolve_attempt_states SET attempt_count=2 WHERE ref_id='r1'")
        with pytest.raises(RuntimeError, match="attempt count is inconsistent"):
            repo.get_resolve_result("r1")
    finally:
        repo.close()


def test_crossref_generic_locator_certificate_round_trips_through_attempt_storage(
    tmp_path, monkeypatch,
):
    raw = "Vale, N. Synthetic Journal 8, 27 (2024)."
    ref = _make_reference(1, raw)
    ref.update({
        "id": "r1", "ref_number": 1, "raw_entry": raw,
        "doi": None, "pmid": None, "isbn": None, "url": None,
    })
    doi = "10.5555/synthetic-storage-proof.2024"
    candidate = {
        "DOI": doi,
        "type": "journal-article",
        "title": ["A synthetic storage contract article"],
        "author": [{"given": "N", "family": "Vale"}],
        "published-print": None,
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Synthetic Journal"],
        "short-container-title": None,
        "volume": "8",
        "article-number": "27",
    }
    nonselected_candidate = {
        "DOI": "10.5555/unselected-book-record",
        "type": "book",
        "title": ["Unrelated synthetic book"],
        "container-title": ["Unrelated Synthetic Book"],
        "volume": 8,
    }
    search_items = [nonselected_candidate, candidate]

    def fake_get(url: str, accept: str = "application/json"):
        parsed = urllib.parse.urlparse(url)
        if parsed.path.rstrip("/").endswith("/works") and parsed.query:
            return 200, json.dumps({
                "message": {"items": search_items, "total-results": len(search_items)},
            })
        if parsed.path.endswith("/works/" + urllib.parse.quote(doi)):
            return 200, json.dumps({"message": candidate})
        return 404, json.dumps({"message": {}})

    monkeypatch.setattr(resolve, "_get", fake_get)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)

    provider_result = crossref_metadata.discover(ref)
    assert provider_result is not None
    assert provider_result["status"] == "resolved"
    assert provider_result["resolved_identifier"]["validated_via"] == (
        "crossref:generic_article_locator_role_certificate"
    )
    proof = provider_result["metadata_match"][
        "_crossref_generic_article_locator_certificate"
    ]
    assert proof["search_evidence"]["records"][0] == nonselected_candidate
    assert proof["native_record_snapshot"]["published-print"] is None
    assert proof["native_record_snapshot"]["short-container-title"] is None
    attempts: list[dict] = []
    resolve._append_provider_attempt(attempts, provider_result)

    proof_key = "_crossref_generic_article_locator_certificate"
    provider_result_before_profile = deepcopy(provider_result)
    attempts_before_profile = deepcopy(attempts)
    provenance = {
        "resolution_basis": provider_result["resolution_basis"],
        "existence_confidence": provider_result["existence_confidence"],
    }
    evidence_profile = resolve._reference_evidence_profile(
        ref,
        provider_result,
        provider_result["status"],
        attempts,
        provider_result.get("title_overlap"),
        provenance,
    )
    assert proof_key not in evidence_profile["metadata_match"]
    assert proof_key not in evidence_profile["best_candidate"]["metadata_match"]
    assert evidence_profile["metadata_match"]["author_match"] is True
    assert provider_result == provider_result_before_profile
    assert attempts == attempts_before_profile
    assert _resolved_identity_is_locked(
        ref, evidence_profile, provider_result, attempts,
    ) is True
    adjudication_before_storage = deepcopy(
        evidence_profile["bibliographic_adjudication"]
    )

    repo = _repo(tmp_path)
    try:
        repo.replace_parse_payload(claims=[], references=[ref], citations=[])
        repo.upsert_resolve_result("r1", {
            "status": provider_result["status"],
            "via": provider_result["via"],
            "matched_title": provider_result["matched_title"],
            "work_type": provider_result.get("work_type"),
            "resolution_basis": provider_result["resolution_basis"],
            "existence_confidence": provider_result["existence_confidence"],
            "resolved_identifier": provider_result["resolved_identifier"],
            "evidence_profile": evidence_profile,
            "attempts": attempts,
        })
        assert provider_result == provider_result_before_profile
        assert attempts == attempts_before_profile
        assert proof_key not in evidence_profile["metadata_match"]
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        stored_proof_attempt = next(
            item for item in stored.attempts
            if proof_key in item.get("metadata_match", {})
        )
        assert stored_proof_attempt["metadata_match"][proof_key] == proof
        assert stored_proof_attempt["metadata_match"][proof_key][
            "search_evidence"
        ]["records"][0] == nonselected_candidate
        assert crossref_metadata._crossref_generic_article_locator_relock(
            ref,
            {"resolution_basis": "metadata_search"},
            provider_result,
            stored.attempts,
        )
        reprofiled = resolve._reference_evidence_profile(
            ref,
            provider_result,
            provider_result["status"],
            stored.attempts,
            provider_result.get("title_overlap"),
            provenance,
        )
        assert proof_key not in reprofiled["metadata_match"]
        assert proof_key not in reprofiled["best_candidate"]["metadata_match"]
        assert reprofiled["bibliographic_adjudication"] == (
            adjudication_before_storage
        )
        assert _resolved_identity_is_locked(
            ref, reprofiled, provider_result, stored.attempts,
        ) is True
        exported = repo.resolve_payload_map()["r1"]
        assert proof_key not in exported["evidence_profile"]["metadata_match"]
        assert proof_key not in exported["evidence_profile"][
            "best_candidate"
        ]["metadata_match"]
        assert exported["evidence_profile"][
            "bibliographic_adjudication"
        ] == adjudication_before_storage

        def assert_rejected(mutate):
            broken = deepcopy(attempts)
            proof_attempt = next(
                item for item in broken
                if "_crossref_generic_article_locator_certificate"
                in item.get("metadata_match", {})
            )
            mutate(proof_attempt["metadata_match"][
                "_crossref_generic_article_locator_certificate"
            ])
            with pytest.raises(ValueError, match="generic article locator certificate"):
                validate_resolve_attempts(broken)

        assert_rejected(lambda value: value.pop("native_record_sha256"))
        assert_rejected(lambda value: value["source"].update(year="2024"))
        assert_rejected(
            lambda value: value["search_evidence"]["query"].update(year=2024.0),
        )
        assert_rejected(
            lambda value: value["search_evidence"]["query"].update(year=True),
        )
        assert_rejected(lambda value: value.update(unrecognized=True))
        assert_rejected(lambda value: value.update(native_record_sha256="0" * 64))
        assert_rejected(
            lambda value: value["source"]["spans"].update(volume=[-1, 8]),
        )

        literal_attempts = deepcopy(attempts)
        literal_profile = next(
            item["metadata_match"] for item in literal_attempts
            if "_crossref_generic_article_locator_certificate"
            in item.get("metadata_match", {})
        )
        literal_certificate = literal_profile[
            "_crossref_generic_article_locator_certificate"
        ]
        literal_venue = literal_certificate["native_venue"]
        literal_profile["_titleless_native_venue_composition"] = {
            "rule": "ordered_native_primary_short_tokens_v1",
            "raw_entry_sha256": literal_certificate["raw_entry_sha256"],
            "native_primary": literal_venue["primary"],
            "native_short": literal_venue["short"] or "synthetic short",
        }
        with pytest.raises(ValueError, match="native venue companion"):
            validate_resolve_attempts(literal_attempts)

        companion_only_attempts = deepcopy(attempts)
        companion_only_profile = next(
            item["metadata_match"] for item in companion_only_attempts
            if "_crossref_generic_article_locator_certificate"
            in item.get("metadata_match", {})
        )
        companion_only_profile["_titleless_native_venue_composition"] = {
            "rule": "ordered_native_primary_short_tokens_v1",
            "raw_entry_sha256": proof["raw_entry_sha256"],
            "native_primary": proof["native_venue"]["primary"],
            "native_short": "Synthetic short",
        }
        companion_only_profile.pop("_crossref_generic_article_locator_certificate")
        with pytest.raises(ValueError, match="native venue companion"):
            validate_resolve_attempts(companion_only_attempts)
    finally:
        repo.close()

    registered_raw = "Vale, N. Northwind Mat 8, 27 (2024)."
    registered_ref = _make_reference(1, registered_raw)
    registered_ref.update({
        "id": "r1", "ref_number": 1, "raw_entry": registered_raw,
        "doi": None, "pmid": None, "isbn": None, "url": None,
    })
    registered_doi = "10.5555/synthetic-registered-locator.2024"
    registered_candidate = {
        "DOI": registered_doi,
        "type": "journal-article",
        "title": ["A synthetic registered-scope article"],
        "author": [{"given": "N", "family": "Vale"}],
        "published-print": {"date-parts": [[2024]]},
        "container-title": ["Northwind Materials"],
        "short-container-title": ["North Mat"],
        "volume": "8",
        "article-number": "27",
        "ISSN": ["1234-5679"],
    }
    authority_path = tmp_path / "generic-locator-authority.sqlite"
    build_catalog(
        authority_path,
        [{
            "record_id": "nlm:synthetic-generic-locator",
            "canonical_title": "Northwind Materials",
            "aliases": ["North Mat"],
            "issns": ["1234-5679"],
        }],
        registry="nlm-pubmed",
        registry_version="storage-test-1",
        created_at="2026-10-08T00:00:00Z",
        source_kind="nlm-pubmed",
        source_url="https://example.invalid/synthetic-registry",
        source_sha256="c" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(authority_path))

    def registered_get(url: str, accept: str = "application/json"):
        parsed = urllib.parse.urlparse(url)
        if "/journals/" in parsed.path and parsed.path.endswith("/works"):
            return 200, json.dumps({
                "message": {"items": [registered_candidate], "total-results": 1},
            })
        if parsed.path.rstrip("/").endswith("/works") and parsed.query:
            return 200, json.dumps({
                "message": {"items": [registered_candidate], "total-results": 2},
            })
        if parsed.path.endswith("/works/" + urllib.parse.quote(registered_doi)):
            return 200, json.dumps({"message": registered_candidate})
        return 404, json.dumps({"message": {}})

    monkeypatch.setattr(resolve, "_get", registered_get)
    monkeypatch.setattr(resolve._rw, "load", lambda _email: None)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    registered_result = crossref_metadata.discover(registered_ref)
    assert registered_result is not None
    assert registered_result["status"] == "resolved", registered_result
    registered_profile = registered_result["metadata_match"]
    registered_proof = registered_profile[
        "_crossref_generic_article_locator_certificate"
    ]
    assert registered_proof["native_venue"]["mode"] == "ordered_primary_short"
    assert set(registered_profile["_titleless_native_venue_composition"]) == {
        "rule", "raw_entry_sha256", "native_primary", "native_short",
    }
    attempts_registered: list[dict] = []
    resolve._append_provider_attempt(attempts_registered, registered_result)

    registered_run = tmp_path / "registered-run"
    registered_run.mkdir()
    repo_registered = _repo(registered_run)
    try:
        repo_registered.replace_parse_payload(
            claims=[], references=[registered_ref], citations=[],
        )
        repo_registered.upsert_resolve_result("r1", {
            "status": "resolved", "via": "crossref_metadata",
            "attempts": attempts_registered,
        })
        stored_registered = repo_registered.get_resolve_result("r1")
        assert stored_registered is not None
        stored_registered_attempt = next(
            item for item in stored_registered.attempts
            if "_crossref_generic_article_locator_certificate"
            in item.get("metadata_match", {})
        )
        stored_registered_profile = stored_registered_attempt["metadata_match"]
        assert stored_registered_profile[
            "_crossref_generic_article_locator_certificate"
        ] == registered_proof
        assert stored_registered_profile[
            "_titleless_native_venue_composition"
        ] == registered_profile["_titleless_native_venue_composition"]
        assert crossref_metadata._crossref_generic_article_locator_relock(
            registered_ref,
            {"resolution_basis": "metadata_search"},
            registered_result,
            stored_registered.attempts,
        )

        def rejected_registered(mutate):
            broken = deepcopy(attempts_registered)
            proof_attempt = next(
                item for item in broken
                if "_crossref_generic_article_locator_certificate"
                in item.get("metadata_match", {})
            )
            mutate(proof_attempt["metadata_match"])
            with pytest.raises(ValueError, match="generic article locator certificate"):
                validate_resolve_attempts(broken)

        rejected_registered(
            lambda profile: profile["_titleless_native_venue_composition"].update(
                native_short="Forged short",
            ),
        )
        rejected_registered(
            lambda profile: profile.pop("_crossref_generic_article_locator_certificate"),
        )
        rejected_registered(
            lambda profile: profile.pop("_titleless_native_venue_composition"),
        )
        rejected_registered(
            lambda profile: profile[
                "_crossref_generic_article_locator_certificate"
            ]["search_evidence"]["registered_source_scope"]["query"].update(
                year=2024.0,
            ),
        )
        rejected_registered(
            lambda profile: profile[
                "_crossref_generic_article_locator_certificate"
            ]["search_evidence"]["registered_source_scope"]["query"].update(
                year=True,
            ),
        )
    finally:
        repo_registered.close()


def test_registered_coordinate_certificate_round_trips_and_relocks(
    tmp_path, monkeypatch,
):
    raw = (
        "Garcia, L. Graph-Based Reconstruction of Coastal Sediment Transport. "
        "Chin Phys B 18, 210-219 (2022)."
    )
    ref = _make_reference(1, raw)
    ref.update({
        "id": "r1", "ref_number": 1, "raw_entry": raw,
        "doi": None, "pmid": None, "isbn": None, "url": None,
        "source_type": "article", "source_kind": "article_like",
        "source_type_confidence": "high", "indexability": "high",
    })
    doi = "10.5555/synthetic-registered-coordinate-2022"
    candidate = {
        "DOI": doi,
        "type": "journal-article",
        "title": ["Graph-Based Reconstruction of Coastal Sediment Transport"],
        "author": [{"given": "L.", "family": "Garcia"}],
        "published-print": {"date-parts": [[2022]]},
        "container-title": ["Chinese Physics B"],
        "volume": "18",
        "page": "210-219",
        "ISSN": ["0028-4793", "1533-4406"],
    }
    authority_path = tmp_path / "registered-coordinate-authority.sqlite"
    build_catalog(
        authority_path,
        [{
            "record_id": "nlm:synthetic-registered-coordinate",
            "canonical_title": "Chinese Physics B",
            "aliases": ["Chin Phys B"],
            "issns": candidate["ISSN"],
        }],
        registry="nlm-pubmed",
        registry_version="registered-coordinate-test-1",
        created_at="2026-10-08T00:00:00Z",
        source_kind="nlm-pubmed",
        source_url="https://example.invalid/registered-coordinate-registry",
        source_sha256="d" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(authority_path))

    def registered_get(url: str, accept: str = "application/json"):
        parsed = urllib.parse.urlparse(url)
        if "/journals/" in parsed.path and parsed.path.endswith("/works"):
            return 200, json.dumps({
                "message": {"items": [candidate], "total-results": 1},
            })
        if parsed.path.endswith("/works/" + urllib.parse.quote(doi)):
            return 200, json.dumps({"message": candidate})
        if parsed.path.rstrip("/").endswith("/works"):
            return 200, json.dumps({
                "message": {"items": [], "total-results": 0},
            })
        return 404, json.dumps({"message": {}})

    monkeypatch.setattr(resolve, "_get", registered_get)
    monkeypatch.setattr(resolve._rw, "load", lambda _email: None)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)

    provider_result = crossref_metadata.discover(ref)
    assert provider_result is not None
    assert provider_result["status"] == "resolved", provider_result
    assert provider_result["resolved_identifier"]["validated_via"] == (
        "crossref:registered_coordinate_certificate"
    )
    proof = provider_result["metadata_match"]["registered_coordinate_certificate"]
    assert set(proof) == {
        "rule_version", "registry", "registry_version", "record_id",
        "snapshot_sha256", "candidate_issns",
    }

    attempts: list[dict] = []
    resolve._append_provider_attempt(attempts, provider_result)
    evidence_profile = resolve._reference_evidence_profile(
        ref,
        provider_result,
        provider_result["status"],
        attempts,
        provider_result.get("title_overlap"),
        {
            "resolution_basis": provider_result["resolution_basis"],
            "existence_confidence": provider_result["existence_confidence"],
        },
    )
    assert evidence_profile["metadata_match"][
        "registered_coordinate_certificate"
    ] == proof
    assert evidence_profile["best_candidate"]["metadata_match"][
        "registered_coordinate_certificate"
    ] == proof
    evidence_profile_before_storage = deepcopy(evidence_profile)
    repo = _repo(tmp_path / "registered-coordinate-run")
    try:
        repo.replace_parse_payload(
            claims=[], references=[ref], citations=[],
        )
        repo.upsert_resolve_result("r1", {
            "status": "resolved", "via": "crossref_metadata",
            "matched_title": provider_result["matched_title"],
            "work_type": provider_result.get("work_type"),
            "resolution_basis": provider_result["resolution_basis"],
            "existence_confidence": provider_result["existence_confidence"],
            "resolved_identifier": provider_result["resolved_identifier"],
            "evidence_profile": evidence_profile,
            "attempts": attempts,
        })
        assert evidence_profile == evidence_profile_before_storage
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        stored_attempt = next(
            item for item in stored.attempts
            if "registered_coordinate_certificate" in item.get("metadata_match", {})
        )
        stored_profile = stored_attempt["metadata_match"]
        assert stored_profile["registered_coordinate_certificate"] == proof
        assert "registered_coordinate_certificate" not in stored.evidence_profile[
            "metadata_match"
        ]
        assert "registered_coordinate_certificate" not in stored.evidence_profile[
            "best_candidate"
        ]["metadata_match"]
        stored_payload = repo.resolve_payload_map()["r1"]
        stored_attempts_before_export = deepcopy(stored.attempts)
        exported_metadata = stored_payload["evidence_profile"]["metadata_match"]
        assert exported_metadata == provider_result["metadata_match"]
        assert stored_payload["evidence_profile"]["best_candidate"][
            "metadata_match"
        ] == evidence_profile["best_candidate"]["metadata_match"]
        before_adjudication = evidence_profile["bibliographic_adjudication"]
        after_adjudication = stored_payload["evidence_profile"][
            "bibliographic_adjudication"
        ]
        assert after_adjudication["identity_status"] == before_adjudication[
            "identity_status"
        ]
        assert after_adjudication["refutations"] == before_adjudication[
            "refutations"
        ]

        # Contextual re-lock control: the current export consumer gets its DOI
        # from resolved_identifier, while this adjudication helper expects the
        # candidate's selected DOI. Rebuild that candidate solely from the
        # persisted row plus its uniquely bound typed attempt.
        bound_attempt = stored_attempt
        relock_result = {
            key: stored_payload[key]
            for key in (
                "status", "via", "matched_title", "work_type",
                "resolution_basis", "resolved_identifier",
            )
        }
        relock_result.update({
            "doi": bound_attempt["doi"],
            "matched_authors": bound_attempt.get("matched_authors"),
            "metadata_match": deepcopy(exported_metadata),
        })
        assert _resolved_identity_is_locked(
            ref, stored_payload["evidence_profile"],
            relock_result, stored.attempts,
        ) is True
        assert stored.attempts == stored_attempts_before_export

        def rejected_storage(mutate):
            broken = deepcopy(attempts)
            proof_attempt = next(
                item for item in broken
                if "registered_coordinate_certificate" in item.get("metadata_match", {})
            )
            mutate(proof_attempt["metadata_match"]["registered_coordinate_certificate"])
            with pytest.raises(ValueError, match="registered coordinate certificate"):
                validate_resolve_attempts(broken)

        rejected_storage(lambda value: value.pop("record_id"))
        rejected_storage(lambda value: value.update(unexpected=True))
        rejected_storage(lambda value: value.update(
            rule_version="crossref-nlm-registered-coordinates/v2",
        ))
        rejected_storage(lambda value: value.update(registry="other-registry"))
        rejected_storage(lambda value: value.update(registry_version=1))
        rejected_storage(lambda value: value.update(snapshot_sha256="A" * 64))
        rejected_storage(lambda value: value.update(candidate_issns="0028-4793"))
        rejected_storage(lambda value: value.update(candidate_issns=[12345678]))
        rejected_storage(lambda value: value.update(candidate_issns=["0028-4794"]))
        rejected_storage(lambda value: value.update(
            candidate_issns=["1533-4406", "0028-4793"],
        ))

        altered_attempts = deepcopy(stored.attempts)
        altered_attempt = next(
            item for item in altered_attempts
            if "registered_coordinate_certificate" in item.get("metadata_match", {})
        )
        altered_attempt["metadata_match"]["registered_coordinate_certificate"][
            "candidate_issns"
        ] = ["1234-5679"]
        validate_resolve_attempts(altered_attempts)
        altered_result = deepcopy(relock_result)
        altered_result["metadata_match"] = deepcopy(altered_attempt["metadata_match"])
        assert _resolved_identity_is_locked(
            ref, {"resolution_basis": "metadata_search"},
            altered_result, altered_attempts,
        ) is False

        def assert_storage_rejected(
            name,
            *,
            attempts_override=None,
            profile_override=None,
            identifier_override=None,
        ):
            negative_repo = _repo(tmp_path / f"registered-coordinate-{name}")
            try:
                negative_repo.replace_parse_payload(
                    claims=[], references=[ref], citations=[],
                )
                negative_payload = {
                    "status": "resolved",
                    "via": "crossref_metadata",
                    "matched_title": provider_result["matched_title"],
                    "work_type": provider_result.get("work_type"),
                    "resolution_basis": provider_result["resolution_basis"],
                    "existence_confidence": provider_result["existence_confidence"],
                    "resolved_identifier": (
                        identifier_override
                        if identifier_override is not None
                        else provider_result["resolved_identifier"]
                    ),
                    "evidence_profile": (
                        profile_override
                        if profile_override is not None else evidence_profile
                    ),
                    "attempts": (
                        attempts_override
                        if attempts_override is not None else attempts
                    ),
                }
                with pytest.raises(
                    RuntimeError,
                    match="registered coordinate certificate",
                ):
                    negative_repo.upsert_resolve_result("r1", negative_payload)
            finally:
                negative_repo.close()

        missing_proof_attempts = deepcopy(attempts)
        missing_proof_attempts[0]["metadata_match"].pop(
            "registered_coordinate_certificate"
        )
        assert_storage_rejected(
            "missing-proof", attempts_override=missing_proof_attempts,
        )

        missing_selected_profile_proof = deepcopy(evidence_profile)
        missing_selected_profile_proof["metadata_match"].pop(
            "registered_coordinate_certificate"
        )
        assert_storage_rejected(
            "missing-selected-profile-proof",
            profile_override=missing_selected_profile_proof,
        )

        missing_best_candidate_proof = deepcopy(evidence_profile)
        missing_best_candidate_proof["best_candidate"]["metadata_match"].pop(
            "registered_coordinate_certificate"
        )
        assert_storage_rejected(
            "missing-best-candidate-proof",
            profile_override=missing_best_candidate_proof,
        )

        wrong_selected_identifier = deepcopy(provider_result["resolved_identifier"])
        wrong_selected_identifier["value"] = "10.5555/synthetic-wrong-doi"
        assert_storage_rejected(
            "wrong-selected-doi", identifier_override=wrong_selected_identifier,
        )

        wrong_attempt_identifier = deepcopy(attempts)
        wrong_attempt_identifier[0]["doi"] = "10.5555/synthetic-wrong-doi"
        wrong_attempt_identifier[0]["resolved_identifier"]["value"] = (
            "10.5555/synthetic-wrong-doi"
        )
        assert_storage_rejected(
            "wrong-attempt-doi", attempts_override=wrong_attempt_identifier,
        )

        unicode_identifier = deepcopy(provider_result["resolved_identifier"])
        unicode_identifier["value"] = "10.5555/stra\u00dfe"
        unicode_collision = deepcopy(attempts)
        unicode_collision[0]["doi"] = "10.5555/STRASSE"
        unicode_collision[0]["resolved_identifier"] = unicode_identifier
        assert_storage_rejected(
            "unicode-doi-collision",
            attempts_override=unicode_collision,
            identifier_override=unicode_identifier,
        )

        lookalike_host_attempts = deepcopy(attempts)
        lookalike_host_attempts[0]["doi"] = (
            "https://do\u0131.org/" + provider_result["doi"]
        )
        assert_storage_rejected(
            "unicode-doi-host", attempts_override=lookalike_host_attempts,
        )

        altered_profile = deepcopy(evidence_profile)
        altered_profile["metadata_match"]["score"] -= 0.01
        assert_storage_rejected(
            "altered-ordinary-metadata", profile_override=altered_profile,
        )

        altered_selected_certificate = deepcopy(evidence_profile)
        altered_selected_certificate["metadata_match"][
            "registered_coordinate_certificate"
        ]["candidate_issns"] = ["1234-5679"]
        assert_storage_rejected(
            "altered-selected-certificate",
            profile_override=altered_selected_certificate,
        )

        altered_best_certificate = deepcopy(evidence_profile)
        altered_best_certificate["best_candidate"]["metadata_match"][
            "registered_coordinate_certificate"
        ]["candidate_issns"] = ["1234-5679"]
        assert_storage_rejected(
            "altered-best-candidate-certificate",
            profile_override=altered_best_certificate,
        )

        ambiguous_attempts = deepcopy(attempts)
        ambiguous_attempts.append(deepcopy(attempts[0]))
        assert_storage_rejected(
            "ambiguous-binding", attempts_override=ambiguous_attempts,
        )

        ambiguous_best_attempts = deepcopy(attempts)
        duplicate_best_attempt = deepcopy(attempts[0])
        duplicate_best_attempt["doi"] = "10.5555/synthetic-other-doi"
        duplicate_best_attempt["resolved_identifier"]["value"] = (
            "10.5555/synthetic-other-doi"
        )
        ambiguous_best_attempts.append(duplicate_best_attempt)
        assert_storage_rejected(
            "ambiguous-best-candidate-binding",
            attempts_override=ambiguous_best_attempts,
        )

        unknown_result = deepcopy(provider_result)
        unknown_result["metadata_match"]["unexpected_extra"] = True
        unknown_profile = resolve._reference_evidence_profile(
            ref,
            unknown_result,
            unknown_result["status"],
            attempts,
            unknown_result.get("title_overlap"),
            {
                "resolution_basis": unknown_result["resolution_basis"],
                "existence_confidence": unknown_result["existence_confidence"],
            },
        )
        assert "unexpected_extra" in unknown_profile["metadata_match"]
        with pytest.raises(ValueError, match="not a supported metadata variant"):
            repo.upsert_resolve_result("r1", {
                "status": "resolved",
                "via": "crossref_metadata",
                "matched_title": provider_result["matched_title"],
                "work_type": provider_result.get("work_type"),
                "resolution_basis": provider_result["resolution_basis"],
                "existence_confidence": provider_result["existence_confidence"],
                "resolved_identifier": provider_result["resolved_identifier"],
                "evidence_profile": unknown_profile,
                "attempts": attempts,
            })

        # Exercise the real Fetch cache-seeding consumer with the exported row.
        # A user-supplied synthetic fixture avoids transport and document-quality
        # policy; the registered certificate remains in the resolve evidence.
        from core.app.phases import fetch as fetch_phase
        from core.fetch.storage import content_store

        seed_run = tmp_path / "registered-coordinate-cache-seed"
        seed_repo = _repo(seed_run)
        try:
            seed_repo.replace_parse_payload(
                claims=[], references=[ref], citations=[],
            )
            seed_repo.upsert_resolve_result("r1", {
                "status": "resolved",
                "via": "crossref_metadata",
                "matched_title": provider_result["matched_title"],
                "work_type": provider_result.get("work_type"),
                "resolution_basis": provider_result["resolution_basis"],
                "existence_confidence": provider_result["existence_confidence"],
                "resolved_identifier": provider_result["resolved_identifier"],
                "evidence_profile": evidence_profile,
                "attempts": attempts,
            })
            seed_payload = seed_repo.resolve_payload_map()["r1"]
        finally:
            seed_repo.close()
        content_store.record_parsed_text(
            str(seed_run),
            ref,
            "fulltext",
            "user",
            ("Graph-Based Reconstruction of Coastal Sediment Transport " * 20),
            supplied_by="user",
            supplied_via="registered-coordinate-roundtrip-test",
            file_format="txt",
        )
        seeded = fetch_phase._seed_reusable_sources(str(seed_run), {"r1": ref})
        assert seeded["fulltext"] == 1
        assert content_store._resolve_has_hard_identity_conflict(seed_payload) is False
    finally:
        repo.close()


def test_native_crossref_preprint_proof_round_trips_and_validates_body_hash(tmp_path):
    repo = _repo(tmp_path)
    doi = "10.9999/synthetic-crossref-preprint-31415"
    body = json.dumps({"message": {
        "DOI": doi, "type": "posted-content", "subtype": "preprint",
    }})
    proof = {
        "version": 1,
        "requested_doi": doi,
        "request_url": f"https://api.crossref.org/works/{urllib.parse.quote(doi)}",
        "http_status": 200,
        "response_body": body,
        "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    attempt = {
        "status": "resolved", "via": "identifier_validation:crossref",
        "identifier_type": "doi", "identifier_value": doi,
        "_crossref_native_work_proof": proof,
    }
    payload = {"status": "resolved", "via": "crossref", "attempts": [attempt]}
    try:
        repo.upsert_resolve_result("r1", payload)
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        assert stored.attempts == [attempt]

        broken = {**attempt, "_crossref_native_work_proof": {
            **proof, "response_body": body + " ",
        }}
        with pytest.raises(ValueError, match="proof.*body|body.*proof"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [broken]})
        extra_field = {**attempt, "_crossref_native_work_proof": {
            **proof, "unrecognized": True,
        }}
        with pytest.raises(ValueError, match="invalid field set"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [extra_field]})
    finally:
        repo.close()


def test_native_datacite_doi_proof_round_trips_and_binds_direct_doi_record(tmp_path):
    repo = _repo(tmp_path)
    doi = "10.9999/synthetic-datacite-software-31415"
    body = json.dumps({"data": {
        "id": doi,
        "type": "dois",
        "attributes": {"doi": doi},
    }})
    proof = {
        "version": 1,
        "requested_doi": doi,
        "request_url": f"https://api.datacite.org/dois/{urllib.parse.quote(doi)}",
        "http_status": 200,
        "response_body": body,
        "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    attempt = {
        "status": "resolved",
        "via": "identifier_validation:datacite",
        "identifier_type": "doi",
        "identifier_value": doi,
        "doi": doi,
        "_datacite_native_doi_proof": proof,
    }
    payload = {"status": "resolved", "via": "datacite_search", "attempts": [attempt]}
    try:
        repo.upsert_resolve_result("r1", payload)
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        assert stored.attempts == [attempt]

        broken_url = {
            **attempt,
            "_datacite_native_doi_proof": {
                **proof,
                "request_url": "https://api.datacite.org/dois/10.9999/other",
            },
        }
        with pytest.raises(ValueError, match="request URL"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [broken_url]})

        broken_hash = {
            **attempt,
            "_datacite_native_doi_proof": {**proof, "response_body": body + " "},
        }
        with pytest.raises(ValueError, match="body digest"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [broken_hash]})

        for field, value in (
            ("id", "10.9999/other-record"),
            ("attributes.doi", "10.9999/other-record"),
            ("type", "records"),
        ):
            changed = json.loads(body)
            if field == "attributes.doi":
                changed["data"]["attributes"]["doi"] = value
            else:
                changed["data"][field] = value
            changed_body = json.dumps(changed)
            changed_proof = {
                **proof,
                "response_body": changed_body,
                "response_sha256": hashlib.sha256(
                    changed_body.encode("utf-8")
                ).hexdigest(),
            }
            broken_record = {
                **attempt,
                "_datacite_native_doi_proof": changed_proof,
            }
            with pytest.raises(ValueError, match="requested DOI record"):
                repo.upsert_resolve_result(
                    "r1", {**payload, "attempts": [broken_record]},
                )

        wrong_doi_attempt = {**attempt, "identifier_value": "10.9999/other-record"}
        with pytest.raises(ValueError, match="identifier value"):
            repo.upsert_resolve_result(
                "r1", {**payload, "attempts": [wrong_doi_attempt]},
            )

        wrong_via = {**attempt, "via": "datacite_search"}
        with pytest.raises(ValueError, match="unknown resolve attempt field: identifier_type"):
            repo.upsert_resolve_result("r1", {**payload, "attempts": [wrong_via]})

        wrong_validation_via = {
            **attempt,
            "via": "identifier_validation:crossref",
        }
        with pytest.raises(ValueError, match="direct DOI validation"):
            repo.upsert_resolve_result(
                "r1", {**payload, "attempts": [wrong_validation_via]},
            )
    finally:
        repo.close()


def test_crossref_same_work_fallback_persists_full_attempt_and_typed_profile(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path)
    ref = {
        "id": "r1", "ref_number": 1,
        "raw_entry": (
            "Smith AB. Treatment effects on clinical outcomes. Journal. 2018. "
            "doi:10.1234/truncated"
        ),
        "title": "Treatment effects on clinical outcomes", "year": 2018,
        "ay_surname": "smith", "doi": "10.1234/truncated",
        "pmid": None, "isbn": None, "url": None,
        "source_type": "article", "source_kind": "article_like",
        "source_type_confidence": "high", "source_type_evidence": [],
        "indexability": "high", "cited_coordinates": [],
    }
    matched_title = "Treatment effects on clinical outcomes"
    matched_doi = "10.1234/truncated.123456789"
    metadata_match = resolve._metadata_match_profile(ref, {
        "title": [matched_title], "author": [{"family": "Smith"}],
        "published": {"date-parts": [[2018]]},
        "container-title": ["Journal"], "DOI": matched_doi,
    }, matched_title)
    fallback = {
        "status": "resolved", "via": "crossref_metadata",
        "reason": "unique Crossref same-work correction candidate",
        "matched_title": matched_title, "matched_authors": ["Smith"],
        "abstract": None, "retracted": False,
        "fulltext_exists": "unknown", "oa_status": "unknown",
        "work_type": "article", "resolution_basis": "metadata_search",
        "existence_confidence": "medium", "metadata_match": metadata_match,
        "fulltext_links": [],
        "resolved_identifier": {
            "type": "doi", "value": matched_doi,
            "validated_via": "crossref:unique_same_work_correction",
        },
    }
    monkeypatch.setattr(resolve, "_identifier_fallback_search", lambda _ref: fallback)
    monkeypatch.setattr(resolve, "_issue_attestation_checks", lambda *_args: [])

    payload = resolve._finalize_resolution(
        ref,
        {"status": "not_found", "via": "doi.org", "reason": "Handle responseCode 100"},
        [],
    )

    try:
        repo.upsert_resolve_result("r1", payload)
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        fallback_attempt = next(
            item for item in stored.attempts
            if item.get("attempt_kind") == "identifier_fallback"
        )
        assert fallback_attempt["resolved_identifier"]["value"] == matched_doi
        assert stored.evidence_profile["identifier_fallback"]["matched_authors"] == ["Smith"]
        assert "resolved_identifier" not in stored.evidence_profile["identifier_fallback"]
    finally:
        repo.close()


@pytest.mark.parametrize("correction_status", ["resolved", "not_found"])
def test_ecitmatch_coordinate_correction_excludes_raw_transport_from_closed_storage(
    tmp_path, monkeypatch, correction_status,
):
    repo = _repo(tmp_path)
    ref = {
        "id": "r1", "ref_number": 1,
        "raw_entry": "Smith. A precise article title. BMJ. 2020.",
        "title": "A precise article title", "year": 2020,
        "ay_surname": "smith", "doi": "10.1234/wrong",
        "pmid": None, "isbn": None, "url": None,
        "source_type": "article", "source_kind": "article_like",
        "source_type_confidence": "high", "source_type_evidence": [],
        "indexability": "high", "cited_coordinates": [],
    }
    correction = {
        "status": correction_status, "via": "pubmed_citation_match",
        "reason": "coordinate lookup completed",
        "identity_search": {
            "resolver": "crossref",
            "query_contract": "Crossref bounded bibliographic title search",
            "completion": "complete",
            "outcome": "candidate_incompatible",
        },
        "ecitmatch_url": "https://example.invalid/ecitmatch",
        "ecitmatch_http_status": 200,
        "ecitmatch_body": "BMJ|2020|323|42|Smith|callimachus|NOT_FOUND\\n",
    }
    if correction_status == "resolved":
        correction.update({
            "matched_title": "A precise article title", "matched_authors": ["Smith"],
            "matched_year": 2020, "pmid": "12345", "abstract": None,
            "retracted": False, "fulltext_exists": "unknown", "oa_status": "unknown",
            "work_type": "article", "resolution_basis": "metadata_search",
            "existence_confidence": "medium", "metadata_match": {
                "score": 1.0, "title_overlap": 1.0, "author_match": True,
                "cited_first_author": "smith", "matched_first_author": "smith",
                "year_match": True, "matched_year": 2020, "venue_overlap": 1.0,
                "matched_venue": "BMJ",
            }, "fulltext_links": [],
        })
    monkeypatch.setattr(resolve, "_identifier_fallback_search", lambda _ref: {
        "status": "not_found", "via": "crossref_metadata", "reason": "no candidate",
    })
    monkeypatch.setattr(resolve, "_pubmed_coordinate_correction_search", lambda _ref: correction)
    monkeypatch.setattr(resolve, "_issue_attestation_checks", lambda *_args: [])

    payload = resolve._finalize_resolution(
        ref,
        {"status": "not_found", "via": "doi.org", "reason": "Handle responseCode 100"},
        [],
    )

    try:
        repo.upsert_resolve_result("r1", payload)
        stored = repo.get_resolve_result("r1")
        assert stored is not None
        ecitmatch_attempt = next(
            item for item in stored.attempts
            if item.get("provider_via") == "pubmed_citation_match"
        )
        assert not any(key.startswith("ecitmatch_") for key in ecitmatch_attempt)
        assert ecitmatch_attempt["identity_search"] == correction["identity_search"]
        fallback = stored.evidence_profile["identifier_fallback"]
        assert fallback is not None
        assert not any(key.startswith("ecitmatch_") for key in fallback)
        assert "identity_search" not in fallback
    finally:
        repo.close()


@pytest.mark.parametrize("identity_search", [
    {
        "resolver": "crossref", "query_contract": "bounded title search",
        "completion": "incomplete", "outcome": "candidate_incompatible",
    },
    {
        "resolver": "crossref", "query_contract": "bounded title search",
        "completion": "complete", "outcome": "inconclusive",
    },
])
def test_identity_search_completion_and_outcome_must_agree(tmp_path, identity_search):
    repo = _repo(tmp_path)
    try:
        with pytest.raises(ValueError, match="completion contradicts outcome"):
            repo.upsert_resolve_result("r1", {
                "status": "unverified", "via": "crossref_metadata",
                "attempts": [{
                    "status": "unverified", "via": "crossref_metadata",
                    "identity_search": identity_search,
                }],
            })
    finally:
        repo.close()
