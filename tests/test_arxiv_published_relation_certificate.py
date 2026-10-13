"""Offline tests for the source-bound arXiv-to-published DOI relation."""

from __future__ import annotations

import json
import re
import urllib.parse
from copy import deepcopy

import pytest

from tests._bootstrap import *  # noqa: F401,F403
from core.parse.parsing_common import _make_reference
from core.resolve.providers import arxiv


_CASES = [
    {
        "name": "modern-45",
        "raw": (
            "O.J.C. Dias, J.E. Santos and B. Way, Localised AdS5 × S5 Black Holes, "
            "Phys. Rev. Lett. 117 (2016) 151101 [arXiv:1605.04911] [INSPIRE]."
        ),
        "arxiv_id": "1605.04911",
        "doi": "10.1103/PhysRevLett.117.151101",
        "journal_ref": "Phys. Rev. Lett. 117, 151101 (2016)",
        "container": "Physical Review Letters",
        "short_container": "Phys. Rev. Lett.",
        "volume": "117",
        "locator": "151101",
        "year": 2016,
        "first_author": "Oscar J. C. Dias",
        "family": "Dias",
        "given": "Óscar J. C.",
        "native_title": "Localized AdS5 × S5 Black Holes",
    },
    {
        "name": "modern-55",
        "raw": (
            "C.-W. Chiang, X.-G. He, J. Tandean and X.-B. Yuan, "
            "$$ {R}_{K^{\\left(\\ast \\right)}} $$ and related "
            "$$ b\\to s\\ell \\overline{\\ell} $$ anomalies in minimal flavor "
            "violation framework with Z? boson, Phys. Rev. D 96 (2017) 115022 "
            "[arXiv:1706.02696] [INSPIRE]."
        ),
        "arxiv_id": "1706.02696",
        "doi": "10.1103/PhysRevD.96.115022",
        "journal_ref": "Phys. Rev. D 96, 115022 (2017)",
        "container": "Physical Review D",
        "short_container": "Phys. Rev. D",
        "volume": "96",
        "locator": "115022",
        "year": 2017,
        "first_author": "Cheng-Wei Chiang",
        "family": "Chiang",
        "given": "Cheng-Wei",
        "native_title": "RK anomalies in minimal flavor violation with a Z′ boson",
    },
    {
        "name": "modern-76",
        "raw": (
            "N. Chen, T. Li, Y. Wu and L. Bian, Complementarity of the future e+e? "
            "colliders and gravitational waves in the probe of complex singlet "
            "extension to the standard model, Phys. Rev. D 101 (2020) 075047 "
            "[arXiv:1911.05579] [INSPIRE]."
        ),
        "arxiv_id": "1911.05579",
        "doi": "10.1103/PhysRevD.101.075047",
        "journal_ref": "Phys. Rev. D 101, 075047 (2020)",
        "container": "Physical Review D",
        "short_container": "Phys. Rev. D",
        "volume": "101",
        "locator": "075047",
        "year": 2020,
        "first_author": "Ning Chen",
        "family": "Chen",
        "given": "Ning",
        "native_title": "Complementarity of future e+e− colliders and gravitational waves",
    },
    {
        "name": "independent-synthetic",
        "raw": (
            "A. Example, A source title with the literal symbols Z? and "
            "e+e? channels, J. Synthetic Syst. 14 (2024) 009271 "
            "[arXiv:2401.01234]."
        ),
        "arxiv_id": "2401.01234",
        "doi": "10.5555/synthetic.2024.009271",
        "journal_ref": "J. Synthetic Syst. 14, 009271 (2024)",
        "container": "Synthetic Journal of Variable Systems",
        "short_container": "J. Synthetic Syst.",
        "volume": "14",
        "locator": "009271",
        "year": 2024,
        "first_author": "Ada Example",
        "family": "Example",
        "given": "Ada",
        "native_title": "Unrelated quartz zebrafish moon silently converged",
    },
]


