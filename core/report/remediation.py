# core/report/remediation.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Closed remediation policy for report findings and verification terminals.

The catalog describes which *kind* of correction may be offered by a later
task or report surface.  It does not establish that a task is available for a
particular run, and it never changes the original automated finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

from core.invocation import project_venv_executable

from core.report.rollup import (
    _DETERMINISTIC_GUARD_TERMINAL_CAUSES,
    _INFRASTRUCTURE_TERMINAL_CAUSES,
    _MECHANICAL_TERMINAL_CAUSES,
    _SEMANTIC_TERMINAL_CAUSES,
)
from core.report.reason_codes import ASSESSMENT_REASON_CODES
from core.report.verification_projection import _OUTCOMES, _RESOLUTIONS


_MECHANISMS: Final = frozenset({
    "task", "child_run", "input_change", "content_change", "retry", "none",
})
_COMPLETED_REPORT_ROUTES: Final = frozenset({"child_run", "none"})
_RUN_PREFIX = f"{project_venv_executable()} run.py"
_TASK_PREFIX = f"{_RUN_PREFIX} tasks"
COMPLETED_CHILD_RUN_COMMAND: Final = (
    f"{_RUN_PREFIX} --remediate-completed {{PARENT_RUN}} --run {{CHILD_RUN}}"
)


@dataclass(frozen=True)
class TechnicalTaskRoute:
    """A closed, documented route to an already-supported human task.

    Commands are templates only: callers must substitute values from the task
    they are answering and must not infer an unsupported task from a report
    finding.
    """

    route_id: str
    slot: str
    task_kind: str
    review_kinds: tuple[str, ...]
    answer_modes: tuple[str, ...]
    commands: tuple[str, ...]
    child_start_command: str = COMPLETED_CHILD_RUN_COMMAND

    def __post_init__(self) -> None:
        if not self.route_id:
            raise ValueError("technical task route_id must be nonempty")
        if not self.slot or not self.task_kind:
            raise ValueError("technical task route metadata is invalid")
        if not self.answer_modes:
            raise ValueError("technical task route answer modes are invalid")
        if not self.commands or any(not command.startswith(f"{_TASK_PREFIX} ")
                                    for command in self.commands):
            raise ValueError("technical task route commands are invalid")
        if not self.child_start_command.startswith(f"{_RUN_PREFIX} --remediate-completed "):
            raise ValueError("technical task route child start command is invalid")


