# tests/test_library_metadata.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json

import pytest

from core.app.library import metadata as library_metadata


def _pubmed_xml(pmid: str = "12345") -> str:
    return f"""<PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation><PMID>{pmid}</PMID><Article>
          <ArticleTitle>PubMed Exact Identity</ArticleTitle>
          <Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
          <AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>
        </Article></MedlineCitation>
        <PubmedData><ArticleIdList>
          <ArticleId IdType="doi">10.1234/pubmed</ArticleId>
          <ArticleId IdType="pmc">PMC12345</ArticleId>
        </ArticleIdList></PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>"""


@pytest.mark.parametrize(("scheme", "identifier", "payload_factory", "expected"), [
    (
        "doi", "10.1234/example",
        lambda: json.dumps({
            "DOI": "10.1234/example", "title": "DOI Exact Identity",
            "author": [{"family": "Doe", "given": "Jane"}],
            "issued": {"date-parts": [[2020]]},
        }),
        {"title": "DOI Exact Identity", "authors": "Doe, Jane", "year": 2020,
         "doi": "10.1234/example"},
    ),
    (
        "pmid", "12345", lambda: _pubmed_xml(),
        {"title": "PubMed Exact Identity", "authors": "Doe", "year": 2020,
         "doi": "10.1234/pubmed", "pmid": "12345", "pmcid": "PMC12345"},
    ),
    (
        "pmcid", "PMC12345",
        lambda: json.dumps({"resultList": {"result": [{
            "pmcid": "PMC12345", "title": "Europe PMC Exact Identity",
            "authorList": {"author": [{"lastName": "Doe", "firstName": "Jane"}]},
            "pubYear": "2021", "doi": "10.1234/europepmc", "pmid": "23456",
        }]}}),
        {"title": "Europe PMC Exact Identity", "authors": "Doe, Jane", "year": 2021,
         "doi": "10.1234/europepmc", "pmid": "23456", "pmcid": "PMC12345"},
    ),
    (
        "arxiv", "2401.12345v2",
        lambda: """<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>http://arxiv.org/abs/2401.12345v2</id>
            <title>arXiv Exact Identity</title><published>2024-01-12T00:00:00Z</published>
            <author><name>Jane Doe</name></author>
          </entry></feed>""",
        {"title": "arXiv Exact Identity", "authors": "Jane Doe", "year": 2024,
         "doi": "10.48550/arxiv.2401.12345", "arxiv_id": "2401.12345v2"},
    ),
    (
        "isbn", "978-0-306-40615-7",
        lambda: json.dumps({"ISBN:9780306406157": {
            "title": "ISBN Exact Identity", "authors": [{"name": "Jane Doe"}],
            "publish_date": "March 2022",
            "identifiers": {"isbn_13": ["9780306406157"]},
        }}),
        {"title": "ISBN Exact Identity", "authors": "Jane Doe", "year": 2022,
         "isbn": "9780306406157"},
    ),
])
def test_exact_identifier_providers_return_normalized_fields_and_provenance(
    monkeypatch, scheme, identifier, payload_factory, expected,
):
    calls = []

    def fake_get(url, *, accept):
        calls.append((url, accept))
        if scheme == "doi":
            assert url.startswith("https://doi.org/10.1234/example")
            assert accept == "application/vnd.citationstyles.csl+json"
        elif scheme == "pmid":
            assert "db=pubmed" in url and "id=12345" in url
        elif scheme == "pmcid":
            assert "PMCID%3APMC12345" in url
        elif scheme == "arxiv":
            assert "id_list=2401.12345v2" in url
        else:
            assert "ISBN%3A9780306406157" in url
        return 200, payload_factory(), url + "#final"

    monkeypatch.setattr("core.resolve.service._get_with_final_url", fake_get)
    result = library_metadata.lookup_identity(scheme, identifier)
    assert result["fields"] == expected
    assert result["provenance"]["scheme"] == ("arxiv_id" if scheme == "arxiv" else scheme)
    assert result["provenance"]["provider"]
    assert result["provenance"]["returned_identifier"]
    assert result["provenance"]["response_sha256"]
    assert len(calls) == 1


