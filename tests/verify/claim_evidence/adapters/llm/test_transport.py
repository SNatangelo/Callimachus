# tests/verify/claim_evidence/adapters/llm/test_transport.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import os
import threading

import pytest

from core.verify.backends import _chat_transport as chat_transport
from core.verify.backends import deepseek, freetoken, groq, mistral, ollama, openai_compat, xai
from core.verify.backends.transport import call_registered_provider
from core.verify.backends.errors import (
    AuthError,
    BackendError,
    BackendTimeoutError,
    ConfigError,
    LaneError,
    RateLimitError,
    TransientError,
)
from core.verify.claim_evidence.adapters.llm.credentials import (
    Credential,
    CredentialResolver,
)
from core.verify.claim_evidence.adapters.llm.registry import (
    ProviderAdapter,
    ProviderRegistry,
    provider_registry,
)
from core.verify.claim_evidence.adapters.llm.transport import (
    AuditedTransport,
    ClaimEvidenceTransport,
)
from core.verify.claim_evidence.domain.fingerprint import (
    FINGERPRINT_VERSION_V2,
    answer_fingerprint,
    provider_prompt_fingerprint,
)
from core.verify.claim_evidence.contracts.jury1_flow import JURY1_SYSTEM_PROMPT
from core.verify.claim_evidence.contracts.jury2 import JURY2_SYSTEM_PROMPT
from core.verify.claim_evidence.domain.replies import (
    SupportGateAnswer,
    ContraryGateAnswer,
    ExplanationEvidenceAnswer,
    FullSupportGateAnswer,
    Jury2Answer,
    TopicGateAnswer,
)
from core.verify.claim_evidence.domain.types import (
    DispatchAssignment,
    LogicalRequest,
    TechnicalFailure,
)


SECRET = "secret-canary"
FINGERPRINT = hashlib.sha256(SECRET.encode()).hexdigest()


def _request(stage="support_gate", *, payload=None):
    jury1_payloads = {
        stage_name: (
            b'{"source_spans":[{"span_id":"s1","text":"Exact evidence"}],'
            b'"source_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"cited_source_mode":"full_text","claim":"C",'
            b'"claim_context":"Manuscript.","citation_marker":"[1]",'
            + (
                b'"task":{"task_id":"explanation_evidence","instructions":"Question."},'
                b'"determined_outcome":"supports"}'
                if stage_name == "explanation_evidence"
                else (
                    b'"task":{"task_id":"' + stage_name.encode("utf-8")
                    + b'","instructions":"Question."}}'
                )
            )
        )
        for stage_name in (
            "support_gate",
            "full_support_gate",
            "contrary_gate",
            "topic_gate",
            "explanation_evidence",
        )
    }
    defaults = {
        **jury1_payloads,
        "jury2": (
            b'{"asserted_relation":"basis","passage_subject":"source-backed role",'
            b'"selected_passages":[{"span_id":"s1","text":"Exact evidence"}]}'
        ),
    }
    payload = payload or defaults[stage]
    prompts = {
        **{stage_name: JURY1_SYSTEM_PROMPT for stage_name in jury1_payloads},
        "jury2": JURY2_SYSTEM_PROMPT,
    }
    return LogicalRequest(
        stage,
        f"{stage}-id",
        payload,
        provider_prompt_fingerprint(prompts[stage], payload),
        1,
        prompts[stage],
        "candidate" if stage == "jury2" else None,
    )

def _assignment(provider="fake"):
    alias = f"{provider}:1"
    return DispatchAssignment(
        provider,
        alias,
        FINGERPRINT,
        "model",
        (provider, alias, "model"),
        "dispatch-1",
    )


def _resolver(provider="fake"):
    alias = f"{provider}:1"
    return CredentialResolver(
        {alias: Credential(alias, FINGERPRINT, SECRET)}
    )


