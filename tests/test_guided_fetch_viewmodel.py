# tests/test_guided_fetch_viewmodel.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import pytest

from core.gui.guided_fetch_viewmodel import GuidedFetchViewModel, present_facts


def _inventory(tmp_path):
    preview = tmp_path / "source.txt"
    preview.write_text("verified preview", encoding="utf-8")
    return {
        "references": [
            {
                "ref_id": "r1", "ref_number": 1,
                "parsed": {"title": "Cited title", "doi": "10.1000/cited"},
                "resolve": {"resolved_identifier": {"type": "doi", "value": "10.1000/resolved"}, "fulltext_links": [{"url": "https://example.test/full"}]},
                "fetch": {"tier": "fulltext", "preview_path": str(preview), "sources": [], "attempts": [], "pending_tasks": []},
                "fabrication_suspicion": {"suspected": True, "reason": "deterministic tag"},
            },
            {
                "ref_id": "r2", "ref_number": 2, "parsed": {"title": "Only abstract"},
                "resolve": {}, "fetch": {"tier": "abstract", "preview_path": None, "pending_tasks": [{"task_kind": "fetch"}]},
                "fabrication_suspicion": {"suspected": False},
            },
            {
                "ref_id": "r3", "ref_number": 3, "parsed": {}, "resolve": {},
                "fetch": {"tier": None, "preview_path": str(tmp_path / "missing.txt")},
                "fabrication_suspicion": {},
            },
        ]
    }


def test_viewmodel_statuses_overlay_and_audit_detail(tmp_path):
    model = GuidedFetchViewModel(_inventory(tmp_path))
    rows = {row.ref_id: row for row in model.rows()}

    assert (rows["r1"].status, rows["r1"].status_color, rows["r1"].status_marker) == ("fulltext", "green", "✓")
    assert rows["r1"].suspected_fabricated is True
    assert (rows["r2"].status, rows["r2"].status_color, rows["r2"].status_marker) == ("abstract", "yellow", "●")
    assert (rows["r3"].status, rows["r3"].status_color, rows["r3"].status_marker) == ("none", "neutral", "○")
    assert rows["r3"].status_label == "Identity uncertain · no text"
    assert model.preferred_link("r1") == "https://example.test/full"
    assert model.detail_tabs("r1")["Preview"]["path"].endswith("source.txt")
    assert model.preview_path("r3") is None
    assert model.can_submit_source("r1") is False
    assert model.can_submit_source("r2") is True
    assert "deterministic tag" in model.action_guidance("r1")
    assert "What not to do:" in model.action_guidance("r1")
    assert "Proceed / skip remaining" in model.action_guidance("r2")
    assert "No Fetch action" in model.action_guidance("r3")


def test_viewmodel_projects_one_safe_pending_identity_review(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][0]["fetch"]["pending_tasks"] = [{
        "task_id": "identity:r1",
        "task_kind": "source_identity_attestation",
        "target_sha256": "a" * 64,
        "source_text_id": "source-1",
        "source_tier": "fulltext",
        "reference": {"title": "Cited title"},
        "source_identity": {
            "identity_key": "doi:10.1000/cited",
            "origin": "publisher",
            "identity_status": "unverified",
        },
        "resolve_identity": {
            "matched_title": "Resolved title",
            "resolved_identifier": {
                "type": "doi", "value": "10.1000/cited",
            },
        },
    }]
    model = GuidedFetchViewModel(inventory)

    review = model.identity_review("r1")

    assert review == {
        "task_id": "identity:r1",
        "target_sha256": "a" * 64,
        "source_text_id": "source-1",
        "source_tier": "fulltext",
        "cited_title": "Cited title",
        "resolved_title": "Resolved title",
        "resolved_identifier": "DOI: 10.1000/cited",
        "source_identity": {
            "identity_key": "doi:10.1000/cited",
            "origin": "publisher",
            "identity_status": "unverified",
        },
    }
    assert model.can_submit_source("r1") is False
    assert model.has_pending_identity_reviews() is True
    assert "stored_path" not in str(review)


def test_viewmodel_rejects_ambiguous_or_unbound_identity_reviews(tmp_path):
    inventory = _inventory(tmp_path)
    task = {
        "task_id": "identity:r1",
        "task_kind": "source_identity_attestation",
        "target_sha256": "a" * 64,
    }
    inventory["references"][0]["fetch"]["pending_tasks"] = [task, dict(task)]
    model = GuidedFetchViewModel(inventory)

    with pytest.raises(ValueError, match="multiple pending"):
        model.identity_review("r1")

    inventory["references"][0]["fetch"]["pending_tasks"] = [{
        "task_id": "identity:r1",
        "task_kind": "source_identity_attestation",
    }]
    with pytest.raises(ValueError, match="closed task target"):
        GuidedFetchViewModel(inventory).identity_review("r1")