TECHNICAL_TASK_ROUTES: Final[Mapping[str, TechnicalTaskRoute]] = MappingProxyType({
    "manual_fetch": TechnicalTaskRoute("manual_fetch", "fetch", "fetch", (), (
        "file_path", "file_path_with_url", "text_file", "text_file_with_url",
        "not_found",
    ), (
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --file-path {{FILE_PATH}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --file-path {{FILE_PATH}} --url {{URL}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --text-file {{TEXT_FILE}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --text-file {{TEXT_FILE}} --url {{URL}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --not-found",
    )),
    "precomputed_ocr": TechnicalTaskRoute("precomputed_ocr", "fetch", "fetch", (), (
        "ocr_text_file",
    ), (
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --ocr-text-file {{OCR_TEXT_FILE}}",
    )),
    "browser_challenge": TechnicalTaskRoute("browser_challenge", "fetch", "browser_challenge", (), (
        "item_file", "item_text_file", "item_not_found",
    ), (
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --item-file {{REF_ID}}={{FILE_PATH}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --item-text-file {{REF_ID}}={{TEXT_FILE}}",
        f"{_TASK_PREFIX} answer-fetch --run {{RUN}} --task {{TASK}} --item-not-found {{REF_ID}}",
    )),
    "web_research": TechnicalTaskRoute("web_research", "research", "web_research", (), (
        "finding", "not_found",
    ), (
        f"{_TASK_PREFIX} answer-research --run {{RUN}} --task {{TASK}} --finding '{{URL}}|{{STANCE}}|{{QUOTE_OR_QUOTE_FILE}}'",
        f"{_TASK_PREFIX} answer-research --run {{RUN}} --task {{TASK}} --not-found",
    )),
    "footnote_source_split": TechnicalTaskRoute("footnote_source_split", "parse_review", "manual_parse_review", ("footnote_source_review",), ("no_sources", "split_sources", "keep_ambiguous"), (
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action no-sources --reason {{REASON}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action split-sources --reason {{REASON}} --source-text-file {{SOURCE_TEXT_FILE_1}} --source-text-file {{SOURCE_TEXT_FILE_2}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action keep-ambiguous --reason {{REASON}}",
    ), f"{_RUN_PREFIX} --remediate-completed {{PARENT_RUN}} --run {{CHILD_RUN}} --manual-review"),
    "reference_identity_correction": TechnicalTaskRoute("reference_identity_correction", "parse_review", "manual_parse_review", ("reference_identity_review",), ("correct_identity", "keep_ambiguous"), (
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action correct-identity --reason {{REASON}} --title {{TITLE}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action correct-identity --reason {{REASON}} --doi {{DOI}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action keep-ambiguous --reason {{REASON}}",
    ), f"{_RUN_PREFIX} --remediate-completed {{PARENT_RUN}} --run {{CHILD_RUN}} --manual-review-ref-number {{REF_NUMBER}}"),
    "citation_attribution": TechnicalTaskRoute("citation_attribution", "parse_review", "manual_parse_review", ("citation_reference_review", "reference_claim_review"), ("select_reference", "select_claim", "keep_unresolved"), (
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action select-reference --reason {{REASON}} --ref {{REF_ID}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action select-claim --reason {{REASON}} --claim {{CLAIM_ID}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action keep-unresolved --reason {{REASON}}",
    ), f"{_RUN_PREFIX} --remediate-completed {{PARENT_RUN}} --run {{CHILD_RUN}} --manual-review"),
    "source_identity_attestation": TechnicalTaskRoute("source_identity_attestation", "verify", "source_identity_attestation", (), ("attest_identity", "keep_unverified"), (
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action attest-identity --reason {{REASON}}",
        f"{_TASK_PREFIX} answer-review --run {{RUN}} --task {{TASK}} --target-sha256 {{TARGET_SHA256}} --action keep-unverified --reason {{REASON}}",
    )),
})


@dataclass(frozen=True)
class Remediation:
    """A deterministic remediation policy, independent of run state."""

    action_id: str
    mechanism: str
    user_override_allowed: bool
    preserve_automatic_finding: bool = True
    active_task_routes: tuple[str, ...] = ()
    child_run_task_routes: tuple[str, ...] = ()
    completed_report_route: str = "none"

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("remediation action_id must be nonempty")
        if self.mechanism not in _MECHANISMS:
            raise ValueError("remediation mechanism is invalid")
        if not self.preserve_automatic_finding:
            raise ValueError("remediation must preserve the automatic finding")
        if self.completed_report_route not in _COMPLETED_REPORT_ROUTES:
            raise ValueError("completed report route is invalid")
        if any(route not in TECHNICAL_TASK_ROUTES
               for route in self.active_task_routes):
            raise ValueError("remediation active task route is invalid")
        if any(route not in TECHNICAL_TASK_ROUTES
               for route in self.child_run_task_routes):
            raise ValueError("remediation child-run task route is invalid")


def _policy(
    action_id: str,
    mechanism: str,
    *,
    override: bool = False,
    active_task_routes: tuple[str, ...] = (),
    child_run_task_routes: tuple[str, ...] = (),
    completed_report_route: str = "none",
) -> Remediation:
    return Remediation(
        action_id,
        mechanism,
        override,
        active_task_routes=active_task_routes,
        child_run_task_routes=child_run_task_routes,
        completed_report_route=completed_report_route,
    )


UNSUPPORTED_REMEDIATION: Final = _policy("unsupported", "none")


ASSESSMENT_REMEDIATIONS: Final[Mapping[str, Remediation]] = MappingProxyType({
    "hard.claim.orphan_citation": _policy(
        "manual_citation_attribution", "child_run", override=True,
        child_run_task_routes=("citation_attribution",),
        completed_report_route="child_run",
    ),
    "technical.claim.ambiguous_citation": _policy(
        "manual_citation_disambiguation", "child_run", override=True,
        child_run_task_routes=("citation_attribution",),
        completed_report_route="child_run",
    ),
    "hard.claim.uncontested_contradiction": _policy(
        "revise_claim_or_source", "content_change"
    ),
    "hard.claim.all_off_topic": _policy("revise_claim_or_source", "content_change"),
    "review.claim.off_topic": _policy("revise_claim_or_source", "content_change"),
    "review.claim.related_only": _policy("revise_claim_or_source", "content_change"),
    "review.claim.non_decidable": _policy("revise_claim_or_source", "content_change"),
    "review.manuscript.low_reference_coverage": _policy(
        "inspect_citation_coverage", "none"
    ),
    "review.claim.partial_support": _policy("revise_claim_or_source", "content_change"),
    "minor.claim.partial_support": _policy("revise_claim_or_source", "content_change"),
    "technical.claim.no_semantic_outcome": _policy(
        "inspect_terminal_cause", "none"
    ),
    "technical.claim.incomplete_pair_coverage": _policy(
        "complete_pending_work", "child_run", completed_report_route="child_run"
    ),
    "technical.claim.contested_assurance": _policy(
        "revise_claim_or_source", "content_change"
    ),
    "hard.source.not_found": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("manual_fetch", "reference_identity_correction"),
        completed_report_route="child_run",
    ),
    "hard.source.identifier_mismatch": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "hard.source.suspected_fabricated": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "hard.source.suspected_fabricated_tag": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "hard.source.reference_refuted": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "hard.source.high_fabrication_suspicion": _policy(
        "correct_identity_or_source", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "hard.source.retracted": _policy("replace_retracted_source", "content_change"),
    "review.source.title_flag_warn": _policy(
        "correct_identity", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
    "review.source.author_list_discrepancy": _policy(
        "inspect_identity_context", "none"
    ),
    "review.source.searched_not_found": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("manual_fetch", "reference_identity_correction"),
        completed_report_route="child_run",
    ),
    "review.source.elevated_bibliographic_suspicion": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("reference_identity_correction",),
        completed_report_route="child_run",
    ),
})


TERMINAL_RESOLUTION_REMEDIATIONS: Final[Mapping[str, Remediation]] = MappingProxyType({
    "jury2_accepted": _policy("inspect_semantic_outcome", "none"),
    "jury2_off": _policy("inspect_semantic_outcome", "none"),
    "jury2_not_eligible": _policy("revise_claim_or_source", "content_change"),
    "jury2_rejected_nonbinding": _policy("revise_claim_or_source", "content_change"),
    "majority_fallback": _policy("revise_claim_or_source", "content_change"),
    "jury1_guard": _policy("retry_verification", "retry"),
    "jury1_technical": _policy("retry_verification", "retry"),
    "jury2_technical": _policy("retry_verification", "retry"),
    "jury2_rejected": _policy("revise_claim_or_source", "content_change"),
    "no_consensus": _policy("revise_claim_or_source", "content_change"),
    "jury1_provider_uncertain": _policy(
        "revise_claim_or_source", "content_change"
    ),
    "jury2_provider_uncertain": _policy(
        "revise_claim_or_source", "content_change"
    ),
    "cancelled": _policy("retry_verification", "retry"),
    "bibliographic_identity_not_corroborated": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("source_identity_attestation",),
        completed_report_route="child_run",
    ),
    "structural_claim_contamination": _policy(
        "correct_manuscript_input", "input_change"
    ),
    "source_integrity_unavailable": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("manual_fetch",),
        completed_report_route="child_run",
    ),
    "source_identity_target_unavailable": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("manual_fetch", "reference_identity_correction"),
        completed_report_route="child_run",
    ),
    "source_identity_check_unavailable": _policy(
        "retry_verification", "retry"
    ),
})


TERMINAL_CAUSE_REMEDIATIONS: Final[Mapping[str, Remediation]] = MappingProxyType({
    **{cause: _policy("retry_verification", "retry")
       for cause in _MECHANICAL_TERMINAL_CAUSES},
    **{cause: _policy("revise_claim_or_source", "content_change")
       for cause in _SEMANTIC_TERMINAL_CAUSES},
    **{cause: _policy("retry_verification", "retry")
       for cause in _INFRASTRUCTURE_TERMINAL_CAUSES},
    "bibliographic_identity_not_corroborated": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("source_identity_attestation",),
        completed_report_route="child_run",
    ),
    "structural_claim_contamination": _policy(
        "correct_manuscript_input", "input_change"
    ),
    "source_integrity_unavailable": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("manual_fetch",),
        completed_report_route="child_run",
    ),
    "source_identity_target_unavailable": _policy(
        "inspect_identity_context", "child_run",
        child_run_task_routes=("manual_fetch", "reference_identity_correction"),
        completed_report_route="child_run",
    ),
    "source_identity_check_unavailable": _policy(
        "retry_verification", "retry"
    ),
})


if set(ASSESSMENT_REMEDIATIONS) != set(ASSESSMENT_REASON_CODES):
    raise RuntimeError("assessment remediation catalog is incomplete")
if set(TERMINAL_RESOLUTION_REMEDIATIONS) != set(_RESOLUTIONS):
    raise RuntimeError("terminal resolution remediation catalog is incomplete")
if set(TERMINAL_CAUSE_REMEDIATIONS) != (
    set(_MECHANICAL_TERMINAL_CAUSES)
    | set(_SEMANTIC_TERMINAL_CAUSES)
    | set(_INFRASTRUCTURE_TERMINAL_CAUSES)
    | set(_DETERMINISTIC_GUARD_TERMINAL_CAUSES)
):
    raise RuntimeError("terminal cause remediation catalog is incomplete")


def remediation_for_assessment_reason(reason_code: object) -> Remediation:
    """Return a fail-closed policy for one HTML assessment reason code."""
    if not isinstance(reason_code, str):
        return UNSUPPORTED_REMEDIATION
    return ASSESSMENT_REMEDIATIONS.get(reason_code, UNSUPPORTED_REMEDIATION)


def remediation_for_manual_parse_signal(signal: object) -> Remediation:
    """Map only an explicit Parse review signal to its existing task route."""
    if signal != "footnote_source_review":
        return UNSUPPORTED_REMEDIATION
    return _policy(
        "split_parsed_footnote_sources", "child_run", override=True,
        child_run_task_routes=("footnote_source_split",),
        completed_report_route="child_run",
    )


def technical_task_route(route_id: object) -> TechnicalTaskRoute | None:
    """Return an existing task route, or ``None`` for unknown route ids."""
    if not isinstance(route_id, str):
        return None
    return TECHNICAL_TASK_ROUTES.get(route_id)


def completed_report_command(remediation: Remediation) -> str | None:
    """Return the single supported completed-report entry command, if any."""
    if not isinstance(remediation, Remediation):
        raise ValueError("completed report remediation policy is invalid")
    return (
        COMPLETED_CHILD_RUN_COMMAND
        if remediation.completed_report_route == "child_run"
        else None
    )


def remediation_for_terminal_resolution(
    resolution: object, semantic_outcome: object = None
) -> Remediation:
    """Return a fail-closed policy for one projected terminal resolution.

    Jury-success resolutions require their semantic outcome: support needs no
    correction, while every other valid outcome remains a content finding.
    """
    if not isinstance(resolution, str):
        return UNSUPPORTED_REMEDIATION
    if resolution == "jury2_accepted":
        if semantic_outcome not in {"supports", "partial", "contradicts"}:
            return UNSUPPORTED_REMEDIATION
        if semantic_outcome == "supports":
            return _policy("no_action", "none")
        return _policy("revise_claim_or_source", "content_change")
    if resolution == "jury2_off":
        if semantic_outcome not in _OUTCOMES:
            return UNSUPPORTED_REMEDIATION
        if semantic_outcome == "supports":
            return _policy("no_action", "none")
        return _policy("revise_claim_or_source", "content_change")
    return TERMINAL_RESOLUTION_REMEDIATIONS.get(resolution, UNSUPPORTED_REMEDIATION)


def remediation_for_terminal_cause(cause: object) -> Remediation:
    """Return a fail-closed policy for one classified terminal cause."""
    if not isinstance(cause, str):
        return UNSUPPORTED_REMEDIATION
    return TERMINAL_CAUSE_REMEDIATIONS.get(cause, UNSUPPORTED_REMEDIATION)
