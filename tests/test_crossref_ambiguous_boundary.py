"""Source-bound Crossref recovery for ambiguous title/journal cuts."""
from __future__ import annotations

import copy
import hashlib
import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from core.app import pipeline
from core.infra.db.repository import RunRepository
from core.parse.parsing_common import _make_reference
from core.resolve import decision, service as resolve
from core.resolve.bibliographic_adjudication import _resolved_identity_is_locked
from core.resolve.journal_authority import CATALOG_ENV, build_catalog
from core.resolve.providers import crossref_metadata


_RAW = "Novotny, L.; van Hulst, N. Antennas for light. Nat. Photonics 2011, 5, 83−90."
_DOI = "10.1038/nphoton.2010.237"


def _ref(raw: str = _RAW) -> dict:
    ref = _make_reference(1, raw)
    ref.update({"id": "ambiguous-boundary", "ref_number": 1, "raw_entry": raw})
    return ref


def _candidate(**overrides) -> dict:
    return {
        "DOI": _DOI,
        "type": "journal-article",
        "title": ["Antennas for light"],
        "author": [
            {"given": "Lukas", "family": "Novotny"},
            {"given": "Niek", "family": "van Hulst"},
        ],
        "published-print": {"date-parts": [[2011]]},
        "published-online": {"date-parts": [[2011, 2, 1]]},
        "container-title": ["Nature Photonics"],
        "short-container-title": ["Nature Photon"],
        "volume": "5",
        "page": "83-90",
        "ISSN": ["1749-4885", "1749-4893"],
        "abstract": "fixture abstract",
        **overrides,
    }


def _install_catalog(tmp_path, monkeypatch) -> None:
    path = tmp_path / "nlm-fixture.sqlite"
    build_catalog(
        path,
        [{
            "record_id": "nlm:fixture-nature-photonics",
            "canonical_title": "Nature Photonics",
            "aliases": ["Nat Photonics"],
            "issns": ["1749-4885", "1749-4893"],
        }],
        registry="nlm-pubmed",
        registry_version="ambiguous-boundary-test-1",
        created_at="2026-10-12T00:00:00Z",
        source_kind="test-fixture",
        source_url="https://example.invalid/nlm-fixture",
        source_sha256="a" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(path))


def _install_crossref(
    monkeypatch, raw: str, candidate: dict, *, total: int = 1,
    extra_items: list[dict] | None = None, direct_candidate: dict | None = None,
) -> list[str]:
    calls: list[str] = []

    def fake_get(url: str, accept: str = "application/json"):
        del accept
        calls.append(url)
        parsed = urlparse(url)
        if parsed.path.rstrip("/").endswith("/works") and parsed.query:
            query = parse_qs(parsed.query)
            assert query["query.bibliographic"] == [raw]
            items = [candidate, *(extra_items or [])]
            return 200, json.dumps({
                "message": {"items": items, "total-results": total},
            })
        if "/works/" in parsed.path:
            requested = unquote(parsed.path.split("/works/", 1)[-1])
            if requested == _DOI:
                return 200, json.dumps({"message": direct_candidate or candidate})
        return 404, json.dumps({"message": {}})

    monkeypatch.setattr(resolve, "_get", fake_get)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    return calls


