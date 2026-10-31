# tests/test_guided_fetch_stage_persistence.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core.app.guided_fetch import GuidedFetchController, build_source_inventory, source_payload
from core.app.commands import tasks
from core.infra.db import ExecutionAssuranceRecord
from core.infra.db.repository import RunRepository
from core.infra.integrity import FileAuthority, RunIntegrityGate
from core.parse.parsing_common import _make_reference


def _run_with_browser_task(run_dir, ref_ids=("r1",), *, grouped=True, assurance=None) -> None:
    repo = RunRepository.create(
        str(run_dir), run_id="guided-stage", input_path="in", input_sha256="x",
        accuracy="standard", style=None, model_id=None, http_profile=None,
        challenge_mode=None, fixture_fingerprint="fixture",
        execution_assurance=assurance,
    )
    try:
        references = []
        for number, ref_id in enumerate(ref_ids, start=1):
            reference = _make_reference(
                number,
                f"Smith J (2020) A cited article {number}. Journal of Tests 12(3): 10-20.",
            )
            reference["id"] = ref_id
            references.append(reference)
        repo.replace_parse_payload(claims=[], references=references, citations=[])
        if not grouped:
            from core.infra.db.task_storage import _FETCH_REFERENCE_FIELDS

            for ref_id in ref_ids:
                repo.upsert_resolve_result(ref_id, {
                    "status": "unverified", "via": "fixture",
                    "reference_status_tag": "unverified", "fabrication_risk": "unknown",
                    "attempts": [],
                })
                reference = repo.get_reference(ref_id)
                result = repo.get_resolve_result(ref_id)
                repo.create_task(
                    task_id=f"fetch:{ref_id}", slot="fetch", ref_id=ref_id,
                    task_payload={
                        "kind": "fetch", "status": "pending", "answer": None,
                        "ref_id": ref_id, "ref_number": reference.ref_number,
                        "reference": {
                            key: getattr(reference, key) for key in _FETCH_REFERENCE_FIELDS
                        },
                        "source_identity": {
                            "reference_status_tag": result.reference_status_tag,
                            "fabrication_risk": result.fabrication_risk,
                            "tag_reason": result.tag_reason,
                            "matched_title": result.matched_title,
                            "metadata_match": None,
                        },
                        "instructions": "Supply a source.",
                    },
                )
            return
        repo.create_task(
            task_id="browser:one", slot="fetch", task_payload={
                "kind": "browser_challenge", "status": "pending", "answer": None,
                "domain": "example.test", "instructions": "Open the source.",
                "candidate_urls": ["https://example.test/source"],
                "references": [
                    {
                        "ref_id": ref_id,
                        **{
                            key: getattr(repo.get_reference(ref_id), key)
                            for key in ("ref_number", "raw_entry", "doi", "url")
                        },
                        "candidate_urls": ["https://example.test/source"],
                    }
                    for ref_id in ref_ids
                ],
            },
        )
    finally:
        repo.close()