@pytest.mark.parametrize(("scheme", "identifier"), [
    ("pmid", "bad123"),
    ("pmid", "12abc34"),
    ("isbn", "isbn junk 9780306406157"),
    ("isbn", "https://example.test/9780306406157"),
])
def test_malformed_strong_identifiers_are_rejected_before_network(monkeypatch, scheme, identifier):
    def unexpected_request(*_args, **_kwargs):
        pytest.fail("invalid identifier must not reach a provider")

    monkeypatch.setattr("core.resolve.service._get_with_final_url", unexpected_request)
    with pytest.raises(library_metadata.LibraryMetadataError) as exc_info:
        library_metadata.lookup_identity(scheme, identifier)
    assert exc_info.value.reason_code == "invalid_identity_identifier"


@pytest.mark.parametrize(("scheme", "identifier", "body"), [
    (
        "doi", "10.1234/requested",
        json.dumps({"DOI": "10.1234/other", "title": "Wrong DOI", "author": []}),
    ),
    ("pmid", "12345", _pubmed_xml("54321")),
])
def test_provider_response_must_attest_exact_requested_doi_or_pmid(
    monkeypatch, scheme, identifier, body,
):
    monkeypatch.setattr(
        "core.resolve.service._get_with_final_url",
        lambda url, *, accept: (200, body, url),
    )
    with pytest.raises(library_metadata.LibraryMetadataError) as exc_info:
        library_metadata.lookup_identity(scheme, identifier)
    assert exc_info.value.reason_code == "identity_metadata_mismatch"
    assert exc_info.value.provenance["requested_identifier"] == identifier


def test_provider_failure_keeps_exact_request_provenance(monkeypatch):
    def fail_request(url, *, accept):
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("core.resolve.service._get_with_final_url", fail_request)
    with pytest.raises(library_metadata.LibraryMetadataError) as exc_info:
        library_metadata.lookup_identity("doi", "10.1234/example")
    assert exc_info.value.reason_code == "identity_metadata_unavailable"
    assert exc_info.value.provenance == {
        "provider": "doi.org", "scheme": "doi",
        "requested_identifier": "10.1234/example",
        "request_url": "https://doi.org/10.1234/example",
    }


def test_request_and_redirect_provenance_redacts_ncbi_credentials(monkeypatch):
    request_url = "https://eutils.ncbi.nlm.nih.gov/efetch.fcgi?id=123&email=secret%40test.invalid&api_key=topsecret"
    final_url = "https://eutils.ncbi.nlm.nih.gov/final?id=123&email=secret%40test.invalid&api_key=topsecret"
    monkeypatch.setattr(
        "core.resolve.service._get_with_final_url",
        lambda _url, *, accept: (200, "<ok/>", final_url),
    )
    _, provenance = library_metadata._response(
        request_url, "application/xml", provider="pubmed", scheme="pmid", requested="123",
    )
    for key in ("request_url", "final_url"):
        assert "secret%40test.invalid" not in provenance[key]
        assert "topsecret" not in provenance[key]
        assert "REDACTED" in provenance[key]


def test_identifier_normalization_accepts_only_explicit_pmid_and_isbn_forms():
    assert library_metadata._canonical("pmid", "PMID: 12345") == "12345"
    assert library_metadata._canonical(
        "pmid", "https://pubmed.ncbi.nlm.nih.gov/12345/"
    ) == "12345"
    assert library_metadata._canonical("pmid", "bad123") is None
    assert library_metadata._canonical("isbn", "ISBN-13: 978-0-306-40615-7") == "9780306406157"
    assert library_metadata._canonical("isbn", "junk9780306406157") is None


def test_arxiv_unversioned_lookup_preserves_exact_atom_version_and_rejects_wrong_version(
    monkeypatch,
):
    body = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/abs/2401.12345v2</id>
        <title>Versioned arXiv Identity</title><published>2024-01-12T00:00:00Z</published>
        <author><name>Jane Doe</name></author>
      </entry></feed>"""
    monkeypatch.setattr(
        "core.resolve.service._get_with_final_url",
        lambda url, *, accept: (200, body, url),
    )

    result = library_metadata.lookup_identity("arxiv", "2401.12345")
    assert result["provenance"]["requested_identifier"] == "2401.12345"
    assert result["provenance"]["returned_identifier"] == "2401.12345v2"
    assert result["fields"]["arxiv_id"] == "2401.12345v2"
    with pytest.raises(library_metadata.LibraryMetadataError) as exc_info:
        library_metadata.lookup_identity("arxiv", "2401.12345v1")
    assert exc_info.value.reason_code == "identity_metadata_mismatch"
