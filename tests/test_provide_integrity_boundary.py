# tests/test_provide_integrity_boundary.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""The legacy source CLI must not bypass trusted external-input admission."""

from __future__ import annotations

import sys
from unittest import mock

import pytest

from core.resolve import provide


@pytest.mark.parametrize("command,arguments", [
    ("ingest", ["--run", "run", "--dir", "sources"]),
    ("map", ["--run", "run", "--ref", "r1", "--file", "paper.txt"]),
    (
        "record",
        [
            "--run", "run", "--ref", "r1", "--tier", "fulltext",
            "--origin", "manual",
        ],
    ),
])
def test_mutating_provide_commands_direct_users_to_controlled_task_ingress(
    command, arguments
):
    with mock.patch.object(
        sys, "argv", ["provide.py", command, *arguments]
    ), mock.patch.object(provide, f"cmd_{command}") as mutate:
        with pytest.raises(SystemExit, match="trusted authority"):
            provide.main()
    mutate.assert_not_called()