def test_confirmed_stage_survives_controller_restart_and_is_admitted_once(tmp_path):
    _run_with_browser_task(tmp_path)
    original = tmp_path / "provided-fulltext.txt"
    original.write_text("The confirmed full text.", encoding="utf-8")

    first = GuidedFetchController(str(tmp_path))
    first.stage_source(
        "r1", source_payload(
            file_path=str(original), tier="fulltext",
            source_ref="https://example.test/source",
        ),
    )
    staged = build_source_inventory(str(tmp_path))["staged_sources"]
    assert len(staged) == 1
    expected_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    assert staged[0]["ref_id"] == "r1"
    assert staged[0]["source_tier"] == "fulltext"
    assert staged[0]["identity_attested"] is True
    assert staged[0]["source_ref"] == "https://example.test/source"
    assert staged[0]["sha256"] == expected_hash
    assert staged[0]["original_filename"] == original.name
    assert staged[0]["original_path"] == str(original)
    assert (tmp_path / staged[0]["file_path"]).read_text(encoding="utf-8") == original.read_text(encoding="utf-8")

    resumed = GuidedFetchController(str(tmp_path))
    result = resumed.proceed()

    assert result["proceeded"] is True
    assert result["submitted_task_ids"] == ["browser:one"]
    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        answer = repo.get_latest_task_answer("browser:one")
        assert answer is not None
        assert answer.accepted_for_processing is True
        assert answer.raw_payload["items"][0]["ref_id"] == "r1"
        assert answer.raw_payload["items"][0]["found"] is True
        admitted_file = tmp_path / answer.raw_payload["items"][0]["file_path"]
        assert hashlib.sha256(admitted_file.read_bytes()).hexdigest() == expected_hash
        assert repo.list_pending_tasks(slot="fetch") == []
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("skip_remaining_confirmed", "expected_intent_count"),
    [(False, 0), (True, 1)],
)
def test_proceed_journals_identity_skip_only_with_explicit_confirmation(
    tmp_path, skip_remaining_confirmed, expected_intent_count,
):
    _run_with_browser_task(tmp_path)
    original = tmp_path / "confirmed.txt"
    original.write_text("The exact source supplied in Guided Fetch.", encoding="utf-8")
    controller = GuidedFetchController(str(tmp_path))
    controller.stage_source("r1", source_payload(file_path=str(original)))

    result = controller.proceed(
        skip_remaining_confirmed=skip_remaining_confirmed,
    )

    assert result["proceeded"] is True
    stage_state = tasks.load_guided_fetch_stage_artifacts(str(tmp_path))
    stage_record = stage_state["latest"][hashlib.sha256(b"r1").hexdigest()]["record"]
    intents = stage_state["identity_skip_intents"]
    assert len(intents) == expected_intent_count
    if intents:
        intent = intents[0]
        assert intent["action"] == "identity_skip"
        assert intent["actor_type"] == "user"
        assert intent["task_id"] == "browser:one"
        assert intent["task_generation"] == stage_record["task_generation"]
        assert intent["ref_id"] == "r1"
        assert intent["source_tier"] == stage_record["source_tier"]
        assert intent["source_sha256"] == stage_record["sha256"]
        assert intent["source_stage_event_id"] == stage_record["event_id"]
    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        answer = repo.get_latest_task_answer("browser:one")
        task = repo.get_task("browser:one")
        assert answer is not None and task is not None
        assert answer.generation == task.generation
        item = answer.raw_payload["items"][0]
        assert "keep_unverified_identity_review" not in item
        assert item["guided_fetch"] is True
        assert item["ref_id"] == "r1"
    finally:
        repo.close()


def test_agent_cannot_record_guided_identity_skip_confirmation(tmp_path):
    _run_with_browser_task(tmp_path)
    controller = GuidedFetchController(str(tmp_path), agent_identity="codex-agent")

    with pytest.raises(ValueError, match="human operator"):
        controller.proceed(skip_remaining_confirmed=True)

    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        assert repo.get_latest_task_answer("browser:one") is None
    finally:
        repo.close()


def test_malformed_identity_skip_event_fails_closed(tmp_path):
    _run_with_browser_task(tmp_path)
    original = tmp_path / "confirmed.txt"
    original.write_text("The exact source supplied in Guided Fetch.", encoding="utf-8")
    controller = GuidedFetchController(str(tmp_path))
    controller.stage_source("r1", source_payload(file_path=str(original)))
    controller.proceed(skip_remaining_confirmed=True)

    ref_key = hashlib.sha256(b"r1").hexdigest()
    stage_dir = tmp_path / "sources" / "guided_fetch_stages" / ref_key
    event_path = max(
        (path for path in stage_dir.glob("*.json") if path.name != ""),
        key=lambda path: path.name,
    )
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["action"] == "identity_skip"
    event["actor_type"] = "agent"
    event_path.write_text(json.dumps(event, sort_keys=True), encoding="utf-8")

    loaded = tasks.load_guided_fetch_stage_artifacts(str(tmp_path))
    assert ref_key in loaded["issues"]
    assert not loaded["identity_skip_intents"]


