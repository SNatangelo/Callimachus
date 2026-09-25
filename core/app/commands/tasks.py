#!/usr/bin/env python3
# core/app/commands/tasks.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Plain-text task inspector and answer submitter for DB-native runs."""

from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace
from typing import Any

try:
    from core.infra.db import RunRepository
except ImportError:  # direct execution
    from db import RunRepository
from core.fetch.admission import provided_fulltext
from core.infra.integrity import AuthorityError, IntegrityGateError, RunIntegrityGate
from core.infra.integrity.admission import admit_task_answer_locally
from core.infra.integrity.execution_assurance import resolve_existing
from core.invocation import run_command


def _repo(run_dir: str) -> RunRepository:
    """Open task views without granting the submitting peer write access."""
    return RunRepository.open_readonly(run_dir)


def _answer_gate(args):
    try:
        return resolve_existing(
            args.run,
            args.agent_identity,
            debug_override=args.debug_override_artifact_integrity,
            override_reason=args.debug_override_reason,
            activate_worker=False,
            mirror_audit_records=False,
            allow_local_downgrade=True,
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate stopped task answer: {exc}") from exc


def _read_text_maybe(path_or_text: str) -> str:
    if os.path.exists(path_or_text):
        with open(path_or_text, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path_or_text


def _require_task(repo: RunRepository, task_id: str):
    task = repo.get_task(task_id)
    if task is None:
        raise SystemExit(f"task not found: {task_id}")
    if task.status == "applied":
        raise SystemExit(f"task already applied: {task_id}")
    if task.status == "cancelled":
        raise SystemExit(f"task cancelled: {task_id}")
    return task


def _contains_guided_fetch_payload(payload: Any) -> bool:
    """Whether a closed Fetch answer claims user-guided provenance."""
    if isinstance(payload, dict):
        return bool(payload.get("guided_fetch")) or any(
            _contains_guided_fetch_payload(value) for value in payload.values()
        )
    if isinstance(payload, list):
        return any(_contains_guided_fetch_payload(value) for value in payload)
    return False


def admit_fetch_payload(
    run_dir: str,
    task_id: str,
    payload: dict[str, Any],
    *,
    agent_identity: str | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict:
    """Admit a closed Fetch answer through the existing integrity boundary.

    Guided Fetch is a human-operated recovery surface. An agent-identified
    process may open its desktop UI, but it cannot submit or attest its
    answers: a separate authenticated operator channel is required.
    """
    if not isinstance(payload, dict):
        raise SystemExit("fetch answer payload must be an object")
    guided_payload = _contains_guided_fetch_payload(payload)
    if guided_payload and agent_identity is not None:
        raise SystemExit(
            "guided Fetch answers require a separately authenticated human "
            "operator; an agent-identified caller cannot submit them"
        )
    args = SimpleNamespace(
        run=run_dir,
        agent_identity=agent_identity,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(run_dir)
    try:
        _require_task(repo, task_id)
    finally:
        repo.close()
    try:
        return (
            gate.admit_task_answer(run_dir, task_id, payload, mirror_checkpoint=False)
            if gate is not None else admit_task_answer_locally(
                run_dir=run_dir,
                task_id=task_id,
                raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc


def admit_source_identity_review(
    run_dir: str,
    task_id: str,
    *,
    action: str,
    target_sha256: str,
    reason: str,
    agent_identity: str | None = None,
    debug_override_artifact_integrity: bool = False,
    debug_override_reason: str | None = None,
) -> dict:
    """Admit one closed human source-identity decision without CLI parsing."""
    action = action.replace("-", "_")
    if action not in {"attest_identity", "keep_unverified"}:
        raise SystemExit("identity attestation action is invalid")
    if agent_identity is not None:
        raise SystemExit("source identity attestation requires the human operator")
    if not isinstance(target_sha256, str) or not target_sha256:
        raise SystemExit("identity attestation requires target_sha256")
    if not isinstance(reason, str) or not reason.strip():
        raise SystemExit("identity attestation requires a reason")
    args = SimpleNamespace(
        run=run_dir,
        agent_identity=agent_identity,
        debug_override_artifact_integrity=debug_override_artifact_integrity,
        debug_override_reason=debug_override_reason,
    )
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(run_dir)
    try:
        task = _require_task(repo, task_id)
        if task.task_kind != "source_identity_attestation" or repo.task_view(task_id) is None:
            raise SystemExit("task is not a source identity attestation")
    finally:
        repo.close()
    payload = {
        "action": action,
        "target_sha256": target_sha256,
        "reason": reason,
    }
    try:
        return (
            gate.admit_task_answer(run_dir, task_id, payload, mirror_checkpoint=False)
            if gate is not None else admit_task_answer_locally(
                run_dir=run_dir,
                task_id=task_id,
                raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc


def _print_task(task: dict) -> None:
    print(f"task_id: {task.get('task_id')}")
    print(f"slot: {task.get('slot')}")
    print(f"db_status: {task.get('db_status')}")
    print(f"kind: {task.get('kind')}")
    if task.get("ref_number") is not None:
        print(f"reference: [{task.get('ref_number')}]")
    if task.get("claim_id"):
        print(f"claim_id: {task.get('claim_id')}")
    if task.get("scope"):
        print(f"scope: {task.get('scope')}")
    if task.get("instructions"):
        print("\ninstructions:")
        print(task["instructions"])
    answer = task.get("answer")
    if answer is not None:
        print("\nlatest_answer:")
        for key, value in answer.items():
            print(f"  {key}: {value}")

    if task.get("kind") == "manual_parse_review":
        for key in (
            "review_kind", "target_sha256", "note_id", "note_number", "raw_note",
            "ref_id", "ref_number", "raw_entry", "title", "doi",
            "occurrence_id", "marker_raw", "candidates",
        ):
            if key in task:
                print(f"{key}: {task[key]}")
    if task.get("kind") == "source_identity_attestation":
        for key in ("ref_id", "ref_number", "source_text_id", "source_tier", "target_sha256", "source_identity", "resolve_identity"):
            if key in task:
                print(f"{key}: {task[key]}")


def cmd_list(args) -> int:
    repo = _repo(args.run)
    try:
        statuses = None
        if args.status:
            statuses = tuple(args.status)
        tasks = repo.list_task_views(slot=args.slot, statuses=statuses)
    finally:
        repo.close()
    if not tasks:
        print("no tasks")
        return 0
    print("task_id\tslot\tdb_status\tkind\tref\tscope\tanswered")
    for task in tasks:
        answered = "yes" if task.get("answer") is not None else "no"
        print(
            f"{task.get('task_id')}\t{task.get('slot')}\t{task.get('db_status')}\t"
            f"{task.get('kind')}\t{task.get('ref_number','')}\t{task.get('scope','')}\t{answered}"
        )
    return 0


def cmd_show(args) -> int:
    repo = _repo(args.run)
    try:
        task = repo.task_view(args.task)
    finally:
        repo.close()
    if task is None:
        raise SystemExit(f"task not found: {args.task}")
    _print_task(task)
    return 0


def cmd_answer_fetch(args) -> int:
    repo = _repo(args.run)
    try:
        _require_task(repo, args.task)
        task = repo.task_view(args.task) or {}
        ocr_text_file = getattr(args, "ocr_text_file", None)
        if ocr_text_file:
            conflicting = (
                args.file_path,
                args.text_file,
                args.url,
                args.not_found,
                args.item_file,
                args.item_text_file,
                args.item_not_found,
            )
            if any(conflicting):
                raise SystemExit(
                    "--ocr-text-file is mutually exclusive with other Fetch answer inputs"
                )
            if task.get("kind") != "fetch":
                raise SystemExit("--ocr-text-file is valid only for an ordinary Fetch task")
            if (
                not isinstance(ocr_text_file, str)
                or not ocr_text_file.lower().endswith(".txt")
                or not os.path.isfile(ocr_text_file)
            ):
                raise SystemExit("--ocr-text-file must name an existing .txt file")
            ref_id = task.get("ref_id")
            if not isinstance(ref_id, str) or not ref_id:
                raise SystemExit("ordinary Fetch task has no reference identity")
            try:
                scan_ref = provided_fulltext.precomputed_ocr_source_ref(
                    args.run, ref_id
                )
            except ValueError as exc:
                raise SystemExit(f"precomputed OCR answer rejected: {exc}") from exc
            payload = {"found": True, "file_path": ocr_text_file, "url": scan_ref}
        elif task.get("kind") == "browser_challenge":
            if (
                getattr(args, "source_tier", "fulltext") == "abstract"
                and (args.item_text_file or [])
            ):
                raise SystemExit("abstract Fetch answers require captured files")
            items = []
            for item in args.item_file or []:
                ref_id, value = item.split("=", 1)
                answer_item = {"ref_id": ref_id, "file_path": value, "found": True}
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    answer_item["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    answer_item.update(guided_fetch=True, identity_attested=True)
                items.append(answer_item)
            for item in args.item_text_file or []:
                ref_id, value = item.split("=", 1)
                answer_item = {"ref_id": ref_id, "text": _read_text_maybe(value), "found": True}
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    answer_item["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    answer_item.update(guided_fetch=True, identity_attested=True)
                items.append(answer_item)
            for ref_id in args.item_not_found or []:
                item = {"ref_id": ref_id, "found": False}
                if getattr(args, "guided_fetch", False):
                    item.update(disposition="user_waived", guided_fetch=True)
                items.append(item)
            payload = {"items": items}
        else:
            payload = {}
            if args.url:
                payload["url"] = args.url
            if args.not_found:
                payload["found"] = False
                if getattr(args, "guided_fetch", False):
                    payload.update(disposition="user_waived", guided_fetch=True)
            elif args.file_path:
                payload["found"] = True
                payload["file_path"] = args.file_path
            elif args.text_file:
                if getattr(args, "source_tier", "fulltext") == "abstract":
                    raise SystemExit("abstract Fetch answers require a captured file")
                payload["found"] = True
                payload["text"] = _read_text_maybe(args.text_file)
            else:
                raise SystemExit("fetch answer requires --not-found, --file-path, or --text-file")
            if payload.get("found") is True:
                if getattr(args, "source_tier", "fulltext") != "fulltext":
                    payload["source_tier"] = args.source_tier
                if getattr(args, "guided_fetch", False):
                    payload.update(guided_fetch=True, identity_attested=True)
    finally:
        repo.close()
    admitted = admit_fetch_payload(
        args.run,
        args.task,
        payload,
        agent_identity=getattr(args, "agent_identity", None),
        debug_override_artifact_integrity=getattr(
            args, "debug_override_artifact_integrity", False
        ),
        debug_override_reason=getattr(args, "debug_override_reason", None),
    )
    producer = admitted["provenance"]
    print(
        f"stored answer for {args.task} as "
        f"{producer['producer_class']}:{producer['producer_identity']}"
    )
    return 0


def _bulk_skip_fetch_args(args, task_id: str):
    """Build the ordinary not-found answer accepted by cmd_answer_fetch."""
    return argparse.Namespace(
        run=args.run,
        task=task_id,
        file_path=None,
        text_file=None,
        ocr_text_file=None,
        url=None,
        not_found=True,
        item_file=None,
        item_text_file=None,
        item_not_found=None,
        source_tier="fulltext",
        guided_fetch=False,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_skip_fetch(args) -> int:
    """Mark every pending ordinary FETCH task as source-text not found.

    This is deliberately a retrieval answer only. It does not modify Resolve's
    bibliographic existence/identity result. Browser-challenge tasks are not
    guessed in bulk and must be handled through their explicit item contract.
    """
    repo = _repo(args.run)
    try:
        pending = sorted(
            (task for task in repo.list_tasks(slot="fetch") if task.status == "pending"),
            key=lambda task: task.task_id,
        )
    finally:
        repo.close()
    if not pending:
        print("no pending FETCH tasks")
        return 0
    ordinary = [task for task in pending if task.task_kind == "fetch"]
    identity = [
        task for task in pending
        if task.task_kind == "source_identity_attestation"
    ]
    unsupported = [
        task.task_id for task in pending
        if task.task_kind not in {"fetch", "source_identity_attestation"}
    ]
    if unsupported:
        raise SystemExit(
            "bulk Fetch skip supports only ordinary fetch tasks; handle these "
            "non-ordinary tasks individually: " + ", ".join(unsupported)
        )
    if not ordinary:
        print("no pending ordinary FETCH tasks to skip")
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
        return 0
    print(f"skipping {len(ordinary)} pending FETCH task(s) as source text not found")
    for task in ordinary:
        cmd_answer_fetch(_bulk_skip_fetch_args(args, task.task_id))
    print(f"skipped {len(ordinary)} pending FETCH task(s)")
    if identity:
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
    print(f"next: {run_command('--run', args.run, '--resume', '--no-fetch')}")
    return 0


def _bulk_guided_proceed_args(args, task: dict):
    """Build one explicit guided waiver without changing not-found semantics."""
    browser_refs = [item["ref_id"] for item in task.get("references") or []]
    return argparse.Namespace(
        run=args.run,
        task=task["task_id"],
        file_path=None,
        text_file=None,
        ocr_text_file=None,
        url=None,
        not_found=task.get("kind") == "fetch",
        item_file=None,
        item_text_file=None,
        item_not_found=browser_refs if task.get("kind") == "browser_challenge" else None,
        source_tier="fulltext",
        guided_fetch=True,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_guided_proceed(args) -> int:
    """Close every pending Fetch task as user-waived after guided recovery."""
    repo = _repo(args.run)
    try:
        pending = sorted(
            (repo.task_view(task.task_id) or {} for task in repo.list_tasks(slot="fetch")
             if task.status == "pending"),
            key=lambda task: task.get("task_id", ""),
        )
    finally:
        repo.close()
    retrieval = [
        task for task in pending
        if task.get("kind") in {"fetch", "browser_challenge"}
    ]
    identity = [
        task for task in pending
        if task.get("kind") == "source_identity_attestation"
    ]
    unsupported = [
        task.get("task_id", "") for task in pending
        if task.get("kind") not in {
            "fetch", "browser_challenge", "source_identity_attestation",
        }
    ]
    if unsupported:
        raise SystemExit("guided Fetch proceed cannot waive non-retrieval tasks: " + ", ".join(unsupported))
    if not retrieval:
        print("no pending retrieval Fetch tasks to close")
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
        return 0
    for task in retrieval:
        cmd_answer_fetch(_bulk_guided_proceed_args(args, task))
    print(f"closed {len(retrieval)} pending Fetch task(s) as user waived")
    if identity:
        print(
            f"{len(identity)} source-identity review(s) remain; answer attest_identity "
            "or keep_unverified before resuming Fetch."
        )
    return 0


def _identity_skip_args(args, task: dict):
    """Build one explicit keep-unverified answer for an identity task."""
    return argparse.Namespace(
        run=args.run,
        task=task["task_id"],
        target_sha256=task["target_sha256"],
        action="keep-unverified",
        reason=args.reason,
        source_text=None,
        source_text_file=None,
        title=None,
        doi=None,
        ref=None,
        claim=None,
        agent_identity=args.agent_identity,
        debug_override_artifact_integrity=args.debug_override_artifact_integrity,
        debug_override_reason=args.debug_override_reason,
    )


def cmd_skip_identity(args) -> int:
    """Keep every pending source-identity review explicitly unverified."""
    if args.agent_identity is not None:
        raise SystemExit("source identity review skip requires the human operator")
    repo = _repo(args.run)
    try:
        pending = sorted(
            (
                repo.task_view(task.task_id) or {}
                for task in repo.list_tasks(status="pending")
                if task.task_kind == "source_identity_attestation"
            ),
            key=lambda task: task.get("task_id", ""),
        )
    finally:
        repo.close()
    if not pending:
        print("no pending source-identity tasks")
        return 0
    if any(
        not task.get("task_id") or not task.get("target_sha256")
        for task in pending
    ):
        raise SystemExit("pending source-identity task is missing its frozen target")
    for task in pending:
        cmd_answer_review(_identity_skip_args(args, task))
    print(
        f"skipped {len(pending)} pending source-identity task(s); "
        "their sources remain unverified"
    )
    print(f"next: {run_command('--run', args.run, '--resume')}")
    return 0


def cmd_answer_research(args) -> int:
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    repo = _repo(args.run)
    try:
        _require_task(repo, args.task)
        if args.not_found:
            payload = {"found": False, "findings": []}
        else:
            findings = []
            for item in args.finding or []:
                url, stance, quote = item.split("|", 2)
                findings.append({
                    "url": url,
                    "stance": stance,
                    "quote": _read_text_maybe(quote),
                })
            payload = {"found": bool(findings), "findings": findings}
    finally:
        repo.close()
    try:
        admitted = (
            gate.admit_task_answer(
                args.run, args.task, payload, mirror_checkpoint=False
            )
            if gate is not None else admit_task_answer_locally(
                run_dir=args.run, task_id=args.task, raw_payload=payload,
                assurance=resolution.assurance,
                agent_identity=args.agent_identity,
            )
        )
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc
    producer = admitted["provenance"]
    print(
        f"stored answer for {args.task} as "
        f"{producer['producer_class']}:{producer['producer_identity']}"
    )
    return 0


def cmd_answer_review(args) -> int:
    repo = _repo(args.run)
    try:
        task = _require_task(repo, args.task)
        view = repo.task_view(args.task)
        is_identity = task.task_kind == "source_identity_attestation" and view is not None
        if is_identity:
            if any((
                args.source_text,
                args.source_text_file,
                args.title,
                args.doi,
                getattr(args, "ref", None),
                getattr(args, "claim", None),
            )):
                raise SystemExit("identity attestation does not accept Parse review inputs")
        elif task.task_kind != "manual_parse_review" or view is None:
            raise SystemExit("task is not a supported manual review")
        else:
            action = args.action.replace("-", "_")
            source_inputs = list(args.source_text or [])
            for path in args.source_text_file or []:
                with open(path, encoding="utf-8", errors="strict") as fh:
                    source_inputs.append(fh.read())
            has_identity = args.title is not None or args.doi is not None
            review_kind = view.get("review_kind")
            if review_kind == "citation_reference_review":
                if action not in {"select_reference", "keep_unresolved"} or source_inputs or has_identity or getattr(args, "claim", None):
                    raise SystemExit("citation review accepts select-reference --ref, or keep-unresolved")
                if action == "select_reference" and not getattr(args, "ref", None):
                    raise SystemExit("select-reference requires --ref")
            elif review_kind == "reference_claim_review":
                if action not in {"select_claim", "keep_unresolved"} or source_inputs or has_identity or getattr(args, "ref", None):
                    raise SystemExit("inverse citation review accepts select-claim --claim, or keep-unresolved")
                if action == "select_claim" and not getattr(args, "claim", None):
                    raise SystemExit("select-claim requires --claim")
            elif action == "split_sources":
                if has_identity:
                    raise SystemExit("split-sources does not accept --title or --doi")
                if len(source_inputs) < 2:
                    raise SystemExit("split-sources requires at least two source inputs")
            elif action == "correct_identity":
                if source_inputs:
                    raise SystemExit("correct-identity does not accept source inputs")
                if not has_identity:
                    raise SystemExit("correct-identity requires --title and/or --doi")
            elif source_inputs or has_identity:
                raise SystemExit(f"{args.action} does not accept source/title/doi inputs")
            if review_kind not in {"citation_reference_review", "reference_claim_review"} and (getattr(args, "ref", None) or getattr(args, "claim", None)):
                raise SystemExit("--ref/--claim are valid only for citation attribution reviews")
            if action == "keep_unresolved" and (getattr(args, "ref", None) or getattr(args, "claim", None)):
                raise SystemExit("keep-unresolved does not accept --ref or --claim")
            payload = {
                "action": action,
                "target_sha256": args.target_sha256,
                "reason": args.reason,
            }
            if action == "split_sources":
                payload["source_texts"] = source_inputs
            elif action == "correct_identity":
                payload.update(title=args.title, doi=args.doi)
            elif action == "select_reference":
                payload["ref_id"] = getattr(args, "ref", None)
            elif action == "select_claim":
                payload["claim_id"] = getattr(args, "claim", None)
    finally:
        repo.close()
    if is_identity:
        admitted = admit_source_identity_review(
            args.run,
            args.task,
            action=args.action,
            target_sha256=args.target_sha256,
            reason=args.reason,
            agent_identity=getattr(args, "agent_identity", None),
            debug_override_artifact_integrity=getattr(args, "debug_override_artifact_integrity", False),
            debug_override_reason=getattr(args, "debug_override_reason", None),
        )
        print(f"stored review answer {args.task} {admitted['provenance']['producer_class']}:{admitted['provenance']['producer_identity']}")
        return 0
    resolution = _answer_gate(args)
    gate = getattr(resolution, "gate", resolution)
    try:
        admitted = gate.admit_task_answer(args.run, args.task, payload, mirror_checkpoint=False) if gate is not None else admit_task_answer_locally(run_dir=args.run, task_id=args.task, raw_payload=payload, assurance=resolution.assurance, agent_identity=args.agent_identity)
    except (AuthorityError, IntegrityGateError) as exc:
        raise SystemExit(f"integrity gate rejected task answer: {exc}") from exc
    print(f"stored review answer {args.task} {admitted['provenance']['producer_class']}:{admitted['provenance']['producer_identity']}")
    return 0


def _add_integrity_debug_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-identity",
        metavar="AGENT_IDENTITY",
        help="stable opaque harness identity (1-64 ASCII characters)",
    )
    parser.add_argument(
        "--debug-override-artifact-integrity",
        action="store_true",
        help="continue diagnostically only after a trusted mismatch override",
    )
    parser.add_argument(
        "--debug-override-reason",
        help="mandatory operator reason for the artifact-integrity debug override",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--run", required=True)
    p.add_argument(
        "--slot", choices=["fetch", "research", "verify", "parse_review"]
    )
    p.add_argument("--status", action="append",
                   choices=["pending", "answered", "applied", "cancelled"])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "skip-fetch",
        help="mark every pending ordinary Fetch task as source text not found",
    )
    p.add_argument("--run", required=True)
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_skip_fetch)

    p = sub.add_parser("answer-fetch")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--file-path")
    p.add_argument("--text-file")
    p.add_argument(
        "--ocr-text-file",
        help="authenticated OCR text for the task's one pending parked PDF",
    )
    p.add_argument("--url")
    p.add_argument("--source-tier", choices=["fulltext", "abstract"], default="fulltext")
    p.add_argument("--guided-fetch", action="store_true",
                   help="record a user-provided source from guided Fetch")
    p.add_argument("--not-found", action="store_true")
    p.add_argument("--item-file", action="append",
                   help="browser challenge item: REF_ID=PATH")
    p.add_argument("--item-text-file", action="append",
                   help="browser challenge item: REF_ID=PATH_TO_TEXT")
    p.add_argument("--item-not-found", action="append",
                   help="browser challenge item: REF_ID")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_fetch)

    p = sub.add_parser("guided-proceed", help="close pending guided Fetch recovery as user waived")
    p.add_argument("--run", required=True)
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_guided_proceed)

    p = sub.add_parser(
        "skip-identity",
        help="skip pending source-identity reviews and keep their sources unverified",
    )
    p.add_argument("--run", required=True)
    p.add_argument(
        "--reason",
        default="Operator skipped source identity review; source remains unverified.",
    )
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_skip_identity)

    p = sub.add_parser("answer-research")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--finding", action="append",
                   help="URL|STANCE|QUOTE_OR_QUOTE_FILE")
    p.add_argument("--not-found", action="store_true")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_research)
    p = sub.add_parser("answer-review")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--target-sha256", required=True)
    p.add_argument("--action", required=True, choices=["no-sources", "split-sources", "correct-identity", "keep-ambiguous", "select-reference", "select-claim", "keep-unresolved", "attest-identity", "keep-unverified"])
    p.add_argument("--reason", required=True)
    p.add_argument("--source-text", action="append")
    p.add_argument("--source-text-file", action="append")
    p.add_argument("--title")
    p.add_argument("--doi")
    p.add_argument("--ref")
    p.add_argument("--claim")
    _add_integrity_debug_args(p)
    p.set_defaults(func=cmd_answer_review)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
