#!/usr/bin/env python3
# core/resolve/providers/__init__.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unified provider registry for resolve and fetch capabilities.

Provider modules may expose a resolve capability (`RESOLVE_NAME` or `NAME`, `supports`,
`discover`) and/or a fetch capability (`enabled`, `disabled_reason`, `candidate_items`,
`candidate_rows`, `direct_text_items`, `direct_text_rows`). `core.resolve` consumes the
resolve-side helpers here, while `core.fetch` consumes the fetch-side helpers.

Note: `core/resolve/providers.json` is the external config file for ordering/rates, while this
`core/resolve/providers/` package holds the Python provider modules themselves.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import importlib
import os
import pkgutil
import re
from types import ModuleType
import urllib.parse
from collections.abc import Mapping

try:
    from .. import http as resolve_http
except ImportError:  # pragma: no cover - direct execution fallback
    from resolve import http as resolve_http


def credential_descriptors(module: object, channel: str | None = None) -> tuple[dict, ...]:
    """Return validated, value-free credentials declared by one provider."""
    if channel is not None and channel not in {"resolve", "fetch", "search"}:
        raise ValueError("invalid provider credential channel")
    raw = getattr(module, "CREDENTIAL_SPECS", ())
    if not isinstance(raw, tuple):
        raise ValueError("provider credential descriptors must be a tuple")
    out = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "provider", "env_name", "channels", "label",
        }:
            raise ValueError("provider credential descriptor must be a mapping")
        provider = str(item.get("provider") or "").strip()
        env_name = str(item.get("env_name") or "").strip()
        channels = item.get("channels")
        label = str(item.get("label") or "").strip()
        if (not provider or not env_name or not label or "\r" in provider + env_name + label
                or "\n" in provider + env_name + label or not isinstance(channels, tuple)
                or not channels or len(set(channels)) != len(channels)
                or re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", provider) is None
                or re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name) is None
                or any(value not in {"resolve", "fetch", "search"} for value in channels)):
            raise ValueError("invalid provider credential descriptor")
        if channel is not None and channel not in channels:
            continue
        out.append({"provider": provider, "env_name": env_name,
                    "channels": tuple(str(v) for v in channels),
                    "label": label})
    if len({(item["provider"], item["env_name"]) for item in out}) != len(out):
        raise ValueError("duplicate provider credential descriptor")
    return tuple(out)


def _credential_for_resolver(module: object) -> tuple[str, str] | None:
    """Return an explicit provider credential only when it is configured.

    The provider registry is the invocation boundary: these modules insert the
    corresponding key in their resolver request.  No hostname/header heuristic
    is used here.
    """
    for descriptor in credential_descriptors(module, "resolve"):
        binding = (descriptor["provider"], descriptor["env_name"])
        if (os.environ.get(binding[1]) or "").strip():
            return binding
    return None


def credential_scope_for_resolver(module: object):
    """Return the explicit credential scope for a resolver invocation.

    Resolver callers which are not routed through :func:`call_resolver` (the
    enrichment and batch accelerators) use this same boundary.  Entering the
    scope alone records nothing; an observation still requires a physical
    Resolve transport attempt.
    """
    binding = _credential_for_resolver(module)
    if binding is None:
        return nullcontext()
    from core.resolve import transport_telemetry

    return transport_telemetry.credential_scope(
        provider=binding[0], env_name=binding[1]
    )


def _credential_for_fetch_provider(
    module: object, environ: Mapping[str, str] | None,
) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    for descriptor in credential_descriptors(module, "fetch"):
        binding = (descriptor["provider"], descriptor["env_name"])
        if str(env.get(binding[1]) or "").strip():
            return binding
    return None

try:
    from .. import provider_config
except ImportError:  # pragma: no cover - direct execution fallback
    from resolve import provider_config


ENV_FETCH_PROVIDERS = provider_config.ENV_FETCH_PROVIDERS
ENV_FETCH_PROVIDER_ORDER = provider_config.ENV_FETCH_PROVIDER_ORDER
ENV_FETCH_PROVIDER_WORKERS = "CITATION_VERIFIER_FETCH_PROVIDER_WORKERS"

_FETCH_CAPABILITY_ATTRS = (
    "enabled",
    "disabled_reason",
    "candidate_items",
    "candidate_rows",
    "landing_items",
    "direct_text_items",
    "direct_text_rows",
)
_REGISTRY_CACHE: dict[str, ModuleType] | None = None
_ISSUE_STATUSES = frozenset({"complete", "enumerated", "incomplete", "not_applicable"})
_ISSUE_TARGET_STATUSES = frozenset({"present", "absent", "inconclusive"})
_OCCUPANCY_STATUSES = frozenset({"resolved", "unverified", "unresolved", "ambiguous"})
_COVERAGE_STATUSES = frozenset({"covered", "not_covered", "incomplete"})
_ARTICLE_LOOKUP_COMPLETIONS = frozenset({"complete", "partial", "incomplete"})


def _module_enabled(
    module: object, configured: Mapping | None = None, *, capability_name: str | None = None,
) -> bool:
    """Apply the provider setting to auxiliary capabilities as well."""
    settings = configured
    if settings is None:
        settings = provider_config.load().get("providers", {})
    if capability_name and capability_name in settings:
        return (settings.get(capability_name) or {}).get("enabled", True) is True
    names = (
        str(getattr(module, "NAME", "") or "").strip(),
        str(origin_name(module) or "").strip(),
    )
    for name in names:
        if name and name in settings:
            return (settings.get(name) or {}).get("enabled", True) is True
    return True


def _incomplete_issue_attestation(module: object, reason: str) -> dict:
    capability = getattr(module, "ISSUE_ATTESTATION", {})
    return {
        "provider": str(capability.get("provider") or _provider_label(module)),
        "rule_version": str(capability.get("rule_version") or "unknown"),
        "status": "incomplete", "reason": reason, "scope": None,
        "target_status": "inconclusive", "target_member_order": None,
        "cited_container": None, "cited_volume": None, "cited_issue": None,
        "journal_title": None, "completeness_basis": None,
        "members": [], "sources": [], "observations": [],
    }


