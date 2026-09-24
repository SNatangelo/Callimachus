# tests/test_cache_maintenance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.app.cache_maintenance import deactivate_reusable_texts
from core.fetch.storage import content_store
from core.infra.db import RunRepository
from core.infra.integrity import IntegrityGateError


REF = {
    "id": "ref-1",
    "ref_number": 1,
    "raw_entry": "Smith J. Example article. Journal. 2020.",
    "title": "Example article",
    "doi": "10.1000/example",
    "pmid": None,
    "isbn": None,
    "url": None,
    "year": 2020,
    "ay_surname": "smith",
    "ay_year": 2020,
}


def _run(tmp_path: Path) -> str:
    run_dir = tmp_path / "runs" / "run-1"
    repo = RunRepository.create(
        str(run_dir),
        run_id="run-1",
        input_path="paper.pdf",
        input_sha256="a" * 64,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )
    repo.replace_parse_payload(claims=[], references=[dict(REF)], citations=[])
    repo.close()
    return str(run_dir)


class _Gate:
    def __init__(self) -> None:
        self.calls = []

    def begin_pipeline_transition(self, run_dir, **kwargs):
        self.calls.append(("begin", run_dir, kwargs))
        return "lease"

    def commit_pipeline_transition(self, run_dir, lease):
        self.calls.append(("commit", run_dir, lease))

    def abort_pipeline_transition(self, run_dir, lease, *, reason):
        self.calls.append(("abort", run_dir, lease, reason))


class _CommitFailureGate(_Gate):
    def commit_pipeline_transition(self, run_dir, lease):
        self.calls.append(("commit", run_dir, lease))
        raise IntegrityGateError("checkpoint failed")


def test_selected_cache_removal_disables_reuse_but_preserves_cached_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    run_dir = _run(tmp_path)
    stored = content_store.record_parsed_text(
        run_dir, dict(REF), "abstract", "crossref", "An abstract."
    )
    assert stored is not None
    stored_path = Path(stored["stored_path"])
    gate = _Gate()

    result = deactivate_reusable_texts(
        run_dir,
        [stored["parsed_text_id"]],
        gate_factory=lambda _run: gate,
    )

    assert result["deactivated"] == 1
    assert result["files_deleted"] == 0
    assert result["user_originals_preserved"] is True
    assert stored_path.is_file()
    assert content_store.find_reusable_parsed_text(
        run_dir, dict(REF), tiers=("abstract",)
    ) is None
    from core.app.desktop import load_cache_inventory
    assert load_cache_inventory(run_dir)["items"] == []
    assert [call[0] for call in gate.calls] == ["begin", "commit"]


def test_cache_removal_rejects_an_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    run_dir = _run(tmp_path)
    repo = RunRepository.open(run_dir)
    try:
        repo.start_session(pid=123, host="test")
    finally:
        repo.close()

    with pytest.raises(RuntimeError, match="while the selected run is active"):
        deactivate_reusable_texts(run_dir, [])


def test_checkpoint_failure_after_mutation_is_left_for_authority_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CITATION_VERIFIER_STATE_DIR", str(tmp_path / "state"))
    run_dir = _run(tmp_path)
    stored = content_store.record_parsed_text(
        run_dir, dict(REF), "abstract", "crossref", "An abstract."
    )
    gate = _CommitFailureGate()

    with pytest.raises(IntegrityGateError, match="checkpoint failed"):
        deactivate_reusable_texts(
            run_dir,
            [stored["parsed_text_id"]],
            gate_factory=lambda _run: gate,
        )

    assert [call[0] for call in gate.calls] == ["begin", "commit"]




