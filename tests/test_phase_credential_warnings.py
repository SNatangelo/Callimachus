#!/usr/bin/env python3
# tests/test_phase_credential_warnings.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""End-of-phase warnings for rejected resolver/fetch API credentials."""

from tests._bootstrap import *  # noqa: F401,F403

from core.app.runtime import credentials as credential_runtime
from core.infra.db import RunRepository


def test_warning_lookup_uses_descriptor_identity_without_a_central_alias_map():
    specs = credential_runtime._credential_specs()
    provider, spec = credential_runtime._credential_spec_for_provider(
        "googlebooks", specs
    )
    assert provider == "google_books"
    assert spec == ("Google Books", ("GOOGLE_BOOKS_API_KEY",))


def _make_run(path: str) -> None:
    repo = RunRepository.create(
        path,
        run_id="run-credential-warning",
        input_path="input.pdf",
        input_sha256="abc123",
        accuracy="standard",
        style=None,
        model_id=None,
        http_profile="default",
        challenge_mode="off",
        fixture_fingerprint="fixture-1",
    )
    repo.replace_parse_payload(
        claims=[],
        references=[{"id": "r1", "ref_number": 1, "raw_entry": "Example"}],
        citations=[],
    )
    repo.close()


def test_resolve_warning_reports_env_name_without_secret(tmp_path, capsys):
    _make_run(str(tmp_path))
    repo = RunRepository.open(str(tmp_path))
    try:
        repo.upsert_resolve_result(
            "r1",
            {
                "ref_id": "r1",
                "status": "unresolved",
                "via": "core_search",
                "attempts": [{
                    "via": "core_search",
                    "status": "unresolved",
                    "reason": "HTTP 403",
                    "error_type": "auth",
                    "error_reason": "auth (HTTP 403)",
                    "retryable": False,
                    "http_status": 403,
                }],
            },
        )
    finally:
        repo.close()

    credential_runtime._print_phase_credential_warnings(
        str(tmp_path),
        "resolve",
        since="0000",
        environ={"CORE_API_KEY": "must-not-be-printed"},
    )
    warning = capsys.readouterr().err
    assert "CORE" in warning
    assert "CORE_API_KEY" in warning
    assert "HTTP 403" in warning
    assert "expired" in warning
    assert "must-not-be-printed" not in warning


def test_fetch_warning_uses_only_typed_provider_auth_diagnostics(tmp_path):
    _make_run(str(tmp_path))
    repo = RunRepository.open(str(tmp_path))
    try:
        repo.append_fetch_attempt(
            "r1",
            method="lens",
            url=None,
            kind="provider_diagnostic",
            final_url=None,
            status_code=401,
            content_type=None,
            outcome="provider_error",
            reason="auth (HTTP 401)",
            trace={
                "provider": "lens",
                "items": [],
                "status": "error",
                "error": None,
                "reason": None,
                "error_type": "auth",
                "error_reason": "auth (HTTP 401)",
                "retryable": False,
                "http_status": 401,
            },
        )
        repo.append_fetch_attempt(
            "r1",
            method="publisher",
            url="https://publisher.example/article",
            kind="landing",
            final_url="https://publisher.example/article",
            status_code=403,
            content_type="text/html",
            outcome="http_error",
            reason="access denied",
            trace=None,
        )
        repo.append_fetch_attempt(
            "r1",
            method="elsevier",
            url=None,
            kind="provider_diagnostic",
            final_url=None,
            status_code=403,
            content_type=None,
            outcome="provider_error",
            reason="Elsevier entitlement/access denied (HTTP 403)",
            trace={
                "provider": "elsevier",
                "items": [],
                "status": "error",
                "error": None,
                "reason": "Elsevier entitlement/access denied (HTTP 403)",
                "error_type": "http_error",
                "error_reason": "Elsevier entitlement/access denied (HTTP 403)",
                "retryable": False,
                "http_status": 403,
            },
        )
    finally:
        repo.close()

    rows = credential_runtime._phase_credential_rejections(
        str(tmp_path),
        "fetch",
        since="0000",
        environ={"LENS_API_KEY": "configured", "ELSEVIER_API_KEY": "configured"},
    )
    assert rows == [{
        "provider": "lens",
        "label": "Lens",
        "env_names": ("LENS_API_KEY",),
        "http_statuses": (401,),
    }]