def _normalize_issue_attestation(module: object, value: object) -> dict:
    """Validate the provider-neutral issue-attestation boundary.

    Invalid provider data is operationally incomplete evidence, never a closed
    world assertion.  The narrow canonical shape is deliberately independent
    of any publisher's HTML or API payload.
    """
    if not isinstance(value, Mapping):
        return _incomplete_issue_attestation(module, "invalid issue attestation result")
    out = dict(value)
    capability = getattr(module, "ISSUE_ATTESTATION", {})
    if out.get("provider") != capability.get("provider") or out.get("rule_version") != capability.get("rule_version"):
        return _incomplete_issue_attestation(module, "issue attestation identity is inconsistent")
    required = {
        "provider", "rule_version", "status", "reason", "scope", "target_status",
        "target_member_order", "cited_container", "cited_volume", "cited_issue",
        "journal_title", "completeness_basis", "members", "sources", "observations",
    }
    if set(out) != required or out["status"] not in _ISSUE_STATUSES or out["target_status"] not in _ISSUE_TARGET_STATUSES:
        return _incomplete_issue_attestation(module, "issue attestation shape is invalid")
    members = out["members"]
    sources = out["sources"]
    observations = out["observations"]
    if not isinstance(members, list) or not isinstance(sources, list) or not isinstance(observations, list):
        return _incomplete_issue_attestation(module, "issue attestation collections are invalid")
    member_keys = {
        "record_id", "title", "first_author", "year", "journal", "volume",
        "issue", "locator", "pmid", "pmcid", "doi", "url",
    }
    if any(not isinstance(item, Mapping) or set(item) != member_keys for item in members):
        return _incomplete_issue_attestation(module, "issue attestation member shape is invalid")
    text_fields = (
        "record_id", "title", "first_author", "journal", "volume", "issue",
        "locator", "pmid", "pmcid", "doi", "url",
    )
    if any(
        any(member[key] is not None and not isinstance(member[key], str) for key in text_fields)
        or not str(member["record_id"] or "").strip()
        or not str(member["title"] or "").strip()
        or (member["year"] is not None and type(member["year"]) is not int)
        for member in members
    ):
        return _incomplete_issue_attestation(module, "issue attestation member value is invalid")
    if len({member["record_id"] for member in members}) != len(members):
        return _incomplete_issue_attestation(module, "issue attestation member ids are duplicated")
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"role", "url", "response_sha256"}
        or not isinstance(item["role"], str)
        or not item["role"].strip()
        or (item["url"] is not None and not isinstance(item["url"], str))
        or (
            item["response_sha256"] is not None
            and (
                not isinstance(item["response_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", item["response_sha256"]) is None
            )
        )
        for item in sources
    ):
        return _incomplete_issue_attestation(module, "issue attestation source shape is invalid")
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"key", "value_type", "text_value", "integer_value"}
        or not isinstance(item["key"], str)
        or not item["key"].strip()
        or not (
            item["value_type"] == "text"
            and isinstance(item["text_value"], str)
            and item["integer_value"] is None
            or item["value_type"] == "integer"
            and type(item["integer_value"]) is int
            and item["text_value"] is None
        )
        for item in observations
    ):
        return _incomplete_issue_attestation(module, "issue attestation observation shape is invalid")
    if len({item["key"] for item in observations}) != len(observations):
        return _incomplete_issue_attestation(module, "issue attestation observation keys are duplicated")
    if not isinstance(out["reason"], str) or not out["reason"].strip():
        return _incomplete_issue_attestation(module, "issue attestation reason is invalid")
    for key in ("scope", "cited_container", "cited_volume", "cited_issue", "journal_title", "completeness_basis"):
        if out[key] is not None and not isinstance(out[key], str):
            return _incomplete_issue_attestation(module, f"issue attestation {key} is invalid")
    if out["status"] in {"complete", "enumerated"} and (
        out["scope"] != "issue"
        or not str(out["completeness_basis"] or "").strip()
        or not members
        or not sources
        or any(
            not str(source["url"] or "").strip()
            or source["response_sha256"] is None
            for source in sources
        )
    ):
        return _incomplete_issue_attestation(module, "materialized issue attestation is incomplete")
    if out["status"] in {"incomplete", "not_applicable"}:
        if out["target_status"] != "inconclusive" or out["target_member_order"] is not None or members:
            return _incomplete_issue_attestation(module, "incomplete issue attestation asserts closed-world evidence")
    if out["status"] == "enumerated" and out["target_status"] == "absent":
        return _incomplete_issue_attestation(module, "enumerated issue attestation cannot prove absence")
    if out["target_status"] == "present":
        order = out["target_member_order"]
        if type(order) is not int or not 0 <= order < len(members):
            return _incomplete_issue_attestation(module, "present issue target has no member")
    elif out["target_member_order"] is not None:
        return _incomplete_issue_attestation(module, "non-present issue target has a member")
    return out


def issue_attestation_capable() -> dict[str, ModuleType]:
    """Return issue adapters in deterministic provider-name order."""
    capable = {}
    for module in get_registry().values():
        capability = getattr(module, "ISSUE_ATTESTATION", None)
        if (isinstance(capability, Mapping)
                and set(capability) == {"provider", "rule_version"}
                and all(isinstance(capability[key], str) and capability[key] for key in capability)
                and callable(getattr(module, "supports_issue_attestation", None))
                and callable(getattr(module, "attest_issue", None))):
            capable[capability["provider"]] = module
    return {name: capable[name] for name in sorted(capable)}


def coordinate_occupancy_capable() -> dict[str, ModuleType]:
    """Return independently registered positive-coordinate adapters."""
    capable: dict[str, ModuleType] = {}
    for module in get_registry().values():
        capability = getattr(module, "COORDINATE_OCCUPANCY", None)
        if (
            isinstance(capability, Mapping)
            and set(capability) == {"provider", "rule_version"}
            and all(isinstance(capability[key], str) and capability[key] for key in capability)
            and callable(getattr(module, "supports_coordinate_occupancy", None))
            and callable(getattr(module, "occupy_coordinates", None))
        ):
            capable[capability["provider"]] = module
    return {name: capable[name] for name in sorted(capable)}


def coordinate_occupancy_via(value: object) -> bool:
    """Whether an attempt name belongs to a registered occupancy adapter."""
    return str(value or "") in coordinate_occupancy_capable()


def _inconclusive_coordinate_occupancy(module: object, reason: str) -> dict:
    capability = getattr(module, "COORDINATE_OCCUPANCY", {})
    return {
        "status": "unresolved", "via": str(capability.get("provider") or _provider_label(module)),
        "reason": reason,
    }