@pytest.mark.parametrize(
    ("stage", "raw_reply", "answer_type"),
    [
        ("support_gate", {"source_supports_any": True}, SupportGateAnswer),
        ("full_support_gate", {"source_supports_fully": True}, FullSupportGateAnswer),
        ("contrary_gate", {"paper_demonstrates_opposite": False}, ContraryGateAnswer),
        ("topic_gate", {"same_specific_subject": True}, TopicGateAnswer),
        (
            "explanation_evidence",
            {
                "reason": "selected",
                "supported_content": None,
                "unsupported_content": None,
                "incompatible_proposition": None,
                "evidence_span_ids": ["s1"],
                "non_decidable_reason": None,
            },
            ExplanationEvidenceAnswer,
        ),
    ],
)
def test_v10_jury1_stages_bind_source_claim_context_and_marker(
    stage, raw_reply, answer_type,
):
    request = _request(stage)
    payload = json.loads(request.payload)

    assert list(payload)[:3] == ["source_spans", "source_hash", "cited_source_mode"]
    assert payload["claim"] == "C"
    assert payload["claim_context"] == "Manuscript."
    assert payload["citation_marker"] == "[1]"
    assert payload["task"]["task_id"] == stage
    assert "citation_scope" not in payload
    assert "targets" not in payload
    if stage == "explanation_evidence":
        assert payload["determined_outcome"] == "supports"

    registry = ProviderRegistry()
    registry.register(ProviderAdapter("fake", lambda *_: json.dumps(raw_reply)))
    reply = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        request, _assignment(),
    )
    assert isinstance(reply, answer_type)


class _RecordingRunRepository:
    def __init__(self, payload_hash, *, fingerprint_version=FINGERPRINT_VERSION_V2):
        self._payload_hash = payload_hash
        self._fingerprint_version = fingerprint_version
        self.finished = None

    def fingerprint_version(self):
        return self._fingerprint_version

    def replay_scheduler_controls(self):
        return 0

    def replay_dispatch_observations(self):
        return 0

    def request_payload_hash(self, logical_request_id):
        return self._payload_hash

    def lease_dispatch(self, attempt):
        self.attempt = attempt

    def start_dispatch(self, *, logical_request_id, dispatch_attempt_id):
        self.started = (logical_request_id, dispatch_attempt_id)

    def finish_dispatch(self, **kwargs):
        self.finished = kwargs


class _RecordingRuntimeRepository:
    def replay_scheduler_controls(self):
        return 0

    def replay_dispatch_observations(self):
        return 0

    def apply_invalid_key_sync(self, *_args):
        raise AssertionError("credential failure must not mutate external state")


def test_transport_preserves_payload_bytes_and_non_secret_trace_context():
    seen = []
    registry = ProviderRegistry()

    def call(system, user, model, secret):
        seen.append((system, user, model, secret, dict(chat_transport._TRACE_CONTEXT.get())))
        return json.dumps({"source_supports_any": False})

    registry.register(ProviderAdapter("fake", call))
    request = _request()
    reply = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        request, _assignment()
    )
    assert isinstance(reply, SupportGateAnswer)
    assert seen[0][0] == request.system_prompt
    assert seen[0][1].encode() == request.payload
    assert seen[0][2] == "model"
    assert seen[0][3] == SECRET
    trace = seen[0][4]
    assert trace["logical_request_id"] == request.logical_request_id
    assert trace["dispatch_attempt_id"] == "dispatch-1"
    assert SECRET not in repr(trace)


def test_jury2_uses_the_same_provider_neutral_transport():
    registry = ProviderRegistry()
    registry.register(ProviderAdapter(
        "fake",
        lambda *_: json.dumps({
            "passages_fit_claim": False,
            "reason": "not enough",
        }),
    ))
    request = _request("jury2")
    payload = json.loads(request.payload)
    assert payload == {
        "asserted_relation": "basis",
        "passage_subject": "source-backed role",
        "selected_passages": [{"span_id": "s1", "text": "Exact evidence"}],
    }
    assert "outcome" not in payload
    reply = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        request, _assignment()
    )
    assert isinstance(reply, Jury2Answer)
    assert reply.decision.passages_fit_claim is False


@pytest.mark.parametrize(
    ("error", "cause", "retry_after"),
    [
        (RateLimitError(SECRET, retry_after=600), "rate_limited", 600),
        (AuthError(SECRET), "credential_invalid", None),
        (LaneError(SECRET), "lane_unavailable", None),
        (ConfigError(SECRET), "lane_unavailable", None),
        (BackendTimeoutError(SECRET), "timeout", None),
        (TransientError(SECRET), "transport", None),
        (BackendError(SECRET), "provider_failure", None),
    ],
)
def test_typed_backend_errors_are_technical_and_secret_free(
    error, cause, retry_after,
):
    registry = ProviderRegistry()

    def fail(*_args):
        raise error

    registry.register(ProviderAdapter("fake", fail))
    reply = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        _request(), _assignment()
    )
    assert isinstance(reply, TechnicalFailure)
    assert reply.cause == cause
    assert reply.retry_after_seconds == retry_after
    assert reply.detail is None
    assert SECRET not in repr(reply)