def test_unique_ambiguous_boundary_candidate_is_directly_source_validated(
    tmp_path, monkeypatch,
):
    ref = _ref()
    assert ref["bibliographic_parse"]["reader"] == "semicolon-journal"
    parse_before = copy.deepcopy(ref["bibliographic_parse"])
    candidate = _candidate()
    profile_before = crossref_metadata._crossref_metadata_match_profile(ref, candidate)
    _install_catalog(tmp_path, monkeypatch)
    noise = _candidate(
        DOI="10.9999/incompatible-search-hit",
        author=[{"given": "Other", "family": "Person"}],
    )
    calls = _install_crossref(
        monkeypatch, _RAW, candidate, total=240,
        extra_items=[copy.deepcopy(noise) for _ in range(239)],
    )

    result = crossref_metadata.discover(ref)

    assert result["status"] == "resolved"
    assert result["doi"] == _DOI
    assert result["resolved_identifier"] == {
        "type": "doi",
        "value": _DOI,
        "validated_via": "crossref:ambiguous_title_journal_boundary_certificate",
    }
    query_url = next(url for url in calls if urlparse(url).query)
    query = parse_qs(urlparse(query_url).query)
    assert query["query.bibliographic"] == [_RAW]
    assert query["rows"] == ["1000"]
    assert query["query.author"] == ["novotny"]
    assert query["filter"] == ["from-pub-date:2011-01-01,until-pub-date:2011-12-31"]
    assert any(urlparse(url).path.endswith(_DOI) for url in calls)
    assert ref["title"] is None
    assert ref.get("journal") is None
    assert ref["bibliographic_parse"] == parse_before
    assert result["metadata_match"] == profile_before
    assert result["_crossref_native_work_proof"]["version"] == 2

    attempts: list[dict] = []
    resolve._append_provider_attempt(attempts, copy.deepcopy(result))
    assert _resolved_identity_is_locked(ref, {}, result, attempts)
    assert decision._promote_validated_identifier(
        ref, copy.deepcopy(result), attempts,
    )["status"] == "resolved"
    monkeypatch.setattr(resolve, "_attest_repository_record", lambda _ref, selected, _attempts: selected)
    monkeypatch.setattr(resolve, "_enrich_resolved_metadata", lambda _ref, selected, _attempts, **_kw: selected)
    monkeypatch.setattr(resolve, "_annotate_primary_fulltext_links", lambda selected: selected)
    monkeypatch.setattr(resolve, "_issue_attestation_checks", lambda *_args: [])
    finalized = resolve._finalize_resolution(ref, copy.deepcopy(result), attempts)
    assert finalized["status"] == "resolved"
    assert finalized["evidence_profile"]["bibliographic_adjudication"]["identity_status"] == "identified"
    assert finalized["evidence_profile"]["journal_alias_assessment"]["status"] == "exact"
    assert pipeline._should_fetch(ref, finalized) is True

    repo = RunRepository.create(
        str(tmp_path), run_id="ambiguous", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="f",
    )
    repo.replace_parse_payload(
        claims=[], references=[copy.deepcopy(ref)],
        citations=[],
    )
    try:
        repo.upsert_resolve_result(ref["id"], finalized)
        stored = repo.get_resolve_result(ref["id"])
        assert stored is not None
        assert stored.resolved_identifier == finalized["resolved_identifier"]
        assert stored.evidence_profile["journal_alias_assessment"]["status"] == "exact"
        proof_attempt = next(
            item for item in attempts if item.get("_crossref_native_work_proof") is not None
        )
        stored_proof_attempt = next(
            item for item in stored.attempts if item.get("_crossref_native_work_proof") is not None
        )
        assert stored_proof_attempt["_crossref_native_work_proof"] == proof_attempt["_crossref_native_work_proof"]
    finally:
        repo.close()

    repo = RunRepository.open(str(tmp_path))
    try:
        persisted_ref = next(
            item for item in repo.parse_payload()["references"]
            if item["id"] == ref["id"]
        )
        stored_result = repo.resolve_payload_map()[ref["id"]]
        assert persisted_ref["bibliographic_parse"] == parse_before
        assert "matched_authors" not in stored_result
        assert "metadata_match" not in stored_result
        assert "doi" not in stored_result
        persisted_proof_attempt = next(
            item for item in stored_result["attempts"]
            if item.get("_crossref_native_work_proof") is not None
        )
        assert persisted_proof_attempt["_crossref_native_work_proof"] == proof_attempt[
            "_crossref_native_work_proof"
        ]
        assert _resolved_identity_is_locked(
            persisted_ref, {}, stored_result, stored_result["attempts"],
        )
        assert pipeline._should_fetch(persisted_ref, stored_result) is True

        missing_proof = copy.deepcopy(stored_result)
        missing_proof["attempts"] = [
            attempt for attempt in missing_proof["attempts"]
            if attempt.get("_crossref_native_work_proof") is None
        ]
        assert not _resolved_identity_is_locked(
            persisted_ref, {}, missing_proof, missing_proof["attempts"],
        )
        tampered_proof = copy.deepcopy(stored_result)
        tampered_attempt = next(
            attempt for attempt in tampered_proof["attempts"]
            if attempt.get("_crossref_native_work_proof") is not None
        )
        tampered_attempt["_crossref_native_work_proof"]["source_raw_sha256"] = "0" * 64
        assert not _resolved_identity_is_locked(
            persisted_ref, {}, tampered_proof, tampered_proof["attempts"],
        )
    finally:
        repo.close()


