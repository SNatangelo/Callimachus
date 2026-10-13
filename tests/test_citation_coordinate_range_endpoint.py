import pytest
from core.parse.parsing_common import _make_reference
from core.parse.citation_coordinates import extract_cited_coordinates

@pytest.mark.parametrize("dash",["-","‐","‑","‒","–","—","−"])
def test_range_endpoint_inside_title_is_not_reporter_volume(dash):
    title=f"Tables of transport measurements in the pressure range 0.1{dash}35 MPa"
    raw=f"Rivera, J., Chen, K. & Tran, R. J. {title}. J. Exact. Stud. 10, 71–88 (2020)."
    ref=_make_reference(1,raw)
    assert ref["title"]==title
    coords={c["kind"]:c for c in ref["cited_coordinates"]}
    assert coords["volume"]["raw_value"]=="10"
    assert coords["container"]["raw_value"]=="J. Exact. Stud"
    assert coords["article_page_range"]["raw_value"]=="71–88"


def test_actual_legal_reporter_tuple_still_has_volume_and_container():
    raw="Rivera, J. A legal investigation. 18 Law Journal 101 (2020)."
    coords={c["kind"]:c for c in extract_cited_coordinates(raw)}
    assert coords["volume"]["raw_value"]=="18"
    assert coords["container"]["raw_value"]=="Law Journal"


def test_range_endpoint_without_publication_tuple_emits_no_container():
    raw="Rivera, J. Pressure range 0.1–35 MPa. Theory Journal 10 (2020)."
    coords={c["kind"]:c for c in extract_cited_coordinates(raw)}
    assert "volume" not in coords
    assert "container" not in coords


@pytest.mark.parametrize("locator", ["M517-M525", "Q83-Q91"])
def test_distinct_uniform_prefix_range_is_a_role_neutral_article_locator(locator):
    raw = f"Smith, J. (2020). A cited study. Journal of Tests, 5(1), {locator}."

    reference = _make_reference(1, raw)

    coordinate = next(
        row for row in reference["cited_coordinates"]
        if row["kind"] in {"article_locator", "elocator"}
    )
    assert coordinate["kind"] == "article_locator"
    assert coordinate["raw_value"] == locator
    span = coordinate["spans"][0]
    assert raw[span["raw_start"]:span["raw_end"]] == locator


@pytest.mark.parametrize("locator", [
    "M517-M517", "M517-Q525", "ab12-cd34",
])
def test_repeated_mixed_prefix_and_opaque_ranges_remain_electronic_locators(locator):
    raw = f"Smith, J. (2020). A cited study. Journal of Tests, 5(1), {locator}."

    coordinates = extract_cited_coordinates(raw)

    coordinate = next(
        row for row in coordinates
        if row["kind"] in {"article_locator", "elocator"}
    )
    assert coordinate["kind"] == "elocator"
    assert coordinate["raw_value"] == locator