def _normalize_coordinate_occupancy(module: object, value: object) -> dict:
    """Reject malformed occupancy data as an operationally inconclusive attempt."""
    if not isinstance(value, Mapping):
        return _inconclusive_coordinate_occupancy(module, "invalid coordinate occupancy result")
    out = dict(value)
    capability = getattr(module, "COORDINATE_OCCUPANCY", {})
    allowed = {
        "status", "via", "reason", "matched_title", "matched_authors", "matched_year",
        "pmid", "doi", "resolution_basis", "existence_confidence", "metadata_match",
    }
    if (
        set(out) - allowed
        or out.get("via") != capability.get("provider")
        or out.get("status") not in _OCCUPANCY_STATUSES
        or not isinstance(out.get("reason"), str)
        or not out["reason"].strip()
    ):
        return _inconclusive_coordinate_occupancy(module, "coordinate occupancy shape is invalid")
    if (
        (out.get("matched_title") is not None and not isinstance(out["matched_title"], str))
        or (out.get("matched_authors") is not None and (
            not isinstance(out["matched_authors"], list)
            or any(not isinstance(name, str) for name in out["matched_authors"])
        ))
        or (out.get("matched_year") is not None and type(out["matched_year"]) is not int)
        or (out.get("pmid") is not None and not isinstance(out["pmid"], str))
        or (out.get("doi") is not None and not isinstance(out["doi"], str))
        or (out.get("resolution_basis") is not None and not isinstance(out["resolution_basis"], str))
        or (out.get("existence_confidence") is not None and not isinstance(out["existence_confidence"], str))
        or (out.get("metadata_match") is not None and not isinstance(out["metadata_match"], Mapping))
    ):
        return _inconclusive_coordinate_occupancy(module, "coordinate occupancy evidence is invalid")
    if out["status"] == "resolved" and (
        not isinstance(out.get("matched_title"), str)
        or not out["matched_title"].strip()
        or not isinstance(out.get("metadata_match"), Mapping)
    ):
        return _inconclusive_coordinate_occupancy(module, "resolved coordinate occupancy lacks identity evidence")
    return out


def occupy_coordinates(ref: dict) -> list[dict]:
    """Run configured occupancy adapters; only a positive result can refute."""
    out = []
    configured = provider_config.load().get("providers", {})
    for _name, module in coordinate_occupancy_capable().items():
        capability = module.COORDINATE_OCCUPANCY
        if not _module_enabled(
            module, configured, capability_name=capability["provider"],
        ):
            continue
        try:
            if not module.supports_coordinate_occupancy(ref):
                continue
            value = module.occupy_coordinates(ref)
        except Exception as exc:
            value = _inconclusive_coordinate_occupancy(module, f"{type(exc).__name__}: {exc}")
        out.append(_normalize_coordinate_occupancy(module, value))
    return out


def journal_coverage_capable() -> dict[str, ModuleType]:
    """Return exact journal-coverage adapters in deterministic resolver order."""
    capable: dict[str, ModuleType] = {}
    for module in get_registry().values():
        capability = getattr(module, "JOURNAL_COVERAGE", None)
        if (
            isinstance(capability, Mapping) and set(capability) == {"resolver", "rule_version"}
            and all(isinstance(capability[key], str) and capability[key] for key in capability)
            and callable(getattr(module, "probe_journal_coverage", None))
        ):
            capable[capability["resolver"]] = module
    return {name: capable[name] for name in sorted(capable)}


def _incomplete_journal_coverage(module: object, reason: str) -> dict:
    capability = getattr(module, "JOURNAL_COVERAGE", {})
    return {
        "resolver": str(capability.get("resolver") or _provider_label(module)),
        "rule_version": str(capability.get("rule_version") or "unknown"),
        "status": "incomplete", "provider_journal_id": None, "work_count": None,
        "source_url": None, "http_status": None, "response": None,
        "query_contract": "invalid", "completion": "incomplete", "reason": reason,
    }


def _normalize_journal_coverage(module: object, value: object) -> dict:
    if not isinstance(value, Mapping):
        return _incomplete_journal_coverage(module, "invalid journal coverage result")
    out = dict(value)
    capability = getattr(module, "JOURNAL_COVERAGE", {})
    required = {"resolver", "rule_version", "status", "provider_journal_id", "work_count", "source_url", "http_status", "response", "query_contract", "completion", "reason"}
    if (
        set(out) != required or out.get("resolver") != capability.get("resolver")
        or out.get("rule_version") != capability.get("rule_version")
        or out.get("status") not in _COVERAGE_STATUSES or out.get("completion") not in {"complete", "partial", "incomplete"}
        or not isinstance(out.get("query_contract"), str) or not out["query_contract"].strip()
        or not isinstance(out.get("reason"), str) or not out["reason"].strip()
        or (out.get("provider_journal_id") is not None and not isinstance(out["provider_journal_id"], str))
        or (out.get("work_count") is not None and (type(out["work_count"]) is not int or out["work_count"] < 0))
        or (out.get("source_url") is not None and not isinstance(out["source_url"], str))
        or (out.get("http_status") is not None and type(out["http_status"]) is not int)
    ):
        return _incomplete_journal_coverage(module, "journal coverage shape is invalid")
    if out["status"] == "incomplete" and out["completion"] != "incomplete":
        return _incomplete_journal_coverage(module, "incomplete coverage claims completed query")
    if out["status"] in {"covered", "not_covered"} and out["completion"] != "complete":
        return _incomplete_journal_coverage(module, "coverage verdict lacks complete exact query")
    return out


def probe_journal_coverage(authority: dict) -> list[dict]:
    """Run exact coverage adapters; failure is stored as incomplete evidence."""
    out = []
    configured = provider_config.load().get("providers", {})
    for _name, module in journal_coverage_capable().items():
        capability = module.JOURNAL_COVERAGE
        if not _module_enabled(
            module, configured, capability_name=capability["resolver"],
        ):
            continue
        try:
            value = module.probe_journal_coverage(authority)
        except Exception as exc:
            value = _incomplete_journal_coverage(module, f"{type(exc).__name__}: {exc}")
        out.append(_normalize_journal_coverage(module, value))
    return out


def article_lookup_capable() -> dict[str, ModuleType]:
    out = {}
    for module in get_registry().values():
        cap = getattr(module, "ARTICLE_LOOKUP", None)
        if (isinstance(cap, Mapping) and set(cap) == {"resolver", "rule_version"}
                and all(isinstance(cap[k], str) and cap[k].strip() for k in cap)
                and callable(getattr(module, "lookup_article", None))
                and callable(getattr(module, "supports_article_lookup", None))):
            out[cap["resolver"]] = module
    return {key: out[key] for key in sorted(out)}