@pytest.mark.parametrize("source_type, source_kind", [
    ("book", "book_like"),
    ("unknown", "chapter_like"),
    ("dataset", "dataset_like"),
    ("legal", "legal_like"),
    ("preprint", "article_like"),
])
def test_article_certificate_cannot_override_a_known_nonarticle_unit(
    tmp_path, monkeypatch, source_type, source_kind,
):
    _install_catalog(tmp_path, monkeypatch)
    ref = _ref()
    _install_crossref(monkeypatch, _RAW, _candidate())
    result = crossref_metadata.discover(ref)
    attempts: list[dict] = []
    resolve._append_provider_attempt(attempts, copy.deepcopy(result))
    assert _resolved_identity_is_locked(ref, {}, result, attempts)

    nonarticle_ref = copy.deepcopy(ref)
    nonarticle_ref.update(source_type=source_type, source_kind=source_kind)
    assert not _resolved_identity_is_locked(nonarticle_ref, {}, result, attempts)
    assert decision._promote_validated_identifier(
        nonarticle_ref, copy.deepcopy(result), attempts,
    )["status"] != "resolved"


@pytest.mark.parametrize("changes", [
    {"title": ["Antennas"]},
    {"title": ["Antennas for light: a survey"]},
    {"author": [{"given": "Other", "family": "Person"}]},
    {"published-print": {"date-parts": [[2012]]}},
    {"volume": "6"},
    {"page": "83"},
    {"ISSN": ["0000-0000"]},
    {"container-title": ["Nature"]},
])
def test_ambiguous_boundary_candidate_requires_full_source_tuple(
    tmp_path, monkeypatch, changes,
):
    _install_catalog(tmp_path, monkeypatch)
    ref = _ref()
    candidate = _candidate()
    candidate.update(changes)
    assert crossref_metadata._ambiguous_boundary_article_proof(ref, candidate) is None


def test_ambiguous_boundary_requires_exact_local_authority(tmp_path, monkeypatch):
    path = tmp_path / "nlm-no-alias.sqlite"
    build_catalog(
        path,
        [{
            "record_id": "nlm:fixture-nature-photonics",
            "canonical_title": "Nature Photonics",
            "aliases": ["Nature Photonix"],
            "issns": ["1749-4885", "1749-4893"],
        }],
        registry="nlm-pubmed", registry_version="ambiguous-boundary-test-1",
        created_at="2026-10-12T00:00:00Z", source_kind="test-fixture",
        source_url="https://example.invalid/nlm-fixture", source_sha256="a" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(path))
    assert crossref_metadata._ambiguous_boundary_article_proof(_ref(), _candidate()) is None


