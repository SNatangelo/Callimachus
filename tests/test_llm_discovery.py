# tests/test_llm_discovery.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import io
import json
import os
import socket
import urllib.error
from unittest import mock

import pytest

from core.app.providers import discovery


class _Response:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.payload[:size]


@pytest.mark.parametrize("host", ["0.0.0.0", "0.0.0.0:11434", "127.0.0.1", "localhost:11434"])
def test_native_ollama_bind_host_checks_the_local_model_catalog(monkeypatch, host):
    urls = []
    monkeypatch.setattr(discovery, "_open_json", lambda url, _headers: urls.append(url) or {"models": [{"name": "local-model"}]})
    result = discovery.check_provider("ollama", {"OLLAMA_HOST": host})
    assert result["ok"] is True and result["models"] == ["local-model"]
    assert urls == ["http://localhost:11434/api/tags" if host.startswith("localhost") else "http://127.0.0.1:11434/api/tags"]


def test_ollama_generation_uses_the_same_canonical_loopback_host(monkeypatch):
    from core.verify.backends import ollama
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(ollama, "get_json_post", lambda: lambda url, *_args, **_kwargs: calls.append(url) or {"message": {"content": "ok"}})
    assert ollama._is_loopback_host("0.0.0.0")
    assert ollama._call_ollama_local("system", "user", "local-model") == "ok"
    assert calls == ["http://127.0.0.1:11434/api/chat"]


def test_codex_catalog_keeps_new_models_available_without_selecting_them(monkeypatch):
    from core.app.providers import cli_discovery

    monkeypatch.setattr(cli_discovery, "cli_catalog", lambda *_args: {
        "ok": True, "installed": True, "authenticated": True,
        "models": ["gpt-6-luna", "gpt-5.6-luna"], "message": "synthetic",
    })
    result = discovery.check_provider("codex_cli", {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "codex_cli",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CODEX_CLI_MODEL": "gpt-6-luna",
    })

    assert result["ok"] is True
    assert [
        (item["selector"], item["jury1_selected"], item["jury2_selected"])
        for item in result["options"]
    ] == [
        ("codex_cli:gpt-6-luna", True, True),
        ("codex_cli:gpt-5.6-luna", False, False),
    ]


def test_codex_catalog_preserves_independent_role_selections(monkeypatch):
    from core.app.providers import cli_discovery

    monkeypatch.setattr(cli_discovery, "cli_catalog", lambda *_args: {
        "ok": True, "installed": True, "authenticated": True,
        "models": ["model-one", "model-two", "new-model"],
        "message": "synthetic",
    })
    result = discovery.check_provider("codex_cli", {
        "CITATION_VERIFIER_VERIFY_BACKENDS": "codex_cli",
        "CITATION_VERIFIER_VERIFY_JURY1_ONLY": "codex_cli:model-one",
        "CITATION_VERIFIER_VERIFY_JURY2_ONLY": "codex_cli:model-two",
        "CITATION_VERIFIER_VERIFY_JURY2_LEVEL": "medium",
        "CODEX_CLI_MODEL": "model-one,model-two",
    })

    assert result["ok"] is True
    assert [
        (item["selector"], item["jury1_selected"], item["jury2_selected"])
        for item in result["options"]
    ] == [
        ("codex_cli:model-one", True, False),
        ("codex_cli:model-two", False, True),
        ("codex_cli:new-model", False, False),
    ]


def test_unreachable_local_catalog_reports_the_endpoint_and_service_state(monkeypatch):
    monkeypatch.setattr(discovery._OPENER, "open", mock.Mock(side_effect=urllib.error.URLError(ConnectionRefusedError())))
    result = discovery.check_provider("freetoken", {})
    assert result["ok"] is False
    assert "http://127.0.0.1:1919" in result["message"]
    assert "running" in result["message"]


class _Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def _json(payload: object) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"))