def article_lookups(ref: dict, authority: dict) -> list[dict]:
    """Strict, provider-owned article lookup capability boundary."""
    out = []
    configured = provider_config.load().get("providers", {})
    for resolver, module in article_lookup_capable().items():
        cap = module.ARTICLE_LOOKUP
        if not _module_enabled(module, configured, capability_name=resolver):
            continue
        if not module.supports_article_lookup(ref, authority):
            continue
        try:
            value = module.lookup_article(ref, authority)
        except Exception as exc:
            value = {"resolver": resolver, "rule_version": cap["rule_version"], "query_contract": "invalid", "scope": "invalid", "completion": "incomplete", "match_status": "incomplete", "source_url": None, "http_status": None, "media_type": None, "body": None, "reason": f"{type(exc).__name__}: {exc}", "candidate": None}
        required = {"resolver", "rule_version", "query_contract", "scope", "completion", "match_status", "source_url", "http_status", "media_type", "body", "reason", "candidate"}
        valid = (isinstance(value, Mapping) and set(value) == required
                 and value.get("resolver") == resolver and value.get("rule_version") == cap["rule_version"]
                 and value.get("completion") in _ARTICLE_LOOKUP_COMPLETIONS
                 and value.get("match_status") in {"no_compatible_article", "compatible", "ambiguous", "incomplete"}
                 and all(isinstance(value.get(k), str) and value[k].strip() for k in ("query_contract", "scope", "reason"))
                 and (value.get("source_url") is None or isinstance(value.get("source_url"), str))
                 and (value.get("http_status") is None or type(value.get("http_status")) is int)
                 and (value.get("media_type") is None or isinstance(value.get("media_type"), str))
                 and (value.get("body") is None or isinstance(value.get("body"), str))
                 and (value.get("candidate") is None or isinstance(value.get("candidate"), dict)))
        if valid and value["completion"] == "complete":
            valid = (value["match_status"] == "no_compatible_article" and value["http_status"] == 200 and value["media_type"] == "text/plain" and isinstance(value["body"], str) and value["candidate"] is None)
        if valid and value["completion"] != "complete" and value["match_status"] == "no_compatible_article":
            valid = False
        if not valid:
            value = {"resolver": resolver, "rule_version": cap["rule_version"], "query_contract": "invalid", "scope": "invalid", "completion": "incomplete", "match_status": "incomplete", "source_url": None, "http_status": None, "media_type": None, "body": None, "reason": "invalid article lookup result", "candidate": None}
        out.append(dict(value))
    return out


def authoritative_identifier_capable() -> dict[str, ModuleType]:
    """Return narrow authoritative-identifier adapters by declared scheme."""
    capable: dict[str, ModuleType] = {}
    for module in get_registry().values():
        capability = getattr(module, "AUTHORITATIVE_IDENTIFIER", None)
        if not isinstance(capability, Mapping) or set(capability) != {"scheme", "supersedes"}:
            continue
        scheme = capability.get("scheme")
        supersedes = capability.get("supersedes")
        if (
            not isinstance(scheme, str)
            or not scheme
            or not isinstance(supersedes, tuple)
            or any(not isinstance(item, str) or not item for item in supersedes)
            or not callable(getattr(module, "supports", None))
            or not callable(getattr(module, "discover", None))
            or scheme in capable
        ):
            continue
        capable[scheme] = module
    return {name: capable[name] for name in sorted(capable)}


def authoritative_identifier_via(via: object) -> bool:
    value = str(via or "").strip().casefold()
    return any(
        value in {_resolve_name(module).casefold(), origin_name(module).casefold()}
        for module in authoritative_identifier_capable().values()
    )


def superseded_identifier_schemes(module: object, ref: dict) -> tuple[str, ...]:
    """Return only schemes this concrete authority claim proves misparsed.

    The capability declaration is an upper bound. A provider may narrow it for
    one citation so an unrelated, independently declared identifier is still
    resolved and retained in the audit trail.
    """
    capability = getattr(module, "AUTHORITATIVE_IDENTIFIER", {})
    declared = capability.get("supersedes", ()) if isinstance(capability, Mapping) else ()
    hook = getattr(module, "superseded_identifier_schemes", None)
    if not callable(hook):
        return tuple(declared)
    try:
        selected = hook(ref)
    except Exception:
        return ()
    if (
        not isinstance(selected, tuple)
        or any(item not in declared for item in selected)
        or len(set(selected)) != len(selected)
    ):
        return ()
    return selected


def attest_issues(ref: dict) -> list[dict]:
    """Run every applicable issue adapter; failures become stored incompleteness."""
    out = []
    configured = provider_config.load().get("providers", {})
    for name, module in issue_attestation_capable().items():
        settings = configured.get(name) or {}
        if settings.get("enabled", True) is not True:
            continue
        try:
            if not module.supports_issue_attestation(ref):
                continue
            value = module.attest_issue(ref)
        except Exception as exc:  # fail closed at the registry boundary
            value = _incomplete_issue_attestation(module, f"{type(exc).__name__}: {exc}")
        out.append(_normalize_issue_attestation(module, value))
    return out


@dataclass(frozen=True)
class ProviderResult:
    """Normalized provider outcome used at registry boundaries.

    Fetch provider functions return a list of candidate dictionaries. Providers
    that need to report an operational failure may return this object (or its
    dictionary form). ``items`` deliberately stays
    separate from diagnostics so an empty successful search cannot be mistaken for
    an unavailable provider.
    """

    items: list[dict]
    status: str = "ok"
    error_type: str | None = None
    error_reason: str | None = None
    retryable: bool | None = None
    http_status: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _http_status_from_exception(exc: BaseException) -> int | None:
    status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _diagnostics_from_exception(exc: BaseException) -> dict:
    return {
        "error_type": resolve_http.error_category(exc),
        "error_reason": resolve_http.error_reason(exc),
        "retryable": resolve_http.is_retryable(exc),
        "http_status": _http_status_from_exception(exc),
    }


def _diagnostics_from_result(result: Mapping[str, object]) -> dict:
    """Fill diagnostics for provider-reported failures that caught their own error."""
    out = {
        key: result.get(key)
        for key in ("error_type", "error_reason", "retryable", "http_status")
        if result.get(key) is not None
    }
    reason = str(result.get("reason") or "")
    match = re.search(r"\bHTTP\s+(\d{3})\b", reason, re.I)
    if match:
        status = int(match.group(1))
        out.setdefault("http_status", status)
        if status in (401, 403):
            category, retryable = "auth", False
        elif status == 429:
            category, retryable = "rate_limit", True
        elif 500 <= status <= 599:
            category, retryable = "transient_server", status in resolve_http.RETRYABLE_SERVER_CODES
        else:
            category, retryable = "http_error", False
        out.setdefault("error_type", category)
        out.setdefault("error_reason", f"{category} (HTTP {status})")
        out.setdefault("retryable", retryable)
    elif str(result.get("status") or "") == "unresolved" and reason.lower().startswith("network:"):
        out.setdefault("error_type", "network")
        out.setdefault("error_reason", reason)
        out.setdefault("retryable", True)
    return out