def test_apa_compound_title_and_issue_use_the_same_raw_span_proof(tmp_path, monkeypatch):
    raw = "Smith, J., & Doe, A. (2011). A model. Survey. Adv. Opt. Mater., 5(2), 83–90."
    ref = _make_reference(1, raw)
    ref.update({"id": "apa-boundary", "ref_number": 1, "raw_entry": raw})
    assert ref["bibliographic_parse"]["reader"] == "apa-journal"
    assert ref.get("title") is None and ref.get("journal") is None
    path = tmp_path / "nlm-advanced-optical-materials.sqlite"
    build_catalog(
        path,
        [{
            "record_id": "nlm:fixture-advanced-optical-materials",
            "canonical_title": "Advanced Optical Materials",
            "aliases": ["Adv Opt Mater"],
            "issns": ["1749-4885"],
        }],
        registry="nlm-pubmed", registry_version="ambiguous-boundary-test-1",
        created_at="2026-10-12T00:00:00Z", source_kind="test-fixture",
        source_url="https://example.invalid/nlm-fixture", source_sha256="b" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(path))
    candidate = _candidate(
        DOI="10.5555/apa-boundary-1",
        title=["A model. Survey"],
        author=[{"given": "Jane", "family": "Smith"}, {"given": "Alex", "family": "Doe"}],
        **{
            "published-print": {"date-parts": [[2011]]},
            "published-online": None,
            "container-title": ["Advanced Optical Materials"],
            "short-container-title": ["Adv. Opt. Mater."],
            "volume": "5",
            "issue": "2",
            "page": "83-90",
            "ISSN": ["1749-4885"],
        },
    )
    proof = crossref_metadata._ambiguous_boundary_article_proof(ref, candidate)
    assert proof is not None
    assert proof["title"] == "A model. Survey"
    assert proof["venue"] == "Adv. Opt. Mater"
    assert crossref_metadata._ambiguous_boundary_article_proof(
        ref, {**candidate, "title": ["A model"]},
    ) is None
    assert crossref_metadata._ambiguous_boundary_article_proof(
        ref, {**candidate, "issue": "3"},
    ) is None


def test_titleless_canonical_field_keeps_existing_quoted_raw_title_route(monkeypatch):
    raw = 'Smith, J. (2011). "Antennas for light and matter". Nature Photonics, 5, 83–90.'
    ref = _make_reference(1, raw)
    ref.update({"id": "title-fallback", "ref_number": 1, "raw_entry": raw, "title": None})
    title_hint = crossref_metadata._matching._article_title_candidate(ref)
    assert ref["title"] is None
    assert title_hint
    assert crossref_metadata._ambiguous_boundary_search_source(ref) is None
    candidate = {
        "DOI": "10.5555/title-fallback",
        "type": "journal-article",
        "title": ["Antennas for light and matter"],
        "author": [{"given": "Jane", "family": "Smith"}],
        "published": {"date-parts": [[2011]]},
        "container-title": ["Nature Photonics"],
        "volume": "5",
        "page": "83-90",
    }
    calls: list[str] = []

    def fake_get(url: str, accept: str = "application/json"):
        del accept
        calls.append(url)
        parsed = urlparse(url)
        if parsed.path.rstrip("/").endswith("/works") and parsed.query:
            query = parse_qs(parsed.query)
            if query.get("query.bibliographic") == [title_hint]:
                return 200, json.dumps({"message": {"items": [candidate], "total-results": 1}})
            return 200, json.dumps({"message": {"items": [candidate], "total-results": 1}})
        return 404, json.dumps({"message": {}})

    monkeypatch.setattr(resolve, "_get", fake_get)
    monkeypatch.setattr(resolve._rw, "is_retracted", lambda _doi: False)
    result = crossref_metadata.discover(ref)
    first_query = parse_qs(urlparse(calls[0]).query)
    assert first_query["query.bibliographic"][0].startswith(title_hint)
    assert result["status"] == "resolved"
    assert result["matched_title"] == "Antennas for light and matter"


@pytest.mark.parametrize("field_name, expected", [
    ("year", 2012),
    ("first_author", "Different"),
])
def test_parsed_anchor_values_must_still_match_the_literal_source(field_name, expected):
    ref = _ref()
    altered = copy.deepcopy(ref)
    altered[field_name] = expected
    parsed_field = altered["bibliographic_parse"]["fields"][field_name]
    parsed_field["value"] = expected
    assert crossref_metadata._ambiguous_boundary_source_anchor(altered) is None