def test_registry_miss_and_malformed_answer_never_become_semantic():
    request = _request()
    missing = ClaimEvidenceTransport(
        ProviderRegistry(), _resolver()
    ).dispatch(request, _assignment())
    assert missing.cause == "lane_unavailable"

    registry = ProviderRegistry()
    registry.register(ProviderAdapter("fake", lambda *_: "not-json"))
    malformed = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        request, _assignment()
    )
    assert malformed.cause == "provider_failure"


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        ("", "response_content_empty"),
        ("not-json", "json_object_missing"),
        ('{"source_supports_any": true} {"source_supports_any": false}',
         "json_object_ambiguous"),
    ],
)
def test_contract_invalid_payload_is_audited_as_protocol_invalid(
    raw, expected_code,
):
    registry = ProviderRegistry()
    registry.register(ProviderAdapter("fake", lambda *_: raw))
    request = _request()
    assignment = _assignment()
    run_repo = _RecordingRunRepository(request.payload_fingerprint)
    runtime_repo = _RecordingRuntimeRepository()

    result = AuditedTransport(
        ClaimEvidenceTransport(registry, _resolver()),
        run_repo,
        runtime_repo,
        selection_hash="selection-hash",
        pacing_hash="pacing-hash",
    ).dispatch(request, assignment)

    assert isinstance(result, TechnicalFailure)
    assert result.cause == "provider_failure"
    assert result.detail == "protocol_invalid"
    assert result.protocol_error_code == expected_code
    assert len(result.response_hash or "") == 64
    assert run_repo.finished["result"] == "failed"
    assert run_repo.finished["technical_result"] == "protocol_invalid"
    assert run_repo.finished["retry_cause"] == "provider_failure"
    assert run_repo.finished["protocol_error_code"] == expected_code
    assert run_repo.finished["response_hash"] == result.response_hash
    assert run_repo.finished["answer"] is None


def test_credential_invalid_never_invokes_external_environment_sync():
    registry = ProviderRegistry()

    def invalid_credential(*_args):
        raise AuthError("invalid credential")

    registry.register(ProviderAdapter("fake", invalid_credential))
    request = _request()
    run_repo = _RecordingRunRepository(request.payload_fingerprint)

    result = AuditedTransport(
        ClaimEvidenceTransport(registry, _resolver()),
        run_repo,
        _RecordingRuntimeRepository(),
        selection_hash="selection-hash",
        pacing_hash="pacing-hash",
    ).dispatch(request, _assignment())

    assert isinstance(result, TechnicalFailure)
    assert result.cause == "credential_invalid"
    assert run_repo.finished["technical_result"] == "credential_invalid"


def test_single_fenced_json_object_is_audited_as_an_answer():
    registry = ProviderRegistry()
    raw = json.dumps({"source_supports_any": False})
    registry.register(ProviderAdapter("fake", lambda *_: f"```json\n{raw}\n```"))
    request = _request()
    assignment = _assignment()
    run_repo = _RecordingRunRepository(request.payload_fingerprint)

    result = AuditedTransport(
        ClaimEvidenceTransport(registry, _resolver()),
        run_repo,
        _RecordingRuntimeRepository(),
        selection_hash="selection-hash",
        pacing_hash="pacing-hash",
    ).dispatch(request, assignment)

    assert isinstance(result, SupportGateAnswer)
    assert run_repo.finished["result"] == "completed"
    assert run_repo.finished["technical_result"] == "answer_received"
    assert run_repo.finished["answer_hash"]
    assert run_repo.finished["answer"]["decision"]["source_supports_any"] is False