def normalize_provider_result(value: object) -> ProviderResult:
    """Normalize the current fetch-provider list or structured result."""
    if isinstance(value, ProviderResult):
        return value
    if isinstance(value, Mapping) and "items" in value:
        raw_items = value.get("items")
        items = list(raw_items) if isinstance(raw_items, (list, tuple)) else []
        return ProviderResult(
            items=[item for item in items if isinstance(item, dict)],
            status=str(value.get("status") or "ok"),
            error_type=value.get("error_type") if isinstance(value.get("error_type"), str) else None,
            error_reason=value.get("error_reason") if isinstance(value.get("error_reason"), str) else None,
            retryable=value.get("retryable") if isinstance(value.get("retryable"), bool) else None,
            http_status=value.get("http_status") if isinstance(value.get("http_status"), int) else None,
        )
    if isinstance(value, (list, tuple)):
        return ProviderResult(items=[item for item in value if isinstance(item, dict)])
    return ProviderResult(
        items=[], status="error", error_type="invalid_provider_result",
        error_reason=f"expected a list or ProviderResult, got {type(value).__name__}", retryable=False,
    )


def _with_scoped_failure(result: ProviderResult, scope: Mapping[str, object]) -> ProviderResult:
    exc = scope.get("exception")
    if not isinstance(exc, BaseException):
        return result
    diagnostics = _diagnostics_from_exception(exc)
    return ProviderResult(
        items=result.items,
        status=result.status if result.status not in {"ok", "skipped"} else (
            "partial" if result.items else "error"
        ),
        error_type=result.error_type or diagnostics["error_type"],
        error_reason=result.error_reason or diagnostics["error_reason"],
        retryable=result.retryable if result.retryable is not None else diagnostics["retryable"],
        http_status=result.http_status if result.http_status is not None else diagnostics["http_status"],
    )


def _manifest(module: object) -> dict[str, object]:
    raw = getattr(module, "MANIFEST", None)
    return raw if isinstance(raw, dict) else {}


def origin_name(module: object | None) -> str:
    if module is None:
        return ""
    manifest = _manifest(module)
    origin = str(manifest.get("origin") or getattr(module, "NAME", "") or "").strip().lower()
    return origin


def _via_aliases(module: object) -> set[str]:
    aliases = set()
    manifest = _manifest(module)
    for value in manifest.get("via_aliases") or []:
        text = str(value or "").strip().lower()
        if text:
            aliases.add(text)
    for value in (
        origin_name(module),
        getattr(module, "NAME", ""),
        getattr(module, "RESOLVE_NAME", ""),
    ):
        text = str(value or "").strip().lower()
        if text:
            aliases.add(text)
    return aliases


def _resolve_name(module: object) -> str:
    return str(
        getattr(module, "RESOLVE_NAME", None)
        or getattr(module, "NAME", "")
        or ""
    ).strip()


def _resolve_capable(module: object) -> bool:
    return bool(
        _resolve_name(module)
        and callable(getattr(module, "supports", None))
        and callable(getattr(module, "discover", None))
    )


def call_resolver(module: object, ref: dict) -> dict | None:
    """Invoke a resolver while preserving structured provider diagnostics.

    Current resolver modules return one result mapping or ``None``.
    """
    via = _resolve_name(module) or _provider_label(module)
    supports = getattr(module, "supports", None)
    try:
        if callable(supports) and not supports(ref):
            return None
        discover = getattr(module, "discover", None)
        if not callable(discover):
            return None
        with resolve_http.provider_failure_scope() as scope:
            with credential_scope_for_resolver(module):
                raw = discover(ref)
    except resolve_http.ProviderCooldownDeferred:
        raise
    except Exception as exc:
        return {
            "status": "unresolved", "via": via,
            "reason": f"provider failure: {resolve_http.error_reason(exc)}",
            **{
                key: value
                for key, value in _diagnostics_from_exception(exc).items()
                if value is not None
            },
        }

    if raw is None:
        scoped = _with_scoped_failure(ProviderResult(items=[]), scope)
        if not scoped.error_type:
            return None
        return {
            "status": "unresolved", "via": via,
            "reason": scoped.error_reason,
            **{key: value for key, value in scoped.to_dict().items()
               if key != "items" and value is not None},
        }
    if isinstance(raw, Mapping):
        result = dict(raw)
        result.setdefault("via", via)
        result.update({key: value for key, value in _diagnostics_from_result(result).items()
                       if result.get(key) is None})
        scoped = _with_scoped_failure(ProviderResult(items=[]), scope)
        if scoped.error_type:
            result.setdefault("error_type", scoped.error_type)
            result.setdefault("error_reason", scoped.error_reason)
            result.setdefault("retryable", scoped.retryable)
            if scoped.http_status is not None:
                result.setdefault("http_status", scoped.http_status)
        return result

    raise TypeError("resolver must return a mapping or None")


def _fetch_capable(module: object) -> bool:
    if bool(getattr(module, "PREPRINT_RESOLVER", False)):
        return True
    for attr in _FETCH_CAPABILITY_ATTRS:
        if callable(getattr(module, attr, None)):
            return True
    return False


def _discover_modules() -> dict[str, ModuleType]:
    registry: dict[str, ModuleType] = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = str(module_info.name or "").strip().lower()
        if not name or name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        registry[name] = module
    return registry


def get_registry() -> dict[str, ModuleType]:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _discover_modules()
    return dict(_REGISTRY_CACHE)


def get(name: str | None) -> ModuleType | None:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return None
    registry = get_registry()
    module = registry.get(normalized)
    if module is not None:
        return module
    for candidate in registry.values():
        resolve_name = _resolve_name(candidate).lower()
        if resolve_name == normalized:
            return candidate
    return None


def _resolve_key(module: object) -> str:
    module_name = str(getattr(module, "__name__", "") or "")
    return module_name.rsplit(".", 1)[-1].strip().lower()


def merge_order(
    registry: dict[str, object],
    configured: list[str] | None = None,
) -> list[str]:
    aliases: dict[str, str] = {}
    for name, module in registry.items():
        normalized_name = str(name).strip().lower()
        aliases[normalized_name] = name
        resolve_name = _resolve_name(module)
        if resolve_name:
            aliases[resolve_name.strip().lower()] = name
        module_key = _resolve_key(module)
        if module_key:
            aliases[module_key] = name
    requested: list[str] = []
    for item in configured or provider_config.load().get("resolve_order") or []:
        normalized = str(item).strip().lower()
        resolved = aliases.get(normalized)
        if resolved is None and f"{normalized}_search" in registry:
            resolved = f"{normalized}_search"
        requested.append(resolved if resolved is not None else normalized)
    return provider_config.merge_order(
        list(registry.keys()),
        requested,
        label="provider resolve",
        warn_unknown=False,
    )


