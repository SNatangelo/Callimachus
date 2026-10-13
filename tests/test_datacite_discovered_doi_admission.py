from __future__ import annotations

import copy
import hashlib
import json

import pytest

from core.parse.citation_coordinates import extract_cited_coordinates
from core.resolve import decision, service
from core.infra.db.repository import RunRepository


WORK_NAME = "Aurora Suite Module"
VERSION = "3.1.4"
SOFTWARE_DOI = "10.5555/aurora-suite-3.1.4"
TITLE = f"{WORK_NAME} (v{VERSION})"


def _reference(*, title: str = TITLE, year: int | None = 2022) -> dict:
    return {
        "id": "synthetic-datacite-admission",
        "ref_number": 1,
        "raw_entry": f"Inventor Q. {title}. [Software]. Example archive. 2022.",
        "title": title,
        "ay_surname": "Inventor",
        "year": year,
        "source_type": "article",
        "source_kind": "article_like",
        "source_type_confidence": "high",
        "indexability": "high",
        "doi": None,
        "pmid": None,
        "pmcid": None,
        "isbn": None,
        "url": None,
    }


def _published_journal_reference() -> dict:
    raw_entry = (
        "Willow R. A deterministic method for invented systems. "
        "Journal of Synthetic Methods. 12(3):45-56 (2024)."
    )
    ref = _reference(title="A deterministic method for invented systems", year=2024)
    ref.update({
        "raw_entry": raw_entry,
        "title": "A deterministic method for invented systems",
        "ay_surname": "Willow",
        "cited_coordinates": extract_cited_coordinates(raw_entry),
    })
    return ref


def _search_candidate(*, doi: str = SOFTWARE_DOI, title: str = WORK_NAME) -> dict:
    return {
        "status": "resolved",
        "via": "datacite_search",
        "matched_title": title,
        "doi": doi,
        "reason": "synthetic DataCite search candidate",
        "resolution_basis": "metadata_search",
        "existence_confidence": "medium",
        "metadata_match": {"title_overlap": 1.0, "matched_year": 2022},
    }


def _native_record(
    *,
    doi: str = SOFTWARE_DOI,
    native_id: str | None = None,
    title: str = WORK_NAME,
    version: str | None = VERSION,
    resource_type: str = "Software",
    author: str = "Inventor Q.",
    year: int = 2022,
    data_type: str = "dois",
    resource_subtype: str | None = None,
    related_identifiers: list[dict] | None = None,
) -> dict:
    types = {"resourceTypeGeneral": resource_type}
    if resource_subtype is not None:
        types["resourceType"] = resource_subtype
    attributes = {
        "doi": doi,
        "titles": [{"title": title}],
        "version": version,
        "types": types,
        "creators": [{"name": author}],
        "publicationYear": year,
    }
    if related_identifiers is not None:
        attributes["relatedIdentifiers"] = related_identifiers
    return {
        "data": {
            "id": native_id or doi,
            "type": data_type,
            "attributes": attributes,
        }
    }


def _run_promotion(
    monkeypatch,
    *,
    ref=None,
    record=None,
    status=200,
    body=None,
    crossref_status="not_found",
    handle_status="resolved",
):
    ref = copy.deepcopy(ref) if ref is not None else _reference()
    original_ref = copy.deepcopy(ref)
    candidate = _search_candidate(
        title=service._article_title_candidate(ref) or WORK_NAME
    )
    candidate["metadata_match"]["matched_year"] = ref.get("year")
    original_candidate = copy.deepcopy(candidate)
    attempts = [copy.deepcopy(candidate)]
    requested_urls = []

    class Crossref:
        @staticmethod
        def resolve_doi(_doi, ref=None):
            return {
                "status": crossref_status,
                "via": "crossref",
                "reason": "synthetic Crossref response",
            }

    if body is None:
        body = json.dumps(record or _native_record(), separators=(",", ":"))

    def fake_get(url, accept="application/json"):
        requested_urls.append((url, accept))
        return status, body

    monkeypatch.setattr(service, "_crossref_resolver_module", lambda: Crossref)
    monkeypatch.setattr(
        service,
        "_doi_handle",
        lambda doi: {"status": handle_status, "via": "doi.org", "doi": doi},
    )
    monkeypatch.setattr(service, "_get", fake_get)
    result = decision._promote_validated_identifier(ref, candidate, attempts)
    return result, ref, original_ref, original_candidate, attempts, requested_urls


