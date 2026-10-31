# tests/verify/claim_evidence/contracts/test_prompt_loader.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json

import pytest

from core.verify.claim_evidence.contracts.jury1_flow import (
    JURY1_FLOW_PROMPT_SPEC, JURY1_FLOW_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.contracts.jury2 import (
    JURY2_PROMPT_SPEC, JURY2_SYSTEM_PROMPT,
)
from core.verify.claim_evidence.contracts.prompt_loader import (
    JURY1_FLOW_TASK_IDS, PromptSpecError, load_jury1_flow_prompt_spec,
    load_prompt_spec,
)


def test_v10_english_prompt_packages_are_positive_editable_and_hashed():
    assert JURY1_FLOW_PROMPT_SPEC.version == 15
    assert JURY2_PROMPT_SPEC.version == 12
    assert JURY1_FLOW_PROMPT_SPEC.system_prompt == JURY1_FLOW_SYSTEM_PROMPT
    assert JURY2_PROMPT_SPEC.system_prompt == JURY2_SYSTEM_PROMPT
    assert "json" in JURY2_SYSTEM_PROMPT.lower()
    assert tuple(task_id for task_id, _ in JURY1_FLOW_PROMPT_SPEC.tasks) == JURY1_FLOW_TASK_IDS
    assert all(any(character.isalpha() and character.isascii() for character in paragraph) for paragraph in (JURY1_FLOW_SYSTEM_PROMPT, JURY2_SYSTEM_PROMPT))
    assert len(JURY1_FLOW_PROMPT_SPEC.sha256) == len(JURY2_PROMPT_SPEC.sha256) == 64


def test_jury1_v10_defines_independent_verdict_gates_before_explanation():
    system = JURY1_FLOW_SYSTEM_PROMPT
    support = JURY1_FLOW_PROMPT_SPEC.task_prompt("support_gate")
    full_support = JURY1_FLOW_PROMPT_SPEC.task_prompt("full_support_gate")
    explanation = JURY1_FLOW_PROMPT_SPEC.task_prompt("explanation_evidence")

    assert "keeping the exact claim intact" in system
    assert "sequence of independent binary questions" in system
    assert "Each classification response contains one boolean only" in system
    assert "fixes the outcome" in system
    assert "all or a substantive part of the citation-linked use" in system
    assert "A modest faithful interpretation counts as support" in system
    assert "complete assigned role" in system
    assert "providing no usable support" in system
    assert "source_supports_any" in support
    assert "complete assigned role" in full_support
    assert "determined_outcome is the final code-determined classification" in explanation
    assert "Preserve the classification exactly" in explanation


def test_jury1_v14_gates_apply_marker_local_role_ownership():
    system = JURY1_FLOW_SYSTEM_PROMPT
    support = JURY1_FLOW_PROMPT_SPEC.task_prompt("support_gate")
    full_support = JURY1_FLOW_PROMPT_SPEC.task_prompt("full_support_gate")

    assert "citation marker assigns this source a local scholarly role" in system
    assert "evaluate the source only for that role" in system
    assert "what this manuscript's authors did, chose, observed" in system
    assert "We used METHOD [X] with our chosen settings" in system
    assert "source_spans always identify the one cited source" in system
    assert "multi-reference marker may establish its own listed item" in system
    assert "Our model outperforms COMPARATOR [X]" in system
    assert "local scholarly role assigned to it within the exact claim" in support
    assert "Author-owned choices and results" in support
    assert "multi-reference marker" in support
    assert "one named choice or example supplies usable support" in support
    assert "local scholarly role assigned to this citation marker" in full_support
    assert "other listed items belong to the other sources" in full_support
    assert "manuscript's use, settings, measurements, and comparison result" in full_support


def test_jury1_v15_explanation_requests_up_to_six_evidence_spans():
    explanation = JURY1_FLOW_PROMPT_SPEC.task_prompt("explanation_evidence")

    assert explanation.count("one to six") == 3
    assert "one to four" not in explanation


def test_jury1_topic_gate_defines_related_and_off_topic_by_field_adjacency():
    system = JURY1_FLOW_SYSTEM_PROMPT
    topic = JURY1_FLOW_PROMPT_SPEC.task_prompt("topic_gate")
    explanation = JURY1_FLOW_PROMPT_SPEC.task_prompt("explanation_evidence")

    assert "specific citation-linked claim level" in system
    assert "same research field or a directly adjacent research field" in topic
    assert "concrete shared research object, task, method family, or immediate problem" in topic
    assert "no direct scholarly connection" in topic
    assert "For a related outcome" in explanation
    assert "For an off_topic outcome" in explanation
    assert "name the citation-linked claim field and the source field" in explanation


def test_generic_prompt_loader_joins_editable_english_paragraphs_deterministically(tmp_path):
    path = tmp_path / "prompt.json"
    path.write_text(json.dumps({
        "prompt_id": "jury2_system_prompt", "version": 1,
        "system_prompt": ["First English paragraph.", "Second English paragraph."],
    }), encoding="utf-8")

    first = load_prompt_spec(path, "jury2_system_prompt")
    second = load_prompt_spec(path, "jury2_system_prompt")
    assert first.system_prompt == "First English paragraph.\n\nSecond English paragraph."
    assert first.sha256 == second.sha256


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        '{"prompt_id":"jury2_system_prompt","prompt_id":"jury2_system_prompt","version":1,"system_prompt":["English."]}',
        json.dumps({"prompt_id": "jury2_system_prompt", "version": 1, "system_prompt": ["English."], "extra": True}),
        json.dumps({"prompt_id": "wrong_prompt", "version": 1, "system_prompt": ["English."]}),
        json.dumps({"prompt_id": "jury2_system_prompt", "version": True, "system_prompt": ["English."]}),
        json.dumps({"prompt_id": "jury2_system_prompt", "version": 1, "system_prompt": ["123"]}),
        json.dumps({"prompt_id": "jury2_system_prompt", "version": 1, "system_prompt": []}),
    ],
)
def test_generic_loader_fails_closed_for_malformed_duplicate_or_non_english_specs(tmp_path, contents):
    path = tmp_path / "prompt.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(PromptSpecError):
        load_prompt_spec(path, "jury2_system_prompt")
    with pytest.raises(PromptSpecError):
        load_prompt_spec(tmp_path / "missing.json", "jury2_system_prompt")


@pytest.mark.parametrize("mutation", ["missing", "extra", "reordered", "duplicate", "invalid"])
def test_jury1_flow_loader_fails_closed_for_closed_task_package_mutations(tmp_path, mutation):
    tasks = {task_id: ["English task instructions."] for task_id in JURY1_FLOW_TASK_IDS}
    if mutation == "missing":
        tasks.pop("support_gate")
    elif mutation == "extra":
        tasks["other"] = ["English task instructions."]
    elif mutation == "reordered":
        tasks = {"explanation_evidence": tasks.pop("explanation_evidence"), **tasks}
    elif mutation == "invalid":
        tasks["support_gate"] = ["123"]
    value = {"prompt_id": "jury1_flow_prompt", "version": 1, "system_prompt": ["English system instructions."], "tasks": tasks}
    contents = json.dumps(value)
    if mutation == "duplicate":
        contents = contents.replace('"prompt_id": "jury1_flow_prompt"', '"prompt_id": "jury1_flow_prompt", "prompt_id": "jury1_flow_prompt"')
    path = tmp_path / "flow.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(PromptSpecError):
        load_jury1_flow_prompt_spec(path, "jury1_flow_prompt")