def load_order(registry: dict[str, object] | None = None) -> list[str]:
    return merge_order(registry or REGISTRY)


def resolve_capable() -> dict[str, ModuleType]:
    registry = get_registry()
    modules_by_name: dict[str, ModuleType] = {}
    for module in registry.values():
        if not _resolve_capable(module):
            continue
        resolve_name = _resolve_name(module)
        if resolve_name:
            modules_by_name[resolve_name] = module
    ordered_names = load_order(modules_by_name)
    return {name: modules_by_name[name] for name in ordered_names if name in modules_by_name}


def resolve_enrichers() -> list[ModuleType]:
    enrichers: list[ModuleType] = []
    for module in resolve_capable().values():
        if callable(getattr(module, "enrich", None)):
            enrichers.append(module)
    return enrichers


def provider_name_for_via(via: str | None) -> str | None:
    via_text = str(via or "").strip().lower()
    if not via_text:
        return None
    for module in get_registry().values():
        aliases = _via_aliases(module)
        if via_text in aliases:
            return origin_name(module) or _resolve_key(module)
        if any(via_text.startswith(f"{alias}:") for alias in aliases):
            return origin_name(module) or _resolve_key(module)
    return None


def is_weak_abstract_origin(via: str | None) -> bool:
    via_text = str(via or "").strip().lower()
    if not via_text:
        return False
    for module in get_registry().values():
        if not _manifest(module).get("weak_abstract_origin"):
            continue
        aliases = _via_aliases(module)
        if via_text in aliases or any(via_text.startswith(f"{alias}:") for alias in aliases):
            return True
    return False


def load_module(name: str):
    module = get_registry().get(str(name or "").strip().lower())
    if module is None or not _fetch_capable(module):
        return None
    return module


def request_headers_for_candidate(
    candidate: Mapping[str, object], *, environ: dict[str, str] | None = None
) -> dict[str, str] | None:
    """Obtain provider credentials only at the outbound-request boundary.

    Candidate rows are persisted and traced, so credentials must never be put
    on them.  A provider may instead expose ``request_headers(candidate,
    environ)``; this narrow hook is invoked immediately before the standard
    fetch transport sends the candidate URL.
    """
    if not isinstance(candidate, Mapping):
        return None
    claims = []
    for candidate_module in get_registry().values():
        owns = getattr(candidate_module, "owns_request_url", None)
        if callable(owns):
            try:
                if owns(candidate):
                    claims.append(candidate_module)
            except Exception:
                claims.append(candidate_module)
    if len(claims) > 1:
        return {}
    if claims:
        module = claims[0]
        claimed = True
    else:
        module = load_module(str(candidate.get("method") or ""))
        claimed = False
    hook = getattr(module, "request_headers", None) if module is not None else None
    if not callable(hook):
        return {} if claimed else None
    try:
        raw = hook(candidate, environ=os.environ if environ is None else environ)
    except Exception:
        return {}
    if not isinstance(raw, Mapping):
        return {}
    headers: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        text = str(value or "").strip()
        if name and text and "\r" not in name + text and "\n" not in name + text:
            headers[name] = text
    binding = (
        _credential_for_fetch_provider(module, environ)
        if module is not None
        else None
    )
    if binding and headers:
        # A dict subclass carries only the public env name/provider, never the
        # header value.  The transport consumes it at the physical-call scope.
        class _CredentialHeaders(dict):
            pass
        result = _CredentialHeaders(headers)
        result.credential_binding = binding
        return result
    return headers


def discover_names() -> list[str]:
    discovered = [
        name for name, module in get_registry().items()
        if _fetch_capable(module)
    ]
    return sorted(discovered)


def fetch_capable(environ: dict[str, str] | None = None) -> dict[str, ModuleType]:
    modules_by_name = {
        name: module for name, module in get_registry().items() if _fetch_capable(module)
    }
    ordered_names = provider_config.merge_order(
        list(modules_by_name.keys()),
        provider_config.load(environ=environ).get("fetch_order") or [],
        label="provider fetch",
        warn_unknown=False,
    )
    return {name: modules_by_name[name] for name in ordered_names if name in modules_by_name}


def configured_names(environ: dict[str, str] | None = None) -> list[str]:
    env = environ or os.environ
    discovered = discover_names()
    config_order = provider_config.load().get("fetch_order") or []
    configured = provider_config.merge_order(
        discovered,
        list(config_order),
        label="fetch provider",
        warn_unknown=False,
    )
    raw = (env.get(ENV_FETCH_PROVIDERS) or "auto").strip()
    if not raw or raw.lower() == "auto":
        return configured
    requested = []
    known = set(discovered)
    for name in provider_config.load(environ=env).get("fetch_order") or []:
        if name in known and name not in requested:
            requested.append(name)
    return requested


def ordered_names(environ: dict[str, str] | None = None) -> list[str]:
    env = environ or os.environ
    configured = configured_names(env)
    raw = (env.get(ENV_FETCH_PROVIDER_ORDER) or "").strip()
    if raw:
        return provider_config.merge_order(
            configured,
            provider_config.load(environ=env).get("fetch_order") or [],
            label="fetch provider",
        )
    config_order = provider_config.load().get("fetch_order") or []
    return provider_config.merge_order(
        configured,
        list(config_order),
        label="fetch provider",
        warn_unknown=False,
    )


def _enabled(module, *, ref=None, email=None, environ=None) -> bool:
    fn = getattr(module, "enabled", None)
    if callable(fn):
        return bool(fn(ref=ref, email=email, environ=environ or os.environ))
    return True


def _disabled_reason(module, *, ref=None, email=None, environ=None) -> str | None:
    fn = getattr(module, "disabled_reason", None)
    if callable(fn):
        return fn(ref=ref, email=email, environ=environ or os.environ)
    return None


def is_preprint_resolver(module) -> bool:
    return bool(getattr(module, "PREPRINT_RESOLVER", False))


def enabled_modules(*, ref=None, email=None, environ=None, preprint_resolvers=False) -> list:
    env = environ or os.environ
    modules = []
    for name in ordered_names(env):
        mod = load_module(name)
        if mod is None:
            continue
        if is_preprint_resolver(mod) != bool(preprint_resolvers):
            continue
        if _enabled(mod, ref=ref, email=email, environ=env):
            modules.append(mod)
    return modules