def test_viewmodel_no_text_presentation_uses_typed_audit_states(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"] = [
        {
            "ref_id": "refuted", "parsed": {}, "fetch": {"tier": None},
            "resolve": {"evidence_profile": {"bibliographic_adjudication": {
                "outcome": "refuted", "identity_status": "not_identified",
            }}},
        },
        {
            "ref_id": "not-found", "parsed": {}, "fetch": {"tier": None},
            "resolve": {"evidence_profile": {"bibliographic_adjudication": {
                "outcome": "not_corroborated", "identity_status": "not_identified",
                "check_status": "complete",
            }}},
        },
        {
            "ref_id": "known", "parsed": {}, "fetch": {"tier": None},
            "resolve": {"status": "resolved"},
        },
        {
            "ref_id": "paywall", "parsed": {}, "fetch": {
                "tier": None, "attempts": [{"paywalled": True}],
            }, "resolve": {"status": "resolved"},
        },
        {
            "ref_id": "challenge", "parsed": {}, "fetch": {
                "tier": None, "attempts": [{"challenge_blocked": True}],
            }, "resolve": {}},
        {"ref_id": "unknown", "parsed": {}, "fetch": {"tier": None}, "resolve": {}},
        {
            "ref_id": "review-not-found", "parsed": {}, "fetch": {"tier": None},
            "resolve": {}, "bibliographic_review_labels": [{
                "code": "not_found_after_completed_searches",
            }],
        },
    ]
    rows = {row.ref_id: row for row in GuidedFetchViewModel(inventory).rows()}

    assert (rows["refuted"].status_label, rows["refuted"].status_color) == (
        "Cited reference contradicted · no text", "red",
    )
    assert "not itself a finding of fabrication" in rows["refuted"].status_tooltip
    assert (rows["not-found"].status_label, rows["not-found"].status_color) == (
        "Not found in completed searches", "amber",
    )
    assert "not proof of fabrication" in rows["not-found"].status_tooltip
    assert (rows["known"].status_label, rows["known"].status_color) == (
        "Known work · no text", "blue",
    )
    assert (rows["paywall"].status_label, rows["paywall"].status_color) == (
        "Access blocked · no text", "purple",
    )
    assert (rows["challenge"].status_label, rows["challenge"].status_color) == (
        "Retrieval blocked · identity uncertain", "purple",
    )
    assert (rows["unknown"].status_label, rows["unknown"].status_color) == (
        "Identity uncertain · no text", "neutral",
    )
    assert (rows["review-not-found"].status_label, rows["review-not-found"].status_color) == (
        "Not found in completed searches", "amber",
    )


def test_viewmodel_detail_panels_keep_raw_audit_payload_opt_in(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][0]["resolve"].update({
        "status": "identifier_mismatch",
        "via": "crossref",
        "reason": "the recorded DOI belongs to another work",
    })
    model = GuidedFetchViewModel(inventory)
    panels = model.detail_panels("r1")

    assert panels["Parsed"]["headline"] == "Citation received"
    assert ("Title", "Cited title") in panels["Parsed"]["rows"]
    assert "received" in panels["Parsed"]["message"].lower()
    assert "did not match" in panels["Resolved"]["message"]
    assert ("Lookup result", "Bibliographic mismatch") in panels["Resolved"]["rows"]
    assert ("Lookup route", "crossref") in panels["Resolved"]["rows"]
    assert panels["Resolved"]["raw"] == model.detail_tabs("r1")["Resolved"]
    assert panels["Acquired"]["raw"] == model.detail_tabs("r1")["Acquired"]
    assert panels["Preview"]["raw"] == {"path": model.preview_path("r1")}


def test_viewmodel_separates_bibliographic_concern_from_informational_review(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][1]["bibliographic_concern"] = {
        "level": "elevated_bibliographic_suspicion",
        "conclusion": "completed_search_misses",
    }
    inventory["references"][1]["bibliographic_review_labels"] = [
        {"code": "incomplete_bibliographic_source"},
        {"code": "not_found_after_completed_searches", "providers": ["crossref", "openalex"]},
    ]
    model = GuidedFetchViewModel(inventory)
    row = model.row("r2")

    assert row.suspected_fabricated is False
    assert row.risk_label == "Elevated bibliographic suspicion"
    assert "non-diagnostic" in row.risk_tooltip.lower()
    assert row.review_label == (
        "Incomplete bibliographic source; Not found after completed bibliographic searches"
    )
    assert "informational only" in row.review_tooltip.lower()
    guidance = model.action_guidance("r2")
    assert "non-diagnostic" in guidance.lower()
    assert "not a fabrication finding" in guidance.lower()
    assert "Not found after completed bibliographic searches" in guidance

    inventory["references"][2]["bibliographic_review_labels"] = [
        {"code": "no_compatible_article_at_cited_coordinates"},
    ]
    review_only = GuidedFetchViewModel(inventory).row("r3")
    assert review_only.risk_label == ""
    assert review_only.review_label == "No compatible article at cited coordinates"
    review_guidance = GuidedFetchViewModel(inventory).action_guidance("r3")
    assert "No compatible article at cited coordinates" in review_guidance
    assert "informational only" in review_guidance.lower()