def test_explicit_versioned_software_doi_needs_and_rechecks_native_datacite_record(monkeypatch):
    result, ref, original_ref, original_candidate, attempts, requested_urls = _run_promotion(
        monkeypatch
    )

    assert result["resolved_identifier"] == {
        "type": "doi",
        "value": SOFTWARE_DOI,
        "validated_via": "datacite:native_doi_metadata",
    }
    assert result["metadata_match"]["author_match"] is True
    assert result["metadata_match"]["year_match"] is True
    assert result["metadata_match"]["title_overlap"] >= 0.85
    assert ref == original_ref
    assert attempts[0] == original_candidate
    assert [attempt["via"] for attempt in attempts[-3:]] == [
        "identifier_validation:crossref",
        "identifier_validation:doi.org",
        "identifier_validation:datacite",
    ]
    native = attempts[-1]
    assert native["identifier_type"] == "doi"
    assert native["identifier_value"] == SOFTWARE_DOI
    assert native["_datacite_native_doi_proof"]["response_sha256"] == hashlib.sha256(
        native["_datacite_native_doi_proof"]["response_body"].encode("utf-8")
    ).hexdigest()
    assert len(requested_urls) == 1
    assert requested_urls[0][0].startswith("https://api.datacite.org/dois/")
    assert requested_urls[0][1] == "application/vnd.api+json"


@pytest.mark.parametrize(
    "record",
    [
        _native_record(native_id="10.5555/other-doi"),
        _native_record(doi="10.5555/other-doi"),
        _native_record(title="Different Synthetic Work"),
        _native_record(resource_type="Dataset"),
        _native_record(version="3.1.5"),
        _native_record(version=None),
        _native_record(author="Unrelated Author"),
        _native_record(year=2020),
        {"data": [_native_record()["data"]]},
        _native_record(data_type="other"),
    ],
    ids=[
        "wrong-native-id",
        "wrong-attribute-doi",
        "wrong-work-name",
        "wrong-resource-type",
        "wrong-version",
        "missing-version",
        "author-conflict",
        "year-conflict",
        "non-single-record-data",
        "wrong-data-type",
    ],
)
def test_nonmatching_or_malformed_native_record_never_admits_doi(monkeypatch, record):
    result, _ref, _original_ref, _original_candidate, attempts, _requested_urls = _run_promotion(
        monkeypatch, record=record
    )

    assert "resolved_identifier" not in result
    assert attempts[-1]["via"] == "identifier_validation:datacite"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, "{}"),
        (503, "{}"),
        (200, "not json"),
    ],
    ids=["not-found", "non-authoritative-http", "malformed-json"],
)
def test_non_authoritative_or_malformed_http_response_never_admits_doi(
    monkeypatch, status, body
):
    result, _ref, _original_ref, _candidate, attempts, _urls = _run_promotion(
        monkeypatch, status=status, body=body
    )

    assert "resolved_identifier" not in result
    assert attempts[-1]["via"] != "identifier_validation:datacite" or (
        attempts[-1]["status"] != "resolved"
    )


