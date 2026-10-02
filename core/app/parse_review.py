# core/app/parse_review.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Read and answer the existing manual Parse tasks for the desktop GUI."""

from __future__ import annotations

import os
from typing import Any, Mapping

from core.infra.db import RunRepository
from core.infra.integrity.admission import admit_task_answer_locally
from core.infra.integrity.execution_assurance import resolve_existing


_SKIP_REMAINING_REASON = "Operator explicitly skipped the remaining manual Parse reviews."


class ParseReviewController:
    def __init__(self, run_dir: str):
        self.run_dir = os.path.abspath(run_dir)

    def pending_count(self) -> int:
        repo = RunRepository.open_readonly(self.run_dir)
        try:
            return len(repo.list_pending_tasks(slot="parse_review"))
        finally:
            repo.close()

    def load_tasks(self) -> list[dict[str, Any]]:
        """Project task choices only while the review window is open."""
        repo = RunRepository.open_readonly(self.run_dir)
        try:
            tasks = repo.list_task_views(
                slot="parse_review", statuses=("pending", "answered"),
            )
            if not tasks:
                return []
            references = {ref.ref_id: ref for ref in repo.list_references()}
            claims = {claim.claim_id: claim for claim in repo.list_claims()}
            views: list[dict[str, Any]] = []
            for task in tasks:
                view = dict(task)
                kind = view.get("review_kind")
                if kind == "citation_reference_review":
                    claim = claims.get(view.get("claim_id"))
                    view["claim_text"] = claim.sentence if claim else ""
                    view["candidates"] = [
                        {
                            **candidate,
                            "label": (
                                f"[{references[candidate['id']].ref_number}] "
                                f"{references[candidate['id']].raw_entry}"
                            ) if candidate.get("id") in references else str(candidate.get("id") or ""),
                        }
                        for candidate in view.get("candidates") or []
                    ]
                elif kind == "reference_claim_review":
                    view["candidates"] = [
                        {
                            **candidate,
                            "label": claims[candidate["id"]].sentence
                            if candidate.get("id") in claims else str(candidate.get("id") or ""),
                        }
                        for candidate in view.get("candidates") or []
                    ]
                views.append(view)
            return views
        finally:
            repo.close()

    def submit_decision(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Admit one explicit operator choice through the normal integrity gate."""
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Parse review task ID is required")
        if not isinstance(payload, Mapping):
            raise ValueError("Parse review answer must be an object")
        repo = RunRepository.open_readonly(self.run_dir)
        try:
            task = repo.get_task(task_id)
            if task is None or task.task_kind != "manual_parse_review":
                raise ValueError("Parse review task is unavailable")
            if task.status != "pending":
                raise ValueError("Parse review task has already been answered")
        finally:
            repo.close()
        answer = dict(payload)
        answer.setdefault("reason", "")
        resolution = resolve_existing(
            self.run_dir, None, activate_worker=False,
            mirror_audit_records=False, allow_local_downgrade=True,
        )
        gate = resolution.gate
        if gate is not None:
            return gate.admit_task_answer(
                self.run_dir, task_id, answer, mirror_checkpoint=False,
            )
        return admit_task_answer_locally(
            run_dir=self.run_dir, task_id=task_id, raw_payload=answer,
            assurance=resolution.assurance,
        )

    def skip_remaining_reviews(self) -> list[dict[str, Any]]:
        """Record an explicit, hash-bound skip answer for every pending Parse review."""
        repo = RunRepository.open_readonly(self.run_dir)
        try:
            pending = repo.list_pending_tasks(slot="parse_review")
            targets = [
                (task.task_id, task.task_payload.get("target_sha256"))
                for task in pending
            ]
        finally:
            repo.close()

        answers = []
        for task_id, target_sha256 in targets:
            if not isinstance(target_sha256, str) or not target_sha256:
                raise ValueError("pending Parse review has no target hash")
            answers.append(self.submit_decision(task_id, {
                "action": "skip_review",
                "target_sha256": target_sha256,
                "reason": _SKIP_REMAINING_REASON,
            }))
        return answers
