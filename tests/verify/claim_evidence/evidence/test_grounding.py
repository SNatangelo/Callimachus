# tests/verify/claim_evidence/evidence/test_grounding.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from tests._bootstrap import *  # noqa: F401,F403

import pytest

from core.verify.source_spans import build_catalog
from core.verify.claim_evidence.domain.types import Jury1Decision
from core.verify.claim_evidence.evidence.context import build_effective_context
from core.verify.claim_evidence.evidence.grounding import (
    GroundingError,
    POLICY,
    ground_jury1_decision,
    ground_jury1_span_decision,
    normalize_for_matching,
)


SOURCE = "The method does not require labels and enrolled 1,284 participants over 17 months at 23 sites. Another sentence follows."


def _decision(outcome, evidence):
    return Jury1Decision(outcome, tuple(evidence), "reason", "part" if outcome in {"supports", "partial"} else None, "incompatible" if outcome == "contradicts" else None, None)


def test_span_decision_maps_short_alias_to_canonical_provenance_fail_closed():
    source = "The cited source establishes this method. Another result follows."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    catalog = build_catalog(source)
    visible = catalog.visible_spans(context.text)
    aliases = catalog.selection_aliases(visible)
    alias, canonical_id = next(iter(aliases.items()))
    decision = Jury1Decision(
        "supports",
        (catalog.get(canonical_id).text,),
        "The selected passage establishes the assigned method role.",
        "The cited source establishes the assigned method role.",
        None,
        None,
    )

    grounded = ground_jury1_span_decision(
        decision, (alias,), source, context
    )

    assert grounded.quotes[0].span_id == canonical_id
    assert grounded.quotes[0].span_id != alias
    with pytest.raises(GroundingError) as raised:
        ground_jury1_span_decision(decision, ("S999",), source, context)
    assert raised.value.code == "span_selection_invalid"


def test_span_alias_materializes_a_prompt_visible_clipped_rag_span():
    source = "A deliberately long source sentence continues beyond the RAG budget."
    context = build_effective_context(
        source,
        mode="extractive_rag",
        budget=24,
        retrieved_ranges=((0, 24),),
        retrieval_algorithm="fixture-v1",
    )
    catalog = build_catalog(source)
    visible = catalog.visible_spans(context.text)
    aliases = catalog.selection_aliases(visible)
    alias, canonical_id = next(iter(aliases.items()))
    assert canonical_id not in {span.id for span in catalog.spans}
    decision = Jury1Decision(
        "supports",
        (visible[0].text,),
        "The visible passage establishes the assigned role.",
        "The visible passage establishes the assigned role.",
        None,
        None,
    )

    grounded = ground_jury1_span_decision(
        decision, (alias,), source, context
    )

    assert grounded.quotes[0].span_id == canonical_id
    assert grounded.quotes[0].raw_end <= 24


def test_span_decision_rejects_distinct_aliases_with_identical_evidence_text():
    source = "Repeated evidence here. Repeated evidence here."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    catalog = build_catalog(source)
    visible = catalog.visible_spans(context.text)
    aliases = catalog.selection_aliases(visible)
    matching = [span for span in visible if span.text == "Repeated evidence here."]
    matching_aliases = [alias for alias, span_id in aliases.items() if catalog.get(span_id) in matching]
    decision = _decision("supports", [matching[0].text, matching[1].text])

    assert len(set(matching_aliases)) == 2
    with pytest.raises(GroundingError) as raised:
        ground_jury1_span_decision(
            decision, tuple(matching_aliases), source, context
        )
    assert raised.value.code == "duplicate_evidence_text"


@pytest.mark.parametrize(("variant", "plain"), [
    ("\u201cstatistically significant\u201d result", '"statistically significant" result'),
    ("\u201edata show significance\u201f", '"data show significance"'),
    ("patients\u2032 outcomes improved", "patients' outcomes improved"),
    ("mortality\u200b was reduced", "mortality was reduced"),
    ("\ufeffthe intervention group", "the intervention group"),
    ("\u2039treatment arm\u203a showed benefit", "'treatment arm' showed benefit"),
])
def test_report_matching_preserves_academic_quote_and_invisible_normalization(variant, plain):
    assert normalize_for_matching(variant) == normalize_for_matching(plain)