def test_explanation_evidence_answer_uses_one_list_canonical_form_for_v2_hash_and_storage():
    registry = ProviderRegistry()
    registry.register(
        ProviderAdapter(
            "fake",
            lambda *_: json.dumps(
                {
                    "reason": "The passage directly supports the citation-linked use.",
                    "supported_content": None,
                    "unsupported_content": None,
                    "incompatible_proposition": None,
                    "evidence_span_ids": ["s1"],
                    "non_decidable_reason": None,
                }
            ),
        )
    )
    request = _request("explanation_evidence")
    run_repo = _RecordingRunRepository(
        request.payload_fingerprint,
        fingerprint_version=FINGERPRINT_VERSION_V2,
    )
    result = AuditedTransport(
        ClaimEvidenceTransport(registry, _resolver()),
        run_repo,
        _RecordingRuntimeRepository(),
        selection_hash="selection-hash",
        pacing_hash="pacing-hash",
    ).dispatch(request, _assignment())

    assert isinstance(result, ExplanationEvidenceAnswer)
    payload = run_repo.finished["answer"]
    assert payload["decision"]["evidence_span_ids"] == ["s1"]
    assert run_repo.finished["answer_hash"] == answer_fingerprint(
        payload, version=FINGERPRINT_VERSION_V2
    )

def test_invalid_payload_fingerprint_fails_before_provider_call():
    calls = []
    registry = ProviderRegistry()
    registry.register(ProviderAdapter("fake", lambda *_: calls.append(1) or "{}"))
    request = _request()
    request = LogicalRequest(
        request.stage,
        request.logical_request_id,
        request.payload,
        "0" * 64,
        request.candidate_cycle,
        request.system_prompt,
    )
    reply = ClaimEvidenceTransport(registry, _resolver()).dispatch(
        request, _assignment()
    )
    assert reply.cause == "provider_failure"
    assert calls == []


