# tests/test_source_identity_attestation_cli.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""CLI contract for operator source-identity decisions."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from core.app.commands import tasks as task_cli
from tests._identity_fixtures import (
    _repo_with_attestation,
)


class _CapturingGate:
    def __init__(self):
        self.payloads = []

    def admit_task_answer(
        self, run_dir, task_id, payload, *, mirror_checkpoint,
    ):
        self.payloads.append((run_dir, task_id, payload, mirror_checkpoint))
        return {
            "provenance": {
                "producer_class": "operator",
                "producer_identity": "test-operator",
            }
        }


def _args(run_dir, target_sha256, action, **overrides):
    values = {
        "run": str(run_dir),
        "task": "identity-review",
        "target_sha256": target_sha256,
        "action": action,
        "reason": "I inspected the exact source.",
        "source_text": None,
        "source_text_file": None,
        "title": None,
        "doi": None,
        "ref": None,
        "claim": None,
        "agent_identity": None,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.mark.parametrize(
    ("cli_action", "stored_action"),
    [
        ("attest-identity", "attest_identity"),
        ("keep-unverified", "keep_unverified"),
    ],
)
def test_answer_review_submits_closed_identity_payload(
    tmp_path, monkeypatch, cli_action, stored_action,
):
    repo, _source_path, target = _repo_with_attestation(tmp_path)
    repo.close()
    gate = _CapturingGate()
    monkeypatch.setattr(
        task_cli,
        "_answer_gate",
        lambda _args: SimpleNamespace(gate=gate, assurance="test"),
    )

    assert task_cli.cmd_answer_review(
        _args(tmp_path / "run", target, cli_action)
    ) == 0

    assert gate.payloads == [(
        str(tmp_path / "run"),
        "identity-review",
        {
            "action": stored_action,
            "target_sha256": target,
            "reason": "I inspected the exact source.",
        },
        False,
    )]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": "Injected title"}, "does not accept Parse"),
        ({"source_text": ["Injected body"]}, "does not accept Parse"),
        ({"ref": "r2"}, "does not accept Parse"),
        ({"agent_identity": "codex"}, "requires the human operator"),
    ],
)
def test_identity_answer_rejects_parse_inputs_and_agent_identity(
    tmp_path, monkeypatch, overrides, message,
):
    repo, _source_path, target = _repo_with_attestation(tmp_path)
    repo.close()
    gate = _CapturingGate()
    monkeypatch.setattr(
        task_cli,
        "_answer_gate",
        lambda _args: SimpleNamespace(gate=gate, assurance="test"),
    )

    with pytest.raises(SystemExit, match=message):
        task_cli.cmd_answer_review(
            _args(tmp_path / "run", target, "attest-identity", **overrides)
        )
    assert gate.payloads == []


def test_show_identity_task_contains_no_source_path_or_body(tmp_path, capsys):
    repo, _source_path, target = _repo_with_attestation(tmp_path)
    try:
        view = repo.task_view("identity-review")
    finally:
        repo.close()

    task_cli._print_task(view)
    shown = capsys.readouterr().out
    assert "source_text_id: source-1" in shown
    assert f"target_sha256: {target}" in shown
    assert "source_tier: fulltext" in shown
    assert "stored_path" not in shown
    assert "Source text inspected by the operator" not in shown


def test_skip_identity_submits_canonical_keep_unverified_answer(
    tmp_path, monkeypatch, capsys,
):
    repo, _source_path, target = _repo_with_attestation(tmp_path, slot="fetch")
    repo.close()
    gate = _CapturingGate()
    monkeypatch.setattr(
        task_cli,
        "_answer_gate",
        lambda _args: SimpleNamespace(gate=gate, assurance="test"),
    )
    args = Namespace(
        run=str(tmp_path / "run"),
        reason="Skip this identity review.",
        agent_identity=None,
        debug_override_artifact_integrity=False,
        debug_override_reason=None,
    )

    assert task_cli.cmd_skip_identity(args) == 0

    assert gate.payloads == [(
        str(tmp_path / "run"),
        "identity-review",
        {
            "action": "keep_unverified",
            "target_sha256": target,
            "reason": "Skip this identity review.",
        },
        False,
    )]
    output = capsys.readouterr().out
    assert "skipped 1 pending source-identity task(s)" in output
    assert "their sources remain unverified" in output
    assert "--resume" in output
