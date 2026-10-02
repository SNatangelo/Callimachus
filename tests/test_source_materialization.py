# tests/test_source_materialization.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression test for Windows path containment in source materialization."""

from tests._bootstrap import *  # noqa: F401,F403
import ntpath
from types import SimpleNamespace
import pytest
from core.infra.db import RunRepository
from core.infra.db import repository as db_repository

def _repo(run):
    repo = RunRepository.create(
        run, run_id="materialization-run", input_path="input", input_sha256="sha",
        accuracy="standard", style=None, model_id=None, http_profile="default",
        challenge_mode="off", fixture_fingerprint="fixture",
    )
    return repo

@pytest.mark.parametrize(
    ("run_dir", "real_target", "escapes"),
    [
        (r"C:\runs\materialization-run", r"\\?\C:\runs\materialization-run\sources\parsed\article.txt", False),
        (r"\\server\share\materialization-run", r"\\?\UNC\server\share\materialization-run\sources\parsed\article.txt", False),
        (r"C:\runs\materialization-run", r"C:\outside\article.txt", True),
        (r"C:\runs\materialization-run", r"\\?\C:\outside\article.txt", True),
    ],
)
def test_materialization_path_handles_equivalent_windows_spelling_and_contains_targets(
    tmp_path, monkeypatch, run_dir, real_target, escapes,
):
    repo = _repo(str(tmp_path))

    class WindowsPaths:
        join = staticmethod(ntpath.join)
        commonpath = staticmethod(ntpath.commonpath)
        normcase = staticmethod(ntpath.normcase)
        normpath = staticmethod(ntpath.normpath)

        @staticmethod
        def realpath(path):
            return run_dir if path == run_dir else real_target

    monkeypatch.setattr(db_repository, "os", SimpleNamespace(name="nt", path=WindowsPaths))
    monkeypatch.setattr(repo, "_run_dir", run_dir)
    try:
        stored_path = "sources/parsed/article.txt"
        if escapes:
            with pytest.raises(RuntimeError, match="escapes run directory"):
                repo._materialization_path(stored_path)
        else:
            assert repo._materialization_path(stored_path) == (stored_path, real_target)
    finally:
        repo.close()