def _provider_workers(modules: list, environ: dict[str, str]) -> int:
    if len(modules) <= 1:
        return len(modules)
    raw = (environ.get(ENV_FETCH_PROVIDER_WORKERS) or "").strip()
    if raw:
        try:
            return max(1, min(len(modules), int(raw)))
        except ValueError:
            pass
    return min(4, len(modules))


def _provider_label(mod) -> str:
    return str(getattr(mod, "NAME", getattr(mod, "__name__", "provider")))


def _is_fetch_admission_deferred(exc: BaseException) -> bool:
    """Keep a Fetch-side admission defer out of provider error normalization."""
    try:
        from core.fetch.transport.http import FetchAdmissionDeferred
    except ImportError:
        return False
    return isinstance(exc, FetchAdmissionDeferred)


def _callback_fetch_admission_deferred(kwargs: dict):
    callback = kwargs.get("get_fn")
    getter = getattr(callback, "fetch_admission_deferred", None)
    return getter() if callable(getter) else None


def _provider_kwargs(kwargs: dict) -> dict:
    """Give each provider an independent Fetch-admission callback latch."""
    callback = kwargs.get("get_fn")
    factory = getattr(callback, "for_provider", None)
    if not callable(factory):
        return kwargs
    scoped = dict(kwargs)
    scoped["get_fn"] = factory()
    return scoped


class _ProviderItems(list):
    """Private list carrier retaining a provider admission deferral."""

    def __init__(self, items=(), *, deferred=None):
        super().__init__(items)
        self.fetch_admission_deferred = deferred


def _earliest_deferred(batches) -> BaseException | None:
    deferred = [
        getattr(batch, "fetch_admission_deferred", None)
        for batch in batches
    ]
    deferred = [item for item in deferred if _is_fetch_admission_deferred(item)]
    if not deferred:
        return None
    return min(deferred, key=lambda item: getattr(item, "not_before", float("inf")))


def _deferred_row(provider: str, exc: BaseException) -> dict:
    return {
        "provider": provider,
        "items": [],
        "status": "rate_limit_deferred",
        "error": None,
        "reason": str(exc),
        "error_type": "rate_limit",
        "error_reason": str(exc),
        "retryable": True,
        "http_status": None,
        "deferred_host": getattr(exc, "host", None),
        "deferred_until": getattr(exc, "not_before", None),
    }


def _call_provider_fn(mod, fn_name: str, kwargs: dict) -> list[dict]:
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        return []
    try:
        provider_kwargs = _provider_kwargs(kwargs)
        with resolve_http.provider_failure_scope() as scope:
            binding = _credential_for_fetch_provider(mod, provider_kwargs.get("environ"))
            if binding:
                from core.fetch.transport import transport_telemetry
                with transport_telemetry.credential_scope(
                    provider=binding[0], env_name=binding[1]
                ):
                    raw = fn(**provider_kwargs)
            else:
                raw = fn(**provider_kwargs)
        deferred = _callback_fetch_admission_deferred(provider_kwargs) or scope.get("exception")
        items = _with_scoped_failure(normalize_provider_result(raw), scope).items
        return _ProviderItems(items, deferred=deferred if _is_fetch_admission_deferred(deferred) else None)
    except Exception as exc:
        if _is_fetch_admission_deferred(exc):
            return _ProviderItems(deferred=exc)
        return []


def _provider_row(mod, fn_name: str, kwargs: dict) -> dict:
    provider = _provider_label(mod)
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        return {
            "provider": provider, "items": [], "status": "skipped",
            "error": "fn_not_available", "reason": f"{fn_name} not available",
        }
    try:
        provider_kwargs = _provider_kwargs(kwargs)
        with resolve_http.provider_failure_scope() as scope:
            binding = _credential_for_fetch_provider(mod, provider_kwargs.get("environ"))
            if binding:
                from core.fetch.transport import transport_telemetry
                with transport_telemetry.credential_scope(
                    provider=binding[0], env_name=binding[1]
                ):
                    raw = fn(**provider_kwargs)
            else:
                raw = fn(**provider_kwargs)
        deferred = _callback_fetch_admission_deferred(provider_kwargs) or scope.get("exception")
        normalized = _with_scoped_failure(normalize_provider_result(raw), scope)
        raw_items = raw.items if isinstance(raw, ProviderResult) else (
            raw.get("items") if isinstance(raw, Mapping) and "items" in raw else raw
        )
        item_count = len(raw_items) if isinstance(raw_items, (list, tuple)) else 0
        invalid_count = item_count - len(normalized.items)
        row = {
            "provider": provider,
            **normalized.to_dict(),
            "status": normalized.status if not invalid_count else "partial",
            "error": None,
        }
        if _is_fetch_admission_deferred(deferred):
            row.update(_deferred_row(provider, deferred))
            row["items"] = normalized.items
            if normalized.items:
                row["status"] = "partial"
        # A provider may use this private list subclass carrier to describe an
        # already-issued API lookup without changing the list return contract.
        attempts = getattr(raw, "_fetch_execution_attempts", None)
        if isinstance(attempts, list):
            row["execution_attempts"] = [dict(item) for item in attempts if isinstance(item, dict)]
        provider_error = getattr(raw, "_provider_error", None)
        if isinstance(provider_error, Mapping):
            row.update({
                key: provider_error.get(key)
                for key in ("status", "error_type", "error_reason", "retryable", "http_status")
                if key in provider_error
            })
        if normalized.error_type == "invalid_provider_result":
            row["reason"] = f"{fn_name} returned {type(raw).__name__}, expected a list"
            row["error"] = "invalid_provider_result"
        elif invalid_count:
            row["reason"] = f"ignored {invalid_count} non-object item(s)"
        elif normalized.error_reason:
            row["reason"] = normalized.error_reason
        return row
    except Exception as exc:
        if _is_fetch_admission_deferred(exc):
            return _deferred_row(provider, exc)
        return {
            "provider": provider, "items": [], "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "reason": f"{fn_name} raised an exception",
            **_diagnostics_from_exception(exc),
        }