def test_partial_browser_group_replacement_and_discard_survive_restart(tmp_path):
    _run_with_browser_task(tmp_path, ("r1", "r2"))
    first = tmp_path / "first.txt"
    replacement = tmp_path / "replacement.txt"
    discarded = tmp_path / "discarded.txt"
    first.write_text("First confirmed source.", encoding="utf-8")
    replacement.write_text("Replacement abstract.", encoding="utf-8")
    discarded.write_text("Source later discarded.", encoding="utf-8")

    controller = GuidedFetchController(str(tmp_path))
    controller.stage_source("r1", source_payload(file_path=str(first)))
    controller.stage_source(
        "r1", source_payload(
            file_path=str(replacement), tier="abstract",
            source_ref="https://example.test/replacement",
        ),
    )
    controller.stage_source("r2", source_payload(file_path=str(discarded)))
    assert controller.discard_source("r2") is True

    staged = build_source_inventory(str(tmp_path))["staged_sources"]
    assert [item["ref_id"] for item in staged] == ["r1"]
    assert staged[0]["source_tier"] == "abstract"
    assert Path(staged[0]["file_path"]).read_text(encoding="utf-8") == "Replacement abstract."

    resumed = GuidedFetchController(str(tmp_path))
    result = resumed.proceed()

    assert result["proceeded"] is True
    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        answer = repo.get_latest_task_answer("browser:one")
        assert answer is not None and answer.accepted_for_processing is True
        items = answer.raw_payload["items"]
        assert [item["ref_id"] for item in items] == ["r1", "r2"]
        assert items[0]["found"] is True
        assert items[0]["source_tier"] == "abstract"
        assert items[1]["found"] is False
        assert items[1]["disposition"] == "user_waived"
    finally:
        repo.close()
    assert len(list((tmp_path / "sources" / "guided_fetch_stages").rglob("*.json"))) == 4


@pytest.mark.parametrize("target", ["controlled_file", "stage_record"])
def test_tampered_latest_committed_stage_fails_closed_without_older_fallback(tmp_path, target):
    _run_with_browser_task(tmp_path)
    old_source = tmp_path / "old.txt"
    latest_source = tmp_path / "latest.txt"
    old_source.write_text("Older confirmed source.", encoding="utf-8")
    latest_source.write_text("Latest confirmed source.", encoding="utf-8")
    controller = GuidedFetchController(str(tmp_path))
    controller.stage_source("r1", source_payload(file_path=str(old_source)))
    controller.stage_source("r1", source_payload(file_path=str(latest_source)))
    latest = build_source_inventory(str(tmp_path))["staged_sources"][0]
    if target == "controlled_file":
        Path(latest["file_path"]).write_text("tampered bytes", encoding="utf-8")
    else:
        record_path = sorted((tmp_path / "sources" / "guided_fetch_stages").rglob("*.json"))[-1]
        record_path.write_text("{broken record", encoding="utf-8")

    resumed = GuidedFetchController(str(tmp_path))

    assert resumed._staged == {}
    assert "r1" in resumed._stage_issues
    assert "r1" in {
        item["ref_id"] for item in build_source_inventory(str(tmp_path))["staging_issues"]
    }
    with pytest.raises(ValueError, match="draft|stage"):
        resumed.proceed()