def test_rate_limit_missing_key_and_old_auth_observation_do_not_warn(tmp_path):
    _make_run(str(tmp_path))
    repo = RunRepository.open(str(tmp_path))
    try:
        repo.append_fetch_attempt(
            "r1",
            method="core",
            url=None,
            kind="provider_diagnostic",
            final_url=None,
            status_code=429,
            content_type=None,
            outcome="provider_error",
            reason="rate_limit (HTTP 429)",
            trace={
                "provider": "core",
                "items": [],
                "status": "error",
                "error": None,
                "reason": None,
                "error_type": "rate_limit",
                "error_reason": "rate_limit (HTTP 429)",
                "retryable": True,
                "http_status": 429,
            },
        )
        repo.append_fetch_attempt(
            "r1",
            method="core",
            url=None,
            kind="provider_diagnostic",
            final_url=None,
            status_code=401,
            content_type=None,
            outcome="provider_error",
            reason="auth (HTTP 401)",
            trace={
                "provider": "core",
                "items": [],
                "status": "error",
                "error": None,
                "reason": None,
                "error_type": "auth",
                "error_reason": "auth (HTTP 401)",
                "retryable": False,
                "http_status": 401,
            },
        )
    finally:
        repo.close()

    assert credential_runtime._phase_credential_rejections(
        str(tmp_path), "fetch", since="9999", environ={"CORE_API_KEY": "set"}
    ) == []
    assert credential_runtime._phase_credential_rejections(
        str(tmp_path), "fetch", since="0000", environ={}
    ) == []


def test_phase_functions_call_warning_summary_at_their_normal_exit(monkeypatch):
    from core.app.phases import fetch as fetch_phase
    from core.app.phases import resolve as resolve_phase

    calls = []
    monkeypatch.setattr(
        resolve_phase,
        "_print_phase_credential_warnings",
        lambda run, phase, **kwargs: calls.append((run, phase, bool(kwargs.get("since")))),
    )
    monkeypatch.setattr(resolve_phase, "_load_parse_payload", lambda _run: {"references": []})
    monkeypatch.setattr(resolve_phase, "_resolve_manuscript_title", lambda _run: None)
    monkeypatch.setattr(resolve_phase._fetch, "new_fetch_run_context", lambda: object())
    assert resolve_phase.phase_resolve({
        "run_dir": "resolve-run",
        "debug_mode": False,
    }) == "fetch"

    monkeypatch.setattr(
        fetch_phase,
        "_print_phase_credential_warnings",
        lambda run, phase, **kwargs: calls.append((run, phase, bool(kwargs.get("since")))),
    )
    monkeypatch.setattr(fetch_phase._user_sources, "ensure_user_source_dirs", lambda _run: None)
    monkeypatch.setattr(fetch_phase, "_load_parse_payload", lambda _run: {"references": []})
    monkeypatch.setattr(fetch_phase, "_resolve_map", lambda _run: {})
    monkeypatch.setattr(fetch_phase, "_ingest_user_source_directories", lambda *_args: None)
    monkeypatch.setattr(fetch_phase, "_materialize_resolve_abstracts", lambda *_args: None)
    assert fetch_phase.phase_fetch({
        "run_dir": "fetch-run",
        "accuracy": "standard",
        "no_fetch": True,
    }) == "gaps"

    assert calls == [
        ("resolve-run", "resolve", True),
        ("fetch-run", "fetch", True),
    ]
