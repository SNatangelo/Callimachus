# tests/infra/integrity/test_admission.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import os

import pytest

from core.infra.db import RunRepository
from core.infra.db import repository as repository_module
from core.infra.integrity import AuthorityError, AuthorityPermissionError, FileAuthority
from core.infra.integrity import admission
from core.infra.integrity.authority import AUTHORITY_PROTOCOL_VERSION
from core.infra.integrity.service import _parse_peer_identities, dispatch_request
from tests._recovery_fixtures import _current_fetch_task


HASH = "a" * 64


def _fixture(tmp_path):
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
    repo.replace_parse_payload(
        claims=[],
        references=[{
            "id": "r1",
            "ref_number": 1,
            "raw_entry": "Example reference",
            "title": "Example reference",
            "source_type": "article",
        }],
        citations=[],
    )
    repo.create_task(
        task_id="fetch:r1",
        slot="fetch",
        ref_id="r1",
        claim_id=None,
        scope=None,
        task_payload=_current_fetch_task(repo),
    )
    repo.close()
    enrolled = authority.enroll(str(run_dir), caller_role="trusted_writer")
    repo = RunRepository.open(str(run_dir))
    repo.append_artifact_checkpoint(enrolled["checkpoint"], enrolled["files"])
    repo.close()
    return authority, run_dir


def _request(run_dir, payload):
    return {
        "version": AUTHORITY_PROTOCOL_VERSION,
        "operation": "admit_task_answer",
        "worker_capability": None,
        "payload": {
            "run_dir": str(run_dir),
            "task_id": "fetch:r1",
            "raw_payload": payload,
        },
    }


def test_peer_identities_are_server_configured_and_strict():
    assert _parse_peer_identities([
        "1001=agent:codex",
        "1002=operator:test-operator",
    ]) == {
        1001: "agent:codex",
        1002: "operator:test-operator",
    }
    with pytest.raises(AuthorityError, match="duplicated"):
        _parse_peer_identities(["1001=agent:codex", "1001=operator:test-operator"])
    with pytest.raises(AuthorityError, match="CLASS:IDENTITY"):
        _parse_peer_identities(["1001=user:codex"])


def test_task_admission_requires_server_configured_peer_identity(tmp_path):
    authority, run_dir = _fixture(tmp_path)

    with pytest.raises(AuthorityPermissionError, match="identity is not configured"):
        dispatch_request(
            authority,
            _request(run_dir, {"found": False}),
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={},
        )

    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.get_task("fetch:r1").status == "pending"
    finally:
        repo.close()


def test_agent_identity_cannot_authorize_a_debug_override(tmp_path):
    authority, run_dir = _fixture(tmp_path)
    request = {
        "version": AUTHORITY_PROTOCOL_VERSION,
        "operation": "override",
        "worker_capability": None,
        "payload": {
            "run_dir": str(run_dir),
            "reason": "agent attempted self-approval",
        },
    }
    with pytest.raises(AuthorityPermissionError, match="capability"):
        dispatch_request(
            authority,
            request,
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={1001: "agent:codex"},
        )


def test_agent_file_is_copied_and_bound_to_authenticated_provenance(tmp_path):
    authority, run_dir = _fixture(tmp_path)
    supplied = tmp_path / "agent-paper.txt"
    supplied.write_text("agent supplied source", encoding="utf-8")

    admitted = dispatch_request(
        authority,
        _request(run_dir, {"found": True, "file_path": str(supplied)}),
        peer_uid=1001,
        trusted_writer_uids=frozenset(),
        peer_identities={1001: "agent:codex"},
    )

    repo = RunRepository.open(str(run_dir))
    try:
        answer = repo.get_latest_task_answer("fetch:r1")
        provenance = repo.get_task_answer_provenance(admitted["answer_id"])
        view = repo.task_view("fetch:r1")
    finally:
        repo.close()
    copied_path = view["answer"]["file_path"]
    assert answer.actor_type == "agent"
    assert os.path.commonpath((str(run_dir), copied_path)) == str(run_dir)
    assert copied_path != str(supplied)
    assert provenance["producer_class"] == "agent"
    assert provenance["producer_identity"] == "codex"
    assert provenance["authenticated_uid"] == 1001
    assert provenance["files"] == [{
        "original_name": "agent-paper.txt",
        "stored_path": os.path.relpath(copied_path, run_dir).replace(os.sep, "/"),
        "sha256": hashlib.sha256(b"agent supplied source").hexdigest(),
        "byte_count": len(b"agent supplied source"),
    }]
    supplied.write_text("changed after admission", encoding="utf-8")
    with open(copied_path, encoding="utf-8") as copied:
        assert copied.read() == "agent supplied source"

    with open(copied_path, "w", encoding="utf-8") as copied:
        copied.write("adulterated after admission")
    checked = authority.check(
        str(run_dir), authenticated_caller="agent:codex@uid:1001"
    )
    assert checked["status"] == "violated"
    assert checked["differences"] == [{
        "difference_kind": "changed",
        "logical_path": os.path.relpath(copied_path, run_dir).replace(os.sep, "/"),
    }]