@pytest.mark.parametrize("has_predecessor", [True, False])
def test_corrupt_replacement_before_identity_skip_blocks_fallback_until_restage(
    tmp_path, has_predecessor,
):
    _run_with_browser_task(tmp_path)
    older = tmp_path / "older.txt"
    replacement = tmp_path / "replacement.txt"
    recovered_source = tmp_path / "reconfirmed.txt"
    older.write_text("Older confirmed source.", encoding="utf-8")
    replacement.write_text("Replacement confirmed source.", encoding="utf-8")
    recovered_source.write_text("Explicitly reconfirmed source.", encoding="utf-8")

    def interrupt_answer_submission(*_args, **_kwargs):
        raise RuntimeError("simulated answer submission interruption")

    controller = GuidedFetchController(
        str(tmp_path), admit=interrupt_answer_submission,
    )
    if has_predecessor:
        controller.stage_source("r1", source_payload(file_path=str(older)))
    controller.stage_source("r1", source_payload(file_path=str(replacement)))
    with pytest.raises(RuntimeError, match="simulated answer submission interruption"):
        controller.proceed(skip_remaining_confirmed=True)

    staged_record = controller._staged_records["r1"]
    ref_key = hashlib.sha256(b"r1").hexdigest()
    stage_dir = tmp_path / "sources" / "guided_fetch_stages" / ref_key
    replacement_event = stage_dir / (
        f"{staged_record['sequence']:08d}-{staged_record['event_id']}.json"
    )
    replacement_event.write_text("{broken replacement event", encoding="utf-8")

    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        assert repo.get_task("browser:one").status == "pending"
    finally:
        repo.close()

    state = tasks.load_guided_fetch_stage_artifacts(str(tmp_path))
    assert ref_key in state["issues"]
    assert ref_key not in state["latest"]
    assert state["identity_skip_intents"] == []

    resumed = GuidedFetchController(str(tmp_path))
    assert resumed._staged == {}
    assert "r1" in resumed._stage_issues
    with pytest.raises(ValueError, match="draft|stage"):
        resumed.proceed()
    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        assert repo.get_task("browser:one").status == "pending"
    finally:
        repo.close()

    resumed.stage_source("r1", source_payload(file_path=str(recovered_source)))
    recovered = tasks.load_guided_fetch_stage_artifacts(str(tmp_path))
    assert ref_key not in recovered["issues"]
    assert recovered["latest"][ref_key]["record"]["action"] == "stage"
    assert Path(recovered["latest"][ref_key]["payload"]["file_path"]).read_text(
        encoding="utf-8",
    ) == "Explicitly reconfirmed source."
    assert recovered["identity_skip_intents"] == []


@pytest.mark.parametrize("grouped", [True, False])
def test_task_generation_change_does_not_restore_a_confirmed_draft(tmp_path, grouped):
    _run_with_browser_task(tmp_path, grouped=grouped)
    task_id = "browser:one" if grouped else "fetch:r1"
    original = tmp_path / "confirmed.txt"
    original.write_text("Confirmed source for generation zero.", encoding="utf-8")
    controller = GuidedFetchController(str(tmp_path))
    controller.stage_source("r1", source_payload(file_path=str(original)))

    repo = RunRepository.open(str(tmp_path))
    try:
        task = repo.get_task(task_id)
        payload = dict(task.task_payload)
        repo.mark_task_answered(task_id)
        repo.reopen_task(task_id, task_payload=payload)
        assert repo.get_task(task_id).generation == 1
    finally:
        repo.close()

    resumed = GuidedFetchController(str(tmp_path))
    assert resumed._staged == {}
    assert "r1" in resumed._stage_issues
    with pytest.raises(ValueError, match=r"Source \[1\].*generation"):
        resumed.proceed()

    # An explicit fresh confirmation binds the retained file to the current task.
    assert original.is_file()
    resumed.stage_source("r1", source_payload(file_path=str(original)))
    assert resumed._stage_issues == {}
    assert resumed._staged_records["r1"]["task_generation"] == 1