def test_datacite_native_lookup_runs_only_after_crossref_not_found(monkeypatch):
    result, _ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch, crossref_status="unresolved"
    )

    assert "resolved_identifier" not in result
    assert requested_urls == []
    assert not any(
        attempt.get("via") == "identifier_validation:datacite" for attempt in attempts
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "lost",
        "wrong-url",
        "wrong-body-digest",
        "wrong-native-id",
        "wrong-attribute-doi",
        "wrong-record-type",
        "wrong-resource-type",
        "wrong-version",
    ],
)
def test_relock_withholds_datacite_admission_without_revalidating_native_proof(
    monkeypatch, tamper
):
    result, ref, _original_ref, _candidate, attempts, _urls = _run_promotion(monkeypatch)
    native = next(
        attempt for attempt in attempts
        if attempt.get("via") == "identifier_validation:datacite"
    )
    if tamper == "lost":
        native.pop("_datacite_native_doi_proof")
    elif tamper == "wrong-url":
        native["_datacite_native_doi_proof"]["request_url"] = (
            "https://api.datacite.org/dois/10.5555/other-record"
        )
    elif tamper == "wrong-body-digest":
        native["_datacite_native_doi_proof"]["response_body"] += " "
    else:
        proof = native["_datacite_native_doi_proof"]
        payload = json.loads(proof["response_body"])
        record = payload["data"]
        if tamper == "wrong-native-id":
            record["id"] = "10.5555/other-record"
        elif tamper == "wrong-attribute-doi":
            record["attributes"]["doi"] = "10.5555/other-record"
        elif tamper == "wrong-record-type":
            record["type"] = "records"
        elif tamper == "wrong-resource-type":
            record["attributes"]["types"]["resourceTypeGeneral"] = "Dataset"
        else:
            record["attributes"]["version"] = "3.1.5"
        proof["response_body"] = json.dumps(payload, separators=(",", ":"))
        proof["response_sha256"] = hashlib.sha256(
            proof["response_body"].encode("utf-8")
        ).hexdigest()

    relocked = decision._promote_validated_identifier(ref, result, attempts)

    assert "resolved_identifier" not in relocked
    assert relocked["status"] == "unverified"


def test_relock_reapplies_source_spanned_locator_guard(monkeypatch):
    reference = _reference(title=WORK_NAME)
    reference["raw_entry"] = (
        f"Inventor Q. {WORK_NAME}. Example journal. 2022."
    )
    result, ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch,
        ref=reference,
        record=_native_record(
            resource_type="JournalArticle",
            version=None,
        ),
    )
    monkeypatch.setattr(
        decision,
        "_metadata_candidate_has_source_spanned_definite_locator_conflict",
        lambda *_args, **_kwargs: True,
    )

    relocked = decision._promote_validated_identifier(ref, result, attempts)

    assert "resolved_identifier" not in relocked
    assert relocked["status"] == "unverified"
    assert len(requested_urls) == 1


def test_nonsoftware_relock_rechecks_new_author_anchor(monkeypatch):
    reference = _reference(title=WORK_NAME)
    reference.update({
        "raw_entry": f"Inventor Q. {WORK_NAME}. Example archive. 2022.",
        "ay_surname": None,
    })
    result, ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch,
        ref=reference,
        record=_native_record(
            resource_type="Dataset",
            version=None,
            author="Outside",
        ),
    )
    assert result["metadata_match"]["author_match"] is False
    assert "resolved_identifier" in result

    changed_reference = {**ref, "ay_surname": "Inventor"}
    relocked = decision._promote_validated_identifier(
        changed_reference, result, attempts
    )

    assert "resolved_identifier" not in relocked
    assert relocked["status"] == "unverified"
    assert len(requested_urls) == 1


def test_nonsoftware_relock_rechecks_changed_cited_year(monkeypatch):
    reference = _reference(title=WORK_NAME)
    reference["raw_entry"] = (
        f"Inventor Q. {WORK_NAME}. Example archive. 2022."
    )
    result, ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch,
        ref=reference,
        record=_native_record(
            resource_type="Dataset",
            version=None,
        ),
    )
    assert result["metadata_match"]["year_match"] is True
    assert "resolved_identifier" in result

    changed_reference = {**ref, "year": 2020}
    relocked = decision._promote_validated_identifier(
        changed_reference, result, attempts
    )

    assert "resolved_identifier" not in relocked
    assert relocked["status"] == "unverified"
    assert len(requested_urls) == 1