def test_client_cannot_add_a_claimed_actor_to_admission_payload(tmp_path):
    authority, run_dir = _fixture(tmp_path)
    request = _request(run_dir, {"found": False})
    request["payload"]["actor"] = "user"

    with pytest.raises(AuthorityError, match="unsupported fields"):
        dispatch_request(
            authority,
            request,
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={1001: "agent:codex"},
        )


def test_missing_admitted_file_aborts_without_persisting_an_answer(tmp_path):
    authority, run_dir = _fixture(tmp_path)
    missing = run_dir.parent / "missing-source.txt"
    with pytest.raises(AuthorityError, match="source file is unavailable"):
        dispatch_request(
            authority,
            _request(run_dir, {"found": True, "file_path": str(missing)}),
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={1001: "agent:codex"},
        )
    repo = RunRepository.open(str(run_dir))
    try:
        assert repo.get_latest_task_answer("fetch:r1") is None
        assert repo.get_task("fetch:r1").status == "pending"
    finally:
        repo.close()
    assert authority.check(
        str(run_dir), authenticated_caller="agent:codex@uid:1001"
    )["status"] == "clean"


def test_admitted_source_accepts_windows_case_canonicalization(
    tmp_path, monkeypatch,
):
    source = tmp_path / "BrowserDownload.pdf"
    source.write_bytes(b"pdf")
    source_path = str(source)
    monkeypatch.setattr(admission.os.path, "realpath", lambda _path: source_path.upper())
    monkeypatch.setattr(admission.os.path, "normcase", lambda path: path.casefold())

    requested, descriptor, before = admission._open_admitted_source(source_path)
    try:
        assert requested == source_path
        assert before.st_size == 3
    finally:
        os.close(descriptor)


def test_admitted_source_does_not_depend_on_post_copy_metadata(tmp_path, monkeypatch):
    source = tmp_path / "browser-download.pdf"
    source.write_bytes(b"stable browser PDF")
    destination = tmp_path / "controlled-copy.pdf"
    real_fstat = admission.os.fstat
    calls = 0

    def initial_metadata_only(descriptor):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("post-copy metadata must not decide content stability")
        return real_fstat(descriptor)

    monkeypatch.setattr(admission.os, "fstat", initial_metadata_only)

    copied = admission._copy_one_file(
        str(source),
        str(destination),
        run_dir=str(tmp_path),
        remaining_answer_bytes=admission.MAX_ADMITTED_TOTAL_BYTES,
    )

    assert calls == 1
    assert destination.read_bytes() == b"stable browser PDF"
    assert copied["sha256"] == hashlib.sha256(b"stable browser PDF").hexdigest()