def _reference(case: dict) -> dict:
    ref = _make_reference(1, case["raw"])
    assert ref["source_type"] == "article"
    assert ref["source_kind"] == "article_like"
    assert ref["year"] == case["year"]
    assert not re.search(r"\b10\.\d{4,9}/", case["raw"], re.I)
    assert ref["doi"] in {
        None,
        f"10.48550/arXiv.{case['arxiv_id']}",
    }
    return ref


def _crossref_message(case: dict, **overrides) -> dict:
    message = {
        "DOI": case["doi"],
        "type": "journal-article",
        "title": [case["native_title"]],
        "author": [{
            "family": case["family"],
            "given": case["given"],
            "sequence": "first",
        }],
        "issued": {"date-parts": [[case["year"]]]},
        "published": {"date-parts": [[case["year"]]]},
        "published-online": {"date-parts": [[case["year"]]]},
        "container-title": [case["container"]],
        "short-container-title": [case["short_container"]],
        "volume": case["volume"],
        "article-number": case["locator"],
        "page": None,
    }
    message.update(overrides)
    return message


def _atom(case: dict, **overrides) -> bytes:
    entry_id = overrides.get("entry_id", f"http://arxiv.org/abs/{case['arxiv_id']}v2")
    author = overrides.get("first_author", case["first_author"])
    doi_values = overrides.get("doi_values", [case["doi"]])
    journal_refs = overrides.get("journal_refs", [case["journal_ref"]])
    entries = overrides.get("entries", 1)
    relation = "".join(f"<arxiv:doi>{value}</arxiv:doi>" for value in doi_values)
    journal = "".join(
        f"<arxiv:journal_ref>{value}</arxiv:journal_ref>" for value in journal_refs
    )
    entry = (
        f"<entry><id>{entry_id}</id><title>Atom title is not identity evidence</title>"
        f"<published>{case['year'] - 1}-06-01T00:00:00Z</published>"
        f"<author><name>{author}</name></author>{relation}{journal}</entry>"
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">'
        + entry * entries + "</feed>"
    ).encode("utf-8")


def _get_for(case: dict, *, atom_overrides=None, crossref_overrides=None, calls=None):
    atom_body = _atom(case, **(atom_overrides or {}))
    message = _crossref_message(case, **(crossref_overrides or {}))
    crossref_body = json.dumps({"message": message}).encode("utf-8")

    def get(url, **_kwargs):
        if calls is not None:
            calls.append(url)
        if "export.arxiv.org/api/query" in url:
            return 200, atom_body
        if "api.crossref.org/works/" in url:
            return 200, crossref_body
        raise AssertionError(f"unexpected request: {url}")

    return get


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_explicit_arxiv_id_resolves_only_through_native_published_unit(case, monkeypatch):
    from core.resolve import service as resolve_mod

    ref = _reference(case)
    original_title = ref["title"]
    calls = []
    monkeypatch.setattr(
        resolve_mod, "_get", _get_for(case, calls=calls),
    )

    result = arxiv.discover(ref)

    atom_calls = [url for url in calls if "export.arxiv.org/api/query" in url]
    assert len(atom_calls) == 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(atom_calls[0]).query)
    assert query == {"id_list": [case["arxiv_id"]]}
    assert len([url for url in calls if "api.crossref.org/works/" in url]) == 1
    assert result["status"] == "resolved"
    assert result["via"] == "crossref_metadata"
    assert result["doi"].casefold() == case["doi"].casefold()
    assert result["matched_title"] == case["native_title"]
    assert ref["title"] == original_title
    assert result["metadata_match"]["title_overlap"] == resolve_mod.title_overlap(
        case["native_title"], ref["raw_entry"],
    )
    if case["name"] == "independent-synthetic":
        assert result["metadata_match"]["title_overlap"] == 0.0
    assert result["resolved_identifier"]["type"] == "doi"
    assert result["resolved_identifier"]["value"].casefold() == case["doi"].casefold()


