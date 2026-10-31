# tests/test_driver_lease.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for durable verify-run ownership and projection storage."""

from tests._bootstrap import *  # noqa: F401,F403

from core.infra.db import RunRepository


def _create_repo(path):
    return RunRepository.create(
        path,
        run_id="run-driver-lease",
        input_path="paper.pdf",
        input_sha256="abc",
        accuracy="standard",
        style=None,
        model_id="deepseek-v4-flash",
        http_profile="plain",
        challenge_mode="off",
        fixture_fingerprint="fixture",
    )


class TestDriverLease(unittest.TestCase):
    def test_second_live_driver_cannot_claim_same_run(self):
        with tempfile.TemporaryDirectory() as run:
            first = _create_repo(run)
            try:
                session = first.claim_driver_session(pid=101, host="host-a")
                self.assertIsNotNone(session)
                second = RunRepository.open(run)
                try:
                    self.assertIsNone(second.claim_driver_session(pid=202, host="host-b"))
                finally:
                    second.close()
                first.end_session(session, status="interrupted")
                second = RunRepository.open(run)
                try:
                    self.assertIsNotNone(second.claim_driver_session(pid=202, host="host-b"))
                finally:
                    second.close()
            finally:
                first.close()


def test_exited_driver_reconciliation_requires_exact_captured_identity():
    with tempfile.TemporaryDirectory() as run:
        repo = _create_repo(run)
        try:
            session = repo.claim_driver_session(pid=101, host="host-a")
            assert session
            for identity in (("wrong", 101, "host-a"), (session, 102, "host-a"),
                             (session, 101, "host-b")):
                assert not repo.mark_exited_driver_interrupted(
                    session_id=identity[0], pid=identity[1], host=identity[2]
                )
                assert repo.get_run().status == "active"
            assert repo.mark_exited_driver_interrupted(
                session_id=session, pid=101, host="host-a"
            )
            assert repo.get_run().status == "interrupted"
            assert repo.get_active_session() is None
            assert not repo.mark_exited_driver_interrupted(
                session_id=session, pid=101, host="host-a"
            )
        finally:
            repo.close()


def test_exited_driver_reconciliation_does_not_close_replacement_session():
    with tempfile.TemporaryDirectory() as run:
        repo = _create_repo(run)
        try:
            first = repo.claim_driver_session(pid=101, host="host-a")
            repo.end_session(first, status="interrupted")
            replacement = repo.claim_driver_session(pid=202, host="host-a")
            assert replacement
            assert not repo.mark_exited_driver_interrupted(
                session_id=first, pid=101, host="host-a"
            )
            assert repo.get_run().status == "active"
            assert repo.get_active_session().session_id == replacement
        finally:
            repo.close()
