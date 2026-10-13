# tests/test_arxiv_lookup_trace.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403

import urllib.error
import os

import pytest


_REF = {
    "id": "r1",
    "title": "Attention Is All You Need",
    "surname": "Vaswani",
    "year": 2017,
}


def _atom(*, title="Attention Is All You Need"):
    return f'''<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/abs/1706.03762v7</id>
        <title>{title}</title><published>2017-06-12T00:00:00Z</published>
        <author><name>Ashish Vaswani</name></author>
      </entry>
    </feed>'''.encode()


def _items(get_fn, ref=None):
    from core.resolve.providers import arxiv

    return arxiv.candidate_items(ref or _REF, get_fn=get_fn)


def test_exact_arxiv_lookup_records_successful_api_attempt():
    items = _items(lambda url, **kwargs: (200, _atom()))

    assert len(items) == 2
    assert items._fetch_execution_attempts == [{
        "method": "arxiv", "url": items._fetch_execution_attempts[0]["url"],
        "kind": "api", "stage": "candidate_generation",
        "outcome": "success", "status": 200,
        "final_url": None, "content_type": None,
    }]


def test_valid_arxiv_no_match_records_no_match_api_attempt():
    items = _items(lambda url, **kwargs: (200, _atom(title="A Different Paper Entirely")))

    assert items == []
    assert items._fetch_execution_attempts[0]["outcome"] == "no_match"
    assert items._fetch_execution_attempts[0]["status"] == 200
    assert items._provider_error is None


@pytest.mark.parametrize(
    ("get_fn", "outcome", "error_type", "status"),
    [
        (
            lambda url, **kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError(url, 429, "rate limited", None, None)
            ),
            "unavailable", "rate_limit", 429,
        ),
        (
            lambda url, **kwargs: (429, b"rate limited"),
            "unavailable", "rate_limit", 429,
        ),
        (
            lambda url, **kwargs: (_ for _ in ()).throw(OSError("offline")),
            "unavailable", "network", None,
        ),
        (lambda url, **kwargs: (200, b"not atom"), "error", "provider_error", 200),
    ],
)
def test_failed_arxiv_lookup_has_one_typed_attempt(get_fn, outcome, error_type, status):
    items = _items(get_fn)

    assert items == []
    assert len(items._fetch_execution_attempts) == 1
    assert items._fetch_execution_attempts[0]["outcome"] == outcome
    assert items._fetch_execution_attempts[0]["status"] == status
    assert items._provider_error["error_type"] == error_type


def test_arxiv_title_guard_issues_no_query_and_records_no_attempt():
    called = False

    def get_fn(url, **kwargs):
        nonlocal called
        called = True
        return 200, _atom()

    assert _items(get_fn, {"title": "Too short title"}) == []
    assert called is False


def test_arxiv_failure_attempt_round_trips_without_duplicate_provider_diagnostic(tmp_path):
    from core.app.runtime.fetch_audit import _record_fetch_attempts
    from core.infra.db import RunRepository
    from core.resolve.providers import arxiv, _provider_row

    row = _provider_row(
        arxiv,
        "candidate_items",
        {"ref": _REF, "get_fn": lambda url, **kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError(url, 429, "rate limited", None, None)
        ), "normalize_doi": lambda value: value},
    )
    attempt = row["execution_attempts"][0]
    assert attempt["stage"] == "candidate_generation"
    assert attempt["final_url"] is None
    assert attempt["content_type"] is None
    RunRepository.create(
        str(tmp_path), run_id="run-arxiv-trace", input_path="input.pdf",
        input_sha256="abc", accuracy="standard", style=None, model_id=None,
        http_profile="default", challenge_mode="off", fixture_fingerprint="fixture",
    ).close()
    repo = RunRepository.open(str(tmp_path))
    repo.replace_parse_payload(claims=[], references=[{"id": "r1", "raw_entry": "Example"}], citations=[])
    repo.close()
    fetched = {"status": "not_found", "fetch_trace": {"execution": {"attempts": [{
        "queue_index": 0, "batch_index": 0, **attempt,
    }]}, "candidate_generation": {"provider_candidates": [row]}}}

    _record_fetch_attempts(str(tmp_path), "r1", fetched)
    repo = RunRepository.open(str(tmp_path))
    try:
        attempts = repo.list_fetch_attempts("r1")
    finally:
        repo.close()
    assert len(attempts) == 1
    assert attempts[0]["method"] == "arxiv"
    assert attempts[0]["outcome"] == "unavailable"
    assert attempts[0]["trace"]["stage"] == "candidate_generation"
    assert attempts[0]["trace"]["queue_index"] == 0
    assert attempts[0]["trace"]["batch_index"] == 0
    assert attempts[0]["trace"]["final_url"] is None
    assert attempts[0]["trace"]["content_type"] is None


def test_provider_attempt_projection_uses_stage_local_coordinates(tmp_path, monkeypatch):
    import core.fetch.service as fetch_mod

    os.makedirs(tmp_path / "sources")
    base_attempt = {
        "method": "arxiv",
        "kind": "api",
        "stage": "candidate_generation",
        "outcome": "no_match",
        "status": 200,
        "final_url": None,
        "content_type": None,
    }
    monkeypatch.setattr(fetch_mod, "_provider_direct_text_rows", lambda *args: [])
    monkeypatch.setattr(fetch_mod, "_provider_candidate_rows", lambda *args: [
        {
            "provider": "arxiv",
            "items": [],
            "execution_attempts": [
                {**base_attempt, "url": "https://export.arxiv.org/api/query?first"},
                {**base_attempt, "url": "https://export.arxiv.org/api/query?second"},
            ],
        }
    ])
    monkeypatch.setattr(fetch_mod, "_candidate_specs", lambda *args, **kwargs: [])
    monkeypatch.setattr(fetch_mod, "_preprint_resolver_specs", lambda *args, **kwargs: [])
    monkeypatch.setattr(fetch_mod._provider_registry, "enabled_modules", lambda **kwargs: [])

    result = fetch_mod.fetch_fulltext(
        {**_REF, "raw_entry": "Vaswani. Attention Is All You Need. 2017."},
        str(tmp_path),
        {"status": "resolved", "fulltext_exists": "unknown"},
    )

    attempts = result["fetch_trace"]["execution"]["attempts"]
    assert [item["stage"] for item in attempts] == [
        "candidate_generation", "candidate_generation",
    ]
    assert [item["queue_index"] for item in attempts] == [0, 1]
    assert [item["batch_index"] for item in attempts] == [0, 0]