def test_datacite_identity_validation_does_not_depend_on_doi_handle_success(
    monkeypatch,
):
    result, _ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch,
        handle_status="unresolved",
    )

    assert result["resolved_identifier"]["validated_via"] == (
        "datacite:native_doi_metadata"
    )
    assert attempts[-2]["via"] == "identifier_validation:doi.org"
    assert attempts[-2]["status"] == "unresolved"
    assert attempts[-1]["via"] == "identifier_validation:datacite"
    assert len(requested_urls) == 1


@pytest.mark.parametrize(
    "record",
    [
        _native_record(
            title="A deterministic method for invented systems",
            author="Willow",
            year=2024,
            resource_type="Text",
            resource_subtype="Preprint",
        ),
        _native_record(
            title="A deterministic method for invented systems",
            author="Willow",
            year=2024,
            resource_type="Text",
            related_identifiers=[{
                "relatedIdentifier": "10.5555/synthetic.published.record",
                "relatedIdentifierType": "DOI",
                "relationType": "IsPreprintOf",
            }],
        ),
    ],
    ids=["native-preprint-subtype", "native-is-preprint-of-relation"],
)
def test_native_datacite_preprint_evidence_withholds_published_journal_doi(
    monkeypatch, record,
):
    result, ref, _original_ref, _candidate, attempts, _urls = _run_promotion(
        monkeypatch,
        ref=_published_journal_reference(),
        record=record,
    )

    assert decision._has_published_journal_unit(ref) is True
    assert "resolved_identifier" not in result
    assert result["status"] == "unverified"
    assert attempts[-1]["via"] == "identifier_validation:datacite"


def test_native_text_without_preprint_evidence_keeps_matching_behavior(monkeypatch):
    result, ref, _original_ref, _candidate, attempts, _urls = _run_promotion(
        monkeypatch,
        ref=_published_journal_reference(),
        record=_native_record(
            title="A deterministic method for invented systems",
            author="Willow",
            year=2024,
            resource_type="Text",
            version=None,
        ),
    )

    assert decision._has_published_journal_unit(ref) is True
    assert result["resolved_identifier"]["validated_via"] == (
        "datacite:native_doi_metadata"
    )
    assert attempts[-1]["work_type"] == "Text"


def test_persisted_datacite_preprint_evidence_still_blocks_relock(
    monkeypatch, tmp_path,
):
    ref = _published_journal_reference()
    result, ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch,
        ref=ref,
        record=_native_record(
            title=ref["title"],
            author="Willow",
            year=2024,
            resource_type="Text",
            related_identifiers=[{
                "relatedIdentifier": "10.5555/synthetic.published.record",
                "relatedIdentifierType": "DOI",
                "relationType": "IsPreprintOf",
            }],
        ),
    )
    assert "resolved_identifier" not in result
    native_attempt = attempts[-1]
    unsafe_resolve = {
        **_search_candidate(title=ref["title"]),
        "matched_title": native_attempt["matched_title"],
        "metadata_match": native_attempt["metadata_match"],
        "work_type": native_attempt["work_type"],
        "resolved_identifier": {
            "type": "doi",
            "value": SOFTWARE_DOI,
            "validated_via": "datacite:native_doi_metadata",
        },
    }
    repo = RunRepository.create(
        str(tmp_path),
        run_id="datacite-preprint-relock",
        input_path="synthetic-input",
        input_sha256="synthetic-sha256",
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile=None,
        challenge_mode=None,
        fixture_fingerprint="synthetic-fixture",
    )
    try:
        repo.replace_parse_payload(
            claims=[],
            references=[{
                "id": ref["id"],
                "ref_number": ref["ref_number"],
                "raw_entry": ref["raw_entry"],
            }],
            citations=[],
        )
        evidence_profile = service._reference_evidence_profile(
            ref,
            unsafe_resolve,
            unsafe_resolve["status"],
            attempts,
            1.0,
            {
                "resolution_basis": unsafe_resolve["resolution_basis"],
                "existence_confidence": unsafe_resolve["existence_confidence"],
            },
        )
        repo.upsert_resolve_result(
            ref["id"],
            {
                **unsafe_resolve,
                "ref_id": ref["id"],
                "evidence_profile": evidence_profile,
                "attempts": attempts,
            },
        )
        persisted = repo.resolve_payload_map()[ref["id"]]
        persisted_native = next(
            attempt
            for attempt in persisted["attempts"]
            if attempt.get("via") == "identifier_validation:datacite"
        )
        native_record = json.loads(
            persisted_native["_datacite_native_doi_proof"]["response_body"]
        )["data"]["attributes"]
        assert native_record["relatedIdentifiers"][0]["relationType"] == (
            "IsPreprintOf"
        )

        relocked = decision._promote_validated_identifier(
            ref, persisted, persisted["attempts"]
        )
        assert "resolved_identifier" not in relocked
        assert relocked["status"] == "unverified"
        assert len(requested_urls) == 1
    finally:
        repo.close()