def test_viewmodel_surfaces_retracted_source_and_guidance(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][1]["resolve"] = {"retracted": True}
    model = GuidedFetchViewModel(inventory)
    row = model.row("r2")
    assert row.retracted is True
    assert "recorded as retracted" in model.action_guidance("r2")


def test_viewmodel_uses_current_task_kind_google_fallback_and_readable_facts(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][2]["parsed"] = {"raw_entry": "Unresolved cited work"}
    model = GuidedFetchViewModel(inventory)
    assert model.can_submit_source("r2") is True
    assert model.browser_url("r3").startswith("https://www.google.com/search?q=")
    rendered = present_facts({
        "pending_tasks": [{
            "task_kind": "browser_challenge",
            "url": "https://example.test/a_file",
        }],
    })
    assert "Pending tasks:" in rendered
    assert "Task kind: browser_challenge" in rendered
    assert "https://example.test/a_file" in rendered
    assert "{" not in rendered


def test_browser_url_prefers_concrete_sources_then_resolve_and_parsed_doi_search(tmp_path):
    inventory = _inventory(tmp_path)
    entry = inventory["references"][0]
    entry["resolve"] = {
        "doi": "10.1000/resolved",
        "url": "https://resolved.test/article",
        "fulltext_links": [{"url": "https://fulltext.test/paper"}],
    }
    model = GuidedFetchViewModel(inventory)
    assert model.preferred_link("r1") == "https://fulltext.test/paper"

    entry["resolve"].pop("fulltext_links")
    assert model.preferred_link("r1") == "https://resolved.test/article"
    entry["resolve"].pop("url")
    entry["parsed"]["url"] = "https://parsed.test/source"
    assert model.preferred_link("r1") == "https://parsed.test/source"
    entry["parsed"].pop("url")
    assert model.browser_url("r1") == "https://www.google.com/search?q=10.1000%2Fresolved"

    entry["resolve"] = {}
    assert model.browser_url("r1") == "https://www.google.com/search?q=10.1000%2Fcited"

    entry["parsed"].pop("doi")
    entry["resolve"] = {"resolved_identifier": {"type": "doi", "value": "https://doi.org/10.1000%2Ffrom-url"}}
    assert model.browser_url("r1") == "https://www.google.com/search?q=10.1000%2Ffrom-url"

    entry["resolve"] = {"fulltext_links": {"url": "https://fulltext.test/single"}}
    assert model.preferred_link("r1") == "https://fulltext.test/single"


def test_browser_url_uses_configured_templates_and_encodes_parsed_citation(tmp_path):
    inventory = _inventory(tmp_path)
    entry = inventory["references"][2]
    entry["parsed"] = {
        "raw_entry": "  García, A.  The  title: evidence & practice. ",
        "title": "The title: evidence & practice", "year": 2024,
    }
    model = GuidedFetchViewModel(inventory, env={
        "CITATION_VERIFIER_GUIDED_SEARCH_URL": "https://search.example/find?term={query}",
        "CITATION_VERIFIER_GUIDED_SEARCH_QUERY": "{title} {year}",
    })
    assert model.browser_url("r3") == (
        "https://search.example/find?term=The+title%3A+evidence+%26+practice+2024"
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CITATION_VERIFIER_GUIDED_SEARCH_URL", "https://search.example/?q={term}"),
        ("CITATION_VERIFIER_GUIDED_SEARCH_URL", "https://search.example/?q={query!r}"),
        ("CITATION_VERIFIER_GUIDED_SEARCH_URL", "https://search.example/?q={query:10}"),
        ("CITATION_VERIFIER_GUIDED_SEARCH_URL", ""),
        ("CITATION_VERIFIER_GUIDED_SEARCH_QUERY", "literal only"),
        ("CITATION_VERIFIER_GUIDED_SEARCH_QUERY", "{unknown}"),
    ],
)
def test_browser_search_templates_fail_closed_when_invalid(tmp_path, name, value):
    with pytest.raises(ValueError, match="guided Fetch"):
        GuidedFetchViewModel(_inventory(tmp_path), env={name: value})


def test_browser_search_template_output_must_be_safe_url(tmp_path):
    inventory = _inventory(tmp_path)
    inventory["references"][2]["parsed"] = {"raw_entry": "Unresolved cited work"}
    with pytest.raises(ValueError, match="credential-free HTTP"):
        GuidedFetchViewModel(inventory, env={
            "CITATION_VERIFIER_GUIDED_SEARCH_URL": "file:///tmp/{query}",
        })


def test_browser_search_empty_per_reference_query_fails_closed(tmp_path):
    model = GuidedFetchViewModel(_inventory(tmp_path))
    with pytest.raises(ValueError, match="rendered an empty value"):
        model.browser_url("r3")
