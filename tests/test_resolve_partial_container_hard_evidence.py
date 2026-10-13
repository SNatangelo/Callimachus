"""Partial source-container spans must not create hard metadata conflicts."""
from __future__ import annotations

from core.parse.citation_coordinates import extract_cited_coordinates
from core.resolve import matching


def _reference(raw: str, title: str | None, *, year: int = 2022) -> dict:
    return {
        "id": "synthetic-partial-container",
        "raw_entry": raw,
        "title": title,
        "year": year,
        "doi": None,
        "source_type": "article",
        "source_kind": "article_like",
        "cited_coordinates": extract_cited_coordinates(raw, title),
    }


def _profile(
    ref: dict,
    *,
    volume_status: str = "match",
    locator_status: str = "match",
    container_matched: str = "Unrelated Journal",
) -> dict:
    coordinates = ref["cited_coordinates"]
    container = next(row for row in coordinates if row["kind"] == "container")
    volume = next(row for row in coordinates if row["kind"] == "volume")
    locator = next(
        row for row in coordinates
        if row["kind"] in {
            "article_page_range", "elocator", "article_number", "article_locator",
        }
    )

    def comparison(row: dict, status: str, mismatched_value: str) -> dict:
        return {
            "kind": row["kind"],
            "cited_value": row["normalized_value"],
            "matched_value": (
                row["normalized_value"] if status == "match" else mismatched_value
            ),
            "status": status,
        }

    return {
        "author_match": True,
        "year_match": True,
        "coordinate_comparisons": [
            comparison(container, "mismatch", container_matched),
            comparison(volume, volume_status, "99"),
            comparison(locator, locator_status, "1-9"),
        ],
    }


def test_abbreviated_venue_prefix_does_not_make_partial_container_volume_conflict():
    title = "Synthetic glacier chronology study"
    raw = (
        "Rao, P. & Lee, K. Synthetic glacier chronology study. "
        "Valley Planet. Sci. Lett. 18, 300–309 (2022)."
    )
    ref = _reference(raw, title)
    profile = _profile(
        ref,
        volume_status="mismatch",
        container_matched="Valley Planetary Science Letters",
    )

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == "Sci. Lett"
    assert profile["coordinate_comparisons"][0]["status"] == "mismatch"
    assert profile["coordinate_comparisons"][1]["status"] == "mismatch"
    assert not matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)


def test_abbreviated_venue_prefix_does_not_make_partial_container_locator_conflict():
    title = "Synthetic air quality study"
    raw = (
        "Chen, K. & Wu, M. Synthetic air quality study. "
        "Delta Planet. Health 4, e210–e212 (2022)."
    )
    ref = _reference(raw, title)
    profile = _profile(
        ref,
        locator_status="mismatch",
        container_matched="Delta Planetary Health",
    )

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == "Health"
    assert profile["coordinate_comparisons"][0]["status"] == "mismatch"
    assert profile["coordinate_comparisons"][2]["status"] == "mismatch"
    assert not matching._metadata_candidate_has_container_and_locator_conflict(ref, profile)


def test_subtitle_gap_does_not_hide_a_complete_venue_contradiction():
    title = "A synthetic article title"
    raw = (
        "Rao, P. & Lee, K. A synthetic article title. "
        "Additional Discussion. J. Test. 18, 300–309 (2022)."
    )
    ref = _reference(raw, title)
    profile = _profile(
        ref,
        volume_status="mismatch",
        container_matched="Journal of Synthetic Studies",
    )

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == "J. Test"
    assert matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)


def test_complete_source_owned_container_still_supports_container_volume_conflict():
    title = "A synthetic article title"
    raw = (
        "Rao, P. & Lee, K. A synthetic article title. "
        "Journal of Synthetic Studies 18, 300–309 (2022)."
    )
    ref = _reference(raw, title)
    profile = _profile(ref, volume_status="mismatch")

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == "Journal of Synthetic Studies"
    assert matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)


def test_complete_atrasentan_container_still_supports_container_volume_conflict():
    title = (
        "Atrasentan for the treatment of IgA nephropathy: interim results of the "
        "AFFINITY study"
    )
    raw = (
        "Kim S-G, Vo N, Lee S-H, et al. Atrasentan for the treatment of IgA "
        "nephropathy: interim results of the AFFINITY study. Kidney Int Rep "
        "2023;8:1902-1902 "
        "(https://www.kireports.org/article/S2468-0249(23)01207-X/fulltext)."
    )
    ref = _reference(raw, title, year=2023)
    profile = _profile(ref, volume_status="mismatch")

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == "Kidney Int Rep"
    assert matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)


def test_complete_source_owned_container_still_supports_container_locator_conflict():
    title = "A synthetic article title"
    raw = (
        "Rao, P. & Lee, K. A synthetic article title. "
        "Journal of Synthetic Studies 18, 300–309 (2022)."
    )
    ref = _reference(raw, title)
    profile = _profile(ref, locator_status="mismatch")

    assert matching._metadata_candidate_has_container_and_locator_conflict(ref, profile)


def test_year_before_container_is_not_treated_as_an_unexplained_venue_prefix():
    title = "A synthetic article title"
    raw = (
        "Rao, P. & Lee, K. A synthetic article title. (2022). "
        "Journal of Synthetic Studies 18, 300–309."
    )
    ref = _reference(raw, title)
    profile = _profile(ref, volume_status="mismatch")

    container = next(row for row in ref["cited_coordinates"] if row["kind"] == "container")
    assert container["raw_value"] == ". Journal of Synthetic Studies"
    assert matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)


def test_no_unique_literal_title_preserves_existing_container_conflict_behavior():
    title = "A synthetic article title"
    raw = (
        "Rao, P. & Lee, K. A synthetic article title. "
        "Journal of Synthetic Studies 18, 300–309 (2022)."
    )
    ref = _reference(raw, title)
    ref["title"] = "A parsed title absent from the source"
    profile = _profile(ref, volume_status="mismatch")

    assert matching._metadata_candidate_has_container_and_volume_conflict(ref, profile)