def test_admitted_source_opens_source_and_destination_in_binary_mode(
    tmp_path, monkeypatch,
):
    source = tmp_path / "browser-download.pdf"
    source.write_bytes(b"%PDF-1.4\nstream\r\ncontent\nendstream")
    destination = tmp_path / "controlled-copy.pdf"
    real_open = admission.os.open
    binary_flag = 1 << 29
    opened_flags = []

    def recording_open(path, flags, mode=0o777):
        opened_flags.append(flags)
        return real_open(path, flags & ~binary_flag, mode)

    monkeypatch.setattr(admission.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(admission.os, "open", recording_open)

    admission._copy_one_file(
        str(source),
        str(destination),
        run_dir=str(tmp_path),
        remaining_answer_bytes=admission.MAX_ADMITTED_TOTAL_BYTES,
    )

    assert len(opened_flags) == 2
    assert all(flags & binary_flag for flags in opened_flags)
    assert destination.read_bytes() == source.read_bytes()


def test_admitted_source_rejects_same_size_content_change(tmp_path, monkeypatch):
    source = tmp_path / "changing-browser-download.pdf"
    source.write_bytes(b"unstable browser PDF")
    destination = tmp_path / "controlled-copy.pdf"
    real_lseek = admission.os.lseek
    real_read = admission.os.read
    verifying = False
    changed = False

    def begin_verification(descriptor, offset, whence):
        nonlocal verifying
        verifying = True
        return real_lseek(descriptor, offset, whence)

    def changed_verification_read(descriptor, size):
        nonlocal changed
        chunk = real_read(descriptor, size)
        if verifying and chunk and not changed:
            changed = True
            return bytes([chunk[0] ^ 1]) + chunk[1:]
        return chunk

    monkeypatch.setattr(admission.os, "lseek", begin_verification)
    monkeypatch.setattr(admission.os, "read", changed_verification_read)

    with pytest.raises(AuthorityError, match="changed while copied"):
        admission._copy_one_file(
            str(source),
            str(destination),
            run_dir=str(tmp_path),
            remaining_answer_bytes=admission.MAX_ADMITTED_TOTAL_BYTES,
        )

    assert changed is True
    assert not destination.exists()


def test_admitted_source_rejects_failed_content_verification(tmp_path, monkeypatch):
    source = tmp_path / "browser-download.pdf"
    source.write_bytes(b"browser PDF")
    destination = tmp_path / "controlled-copy.pdf"
    monkeypatch.setattr(
        admission.os,
        "lseek",
        lambda *_args: (_ for _ in ()).throw(OSError("rewind failed")),
    )

    with pytest.raises(AuthorityError, match="could not be verified"):
        admission._copy_one_file(
            str(source),
            str(destination),
            run_dir=str(tmp_path),
            remaining_answer_bytes=admission.MAX_ADMITTED_TOTAL_BYTES,
        )

    assert not destination.exists()


def test_admitted_source_rejects_file_symlink(tmp_path):
    target = tmp_path / "target.pdf"
    target.write_bytes(b"pdf")
    link = tmp_path / "link.pdf"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(AuthorityError, match="must not use symlinks"):
        admission._open_admitted_source(str(link))


def test_admitted_source_rejects_symlinked_parent_directory(tmp_path):
    target_directory = tmp_path / "actual-downloads"
    target_directory.mkdir()
    source = target_directory / "source.pdf"
    source.write_bytes(b"pdf")
    linked_directory = tmp_path / "downloads"
    try:
        linked_directory.symlink_to(target_directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(AuthorityError, match="must not use symlinks"):
        admission._open_admitted_source(str(linked_directory / source.name))


def test_authority_provenance_failure_rolls_back_answer_and_task_state(
    tmp_path, monkeypatch,
):
    authority, run_dir = _fixture(tmp_path)
    monkeypatch.setattr(
        repository_module,
        "_insert_task_answer_provenance",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("provenance failed")),
    )

    with pytest.raises(RuntimeError, match="provenance failed"):
        dispatch_request(
            authority,
            _request(run_dir, {"found": False}),
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={1001: "agent:codex"},
        )

    repo = RunRepository.open_readonly(str(run_dir))
    try:
        assert repo.get_task("fetch:r1").status == "pending"
        assert repo.get_latest_task_answer("fetch:r1") is None
    finally:
        repo.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"found": False, "file_path": "unused.txt"},
        {"found": False, "unsupported": [{"file_path": "unused.txt"}]},
    ],
)
def test_invalid_answer_shape_is_rejected_before_file_copy(
    tmp_path, monkeypatch, payload
):
    authority, run_dir = _fixture(tmp_path)
    copied = []
    monkeypatch.setattr(
        admission,
        "_copy_one_file",
        lambda *args, **kwargs: copied.append((args, kwargs)),
    )

    with pytest.raises(ValueError):
        dispatch_request(
            authority,
            _request(run_dir, payload),
            peer_uid=1001,
            trusted_writer_uids=frozenset(),
            peer_identities={1001: "agent:codex"},
        )

    assert copied == []


def test_file_count_limit_is_checked_before_copy(tmp_path, monkeypatch):
    copied = []
    monkeypatch.setattr(
        admission,
        "_copy_one_file",
        lambda *args, **kwargs: copied.append((args, kwargs)),
    )
    payload = {
        "items": [
            {"file_path": f"unused-{index}.txt"}
            for index in range(admission.MAX_ADMITTED_FILES + 1)
        ]
    }

    with pytest.raises(AuthorityError, match="file-count limit"):
        admission._copy_payload_files(
            payload,
            run_dir=str(tmp_path),
            answer_id="answer-too-many",
        )

    assert copied == []


def test_aggregate_size_limit_is_checked_before_copy(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"aa")
    second.write_bytes(b"bb")
    copied = []
    monkeypatch.setattr(admission, "MAX_ADMITTED_TOTAL_BYTES", 3)
    monkeypatch.setattr(
        admission,
        "_copy_one_file",
        lambda *args, **kwargs: copied.append((args, kwargs)),
    )

    with pytest.raises(AuthorityError, match="aggregate size limit"):
        admission._copy_payload_files(
            {
                "items": [
                    {"file_path": str(first)},
                    {"file_path": str(second)},
                ]
            },
            run_dir=str(tmp_path),
            answer_id="answer-too-large",
        )

    assert copied == []