def test_openai_compatible_omits_max_tokens_when_budget_is_none(monkeypatch):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(url=url, body=body, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(openai_compat, "get_json_post", lambda: fake_post)
    adapter = provider_registry(
        ("openai_compatible",), max_tokens=None, reasoning="off"
    ).get("openai_compatible")

    assert adapter.call(
        "system", "user", "deepseek-v4-flash", SECRET
    ) == "{}"
    assert "max_tokens" not in observed["body"]
    assert observed["body"]["thinking"] == {"type": "disabled"}
    assert observed["body"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_auto_disables_thinking_for_deepseek_json(monkeypatch):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(body=body)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(openai_compat, "get_json_post", lambda: fake_post)
    adapter = provider_registry(
        ("openai_compatible",), reasoning="auto"
    ).get("openai_compatible")

    assert adapter.call(
        "system", "user", "deepseek-v4-flash", SECRET
    ) == "{}"
    assert observed["body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in observed["body"]
    assert observed["body"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_preserves_explicit_deepseek_thinking(monkeypatch):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(body=body)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(openai_compat, "get_json_post", lambda: fake_post)
    adapter = provider_registry(
        ("openai_compatible",),
        reasoning="on",
        reasoning_effort="medium",
    ).get("openai_compatible")

    assert adapter.call(
        "system", "user", "deepseek-v4-flash", SECRET
    ) == "{}"
    assert observed["body"]["thinking"] == {"type": "enabled"}
    assert observed["body"]["reasoning_effort"] == "high"
    assert observed["body"]["response_format"] == {"type": "json_object"}


def test_openai_deepseek_groq_and_xai_calls_keep_credentials_and_routes_isolated(monkeypatch):
    environment = {
        "OPENAI_API_KEY": "ambient-openai-key",
        "OPENAI_MODEL": "ambient-openai-model",
        "OPENAI_BASE_URL": "https://custom.example/v1",
        "DEEPSEEK_API_KEY": "ambient-deepseek-key",
        "DEEPSEEK_MODEL": "ambient-deepseek-model",
        "GROQ_API_KEY": "ambient-groq-key",
        "GROQ_MODEL": "ambient-groq-model",
        "XAI_API_KEY": "ambient-xai-key",
        "XAI_MODEL": "ambient-xai-model",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    observed = []

    def fake_post(url, body, headers, *, timeout):
        observed.append((url, body, headers))
        if url.endswith("/responses"):
            return {"output_text": "ok"}
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(chat_transport, "_json_post", fake_post)
    providers = (
        ("openai", "https://api.openai.com/v1/responses", "OPENAI_API_KEY"),
        (
            "openai_compatible", "https://custom.example/v1/chat/completions",
            "OPENAI_API_KEY",
        ),
        ("deepseek", f"{deepseek.DEEPSEEK_BASE_URL}/chat/completions", "DEEPSEEK_API_KEY"),
        ("groq", f"{groq.GROQ_BASE_URL}/chat/completions", "GROQ_API_KEY"),
        ("xai", f"{xai.XAI_BASE_URL}/chat/completions", "XAI_API_KEY"),
    )

    for provider, expected_url, key_env in providers:
        selected_key = f"selected-{provider}-key"
        selected_model = f"selected-{provider}-model"
        assert call_registered_provider(
            provider, "system", "user", selected_model, selected_key,
        ) == "ok"
        url, body, headers = observed[-1]
        assert url == expected_url
        assert headers["Authorization"] == f"Bearer {selected_key}"
        assert body["model"] == selected_model
        assert os.environ[key_env] == environment[key_env]
        assert {name: os.environ[name] for name in environment} == environment


def test_xai_requires_its_own_key_when_openai_and_groq_keys_exist(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")
    monkeypatch.setenv("GROQ_API_KEY", "ambient-groq-key")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(chat_transport, "_json_post", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ConfigError, match="XAI_API_KEY not set"):
        call_registered_provider("xai", "system", "user", "grok-model", "")

    assert calls == []


def test_ollama_local_chat_disables_thinking_and_uses_message_content(monkeypatch):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(url=url, body=body, headers=headers, timeout=timeout)
        return {
            "message": {
                "content": "  final answer  ",
                "thinking": "non-authoritative reasoning",
            },
            "response": "deprecated fallback",
        }

    monkeypatch.setenv("OLLAMA_HOST", "localhost:11434/")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(ollama, "get_json_post", lambda: fake_post)

    assert ollama._call_ollama_api("system", "user", "qwen3.5:9b", 256) == "final answer"
    assert observed == {
        "url": "http://localhost:11434/api/chat",
        "body": {
            "model": "qwen3.5:9b",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            "stream": False,
            "think": False,
            "options": {"num_predict": 256, "temperature": 0},
        },
        "headers": {"Content-Type": "application/json"},
        "timeout": ollama._ollama_timeout(),
    }


@pytest.mark.parametrize(
    ("model", "expected_model"),
    [("glm-5.1-cloud", "glm-5.1"), ("glm-5.1:cloud", "glm-5.1")],
)
def test_ollama_cloud_keeps_chat_endpoint_and_model_normalization(
    monkeypatch, model, expected_model,
):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(url=url, body=body, headers=headers, timeout=timeout)
        return {"message": {"content": "answer"}}

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", SECRET)
    monkeypatch.setattr(ollama, "get_json_post", lambda: fake_post)

    assert ollama._call_ollama_api("system", "user", model, 123) == "answer"
    assert observed["url"] == "https://ollama.com/api/chat"
    assert observed["body"]["model"] == expected_model
    assert observed["body"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert observed["body"]["stream"] is False
    assert observed["body"]["options"] == {"num_predict": 123, "temperature": 0}
    assert "think" not in observed["body"]


@pytest.mark.parametrize(
    ("host", "max_tokens", "expected_url"),
    [
        (None, 123, "http://127.0.0.1:1919/v1/chat/completions"),
        ("localhost:2919/", None, "http://localhost:2919/v1/chat/completions"),
    ],
)
def test_freetoken_uses_credentialless_openai_compatible_transport(
    monkeypatch, host, max_tokens, expected_url,
):
    observed = {}

    def fake_post(url, body, headers, *, timeout):
        observed.update(url=url, body=body, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": "  result  "}}]}

    monkeypatch.delenv("FREETOKEN_HOST", raising=False)
    monkeypatch.setenv("FREETOKEN_MODEL", "ft-model")
    if host is not None:
        monkeypatch.setenv("FREETOKEN_HOST", host)
    monkeypatch.setattr(freetoken, "get_json_post", lambda: fake_post)
    adapter = provider_registry(("freetoken",), max_tokens=max_tokens).get(
        "freetoken"
    )

    assert adapter.call("system", "user", "selected-model", None) == "result"
    assert observed == {
        "url": expected_url,
        "body": {
            "model": "selected-model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            **({"max_tokens": max_tokens} if max_tokens is not None else {}),
            "temperature": 0,
            "stream": False,
        },
        "headers": {"Content-Type": "application/json"},
        "timeout": chat_transport.llm_timeout(),
    }
    with pytest.raises(ValueError, match="credentialless backend received a secret"):
        adapter.call("system", "user", "selected-model", SECRET)


def test_registered_bridge_injects_one_key_context_and_single_attempt(monkeypatch):
    observed = {}
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("CITATION_VERIFIER_REASONING", "on")
    monkeypatch.setenv("CITATION_VERIFIER_REASONING_EFFORT", "low")

    def fake_call(system, user, model, max_tokens):
        observed.update({
            "single": chat_transport._SINGLE_PHYSICAL_ATTEMPT.get(),
            "secret": chat_transport.credential_value("MISTRAL_API_KEY"),
            "trace": dict(chat_transport._TRACE_CONTEXT.get()),
            "max_tokens": max_tokens,
            "reasoning": chat_transport.reasoning_mode(),
            "reasoning_effort": chat_transport.reasoning_effort(),
        })
        raise RateLimitError("rate", retry_after=600)

    monkeypatch.setattr(mistral.SPEC, "call", fake_call)
    assignment = _assignment("mistral")
    reply = ClaimEvidenceTransport(
        provider_registry(
            ("mistral",),
            max_tokens=321,
            reasoning="off",
            reasoning_effort="high",
        ),
        _resolver("mistral"),
    ).dispatch(_request(), assignment)
    assert reply.cause == "rate_limited"
    assert reply.retry_after_seconds == 600
    assert observed["single"] is True
    assert observed["secret"] == SECRET
    assert observed["max_tokens"] == 321
    assert observed["reasoning"] == "off"
    assert observed["reasoning_effort"] == "high"
    assert SECRET not in repr(observed["trace"])
    assert chat_transport.credential_value("MISTRAL_API_KEY") is None
    assert chat_transport.reasoning_mode() == "on"
    assert chat_transport.reasoning_effort() == "low"


def test_concurrent_registered_calls_do_not_cross_reasoning_contexts(monkeypatch):
    barrier = threading.Barrier(2)
    observed = {}

    def fake_call(system, user, model, max_tokens):
        barrier.wait(timeout=1)
        observed[model] = (
            chat_transport.reasoning_mode(),
            chat_transport.reasoning_effort(),
        )
        return "{}"

    monkeypatch.setattr(mistral.SPEC, "call", fake_call)

    def invoke(model, mode, effort):
        adapter = provider_registry(
            ("mistral",), reasoning=mode, reasoning_effort=effort
        ).get("mistral")
        adapter.call("system", "user", model, SECRET)

    workers = (
        threading.Thread(target=invoke, args=("model-off", "off", "low")),
        threading.Thread(target=invoke, args=("model-on", "on", "xhigh")),
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert not any(worker.is_alive() for worker in workers)
    assert observed == {
        "model-off": ("off", "low"),
        "model-on": ("on", "xhigh"),
    }


def test_concurrent_registered_calls_do_not_cross_credential_contexts(monkeypatch):
    barrier = threading.Barrier(2)
    observed = []

    def fake_call(system, user, model, max_tokens):
        barrier.wait(timeout=1)
        observed.append(chat_transport.credential_value("MISTRAL_API_KEY"))
        return json.dumps({"source_supports_any": False})

    monkeypatch.setattr(mistral.SPEC, "call", fake_call)
    secrets = ("first-secret", "second-secret")
    credentials = {}
    assignments = []
    for index, secret in enumerate(secrets, 1):
        alias = f"mistral:{index}"
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()
        credentials[alias] = Credential(alias, fingerprint, secret)
        assignments.append(DispatchAssignment(
            "mistral", alias, fingerprint, "model",
            ("mistral", alias, "model"), f"dispatch-{index}",
        ))
    transport = ClaimEvidenceTransport(
        provider_registry(("mistral",)),
        CredentialResolver(credentials),
    )
    replies = []
    workers = [
        threading.Thread(
            target=lambda assignment=assignment: replies.append(
                transport.dispatch(_request(), assignment)
            )
        )
        for assignment in assignments
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1)
    assert all(isinstance(reply, SupportGateAnswer) for reply in replies)
    assert sorted(observed) == sorted(secrets)
