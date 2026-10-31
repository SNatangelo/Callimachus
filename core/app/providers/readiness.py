# core/app/providers/readiness.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit pre-run response probe, isolated from the GUI's process environment."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from core.app.providers.cli_discovery import _Session
from core.app.runtime_paths import application_argv


def _failure(exc: Exception, selector: str) -> dict:
    from core.verify.backends.errors import (
        AuthError, BackendTimeoutError, ConfigError, LaneError, RateLimitError,
    )
    status = None
    current = exc
    for _ in range(4):
        code = getattr(current, "code", None) or getattr(current, "status_code", None)
        if isinstance(code, int) and 400 <= code < 600:
            status = code
            break
        match = re.search(r"(?:HTTP|status(?:_code)?)\s*[:=]?\s*(401|403|429)\b", str(current), re.I)
        if match:
            status = int(match[1])
            break
        current = current.__cause__ or current.__context__
        if current is None:
            break
    if status == 401:
        message = "The API key was rejected or has expired."
    elif status == 403:
        message = "The API key was rejected or lacks permission to use this model."
    elif isinstance(exc, AuthError):
        message = "Authentication failed. Check the API key or sign in to the CLI."
    elif status == 429 or isinstance(exc, RateLimitError):
        message = "The provider rate limit or account quota was reached."
    elif isinstance(exc, (BackendTimeoutError, TimeoutError)):
        message = "The provider response timed out."
    elif isinstance(exc, LaneError):
        message = "The selected model is unavailable for this account."
    elif isinstance(exc, ConfigError):
        message = "The selected provider configuration is invalid."
    else:
        message = "The selected model did not return a usable response."
    return {"ok": False, "selector": selector, "message": message, "status_code": status}


def _worker_check(environ: Mapping[str, str]) -> dict:
    """Run only in a dedicated child, whose env is the exact proposed run env."""
    from core.verify.claim_evidence.config import (
        GENERIC_MODEL_ENV, ProviderEnvSpec, resolve_config,
    )
    from core.verify.claim_evidence.adapters.llm.registry import registered_provider_metadata
    from core.verify.claim_evidence.adapters.llm.credentials import credentials_from_config
    from core.verify.backends.transport import call_registered_provider
    from core.app.providers.discovery import normalize_endpoint, _model_id

    if not str(environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or "").strip():
        return {"ok": True, "checked": []}
    try:
        specs = tuple(ProviderEnvSpec(name, key, model or GENERIC_MODEL_ENV, keyless, capability)
                      for name, key, model, keyless, capability in registered_provider_metadata())
        config = resolve_config(environ, specs, run_id="desktop-readiness-probe")
        selected_names = {provider.name for provider in config.providers}
        for name, providers in (
            ("OPENAI_BASE_URL", {"openai_compatible"}),
            ("ANTHROPIC_BASE_URL", {"anthropic"}),
            ("OLLAMA_HOST", {"ollama", "ollama_local"}),
            ("FREETOKEN_HOST", {"freetoken", "freetoken_authenticated"}),
        ):
            if selected_names & providers and environ.get(name):
                normalize_endpoint(environ[name])
        credentials = credentials_from_config(config, specs, environ)
    except Exception:
        return {"ok": False, "message": "The selected jury configuration is invalid.", "checked": []}
    checked = []
    for provider in config.providers:
        for lane in provider.lanes:
            if not lane.jury1_eligible and not (config.jury2_level != "off" and lane.jury2_eligible):
                continue
            selector = f"{provider.name}:{lane.model}"
            try:
                if _model_id(lane.model) != lane.model:
                    raise ValueError("invalid model identifier")
                secret = credentials.resolve(lane.key_alias, lane.key_fingerprint)
                if secret is not None and any(ord(char) < 32 or ord(char) == 127 for char in secret):
                    raise ValueError("invalid credential")
                raw = call_registered_provider(
                    provider.name, "Connectivity check. Reply briefly with OK.",
                    json.dumps({"task": {"task_id": "provider_readiness",
                                          "instructions": "Reply briefly with OK."}}),
                    lane.model, secret, max_tokens=256, reasoning="off",
                )
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError("empty response")
                try:
                    envelope = json.loads(raw)
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict) and (
                    not envelope or envelope.get("is_error") is True or envelope.get("error")
                ):
                    raise ValueError("provider error envelope")
            except Exception as exc:
                return {**_failure(exc, selector), "checked": checked}
            checked.append(selector)
    return {"ok": bool(checked), "checked": checked,
            "message": "Selected juries responded." if checked else "No active jury is configured."}


def check_verify_readiness(environ: Mapping[str, str]) -> dict:
    if not str(environ.get("CITATION_VERIFIER_VERIFY_BACKENDS") or "").strip():
        return {"ok": True, "checked": []}
    # Adapters read endpoints/timeouts from os.environ. A child avoids changing
    # shared GUI state or racing other provider/catalog checks.
    values = {**environ, "CITATION_VERIFIER_LLM_TIMEOUT": "15", "PYTHONIOENCODING": "utf-8"}
    try:
        with _Session(application_argv("--jury-readiness-probe"), values) as session:
            # Multiple explicitly selected lanes may each need one response.
            session.deadline += 52
            result = session.document()
            if result.get("ok") is True and isinstance(result.get("checked"), list):
                return result
            if result.get("ok") is False:
                return result
    except Exception:
        pass
    return {"ok": False, "checked": [], "message": "The jury response check failed or timed out."}


def main() -> int:
    print(json.dumps(_worker_check(dict(os.environ))), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