@pytest.mark.parametrize("outcome", ["supports", "partial", "contradicts"])
def test_exact_normalized_and_guarded_fuzzy_return_exact_raw_offsets(outcome):
    context = build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE))
    exact = ground_jury1_decision(_decision(outcome, ["Another sentence follows."]), SOURCE, context).quotes[0]
    normalized = ground_jury1_decision(_decision(outcome, ["The method does not require labels and enrolled 1,284 participants over 17 months at 23 sites."]), SOURCE, context).quotes[0]
    fuzzy = ground_jury1_decision(_decision(outcome, ["The method does not require labels and enrolled 1284 participants over 17 months at 23 sites."]), SOURCE, context).quotes[0]
    assert exact.text == SOURCE[exact.raw_start:exact.raw_end]
    assert normalized.match_mode == "exact_raw"
    assert fuzzy.match_mode == "fuzzy"
    assert fuzzy.text == SOURCE[fuzzy.raw_start:fuzzy.raw_end]
    assert fuzzy.source_hash == context.source_hash


def test_approved_whitespace_and_dehyphenation_normalization_materializes_raw():
    source = "We study informa- tion retrieval systems across many practical domains."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    quote = "We study information retrieval systems across many practical domains."
    grounded = ground_jury1_decision(_decision("supports", [quote]), source, context).quotes[0]
    assert grounded.match_mode == "normalized"
    assert grounded.text == source


def test_normalization_can_ground_one_raw_range_across_sentences_and_policy_is_snapshotted():
    source = "First sentence.  Second informa- tion sentence."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    result = ground_jury1_decision(_decision("supports", ["First sentence. Second information sentence."]), source, context)
    assert result.quotes[0].match_mode == "normalized"
    assert result.policy.snapshot() == {
        "version": "grounding-v2",
        "normalization": "nfc-whitespace-dehyphenation-superscript-v2",
        "fuzzy_min_chars": 40,
        "fuzzy_min_tokens": 8,
        "fuzzy_min_anchors": 3,
        "fuzzy_ratio": 0.92,
        "fuzzy_margin": 0.02,
    }
    assert result.policy == POLICY


def test_extractor_superscript_marker_matches_bracketed_citation_with_raw_offsets():
    source = (
        "Sentences were encoded using byte-pair encoding described by Sennrich, "
        "Haddow and Birch, following Moses⟦SUP:2⟧, with a shared source-target "
        "vocabulary of approximately 37,000 tokens."
    )
    quote = source.replace("⟦SUP:2⟧", "[2]")
    context = build_effective_context(
        source, mode="full_text", budget=len(source)
    )

    grounded = ground_jury1_decision(
        _decision("supports", [quote]), source, context
    ).quotes[0]

    assert grounded.match_mode == "normalized"
    assert grounded.text == source
    assert (grounded.raw_start, grounded.raw_end) == (0, len(source))
    with pytest.raises(GroundingError):
        ground_jury1_decision(
            _decision("supports", [quote.replace("[2]", "[3]")]),
            source,
            context,
        )


def test_literal_source_ellipsis_is_an_exact_quote_and_invalid_decision_fails_before_locator():
    source = "A literal ... source quotation."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    assert ground_jury1_decision(_decision("supports", [source]), source, context).quotes[0].match_mode == "exact_raw"
    invalid = Jury1Decision("partial", (source,), "reason", None, None, None)
    with pytest.raises(GroundingError, match="supported_part"):
        ground_jury1_decision(invalid, source, context, locator=lambda *_: pytest.fail("locator called"))
    invalid = Jury1Decision("supports", (source,), "reason", 7, None, None)
    with pytest.raises(GroundingError, match="supported_part"):
        ground_jury1_decision(invalid, source, context, locator=lambda *_: pytest.fail("locator called"))