def test_openai_compatible_uses_exact_generation_base_and_sanitizes_models(monkeypatch):
    opener = _Opener([_json({"data": [
        {"id": "provider/model-v1"}, {"id": "llama3:8b"},
        {"id": "-inject"}, {"id": "<b>bad</b>"}, {"id": "provider/model-v1"},
    ]})])
    monkeypatch.setattr(discovery, "_OPENER", opener)
    secret = "do-not-return-this-token"
    result = discovery.check_provider("openai", {
        "OPENAI_API_KEY": secret,
        "OPENAI_BASE_URL": "https://api.example.test/v1/",
    })
    assert result["ok"] is True
    assert result["provider"] == "openai"
    assert result["model_env"] == "OPENAI_MODEL"
    assert result["models"] == ["provider/model-v1", "llama3:8b"]
    assert result["settings"] == {"base_url": "https://api.example.test/v1"}
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.example.test/v1/models"
    assert request.get_header("Authorization") == f"Bearer {secret}"
    assert timeout == discovery._TIMEOUT
    assert secret not in repr(result)
    assert secret not in request.full_url


@pytest.mark.parametrize(
    ("provider", "key_env", "model_env", "base_url"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "https://api.deepseek.com"),
        ("groq", "GROQ_API_KEY", "GROQ_MODEL", "https://api.groq.com/openai/v1"),
    ],
)
def test_dedicated_openai_compatible_providers_use_isolated_keys_and_models(
    monkeypatch, provider, key_env, model_env, base_url,
):
    observed = []
    secret = f"private-{provider}-key"

    def open_json(url, headers):
        observed.append((url, headers))
        return {"data": [{"id": "served-model"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider(provider, {key_env: secret})

    assert result["ok"] is True
    assert result["provider"] == provider
    assert result["model_env"] == model_env
    assert result["models"] == ["served-model"]
    assert result["options"] == [{
        "selector": f"{provider}:served-model",
        "backend": provider,
        "model": "served-model",
        "model_env": model_env,
        "label": f"{provider}: served-model",
        "configured": False,
        "jury1_selected": False,
        "jury2_selected": False,
    }]
    assert observed == [(
        f"{base_url}/models", {"Authorization": f"Bearer {secret}"},
    )]
    assert secret not in repr(result)


def test_xai_catalog_uses_its_language_model_endpoint_and_isolated_key(monkeypatch):
    observed = []
    secret = "private-xai-key"

    def open_json(url, headers):
        observed.append((url, headers))
        return {"models": [{"id": "grok-model-a"}, {"id": "grok-model-b"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider("xai", {
        "XAI_API_KEY": secret,
        "OPENAI_API_KEY": "different-openai-key",
        "GROQ_API_KEY": "different-groq-key",
    })

    assert result["ok"] is True
    assert result["provider"] == "xai"
    assert result["model_env"] == "XAI_MODEL"
    assert result["models"] == ["grok-model-a", "grok-model-b"]
    assert [item["selector"] for item in result["options"]] == [
        "xai:grok-model-a", "xai:grok-model-b",
    ]
    assert observed == [(
        "https://api.x.ai/v1/language-models",
        {"Authorization": f"Bearer {secret}"},
    )]
    assert secret not in repr(result)


def test_xai_catalog_does_not_borrow_openai_or_groq_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_open_json", lambda *args: calls.append(args))
    result = discovery.check_provider("xai", {
        "OPENAI_API_KEY": "openai-secret",
        "GROQ_API_KEY": "groq-secret",
    })
    assert result["ok"] is False
    assert "xAI API key" in result["message"]
    assert calls == []
    assert "openai-secret" not in repr(result)
    assert "groq-secret" not in repr(result)


def test_openrouter_authenticates_before_listing_public_models(monkeypatch):
    calls = []

    def open_json(url, headers):
        calls.append(url)
        if url.endswith("/auth/key"):
            return {"data": {"label": "workspace", "limit_remaining": 10}}
        return {"data": [{"id": "openai/gpt-model"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider("openrouter", {"OPENROUTER_API_KEY": "secret-value"})
    assert calls == [
        "https://openrouter.ai/api/v1/auth/key",
        "https://openrouter.ai/api/v1/models",
    ]
    assert result["ok"] is True
    assert result["authenticated"] is True
    assert result["models"] == ["openai/gpt-model"]
    assert "secret-value" not in repr(result)


def test_typesafe_catalog_reads_real_models_schema(monkeypatch):
    observed = []

    def open_json(url, headers):
        observed.append((url, headers))
        return {"models": [
            {"name": "jev-latest", "description": "ignored"},
            {"name": "jev-1.13.0"},
        ]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider("typesafe", {"TYPESAFE_API_KEY": "hidden"})
    assert observed == [(
        "https://api.typesafe.ai/v1/models", {"Authorization": "Bearer hidden"},
    )]
    assert result["models"] == ["jev-latest", "jev-1.13.0"]
    assert result["ok"] is True
    assert "hidden" not in repr(result)


def test_gemini_keeps_only_generate_content_models(monkeypatch):
    monkeypatch.setattr(discovery, "_open_json", lambda *_args: {"models": [
        {"name": "models/gemini-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
    ]})
    result = discovery.check_provider("gemini", {"GEMINI_API_KEY": "hidden"})
    assert result["models"] == ["models/gemini-flash"]
    assert result["ok"] is True


def test_keyless_local_ollama_uses_distinct_route_and_tagged_model(monkeypatch):
    observed = []

    def open_json(url, headers):
        observed.append((url, headers))
        return {"models": [{"name": "llama3:8b"}, {"name": "qwen:14b"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider("ollama", {})
    assert result["ok"] is True
    assert result["model_env"] == "OLLAMA_MODEL"
    assert [item["backend"] for item in result["options"]] == ["ollama_local", "ollama_local"]
    assert observed == [("http://127.0.0.1:11434/api/tags", {})]


def test_authenticated_freetoken_uses_keyed_route_without_secret_in_result(monkeypatch):
    observed = []

    def open_json(url, headers):
        observed.append((url, headers))
        return {"data": [{"id": "served-model"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    secret = "private-ft-token"
    result = discovery.check_provider("freetoken", {
        "FREETOKEN_API_KEY": secret,
        "FREETOKEN_HOST": "https://ft.example.test",
    })
    assert result["ok"] is True
    assert result["models"] == ["served-model"]
    assert {item["backend"] for item in result["options"]} == {"freetoken_authenticated"}
    assert observed == [(
        "https://ft.example.test/v1/models", {"Authorization": f"Bearer {secret}"},
    )]
    assert secret not in repr(result)


def test_ollama_key_without_host_checks_the_same_cloud_default_as_generation(monkeypatch):
    observed = []

    def open_json(url, headers):
        observed.append(url)
        return {"models": [{"name": "cloud-model"}]}

    monkeypatch.setattr(discovery, "_open_json", open_json)
    result = discovery.check_provider("ollama", {"OLLAMA_API_KEY": "cloud-key"})
    assert result["ok"] is True
    assert observed == ["https://ollama.com/api/tags"]
    assert result["settings"] == {"host": "https://ollama.com"}
    assert result["options"][0]["backend"] == "ollama"


@pytest.mark.parametrize(
    ("code", "message"),
    [(401, "rejected or has expired"), (403, "lacks permission"), (302, "redirected")],
)
def test_http_status_errors_are_safe(code, message, monkeypatch):
    secret = "sensitive-provider-detail"
    error = urllib.error.HTTPError(
        "https://api.example.test/models", code, secret,
        {"Location": "https://elsewhere.test/" + secret}, io.BytesIO(secret.encode()),
    )
    monkeypatch.setattr(discovery, "_OPENER", _Opener([error]))
    with pytest.raises(discovery._DiscoveryError) as caught:
        discovery._open_json("https://api.example.test/models", {"Authorization": "Bearer key"})
    assert message in str(caught.value)
    assert secret not in str(caught.value)
    assert caught.value.status_code == code


def test_http_transport_rejects_oversized_and_malformed_data(monkeypatch):
    huge = _Response(b"{}", {"Content-Length": str(discovery._MAX_BODY + 1)})
    monkeypatch.setattr(discovery, "_OPENER", _Opener([huge]))
    with pytest.raises(discovery._DiscoveryError, match="size limit"):
        discovery._open_json("https://api.example.test/models", {})

    monkeypatch.setattr(
        discovery, "_OPENER",
        _Opener([_Response(b"x" * (discovery._MAX_BODY + 1), {})]),
    )
    with pytest.raises(discovery._DiscoveryError, match="size limit"):
        discovery._open_json("https://api.example.test/models", {})

    monkeypatch.setattr(discovery, "_OPENER", _Opener([_Response(b'{"data": {}}')]))
    with pytest.raises(discovery._DiscoveryError, match="malformed"):
        discovery._openai_models("https://api.example.test/v1", "key")

    monkeypatch.setattr(discovery, "_OPENER", _Opener([_Response(b'{"data": [], "data": []}')]))
    with pytest.raises(discovery._DiscoveryError, match="malformed"):
        discovery._open_json("https://api.example.test/models", {})


def test_https_catalog_uses_strict_default_context_with_certifi_roots(monkeypatch):
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    provider_opener = discovery._ProviderCheckOpener()
    response_opener = _Opener([_Response(b'{"data": []}')])
    captured = {}

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return response_opener

    create_context = mock.Mock(return_value=context)
    monkeypatch.setattr(discovery.ssl, "create_default_context", create_context)
    monkeypatch.setattr(discovery.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(discovery, "_OPENER", provider_opener)

    result = discovery._open_json("https://api.example.test/models", {})

    assert result == {"data": []}
    create_context.assert_called_once_with()
    assert context.get_ca_certs(binary_form=True)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    handlers = captured["handlers"]
    assert discovery._NoRedirect in handlers
    https_handler = next(
        handler for handler in handlers
        if isinstance(handler, discovery.urllib.request.HTTPSHandler)
    )
    assert https_handler._context is context


def test_https_catalog_fails_closed_when_certifi_bundle_is_invalid(monkeypatch, tmp_path):
    provider_opener = discovery._ProviderCheckOpener()
    build_opener = mock.Mock()
    monkeypatch.setattr(discovery, "_OPENER", provider_opener)
    monkeypatch.setattr(
        discovery.certifi, "where", lambda: str(tmp_path / "missing-ca.pem"),
    )
    monkeypatch.setattr(discovery.urllib.request, "build_opener", build_opener)

    with pytest.raises(discovery._DiscoveryError, match="could not be reached"):
        discovery._open_json("https://api.example.test/models", {})

    build_opener.assert_not_called()
    assert provider_opener._https_opener is None


def test_loopback_http_catalog_keeps_the_unencrypted_opener(monkeypatch):
    provider_opener = discovery._ProviderCheckOpener()
    response_opener = _Opener([_Response(b'{"models": []}')])
    provider_opener._http_opener = response_opener
    monkeypatch.setattr(discovery, "_OPENER", provider_opener)
    create_context = mock.Mock(side_effect=AssertionError("HTTP must not create TLS context"))
    monkeypatch.setattr(discovery.ssl, "create_default_context", create_context)

    result = discovery._open_json("http://127.0.0.1:11434/api/tags", {})

    assert result == {"models": []}
    assert len(response_opener.calls) == 1
    create_context.assert_not_called()
    assert provider_opener._https_opener is None


def test_timeout_is_distinguished_from_unreachable(monkeypatch):
    monkeypatch.setattr(discovery, "_OPENER", _Opener([socket.timeout()]))
    with pytest.raises(discovery._DiscoveryError, match="timed out"):
        discovery._open_json("https://api.example.test/models", {})


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://not-local.example/v1", "https://user:pass@example.test/v1",
        "https://api.example.test/v1?token=abc", "https://api.example.test/v1#frag",
        "https://api.example.test/v1\r\nX-Test: x", "https://api.example.test:99999/v1",
    ],
)
def test_unsafe_endpoint_inputs_are_rejected(endpoint):
    with pytest.raises(ValueError):
        discovery.normalize_endpoint(endpoint)


def test_secret_controls_are_rejected_without_echoing_secret(monkeypatch):
    value = "credential\r\nAuthorization: stolen"
    result = discovery.check_provider("openai", {"OPENAI_API_KEY": value})
    assert result["ok"] is False
    assert "control characters" in result["message"]
    assert value not in repr(result)


def test_provider_check_does_not_mutate_process_environment(monkeypatch):
    monkeypatch.setattr(
        discovery, "_open_json",
        lambda *_args: {"data": [{"id": "gpt-test"}]},
    )
    before = os.environ.get("CITATION_VERIFIER_MODEL")
    result = discovery.check_provider("openai", {"OPENAI_API_KEY": "test-key"})
    assert result["ok"] is True
    assert os.environ.get("CITATION_VERIFIER_MODEL") == before


def test_new_local_ollama_route_is_not_automatically_available_without_configuration(monkeypatch):
    from core.verify.backends._registry import all_specs

    for name in ("OLLAMA_API_KEY", "OLLAMA_HOST", "OLLAMA_MODEL"):
        monkeypatch.delenv(name, raising=False)
    local = next(spec for spec in all_specs() if spec.name == "ollama_local")
    assert local.available() is False
    monkeypatch.setenv("OLLAMA_MODEL", "llama3:8b")
    assert local.available() is True