def test_incomplete_search_and_multiple_dois_do_not_direct_validate(
    tmp_path, monkeypatch,
):
    _install_catalog(tmp_path, monkeypatch)
    ref = _ref()
    calls = _install_crossref(monkeypatch, _RAW, _candidate(), total=1001)
    result = crossref_metadata.discover(ref)
    assert result["status"] == "unverified"
    assert not any("/works/10.1038/" in url for url in calls)

    other = _candidate(DOI="10.9999/second-compatible-doi")
    calls = _install_crossref(
        monkeypatch, _RAW, _candidate(), total=2, extra_items=[other],
    )
    result = crossref_metadata.discover(ref)
    assert result["status"] == "unverified"
    assert not any("/works/10.1038/" in url for url in calls)


def test_direct_response_and_persisted_proof_are_rechecked(tmp_path, monkeypatch):
    _install_catalog(tmp_path, monkeypatch)
    ref = _ref()
    candidate = _candidate()
    wrong_direct = _candidate(page="84-91")
    _install_crossref(monkeypatch, _RAW, candidate, direct_candidate=wrong_direct)
    result = crossref_metadata.discover(ref)
    assert result["status"] == "unverified"
    assert result.get("resolved_identifier") is None

    _install_crossref(monkeypatch, _RAW, candidate)
    result = crossref_metadata.discover(ref)
    attempts: list[dict] = []
    resolve._append_provider_attempt(attempts, copy.deepcopy(result))
    attempt = attempts[0]
    assert crossref_metadata._crossref_ambiguous_boundary_relock(ref, result, attempts)

    changed_raw_hash = copy.deepcopy(attempt)
    changed_raw_hash["_crossref_native_work_proof"]["source_raw_sha256"] = "0" * 64
    assert not crossref_metadata._crossref_ambiguous_boundary_relock(
        ref, result, [changed_raw_hash],
    )

    altered_issn = copy.deepcopy(attempt)
    proof = altered_issn["_crossref_native_work_proof"]
    body = json.loads(proof["response_body"])
    body["message"]["ISSN"] = ["0000-0000"]
    proof["response_body"] = json.dumps(body)
    proof["response_sha256"] = hashlib.sha256(proof["response_body"].encode()).hexdigest()
    assert not crossref_metadata._crossref_ambiguous_boundary_relock(
        ref, result, [altered_issn],
    )

    changed_attempt_doi = copy.deepcopy(attempt)
    changed_attempt_doi["doi"] = "10.9999/other"
    assert not crossref_metadata._crossref_ambiguous_boundary_relock(
        ref, result, [changed_attempt_doi],
    )

    wrong_digest = copy.deepcopy(attempt)
    wrong_digest["_crossref_native_work_proof"]["response_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="body digest"):
        from core.infra.db.resolve_attempt_storage import validate_resolve_attempts

        validate_resolve_attempts([wrong_digest])

    missing_proof = {key: value for key, value in attempt.items() if key != "_crossref_native_work_proof"}
    assert not crossref_metadata._crossref_ambiguous_boundary_relock(
        ref, result, [missing_proof],
    )
    assert not crossref_metadata._crossref_ambiguous_boundary_relock(
        ref, result, [attempt, copy.deepcopy(attempt)],
    )

    wrong_body = copy.deepcopy(attempt)
    wrong_body["_crossref_native_work_proof"]["response_body"] = json.dumps({
        "message": {"DOI": _DOI, "type": "posted-content"},
    })
    wrong_body["_crossref_native_work_proof"]["response_sha256"] = hashlib.sha256(
        wrong_body["_crossref_native_work_proof"]["response_body"].encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="requested journal article"):
        validate_resolve_attempts([wrong_body])

    changed_doi_body = copy.deepcopy(attempt)
    changed_doi_body["_crossref_native_work_proof"]["response_body"] = json.dumps({
        "message": {"DOI": "10.9999/other", "type": "journal-article"},
    })
    changed_doi_body["_crossref_native_work_proof"]["response_sha256"] = hashlib.sha256(
        changed_doi_body["_crossref_native_work_proof"]["response_body"].encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="not the requested journal article"):
        validate_resolve_attempts([changed_doi_body])


def test_same_doi_direct_response_cannot_switch_to_another_literal_cut(
    tmp_path, monkeypatch,
):
    path = tmp_path / "nlm-two-venues.sqlite"
    build_catalog(
        path,
        [
            {
                "record_id": "nlm:fixture-nature-photonics",
                "canonical_title": "Nature Photonics",
                "aliases": ["Nat Photonics"],
                "issns": ["1749-4885", "1749-4893"],
            },
            {
                "record_id": "nlm:fixture-photonics",
                "canonical_title": "Photonics",
                "aliases": ["Photonics"],
                "issns": ["0028-0836"],
            },
        ],
        registry="nlm-pubmed", registry_version="ambiguous-boundary-test-2",
        created_at="2026-10-12T00:00:00Z", source_kind="test-fixture",
        source_url="https://example.invalid/nlm-fixture", source_sha256="c" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(path))
    ref = _ref()
    search_candidate = _candidate()
    direct_candidate = _candidate(
        title=["Antennas for light. Nat"],
        **{
            "container-title": ["Photonics"],
            "short-container-title": ["Photonics"],
            "ISSN": ["0028-0836"],
        },
    )
    assert crossref_metadata._ambiguous_boundary_article_proof(
        ref, search_candidate,
    )["venue"] == "Nat. Photonics"
    assert crossref_metadata._ambiguous_boundary_article_proof(
        ref, direct_candidate,
    )["venue"] == "Photonics"
    calls = _install_crossref(
        monkeypatch, _RAW, search_candidate, direct_candidate=direct_candidate,
    )

    result = crossref_metadata.discover(ref)

    assert any(urlparse(url).path.endswith(_DOI) for url in calls)
    assert result["status"] == "unverified"
    assert result.get("resolved_identifier") is None


@pytest.mark.parametrize("reverse", [False, True])
def test_same_doi_conflicting_search_proofs_are_withheld_in_either_order(
    tmp_path, monkeypatch, reverse,
):
    path = tmp_path / "nlm-two-venues.sqlite"
    build_catalog(
        path,
        [
            {
                "record_id": "nlm:fixture-nature-photonics",
                "canonical_title": "Nature Photonics",
                "aliases": ["Nat Photonics"],
                "issns": ["1749-4885", "1749-4893"],
            },
            {
                "record_id": "nlm:fixture-photonics",
                "canonical_title": "Photonics",
                "aliases": ["Photonics"],
                "issns": ["0028-0836"],
            },
        ],
        registry="nlm-pubmed", registry_version="ambiguous-boundary-test-2",
        created_at="2026-10-12T00:00:00Z", source_kind="test-fixture",
        source_url="https://example.invalid/nlm-fixture", source_sha256="d" * 64,
    )
    monkeypatch.setenv(CATALOG_ENV, str(path))
    ref = _ref()
    one = _candidate()
    two = _candidate(
        title=["Antennas for light. Nat"],
        **{
            "container-title": ["Photonics"],
            "short-container-title": ["Photonics"],
            "ISSN": ["0028-0836"],
        },
    )
    ordered = [two, one] if reverse else [one, two]
    calls = _install_crossref(
        monkeypatch, _RAW, ordered[0], total=2, extra_items=[ordered[1]],
    )

    result = crossref_metadata.discover(ref)

    assert result["status"] == "unverified"
    assert result.get("resolved_identifier") is None
    assert not any(urlparse(url).path.endswith(_DOI) for url in calls)