@pytest.mark.parametrize("quote", [
    "The method does require labels and enrolled 1284 participants over 17 months at 23 sites.",
    "The method does not require labels and enrolled 1284 participants over 17 months at 99 sites.",
    "method does require labels",
    "The method does not require labels ... at 23 sites.",
])
def test_changed_negation_numbers_short_and_ellipsis_fail_closed(quote):
    context = build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE))
    with pytest.raises(GroundingError):
        ground_jury1_decision(_decision("supports", [quote]), SOURCE, context)


def test_ambiguous_and_repeated_grounding_fail_closed():
    source = "Repeated evidence here. Repeated evidence here."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    with pytest.raises(GroundingError, match="ambiguous"):
        ground_jury1_decision(_decision("supports", ["Repeated evidence here."]), source, context)
    source = "Alpha  beta evidence with sufficient length for this check."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    with pytest.raises(GroundingError, match="same raw"):
        ground_jury1_decision(_decision("supports", [source, "Alpha beta evidence with sufficient length for this check."]), source, context)


def test_normalized_and_fuzzy_ambiguity_fail_closed():
    source = "Alpha  beta evidence is here. Alpha\tbeta evidence is here."
    context = build_effective_context(source, mode="full_text", budget=len(source))
    with pytest.raises(GroundingError, match="ambiguous normalized"):
        ground_jury1_decision(
            _decision("supports", ["Alpha beta evidence is here."]),
            source,
            context,
        )
    first = "The careful method evaluates alpha evidence across many stable datasets with 12 recorded trials."
    second = "The careful method evaluates delta evidence across many stable datasets with 12 recorded trials."
    quote = "The careful method evaluates gamma evidence across many stable datasets with 12 recorded trials."
    source = first + " " + second
    context = build_effective_context(source, mode="full_text", budget=len(source))
    with pytest.raises(GroundingError, match="ambiguous guarded fuzzy"):
        ground_jury1_decision(_decision("supports", [quote]), source, context)


def test_fuzzy_anchor_offsets_locate_a_later_passage_and_preserve_apostrophe_negation():
    passage = "The system can't safely omit labels and evaluated 1,284 records over 17 months at 23 sites."
    source = "Unrelated preamble with enough words to move the target far away. " + passage
    context = build_effective_context(source, mode="full_text", budget=len(source))
    quote = "The system can't safely omit labels and evaluated 1284 records over 17 months at 23 sites."
    grounded = ground_jury1_decision(_decision("supports", [quote]), source, context).quotes[0]
    assert grounded.match_mode == "fuzzy"
    assert "can't" in grounded.text
    changed = "The system can safely omit labels and evaluated 1284 records over 17 months at 23 sites."
    with pytest.raises(GroundingError):
        ground_jury1_decision(_decision("supports", [changed]), source, context)


def test_injected_locator_cannot_weaken_the_frozen_policy():
    context = build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE))
    with pytest.raises(GroundingError, match="frozen policy"):
        ground_jury1_decision(
            _decision("supports", [SOURCE]),
            SOURCE,
            context,
            locator=lambda *_: (0, len(SOURCE), "fuzzy", 0.5),
        )


@pytest.mark.parametrize("outcome", ["related", "off_topic", "non_decidable"])
def test_non_evidence_outcomes_bypass_locator(outcome):
    calls = []
    decision = Jury1Decision(outcome, (), "reason", None, None, "attribution" if outcome == "non_decidable" else None)
    context = build_effective_context(SOURCE, mode="full_text", budget=len(SOURCE))
    result = ground_jury1_decision(decision, SOURCE, context, locator=lambda quote, source: calls.append((quote, source)))
    assert result.quotes == ()
    assert calls == []


def test_rag_cannot_ground_hidden_source_evidence():
    context = build_effective_context(SOURCE, mode="extractive_rag", budget=20, retrieved_ranges=((0, 20),), retrieval_algorithm="fixture-v1")
    with pytest.raises(GroundingError, match="outside"):
        ground_jury1_decision(_decision("supports", ["Another sentence follows."]), SOURCE, context)