def canonical_candidate_key(
    url: object,
    *,
    normalize_doi=None,
) -> str:
    """Stable, conservative identity for logical candidate URL de-duplication.

    It intentionally keeps paths and query strings untouched: repository query
    parameters often select a particular file.  DOI resolver aliases are the
    one semantic rewrite we make, because they identify the same DOI rather than
    different HTTP resources.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    host = (parsed.hostname or "").lower()
    doi_hosts = {"doi.org", "dx.doi.org", "www.doi.org", "www.dx.doi.org"}
    if host in doi_hosts:
        raw_doi = urllib.parse.unquote(parsed.path.lstrip("/"))
        normalized_doi = normalize_doi(raw_doi) if callable(normalize_doi) else raw_doi.strip()
        if normalized_doi:
            # DOI matching is case-insensitive; preserve original URL spelling
            # separately as an alias rather than splitting one logical DOI.
            return f"doi:{str(normalized_doi).lower()}"
    scheme = parsed.scheme.lower()
    if not scheme or not host:
        return text
    try:
        port = parsed.port
    except ValueError:
        return text
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _collect_provider_items(modules: list, fn_name: str, kwargs: dict, environ: dict[str, str]):
    workers = _provider_workers(modules, environ)
    if workers <= 1:
        return [_call_provider_fn(mod, fn_name, kwargs) for mod in modules]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_call_provider_fn, mod, fn_name, kwargs) for mod in modules]
        return [future.result() for future in futures]


def _collect_provider_rows(modules: list, fn_name: str, kwargs: dict, environ: dict[str, str]):
    workers = _provider_workers(modules, environ)
    if workers <= 1:
        return [_provider_row(mod, fn_name, kwargs) for mod in modules]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_provider_row, mod, fn_name, kwargs) for mod in modules]
        return [future.result() for future in futures]


def status_rows(*, ref=None, email=None, environ=None) -> list[dict]:
    env = environ or os.environ
    rows = []
    for name in configured_names(env):
        mod = load_module(name)
        if mod is None:
            rows.append({"name": name, "enabled": False, "reason": "module not loadable"})
            continue
        enabled = _enabled(mod, ref=ref, email=email, environ=env)
        rows.append(
            {
                "name": getattr(mod, "NAME", name),
                "enabled": enabled,
                "reason": None if enabled else _disabled_reason(
                    mod, ref=ref, email=email, environ=env
                ),
            }
        )
    rows.sort(key=lambda row: row["name"])
    return rows


def candidate_items(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    kind_from_url,
    get_fn,
    environ: dict[str, str] | None = None,
    preprint_resolvers: bool = False,
) -> list[dict]:
    env = environ or os.environ
    out = []
    seen: dict[tuple[str, tuple[str, str]], dict] = {}
    modules = enabled_modules(
        ref=ref, email=email, environ=env, preprint_resolvers=preprint_resolvers
    )
    batches = _collect_provider_items(
        modules,
        "candidate_items",
        {
            "ref": ref,
            "email": email,
            "normalize_doi": normalize_doi,
            "kind_from_url": kind_from_url,
            "get_fn": get_fn,
            "environ": env,
        },
        env,
    )
    for items in batches:
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            key = canonical_candidate_key(url, normalize_doi=normalize_doi)
            scope = (item.get("profile") or item.get("fetch_profile") or "",
                     item.get("strategy") or item.get("fetch_strategy") or "")
            if not url or not key:
                continue
            dedup_key = (key, scope)
            current = seen.get(dedup_key)
            if current is not None:
                aliases = list(current.get("url_aliases") or [])
                for alias in (current.get("url"), url, *(item.get("url_aliases") or [])):
                    if alias and alias not in aliases:
                        aliases.append(alias)
                if len(aliases) > 1:
                    current["url_aliases"] = aliases
                provenance = list(current.get("provenance") or [])
                for value in (current.get("discovered_via"), item.get("discovered_via"),
                              current.get("method"), item.get("method"), *(item.get("provenance") or [])):
                    if value and value not in provenance:
                        provenance.append(value)
                if provenance:
                    current["provenance"] = provenance
                continue
            row = dict(item)
            row.setdefault("candidate_key", key)
            seen[dedup_key] = row
            out.append(row)
    return _ProviderItems(out, deferred=_earliest_deferred(batches))


def preprint_candidate_items(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    kind_from_url,
    get_fn,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    return candidate_items(
        ref,
        email=email,
        normalize_doi=normalize_doi,
        kind_from_url=kind_from_url,
        get_fn=get_fn,
        environ=environ,
        preprint_resolvers=True,
    )


def landing_items(
    url: str,
    *,
    ref: dict | None = None,
    email=None,
    kind_from_url,
    get_fn,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    """Expand an already-fetched landing through provider-specific APIs.

    Landing expansion is intentionally separate from ``candidate_items``: it
    must not cause every provider to pre-fetch arbitrary publisher pages while
    the initial candidate queue is being assembled.
    """
    env = environ or os.environ
    modules = enabled_modules(ref=ref, email=email, environ=env)
    batches = _collect_provider_items(
        modules,
        "landing_items",
        {
            "url": url,
            "ref": ref,
            "email": email,
            "kind_from_url": kind_from_url,
            "get_fn": get_fn,
            "environ": env,
        },
        env,
    )
    out = []
    seen = set()
    for items in batches:
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_url = item.get("url")
            if not candidate_url or candidate_url in seen:
                continue
            seen.add(candidate_url)
            out.append(item)
    return _ProviderItems(out, deferred=_earliest_deferred(batches))


def candidate_rows(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    kind_from_url,
    get_fn,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    env = environ or os.environ
    modules = enabled_modules(ref=ref, email=email, environ=env)
    return _collect_provider_rows(
        modules,
        "candidate_items",
        {
            "ref": ref,
            "email": email,
            "normalize_doi": normalize_doi,
            "kind_from_url": kind_from_url,
            "get_fn": get_fn,
            "environ": env,
        },
        env,
    )


def direct_text_items(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    get_fn,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    env = environ or os.environ
    out = []
    seen = set()
    modules = enabled_modules(ref=ref, email=email, environ=env)
    batches = _collect_provider_items(
        modules,
        "direct_text_items",
        {
            "ref": ref,
            "email": email,
            "normalize_doi": normalize_doi,
            "get_fn": get_fn,
            "environ": env,
        },
        env,
    )
    for items in batches:
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            source_ref = item.get("source_ref") or item.get("url") or item.get("method")
            key = (item.get("method"), source_ref, text)
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(item)
    return _ProviderItems(out, deferred=_earliest_deferred(batches))


def direct_text_rows(
    ref: dict,
    *,
    email=None,
    normalize_doi,
    get_fn,
    environ: dict[str, str] | None = None,
) -> list[dict]:
    env = environ or os.environ
    modules = enabled_modules(ref=ref, email=email, environ=env)
    return _collect_provider_rows(
        modules,
        "direct_text_items",
        {
            "ref": ref,
            "email": email,
            "normalize_doi": normalize_doi,
            "get_fn": get_fn,
            "environ": env,
        },
        env,
    )


def reset_for_tests() -> None:
    global _REGISTRY_CACHE, REGISTRY, ORDER
    _REGISTRY_CACHE = None
    REGISTRY = resolve_capable()
    ORDER = load_order(REGISTRY)


REGISTRY = resolve_capable()
ORDER = load_order(REGISTRY)