class _DirectAuthorityClient:
    def __init__(self, authority: FileAuthority):
        self.authority = authority

    def enroll(self, run_dir: str):
        return self.authority.enroll(run_dir, caller_role="trusted_writer")

    def check(self, run_dir: str):
        return self.authority.check(run_dir, authenticated_caller="uid:test")

    def begin_transition(
        self, run_dir: str, checkpoint_kind: str, *, transition_id: str,
        content_store_required: bool,
    ):
        return self.authority.begin_transition(
            run_dir, checkpoint_kind=checkpoint_kind,
            caller_role="trusted_writer", authenticated_caller="uid:test",
            authenticated_process="process:test", transition_id=transition_id,
            content_store_required=content_store_required,
        )

    def commit_transition(self, run_dir: str, lease_id: str):
        return self.authority.commit_transition(
            run_dir, lease_id=lease_id, caller_role="trusted_writer",
            authenticated_caller="uid:test", authenticated_process="process:test",
        )

    def abort_transition(self, run_dir: str, lease_id: str, reason: str):
        return self.authority.abort_transition(
            run_dir, lease_id=lease_id, reason=reason,
            caller_role="trusted_writer", authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def recover_pipeline_transition(self, run_dir: str):
        return self.authority.recover_pipeline_transition(
            run_dir, caller_role="trusted_writer", authenticated_caller="uid:test",
            authenticated_process="process:resume",
        )


def _protected_gate(tmp_path, protected_root) -> RunIntegrityGate:
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    key_file = authority_root / "key"
    key_file.write_text("k" * 64, encoding="ascii")
    os.chmod(key_file, 0o600)
    authority = FileAuthority(
        authority_root=str(authority_root), key_file=str(key_file),
        protected_root=str(protected_root), authority_id="guided-stage-test",
        enforce_permissions=False,
    )
    return RunIntegrityGate(_DirectAuthorityClient(authority))


def test_uncommitted_later_stage_does_not_hide_committed_stage_after_recovery(
    monkeypatch, tmp_path,
):
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    run_dir = protected_root / "run"
    _run_with_browser_task(
        run_dir, assurance=ExecutionAssuranceRecord(
            "agent", "agent_attested", agent_identity="test-runner",
        ),
    )
    gate = _protected_gate(tmp_path, protected_root)
    gate.enroll(str(run_dir))
    monkeypatch.setattr(tasks, "_answer_gate", lambda _args: gate)

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Committed source.", encoding="utf-8")
    second.write_text("Interrupted replacement.", encoding="utf-8")
    controller = GuidedFetchController(
        str(run_dir), admit=lambda *_args, **_kwargs: {"answer_id": "test"},
    )
    controller.stage_source("r1", source_payload(file_path=str(first)))

    commit = gate.client.commit_transition

    def interrupt_before_checkpoint(_run_dir: str, _lease_id: str):
        raise KeyboardInterrupt("simulated process death before checkpoint commit")

    monkeypatch.setattr(gate.client, "commit_transition", interrupt_before_checkpoint)
    try:
        controller.stage_source("r1", source_payload(file_path=str(second)))
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("second stage should stop before its signed commit")
    monkeypatch.setattr(gate.client, "commit_transition", commit)

    before_recovery = tasks.load_guided_fetch_stage_artifacts(str(run_dir))
    ref_key = hashlib.sha256(b"r1").hexdigest()
    latest = before_recovery["latest"][ref_key]
    assert latest["record"]["sequence"] == 1
    assert Path(latest["payload"]["file_path"]).read_text(encoding="utf-8") == "Committed source."

    recovered = gate.recover_pipeline_transition(str(run_dir))
    assert recovered["status"] == "recovered"
    resumed = GuidedFetchController(
        str(run_dir), admit=lambda *_args, **_kwargs: {"answer_id": "test"},
    )
    assert Path(resumed._staged["r1"]["file_path"]).read_text(encoding="utf-8") == "Committed source."
    assert resumed.proceed()["submitted_task_ids"] == ["browser:one"]


def test_partial_admission_restart_restores_only_unanswered_fetch_task(tmp_path):
    _run_with_browser_task(tmp_path, ("r1", "r2"), grouped=False)
    original = tmp_path / "confirmed.txt"
    original.write_text("Confirmed source content.", encoding="utf-8")

    def admit_until_second(run_dir, task_id, payload, **kwargs):
        if task_id == "fetch:r2":
            raise OSError("simulated second admission failure")
        return tasks.admit_fetch_payload(run_dir, task_id, payload, **kwargs)

    controller = GuidedFetchController(str(tmp_path), admit=admit_until_second)
    for ref_id in ("r1", "r2"):
        controller.stage_source(ref_id, source_payload(file_path=str(original)))
    original.unlink()

    with pytest.raises(OSError, match="second admission"):
        controller.proceed()
    assert controller.summary["submitted_task_ids"] == ["fetch:r1"]

    resumed = GuidedFetchController(str(tmp_path))
    assert set(resumed._staged) == {"r2"}
    assert [item["ref_id"] for item in build_source_inventory(str(tmp_path))["staged_sources"]] == ["r2"]
    assert resumed.proceed()["submitted_task_ids"] == ["fetch:r2"]
    assert GuidedFetchController(str(tmp_path)).proceed()["submitted_task_ids"] == []

    repo = RunRepository.open_readonly(str(tmp_path))
    try:
        for ref_id in ("r1", "r2"):
            answers = repo.list_task_answers(f"fetch:{ref_id}")
            assert len(answers) == 1
            assert answers[0].accepted_for_processing is True
            assert answers[0].raw_payload["found"] is True
    finally:
        repo.close()
