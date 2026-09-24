# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO

from core.app import run as driver
from core.app.commands import configure
from core.infra.integrity import signing


def test_missing_signing_key_does_not_block_an_otherwise_configured_run(
    monkeypatch,
):
    for name in (
        "CITATION_VERIFIER_MAILTO",
        "GOOGLE_BOOKS_API_KEY",
        "CORE_API_KEY",
        "CITATION_VERIFIER_ACCURACY",
        "CITATION_VERIFIER_HTTP_PROFILE",
        "CITATION_VERIFIER_CHALLENGE_MODE",
        "CITATION_VERIFIER_OCR_LANG",
        "CITATION_VERIFIER_STATE_DIR",
        "CITATION_VERIFIER_SIGNING_KEY",
        "CITATION_VERIFIER_SIGNING_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "test-key")
    monkeypatch.setattr(
        driver._startup_preflight, "key_status_rows", lambda *args, **kwargs: []
    )
    output = StringIO()

    with redirect_stdout(output):
        result = driver._resolve_config(Namespace(
            accuracy="standard",
            mailto="operator@example.test",
            proceed=False,
            http_profile=None,
            challenge_mode=None,
        ))

    assert result[:3] == ("standard", "operator@example.test", True)
    assert "trusted HMAC is not enabled in this process" in output.getvalue()


def test_generated_signing_key_is_private_idempotent_and_verifiable(tmp_path):
    key_path = tmp_path / "nested" / "signing.key"
    configure.generate_key(str(key_path))

    mode = key_path.stat().st_mode & 0o777
    if os.name != "nt":
        probe = tmp_path / "chmod-probe"
        probe.write_text("probe", encoding="utf-8")
        try:
            probe.chmod(0o600)
        except OSError:
            supports_posix_chmod = False
        else:
            supports_posix_chmod = (probe.stat().st_mode & 0o777) == 0o600
        if supports_posix_chmod:
            assert mode == 0o600

    first = key_path.read_text(encoding="ascii")
    configure.generate_key(str(key_path))
    assert key_path.read_text(encoding="ascii") == first

    configure.generate_key(str(key_path), overwrite=True)
    replacement = key_path.read_text(encoding="ascii")
    assert replacement != first
    seal = signing.sign(b"deployment-key-check", key=replacement.strip())
    assert seal["alg"] == "hmac-sha256"
    assert signing.verify(
        b"deployment-key-check", seal["alg"], seal["sig"], key=replacement.strip()
    )