def test_persisted_native_doi_proof_relocks_and_tampering_withholds_identity(
    monkeypatch, tmp_path,
):
    result, ref, _original_ref, _candidate, attempts, requested_urls = _run_promotion(
        monkeypatch
    )
    repo = RunRepository.create(
        str(tmp_path),
        run_id="datacite-proof-relock",
        input_path="synthetic-input",
        input_sha256="synthetic-sha256",
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile=None,
        challenge_mode=None,
        fixture_fingerprint="synthetic-fixture",
    )
    try:
        repo.replace_parse_payload(
            claims=[],
            references=[{
                "id": ref["id"],
                "ref_number": ref["ref_number"],
                "raw_entry": ref["raw_entry"],
            }],
            citations=[],
        )
        persisted_evidence = service._reference_evidence_profile(
            ref,
            result,
            result["status"],
            attempts,
            1.0,
            {
                "resolution_basis": result["resolution_basis"],
                "existence_confidence": result["existence_confidence"],
            },
        )
        repo.upsert_resolve_result(
            ref["id"],
            {
                **result,
                "ref_id": ref["id"],
                "evidence_profile": persisted_evidence,
                "attempts": attempts,
            },
        )
        persisted = repo.resolve_payload_map()[ref["id"]]
        persisted_attempt = next(
            attempt
            for attempt in persisted["attempts"]
            if attempt.get("via") == "identifier_validation:datacite"
        )
        assert persisted_attempt["_datacite_native_doi_proof"]
        assert decision._promote_validated_identifier(
            ref, persisted, persisted["attempts"]
        )["resolved_identifier"]["validated_via"] == (
            "datacite:native_doi_metadata"
        )
        assert len(requested_urls) == 1

        identity_only = copy.deepcopy(persisted)
        identity_attempt = next(
            attempt
            for attempt in identity_only["attempts"]
            if attempt.get("via") == "identifier_validation:datacite"
        )
        identity_attempt.pop("_datacite_native_doi_proof")
        withheld = decision._promote_validated_identifier(
            ref, identity_only, identity_only["attempts"]
        )
        assert "resolved_identifier" not in withheld
        assert withheld["status"] == "unverified"
        assert len(requested_urls) == 1

        persisted_attempt["_datacite_native_doi_proof"]["response_body"] += " "
        relocked = decision._promote_validated_identifier(
            ref, persisted, persisted["attempts"]
        )
        assert "resolved_identifier" not in relocked
        assert relocked["status"] == "unverified"
        assert len(requested_urls) == 1
    finally:
        repo.close()
