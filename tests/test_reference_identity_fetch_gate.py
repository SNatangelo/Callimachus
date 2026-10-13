# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

from core.app import pipeline
from core.parse.parsing_common import _make_reference
from core.resolve import service
from core.resolve.providers import crossref_metadata


def test_rejected_related_work_cannot_reach_fetch_or_cached_text(tmp_path, monkeypatch):
    ref = _make_reference(3, (
        "Guidon AC, et al. Consensus disease definitions for neurologic "
        "immune-related adverse events of ICI. J Immunother Cancer 2021;9:e002890."
    ))
    wrong = {
        "DOI": "10.1200/jco.2021.39.15_suppl.2647",
        "type": "journal-article",
        "title": ["Consensus disease definitions for the spectrum of neurologic immune related adverse events."],
        "author": [{"family": "Guidon"}],
        "published-print": {"date-parts": [[2021]]},
        "container-title": ["Journal of Clinical Oncology"],
        "volume": "39", "issue": "15_suppl", "page": "2647",
        "abstract": "Text of a different work",
    }
    monkeypatch.setattr(service, "_get", lambda *_args, **_kwargs: (
        200, json.dumps({"message": {"items": [wrong]}}),
    ))
    discovered = crossref_metadata.discover(ref)
    attempts = []
    service._append_provider_attempt(attempts, discovered)
    result = service._finalize_discovery(ref, {"result": discovered, "attempts": attempts})
    assert result["identity_state"] == "unverified"
    assert result["identity"]["scheme"] == "none"
    assert result["evidence_profile"]["bibliographic_adjudication"]["identity_status"] == "not_identified"
    assert result["attempts"][0]["doi"] == wrong["DOI"]
    assert not result.get("doi")
    assert not result.get("abstract")

    calls = []

    def cache_lookup(*_args, **_kwargs):
        calls.append("cache")
        return {"tier": "fulltext", "stored_path": "wrong-work.txt"}

    def fetch_wrong_work(*_args, **_kwargs):
        calls.append("fetch")
        return {"status": "stored", "text": "Text of a different work"}

    monkeypatch.setattr(pipeline.content_store, "find_reusable_parsed_text", cache_lookup)
    processed = pipeline.process_source(ref, str(tmp_path), deps=pipeline.ProcessSourceDeps(
        resolve_ref=lambda _ref: result, fetch_fulltext=fetch_wrong_work,
        allow_inline_fulltext=True,
    ))
    assert calls == []
    assert processed.fetched is None
    assert processed.cached_source is None
