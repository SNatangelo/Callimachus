# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Citation-title boundaries must not become asserted journal names."""
from __future__ import annotations

import pytest

from core.parse.citation_coordinates import extract_cited_coordinates


@pytest.mark.parametrize("title,journal,tail", [
    (
        "Consensus disease definitions for neurologic immune-related adverse events of ICI",
        "J Immunother Cancer", "2021;9:e002890",
    ),
    (
        "Association between duration of immunotherapy and overall survival in advanced NSCLC",
        "JAMA Oncol", "2023;9:1075-82",
    ),
    (
        "Male choice of mates and mating resources in the rose bitterling (Rhodeus ocellatus)",
        "Behav Ecol", "2020;31:101-109",
    ),
    (
        "Introduction to “reproduction: the female perspective from an integrative and comparative framework”",
        "Integr Comp Biol", "2020;60:101-109",
    ),
    (
        "Misrepresentation of Randomized Controlled Trials in Press Releases and News Coverage: A Cohort Study",
        "PLoS Med", "2012;9:e1001308",
    ),
    (
        "Genomic epidemiology and phylodynamics of Acinetobacter baumannii bloodstream isolates in China",
        "Nat. Commun", "2025;16:3536",
    ),
    (
        "Global emergence of Acinetobacter baumannii International Clone 12 predominantly found in the Middle East",
        "Microb Genom", "2025;11:001572",
    ),
])
def test_literal_article_title_precedes_punctuation_heuristics(title, journal, tail):
    raw = f"Author AC, et al. {title}. {journal} {tail}."
    actual = extract_cited_coordinates(raw, title)
    assert actual == extract_cited_coordinates(raw, title)
    container = next(item for item in actual if item["kind"] == "container")
    assert container["raw_value"] == journal
    for field in actual:
        assert "".join(raw[s["raw_start"]:s["raw_end"]] for s in field["spans"]) == field["raw_value"]


@pytest.mark.parametrize("raw,title,journal", [
    (
        "Author AC. A complete article title. Light: Sci. Appl. 2020;9:101-109.",
        "Light: Sci", "Light: Sci. Appl",
    ),
    (
        "Author AC. A complete article title. Clin J Pain. 2023;39:377-85.",
        "Clin J Pain", "Clin J Pain",
    ),
    (
        "Author AC. Another source paper. Science 2020;9:101-109.",
        "A title absent from this citation", "Science",
    ),
])
def test_ambiguous_or_missing_parsed_title_keeps_existing_container(raw, title, journal):
    actual = extract_cited_coordinates(raw, title)
    assert next(item for item in actual if item["kind"] == "container")["raw_value"] == journal


def test_short_quoted_title_is_an_explicit_boundary():
    raw = 'Author AC. "Short title". Nat. Commun. 2020;9:101-109.'
    actual = extract_cited_coordinates(raw, "Short title")
    assert next(item for item in actual if item["kind"] == "container")["raw_value"] == "Nat. Commun"


@pytest.mark.parametrize("raw,title,journal", [
    (
        "Author AC. Improving the quality of reporting. The CONSORT statement. JAMA 1996;276:637-639.",
        "Improving the quality of reporting", "JAMA",
    ),
    (
        "Author AC. An empirical investigation of bias. Dimensions of methodological quality associated with estimates of treatment effects in controlled trials. JAMA 1995;273:408-412.",
        "An empirical investigation of bias", "JAMA",
    ),
    (
        "Author AC. What kind of category is “sex”? Redescriptions 2019;22:4-17.",
        "What kind of category is “sex”", "Redescriptions",
    ),
])
def test_truncated_title_does_not_promote_remaining_title_to_container(raw, title, journal):
    actual = extract_cited_coordinates(raw, title)
    assert next(item for item in actual if item["kind"] == "container")["raw_value"] == journal
    assert next(item for item in actual if item["kind"] == "volume")
    assert next(item for item in actual if item["kind"] == "article_page_range")


@pytest.mark.parametrize("parsed_title", [
    "Can models predict unusual storms",
    "Can models predict unusual storms? A quantitative assessment of Árboles",
])
def test_lowercase_prefix_abbreviations_preserve_terminal_venue(parsed_title):
    raw = (
        "Researcher, A. J. et al. Can models predict unusual storms? "
        "A quantitative assessment of Árboles. qzx Mod. Stud. 8, 94 (2024)."
    )
    actual = extract_cited_coordinates(raw, parsed_title)
    container = next(item for item in actual if item["kind"] == "container")
    assert container["raw_value"] == "qzx Mod. Stud"
    start = raw.index("qzx Mod. Stud")
    assert container["spans"] == [{"raw_start": start, "raw_end": start + 13}]
    for field in actual:
        assert "".join(
            raw[s["raw_start"]:s["raw_end"]] for s in field["spans"]
        ) == field["raw_value"]


@pytest.mark.parametrize("tail", [
    "ordinary prose after title",
    "qzx Modern Studies",
    "qzx M. S.",
    "qzx Mod.",
    "qzx Mod. Stud. additional prose",
    "qzx Mod. Studies",
])
def test_lowercase_prefix_venue_closure_rejects_prose_and_weak_shapes(tail):
    from core.parse.citation_coordinates import _lowercase_prefix_abbreviated_container_tail

    assert not _lowercase_prefix_abbreviated_container_tail(tail)