def test_preprint_only_identifier_does_not_produce_published_relation(monkeypatch):
    from core.resolve import service as resolve_mod

    raw = "A. Reader. A preprint-only title. [arXiv:2401.01234]."
    ref = _make_reference(1, raw)
    assert {
        row.get("kind") for row in ref.get("cited_coordinates") or ()
        if isinstance(row, dict)
    } <= {"repository"}
    case = {
        **_CASES[-1],
        "raw": raw,
        "native_title": "A preprint-only title",
    }
    calls = []
    monkeypatch.setattr(
        resolve_mod, "_get", _get_for(case, calls=calls),
    )

    result = arxiv.discover(ref)

    assert not any("api.crossref.org/works/" in url for url in calls)
    assert not isinstance(result.get("resolved_identifier"), dict)
    assert result.get("via") == "arxiv_search"


def test_serialized_relation_proof_rechecks_source_and_native_observations(monkeypatch):
    from core.resolve import service as resolve_mod

    case = _CASES[0]
    ref = _reference(case)
    monkeypatch.setattr(resolve_mod, "_get", _get_for(case))
    result = arxiv.discover(ref)
    attempts = [result]

    assert arxiv._published_relation_proof_is_valid(ref, result, attempts)
    assert not arxiv._published_relation_proof_is_valid(ref, result, [])
    assert resolve_mod._promote_validated_identifier(ref, result, attempts) is result
    assert resolve_mod._resolved_identity_is_locked(ref, {}, result, attempts)

    changed_source = deepcopy(ref)
    changed_source["raw_entry"] += " altered"
    assert not arxiv._published_relation_proof_is_valid(
        changed_source, result, attempts,
    )

    altered_relation = deepcopy(result)
    altered_relation["metadata_match"]["arxiv_published_relation_certificate"][
        "atom"
    ]["entry_id"] = "http://arxiv.org/abs/9999.00001v1"
    assert not arxiv._published_relation_proof_is_valid(
        ref, altered_relation, [altered_relation],
    )

    forged_source = deepcopy(result)
    forged_source["metadata_match"]["arxiv_published_relation_certificate"][
        "source"
    ]["title"] = "a different source title"
    assert not arxiv._published_relation_proof_is_valid(
        ref, forged_source, [forged_source],
    )

    conflicted_profile = deepcopy(result)
    conflicted_profile["metadata_match"]["metadata_conflict"] = True
    assert not arxiv._published_relation_proof_is_valid(
        ref, conflicted_profile, [conflicted_profile],
    )


@pytest.mark.parametrize("malformed_author", [None, "not-an-author", 7, True, []])
def test_serialized_relation_rejects_malformed_native_author(monkeypatch, malformed_author):
    from core.resolve import service as resolve_mod

    case = _CASES[0]
    ref = _reference(case)
    monkeypatch.setattr(resolve_mod, "_get", _get_for(case))
    result = arxiv.discover(ref)
    assert arxiv._published_relation_proof_is_valid(ref, result, [result])

    corrupted = deepcopy(result)
    corrupted["metadata_match"]["arxiv_published_relation_certificate"][
        "crossref"
    ]["authors"] = [malformed_author]
    assert not arxiv._published_relation_proof_is_valid(ref, corrupted, [corrupted])


def test_relation_identity_survives_title_mismatch_without_hiding_title_error(monkeypatch):
    from core.resolve import service as resolve_mod

    case = _CASES[-1]
    ref = _reference(case)
    monkeypatch.setattr(resolve_mod, "_get", _get_for(case))
    monkeypatch.setattr(
        resolve_mod, "_attest_repository_record",
        lambda _ref, result, _attempts: result,
    )
    monkeypatch.setattr(
        resolve_mod, "_enrich_resolved_metadata",
        lambda _ref, result, _attempts, continuation=None: result,
    )
    monkeypatch.setattr(
        resolve_mod, "_annotate_primary_fulltext_links", lambda result: result,
    )
    result = arxiv.discover(ref)

    finalized = resolve_mod._finalize_resolution(ref, result, [result])

    assert finalized["status"] == "resolved"
    assert finalized["resolved_identifier"]["value"] == case["doi"]
    assert finalized["matched_title"] == case["native_title"]
    assert finalized["title_overlap"] == 0.0
    assert finalized["title_flag"] == "mismatch"