@pytest.mark.skipif(os.name != "posix", reason="integrity authority requires POSIX")
def test_authority_recovery_restores_cache_after_paired_checkpoint_failure(
    tmp_path, monkeypatch
):
    from core.infra.integrity import (
        AuthorityUnavailable,
        FileAuthority,
        RunIntegrityGate,
    )
    from tests._integrity_fixtures import _DirectClient

    protected_root = tmp_path / "protected"
    authority_root = tmp_path / "authority"
    protected_root.mkdir()
    authority_root.mkdir()
    run_dir = _run(protected_root)
    stored = content_store.record_parsed_text(
        run_dir, dict(REF), "abstract", "crossref", "An abstract."
    )
    original = tmp_path / "user-original.pdf"
    original.write_bytes(b"user original bytes")
    archived = content_store.archive_user_original(
        run_dir,
        str(original),
        ref=dict(REF),
        supplied_via="test",
        move=False,
    )
    assert stored is not None
    assert archived is not None

    key_file = authority_root / "key"
    key_file.write_text("k" * 64, encoding="ascii")
    os.chmod(key_file, 0o600)
    authority = FileAuthority(
        authority_root=str(authority_root),
        key_file=str(key_file),
        protected_root=str(protected_root),
        content_store_root=content_store.storage_root(run_dir),
        authority_id="test-authority",
        enforce_permissions=False,
    )
    gate = RunIntegrityGate(_DirectClient(authority))
    gate.client.enroll_content_store()
    gate.enroll(run_dir)
    gate.preflight(run_dir)
    gate.preflight_content_store(run_dir)
    stored_path = Path(stored["stored_path"])
    archived_path = Path(archived["stored_path"])
    stored_bytes = stored_path.read_bytes()
    archived_bytes = archived_path.read_bytes()

    def fail_store_checkpoint(_run_dir, _lease_id):
        raise AuthorityUnavailable("forced store checkpoint failure")

    monkeypatch.setattr(
        gate.client, "commit_content_store_transition", fail_store_checkpoint
    )
    with pytest.raises(IntegrityGateError, match="content store integrity transition"):
        deactivate_reusable_texts(
            run_dir,
            [stored["parsed_text_id"]],
            gate_factory=lambda _run: gate,
        )
    assert content_store.find_reusable_parsed_text(
        run_dir, dict(REF), tiers=("abstract",)
    ) is None
    assert stored_path.read_bytes() == stored_bytes
    assert archived_path.read_bytes() == archived_bytes

    recovered = gate.recover_pipeline_transition(run_dir)

    assert recovered["status"] == "recovered"
    assert gate.preflight(run_dir)["status"] == "clean"
    assert gate.preflight_content_store(run_dir)["status"] == "clean"
    assert stored_path.read_bytes() == stored_bytes
    assert archived_path.read_bytes() == archived_bytes
    assert content_store.find_reusable_parsed_text(
        run_dir, dict(REF), tiers=("abstract",)
    ) is not None


def test_cache_removal_uses_the_effective_environment_mapping(tmp_path, monkeypatch):
    monkeypatch.delenv("CITATION_VERIFIER_STATE_DIR", raising=False)
    state = tmp_path / "dotenv-state"
    environ = {"CITATION_VERIFIER_STATE_DIR": str(state)}
    run_dir = _run(tmp_path)
    stored = content_store.record_parsed_text(
        run_dir,
        dict(REF),
        "abstract",
        "crossref",
        "An abstract.",
    )
    # Move the fixture into the store selected by the explicit environment,
    # mirroring a STATE_DIR configured only in the desktop's .env file.
    default_store = Path(content_store.storage_root(run_dir))
    configured_store = Path(content_store.storage_root(run_dir, environ))
    configured_store.parent.mkdir(parents=True, exist_ok=True)
    default_store.rename(configured_store)

    result = deactivate_reusable_texts(
        run_dir,
        [stored["parsed_text_id"]],
        environ=environ,
        gate_factory=lambda _run: _Gate(),
    )

    assert result["deactivated"] == 1
    from core.app.desktop import load_cache_inventory
    assert load_cache_inventory(run_dir, environ=environ)["items"] == []
