# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal isolated authority fixtures for selected recovery tests."""
from __future__ import annotations

import os

from core.infra.db import RunRepository
from core.infra.integrity import FileAuthority, RunIntegrityGate

HASH = "a" * 64

class _DirectClient:
    def __init__(self, authority: FileAuthority):
        self.authority = authority

    def enroll(self, run_dir: str):
        return self.authority.enroll(run_dir, caller_role="trusted_writer")

    def check(self, run_dir: str):
        return self.authority.check(run_dir, authenticated_caller="uid:test")

    def begin_transition(
        self,
        run_dir: str,
        checkpoint_kind: str,
        *,
        transition_id: str,
        content_store_required: bool,
    ):
        return self.authority.begin_transition(
            run_dir,
            checkpoint_kind=checkpoint_kind,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
            transition_id=transition_id,
            content_store_required=content_store_required,
        )

    def commit_transition(self, run_dir: str, lease_id: str):
        return self.authority.commit_transition(
            run_dir,
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def abort_transition(self, run_dir: str, lease_id: str, reason: str):
        return self.authority.abort_transition(
            run_dir,
            lease_id=lease_id,
            reason=reason,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def override(self, run_dir: str, reason: str):
        return self.authority.override(
            run_dir,
            reason=reason,
            authenticated_caller="uid:test",
        )

    def enroll_content_store(self):
        return self.authority.enroll_content_store(caller_role="trusted_writer")

    def check_content_store(self):
        return self.authority.check_content_store(authenticated_caller="uid:test")

    def begin_content_store_transition(self, run_dir: str, transition_id: str):
        return self.authority.begin_content_store_transition(
            run_dir,
            transition_id=transition_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def commit_content_store_transition(self, run_dir: str, lease_id: str):
        return self.authority.commit_content_store_transition(
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def abort_content_store_transition(
        self, run_dir: str, lease_id: str, reason: str
    ):
        return self.authority.abort_content_store_transition(
            lease_id=lease_id,
            reason=reason,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def heartbeat_transition(self, run_dir: str, lease_id: str):
        return self.authority.heartbeat_transition(
            run_dir,
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def heartbeat_content_store_transition(self, run_dir: str, lease_id: str):
        return self.authority.heartbeat_content_store_transition(
            lease_id=lease_id,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:test",
        )

    def recover_pipeline_transition(self, run_dir: str):
        return self.authority.recover_pipeline_transition(
            run_dir,
            caller_role="trusted_writer",
            authenticated_caller="uid:test",
            authenticated_process="process:resume",
        )

    def override_content_store(self, run_dir: str, reason: str):
        return self.authority.override_content_store(
            reason=reason, authenticated_caller="uid:test"
        )


def _run(protected_root):
    run_dir = protected_root / "run-1"
    repo = RunRepository.create(
        str(run_dir),
        run_id="run-1",
        input_path="paper.pdf",
        input_sha256=HASH,
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )
    repo.close()
    (run_dir / "artifact.txt").write_text("original", encoding="utf-8")
    return run_dir


def _gate(tmp_path):
    authority_root = tmp_path / "authority"
    protected_root = tmp_path / "protected"
    authority_root.mkdir()
    protected_root.mkdir()
    key_file = authority_root / "key"
    key_file.write_text("k" * 64, encoding="ascii")
    os.chmod(key_file, 0o600)
    authority = FileAuthority(
        authority_root=str(authority_root),
        key_file=str(key_file),
        protected_root=str(protected_root),
        authority_id="test-authority",
        enforce_permissions=False,
    )
    return RunIntegrityGate(_DirectClient(authority)), protected_root