@pytest.mark.parametrize(
    "mutation",
    [
        "literal-doi",
        "duplicate-arxiv-id",
        "wrong-source-year",
        "forged-coordinate-span",
        "unclosed-source-tail",
    ],
)
def test_source_relation_unit_rejects_conflicting_or_unclosed_source(mutation):
    case = _CASES[0]
    ref = _reference(case)
    if mutation == "literal-doi":
        ref["raw_entry"] = ref["raw_entry"].replace(
            " [arXiv:", " doi: 10.1103/PhysRevLett.117.151101 [arXiv:",
        )
    elif mutation == "duplicate-arxiv-id":
        ref["raw_entry"] += " [arXiv:1605.04911]"
    elif mutation == "wrong-source-year":
        ref["year"] = 2017
    elif mutation == "forged-coordinate-span":
        repository = next(
            row for row in ref["cited_coordinates"]
            if row.get("kind") == "repository"
        )
        repository["spans"][0]["raw_start"] += 1
    else:
        ref["raw_entry"] += " additional trailing prose"

    assert arxiv._source_published_relation_unit(ref) is None


@pytest.mark.parametrize(
    ("atom_overrides", "crossref_overrides"),
    [
        ({"entry_id": "http://arxiv.org/abs/9999.00001v1"}, None),
        ({"entries": 2}, None),
        ({"doi_values": []}, None),
        ({"doi_values": [_CASES[0]["doi"], "10.9999/ambiguous"]}, None),
        ({"journal_refs": []}, None),
        ({"journal_refs": [_CASES[0]["journal_ref"], "Other Journal 1, 2 (2016)"]}, None),
        ({"journal_refs": ["Other Journal 117, 151101 (2016)"]}, None),
        ({"first_author": "Wrong Person"}, None),
        (None, {"DOI": "10.5555/wrong-doi"}),
        (None, {"type": "book-chapter"}),
        (None, {"author": [{"family": "Wrong", "given": "A"}]}),
        (None, {"issued": {"date-parts": [[2015]]}}),
        (None, {
            "container-title": ["Wrong Journal"],
            "short-container-title": ["Wrong Journal"],
        }),
        (None, {"volume": "999"}),
        (None, {"article-number": "000000"}),
    ],
    ids=[
        "wrong-atom-id", "duplicate-atom-entry", "missing-atom-doi",
        "multiple-atom-dois", "missing-journal-ref", "multiple-journal-refs",
        "wrong-atom-journal-ref", "wrong-atom-author", "wrong-crossref-doi",
        "wrong-crossref-type", "wrong-crossref-author",
        "wrong-crossref-year", "wrong-crossref-container", "wrong-crossref-volume",
        "wrong-crossref-locator",
    ],
)
def test_conflicting_or_ambiguous_native_relation_fails_closed(
    atom_overrides, crossref_overrides, monkeypatch,
):
    from core.resolve import service as resolve_mod

    case = _CASES[0]
    ref = _reference(case)
    calls = []
    monkeypatch.setattr(
        resolve_mod, "_get",
        _get_for(
            case, atom_overrides=atom_overrides,
            crossref_overrides=crossref_overrides, calls=calls,
        ),
    )

    result = arxiv.discover(ref)

    proof_check = getattr(arxiv, "_published_relation_proof_is_valid", None)
    assert callable(proof_check)
    assert not proof_check(ref, result, [result])
    assert not (
        isinstance(result.get("resolved_identifier"), dict)
        and result["resolved_identifier"].get("validated_via")
        == "crossref:arxiv_published_journal_unit_certificate"
    )
