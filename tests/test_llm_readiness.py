# tests/test_llm_readiness.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused tests for the isolated pre-run Verify response probe."""
from __future__ import annotations

import os

import pytest

from core.app.providers import readiness as llm_readiness


def _freetoken_env(*, models="model-one", keys="key-one", jury2="off"):
    return {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "freetoken_authenticated",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": jury2,
        "FREETOKEN_MODEL": models,
        "FREETOKEN_API_KEY": keys,
    }


def test_worker_checks_a_selected_model_and_accepts_nonempty_response(monkeypatch):
    calls = []

    def call(provider, system, user, model, secret, **kwargs):
        calls.append((provider, model, secret, kwargs))
        return "  OK  "

    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider", call)

    result = llm_readiness._worker_check(_freetoken_env())

    assert result["ok"] is True
    assert result["checked"] == ["freetoken_authenticated:model-one"]
    assert len(calls) == 1
    assert calls[0][0:3] == ("freetoken_authenticated", "model-one", "key-one")


@pytest.mark.parametrize("raw", [" \n ", "{}", '{"error":"provider error"}', '{"is_error":true}'])
def test_worker_rejects_an_empty_provider_response(monkeypatch, raw):
    monkeypatch.setattr(
        "core.verify.backends.transport.call_registered_provider",
        lambda *_args, **_kwargs: raw,
    )

    result = llm_readiness._worker_check(_freetoken_env())

    assert result["ok"] is False
    assert result["checked"] == []
    assert "usable response" in result["message"]


@pytest.mark.parametrize("field,value", [
    ("FREETOKEN_HOST", "https://user:password@example.test"),
    ("FREETOKEN_MODEL", "--unsafe"),
    ("FREETOKEN_API_KEY", "key\nInjected: x"),
])
def test_readiness_rejects_unsafe_config_before_dispatch(monkeypatch, field, value):
    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider",
                        lambda *args, **kwargs: pytest.fail("unsafe config must not dispatch"))
    env = _freetoken_env()
    env[field] = value
    assert llm_readiness._worker_check(env)["ok"] is False


def test_unselected_provider_does_not_block_remaining_juries(monkeypatch):
    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider",
                        lambda *args, **kwargs: "OK")
    env = _freetoken_env()
    env["OPENAI_BASE_URL"] = "not a valid URL"
    assert llm_readiness._worker_check(env)["ok"] is True


@pytest.mark.parametrize(
    ("status", "expected_message"),
    [
        (401, "rejected or has expired"),
        (403, "rejected or lacks permission"),
    ],
)
def test_worker_redacts_secrets_from_auth_failures(monkeypatch, status, expected_message):
    secret = "test-secret-must-not-escape"

    def reject(*_args, **_kwargs):
        raise RuntimeError(f"HTTP {status} rejected Authorization: Bearer {secret}")

    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider", reject)

    result = llm_readiness._worker_check(_freetoken_env(keys=secret))

    assert result["ok"] is False
    assert result["status_code"] == status
    assert expected_message in result["message"]
    assert secret not in repr(result)


def test_worker_checks_each_active_model_with_its_credential_once(monkeypatch):
    calls = []

    def call(provider, _system, _user, model, secret, **_kwargs):
        calls.append((provider, model, secret))
        return "OK"

    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider", call)

    result = llm_readiness._worker_check(_freetoken_env(
        models="model-one,model-two", keys="key-one,key-two", jury2="medium",
    ))

    assert result["ok"] is True
    assert result["checked"] == [
        "freetoken_authenticated:model-one",
        "freetoken_authenticated:model-two",
    ]
    assert calls == [
        ("freetoken_authenticated", "model-one", "key-one"),
        ("freetoken_authenticated", "model-two", "key-two"),
    ]


def test_worker_skips_jury2_only_lanes_when_jury2_is_off(monkeypatch):
    calls = []

    def call(provider, _system, _user, model, secret, **_kwargs):
        calls.append((provider, model, secret))
        return "OK"

    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider", call)
    env = _freetoken_env(models="model-one,model-two", keys="key-one,key-two")
    env["CITATION_VERIFIER_VERIFY_JURY1_ONLY"] = "freetoken_authenticated:model-one"
    env["CITATION_VERIFIER_VERIFY_JURY2_ONLY"] = "freetoken_authenticated:model-two"

    result = llm_readiness._worker_check(env)

    assert result["ok"] is True
    assert result["checked"] == ["freetoken_authenticated:model-one"]
    assert calls == [("freetoken_authenticated", "model-one", "key-one")]


def test_worker_skips_cleanly_when_no_verify_backends_are_selected(monkeypatch):
    def unexpected_call(*_args, **_kwargs):
        pytest.fail("no-backend readiness must not call a provider")

    monkeypatch.setattr("core.verify.backends.transport.call_registered_provider", unexpected_call)

    assert llm_readiness._worker_check({}) == {"ok": True, "checked": []}


def test_parent_uses_an_isolated_child_environment_snapshot(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, argv, environ):
            captured["argv"] = argv
            captured["environ"] = dict(environ)
            self.deadline = 1.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def document(self):
            return {"ok": True, "checked": ["freetoken_authenticated:model-one"]}

    monkeypatch.setattr(llm_readiness, "_Session", FakeSession)
    monkeypatch.setattr(llm_readiness, "application_argv", lambda flag: ["app.exe", flag])
    monkeypatch.setenv("FREETOKEN_API_KEY", "parent-secret")
    supplied = _freetoken_env(keys="run-secret")

    result = llm_readiness.check_verify_readiness(supplied)

    assert result["ok"] is True
    assert captured["argv"] == ["app.exe", "--jury-readiness-probe"]
    assert captured["environ"]["FREETOKEN_API_KEY"] == "run-secret"
    assert captured["environ"]["CITATION_VERIFIER_LLM_TIMEOUT"] == "15"
    assert captured["environ"]["PYTHONIOENCODING"] == "utf-8"
    assert supplied["FREETOKEN_API_KEY"] == "run-secret"
    assert os.environ["FREETOKEN_API_KEY"] == "parent-secret"
