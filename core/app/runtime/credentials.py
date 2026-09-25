# core/app/runtime/credentials.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-rejection diagnostics for resolve and fetch phases."""

from __future__ import annotations

import os
import sys

from core.fetch import service as _fetch
from core.app.runtime.repository import _repo_open

def _credential_specs() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Build runtime labels from declarations, never a hard-coded provider map."""
    from core.infra.credential_catalog import credential_catalog

    labels: dict[str, str] = {}
    names: dict[str, list[str]] = {}
    for item in credential_catalog():
        provider = item["provider"]
        label = item["label"]
        if provider in labels and labels[provider] != label:
            raise ValueError("credential provider labels are inconsistent")
        labels[provider] = label
        names.setdefault(provider, []).append(item["env_name"])
    return {
        provider: (labels[provider], tuple(env_names))
        for provider, env_names in names.items()
    }


def _credential_provider_name(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    provider_name_for_via = getattr(
        getattr(_fetch, "_provider_registry", None),
        "provider_name_for_via",
        None,
    )
    if callable(provider_name_for_via):
        canonical = provider_name_for_via(text)
        if canonical:
            return str(canonical).strip().lower()
    text = text.split(":", 1)[0]
    return text.removesuffix("_search")


def _credential_spec_for_provider(provider: str, specs):
    if provider in specs:
        return provider, specs[provider]
    normalized = "".join(character for character in provider if character.isalnum())
    matches = [
        name
        for name in specs
        if "".join(character for character in name if character.isalnum())
        == normalized
    ]
    if len(matches) == 1:
        canonical = matches[0]
        return canonical, specs[canonical]
    return None


def _phase_credential_rejections(
    run_dir: str,
    phase: str,
    *,
    since: str | None = None,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    """Return current-phase API credential rejections without inspecting values.

    Only typed provider diagnostics with HTTP 401/403 qualify.  Publisher/document
    403s, rate limits, missing keys and stale observations from an earlier phase
    invocation are deliberately excluded.
    """
    if phase not in {"resolve", "fetch"}:
        raise ValueError("credential warning phase must be resolve or fetch")
    env = os.environ if environ is None else environ
    specs = _credential_specs()
    rejected: dict[str, dict] = {}

    def observe(row: object) -> None:
        if not isinstance(row, dict) or row.get("error_type") != "auth":
            return
        http_status = row.get("http_status")
        if type(http_status) is not int or http_status not in {401, 403}:
            return
        provider = _credential_provider_name(
            row.get("provider") or row.get("via") or row.get("method")
        )
        matched = _credential_spec_for_provider(provider, specs)
        if matched is None:
            return
        provider, spec = matched
        label, env_names = spec
        active_envs = tuple(
            name for name in env_names if str(env.get(name) or "").strip()
        )
        if not active_envs:
            return
        item = rejected.setdefault(
            provider,
            {
                "provider": provider,
                "label": label,
                "env_names": active_envs,
                "http_statuses": set(),
            },
        )
        item["http_statuses"].add(http_status)

    def current(timestamp: object) -> bool:
        return since is None or (
            isinstance(timestamp, str) and timestamp >= since
        )

    repo = _repo_open(run_dir)
    if repo is None:
        return []
    try:
        if phase == "resolve":
            for result in repo.list_resolve_results():
                if not current(result.updated_at):
                    continue
                for attempt in result.attempts if isinstance(result.attempts, list) else []:
                    observe(attempt)
        # Resolve may run inline Fetch repair, so its provider diagnostics also
        # belong to the current Resolve invocation.
        with repo._connection_lock:
            for ref in repo._conn.execute(
                "SELECT ref_id FROM operational_references ORDER BY ref_number"
            ):
                for attempt in repo.list_fetch_attempts(ref["ref_id"]):
                    if (
                        current(attempt.get("created_at"))
                        and attempt.get("kind") == "provider_diagnostic"
                    ):
                        observe(attempt.get("trace"))
    finally:
        repo.close()
    return [
        {
            **item,
            "http_statuses": tuple(sorted(item["http_statuses"])),
        }
        for _provider, item in sorted(rejected.items())
    ]


def _print_phase_credential_warnings(
    run_dir: str,
    phase: str,
    *,
    since: str | None = None,
    environ: dict[str, str] | None = None,
) -> None:
    for row in _phase_credential_rejections(
        run_dir,
        phase,
        since=since,
        environ=environ,
    ):
        statuses = "/".join(str(value) for value in row["http_statuses"])
        env_names = " or ".join(f"${name}" for name in row["env_names"])
        print(
            f"[warning] {phase.capitalize()}: {row['label']} rejected the API "
            f"credential ({env_names}; HTTP {statuses}). The key may be expired, "
            "invalid, or not authorised; update it in .env before the next run.",
            file=sys.stderr,
            flush=True,
        )
