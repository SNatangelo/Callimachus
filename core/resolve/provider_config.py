#!/usr/bin/env python3
# core/resolve/provider_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared provider configuration loader.

Schema:
{
  "resolve_order": ["openalex", "neurips", ...],
  "fetch_order": ["acl", "arxiv", ...],
  "default_rate": {"per_second": 10},
  "providers": {"openalex": {"enabled": true, ...}, ...},
  "hosts": {"api.openalex.org": "openalex", ...}
}

Unknown keys are tolerated with a warning. Environment overrides keep the
existing fetch-provider variable names:
- CITATION_VERIFIER_FETCH_PROVIDERS
- CITATION_VERIFIER_FETCH_PROVIDER_ORDER
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import warnings


ENV_FETCH_PROVIDERS = "CITATION_VERIFIER_FETCH_PROVIDERS"
ENV_FETCH_PROVIDER_ORDER = "CITATION_VERIFIER_FETCH_PROVIDER_ORDER"

DEFAULT_RESOLVE_ORDER = [
    "openalex",
    "springer_meta",
    "jmlr",
    "neurips",
    "openreview",
    "tac",
    "aaai",
    "acm",
    "acl",
    "semantic_scholar",
    "arxiv",
    "core",
    "datacite",
    "lens",
    "crossref_metadata",
]

DEFAULT_FETCH_ORDER = [
    "acl",
    "arxiv",
    "biorxiv",
    "core",
    "elsevier",
    "springer_openaccess",
    "wiley",
    "curated_copies",
    "cvf",
    "europepmc",
    "openai_reports",
    "openalex",
    "pmlr",
    "repository",
    "ssrn",
    "unpaywall",
    "zenodo",
]

DEFAULT_RATE = {"per_second": 10}

DEFAULT_TRUSTED_HOSTS_EXTRA = [
    "figshare.com",
    "hal.science",
    "handle.net",
    "osf.io",
    "zenodo.org",
]

DEFAULT_TRUSTED_HOST_MARKERS = [
    "repository",
    "repo.",
    "eprints",
    "dspace",
]

DEFAULT_TRUSTED_HOST_TLDS = [
    ".edu",
    ".ac.uk",
    ".edu.au",
    ".ac.jp",
    ".ac.nz",
]

DEFAULT_CHALLENGE_PRONE_HOSTS = [
    "science.org",
    "jamanetwork.com",
    "academic.oup.com",
    "oup.com",
    "direct.mit.edu",
    "mitpressjournals.org",
    "pnas.org",
]

DEFAULT_JMLR_HOSTS = [
    "jmlr.org",
    "jmlr.csail.mit.edu",
]

DEFAULT_PREPRINT_HOSTS_EXTRA = [
    "osf.io",
    "preprints.org",
    "researchsquare.com",
]

DEFAULT_PREPRINT_MARKERS_EXTRA = [
    "preprint",
]

DEFAULT_PREPRINT_DOI_PATTERNS_EXTRA = [
    r"^10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.\d+|\d{6})",
    r"^10\.48550/arxiv\.",
    r"^10\.2139/ssrn\.",
    r"^10\.21203/",
    r"^10\.26434/",
    r"^10\.20944/preprints",
    r"^10\.31(?:219|234|235)/",
]

DEFAULT_PROVIDERS = {
    "acl": {"enabled": True},
    "arxiv": {"enabled": True, "preprint_host": True, "rate": {"per_seconds": [1, 3]}},
    "biorxiv": {"enabled": True, "preprint_host": True},
    "core": {"enabled": True, "api_key_env": "CORE_API_KEY", "rate": {"per_second": 2}},
    "elsevier": {
        "enabled": True,
        "api_key_env": "ELSEVIER_API_KEY",
        "rate": {"per_second": 2},
    },
    "springer_meta": {
        "enabled": True,
        "api_key_env": "SPRINGER_NATURE_META_API_KEY",
    },
    "springer_openaccess": {
        "enabled": True,
        "api_key_env": "SPRINGER_NATURE_OPEN_ACCESS_API_KEY",
    },
    "springer_nature": {
        "enabled": True,
        "api_key_envs": [
            "SPRINGER_NATURE_META_API_KEY",
            "SPRINGER_NATURE_OPEN_ACCESS_API_KEY",
        ],
        "rate": {"per_second": 1},
    },
    "wiley": {
        "enabled": True,
        "api_key_env": "TDM_API_TOKEN",
        "rate": {"per_seconds": [1, 10]},
    },
    "crossref": {"enabled": True, "rate": {"per_second": 20}},
    "crossref_metadata": {"enabled": True},
    "crossref_coordinate_occupancy": {"enabled": True, "rate": {"per_second": 20}},
    "cvf": {"enabled": True},
    "datacite": {"enabled": True, "rate": {"per_second": 10}},
    "europepmc": {"enabled": True},
    "googlebooks": {"enabled": True, "rate": {"per_second": 5}},
    "jmlr": {"enabled": True},
    "lens": {"enabled": True, "rate": {"per_second": 1}},
    "ncbi": {
        "enabled": True,
        "api_key_env": "NCBI_API_KEY",
        "api_key_envs": ["NCBI_API_KEY", "ENTREZ_API_KEY"],
        "rate": {"per_second": 3, "with_api_key_per_second": 10},
    },
    "pmc_complete_issue": {"enabled": True},
    "springerlink_official_issue": {"enabled": True, "rate": {"per_second": 1}},
    "gwilr_official_issue": {"enabled": True, "rate": {"per_second": 2}},
    "stanford_law_review_official_issue": {"enabled": True, "rate": {"per_second": 2}},
    "ucea_review_official_issue": {"enabled": True, "rate": {"per_second": 1}},
    "jstor": {"enabled": True, "rate": {"per_second": 2}},
    "neurips": {"enabled": True},
    "openalex": {
        "enabled": True,
        "api_key_env": "OPENALEX_API_KEY",
        "rate": {"per_second": 5, "with_api_key_per_second": 30},
    },
    "openreview": {"enabled": True},
    "repository": {"enabled": True},
    "semantic_scholar": {"enabled": True, "rate": {"per_seconds": [1, 2]}},
    "ssrn": {"enabled": True, "preprint_host": True},
    "tac": {"enabled": True},
    "aaai": {"enabled": True},
    "acm": {"enabled": True},
    "unpaywall": {"enabled": True},
    "zenodo": {
        "enabled": True,
        "rate": {"per_seconds": [1, 2]},
    },
}

DEFAULT_HOSTS = {
    "openalex.org": "openalex",
    "crossref.org": "crossref",
    "export.arxiv.org": "arxiv",
    "eutils.ncbi.nlm.nih.gov": "ncbi",
    "cdn.ncbi.nlm.nih.gov": "ncbi",
    "datacite.org": "datacite",
    "core.ac.uk": "core",
    "api.elsevier.com": "elsevier",
    "api.springernature.com": "springer_nature",
    "api.wiley.com": "wiley",
    "semanticscholar.org": "semantic_scholar",
    "lens.org": "lens",
    "books.googleapis.com": "googlebooks",
    "zenodo.org": "zenodo",
    "www.thegwilr.org": "gwilr_official_issue",
    "thegwilr.org": "gwilr_official_issue",
    "www.stanfordlawreview.org": "stanford_law_review_official_issue",
    "review.law.stanford.edu": "stanford_law_review_official_issue",
    "www.ucea.org": "ucea_review_official_issue",
    "link.springer.com": "springerlink_official_issue",
    "www.jstor.org": "jstor",
}

_TOP_LEVEL_KEYS = {
    "resolve_order",
    "fetch_order",
    "default_rate",
    "providers",
    "hosts",
    "trusted_hosts_extra",
    "trusted_host_markers",
    "trusted_host_tlds",
    "challenge_prone_hosts",
    "jmlr_hosts",
    "preprint_hosts_extra",
    "preprint_markers_extra",
    "preprint_doi_patterns_extra",
}
_FILE_CACHE: dict[str, object] | None = None
_HERE = Path(__file__).resolve().parent
_CONFIG_PATH = _HERE / "providers.json"


def _warn(message: str) -> None:
    warnings.warn(message, RuntimeWarning, stacklevel=2)


def _normalize_name_list(raw, *, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warn(f"{label} must be a list; ignoring invalid value")
        return []
    out: list[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            _warn(f"{label} contains a non-string item; skipping it")
            continue
        name = item.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _normalize_text_list(raw, *, label: str, lowercase: bool = True) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warn(f"{label} must be a list; ignoring invalid value")
        return []
    out: list[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            _warn(f"{label} contains a non-string item; skipping it")
            continue
        text = item.strip()
        if lowercase:
            text = text.lower()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_env_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    seen = set()
    for piece in raw.split(","):
        name = piece.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def merge_order(
    available: list[str],
    configured: list[str] | None,
    *,
    label: str,
    warn_unknown: bool = True,
) -> list[str]:
    available_list = [str(item).strip().lower() for item in available if str(item).strip()]
    known = set(available_list)
    ordered: list[str] = []
    seen = set()
    for name in configured or []:
        normalized = str(name).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in known:
            ordered.append(normalized)
        else:
            if warn_unknown:
                _warn(f"Unknown {label} '{normalized}' in configured order; ignoring it")
    for name in sorted(known):
        if name not in seen:
            ordered.append(name)
    return ordered


def _defaults() -> dict[str, object]:
    return {
        "resolve_order": list(DEFAULT_RESOLVE_ORDER),
        "fetch_order": list(DEFAULT_FETCH_ORDER),
        "default_rate": deepcopy(DEFAULT_RATE),
        "providers": deepcopy(DEFAULT_PROVIDERS),
        "hosts": dict(DEFAULT_HOSTS),
        "trusted_hosts_extra": list(DEFAULT_TRUSTED_HOSTS_EXTRA),
        "trusted_host_markers": list(DEFAULT_TRUSTED_HOST_MARKERS),
        "trusted_host_tlds": list(DEFAULT_TRUSTED_HOST_TLDS),
        "challenge_prone_hosts": list(DEFAULT_CHALLENGE_PRONE_HOSTS),
        "jmlr_hosts": list(DEFAULT_JMLR_HOSTS),
        "preprint_hosts_extra": list(DEFAULT_PREPRINT_HOSTS_EXTRA),
        "preprint_markers_extra": list(DEFAULT_PREPRINT_MARKERS_EXTRA),
        "preprint_doi_patterns_extra": list(DEFAULT_PREPRINT_DOI_PATTERNS_EXTRA),
    }


def _read_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"Could not read provider config '{path}': {exc}")
        return {}
    if not isinstance(raw, dict):
        _warn(f"Provider config '{path}' must be a JSON object; ignoring it")
        return {}
    return raw


def _merge_file_data(raw: dict[str, object]) -> dict[str, object]:
    merged = _defaults()
    for key in raw.keys():
        if key not in _TOP_LEVEL_KEYS:
            _warn(f"Unknown provider config key '{key}'; ignoring it")
    resolve_order = _normalize_name_list(raw.get("resolve_order"), label="resolve_order")
    if resolve_order:
        merged["resolve_order"] = resolve_order
    fetch_order = _normalize_name_list(raw.get("fetch_order"), label="fetch_order")
    if fetch_order:
        merged["fetch_order"] = fetch_order
    default_rate = raw.get("default_rate")
    if isinstance(default_rate, dict):
        merged["default_rate"] = deepcopy(default_rate)
    elif default_rate is not None:
        _warn("default_rate must be an object; ignoring invalid value")
    providers = raw.get("providers")
    if isinstance(providers, dict):
        merged_providers = merged["providers"]
        assert isinstance(merged_providers, dict)
        for name, config in providers.items():
            if not isinstance(name, str) or not isinstance(config, dict):
                _warn("providers entries must be provider-name -> object; skipping invalid entry")
                continue
            merged_providers[name.strip().lower()] = deepcopy(config)
    elif providers is not None:
        _warn("providers must be an object; ignoring invalid value")
    hosts = raw.get("hosts")
    if isinstance(hosts, dict):
        merged_hosts = merged["hosts"]
        assert isinstance(merged_hosts, dict)
        for host, provider_name in hosts.items():
            if not isinstance(host, str) or not isinstance(provider_name, str):
                _warn("hosts entries must be host -> provider string; skipping invalid entry")
                continue
            merged_hosts[host.strip().lower()] = provider_name.strip().lower()
    elif hosts is not None:
        _warn("hosts must be an object; ignoring invalid value")
    list_keys = (
        ("trusted_hosts_extra", True),
        ("trusted_host_markers", True),
        ("trusted_host_tlds", True),
        ("challenge_prone_hosts", True),
        ("jmlr_hosts", True),
        ("preprint_hosts_extra", True),
        ("preprint_markers_extra", True),
        ("preprint_doi_patterns_extra", False),
    )
    for key, lowercase in list_keys:
        if key not in raw:
            continue
        merged[key] = _normalize_text_list(raw.get(key), label=key, lowercase=lowercase)
    return merged


def _base_config(path: Path | None = None) -> dict[str, object]:
    global _FILE_CACHE
    resolved_path = Path(path) if path is not None else _CONFIG_PATH
    if path is None and _FILE_CACHE is not None:
        return deepcopy(_FILE_CACHE)
    merged = _merge_file_data(_read_file(resolved_path))
    if path is None:
        _FILE_CACHE = deepcopy(merged)
    return merged


def _apply_fetch_env_overrides(config: dict[str, object], environ: dict[str, str]) -> dict[str, object]:
    out = deepcopy(config)
    fetch_order = list(out.get("fetch_order") or [])
    raw_providers = (environ.get(ENV_FETCH_PROVIDERS) or "").strip()
    providers_override = False
    if raw_providers and raw_providers.lower() != "auto":
        providers_override = True
        requested = _parse_env_list(raw_providers)
        known = set(fetch_order)
        fetch_order = [name for name in requested if name in known]
    raw_order = (environ.get(ENV_FETCH_PROVIDER_ORDER) or "").strip()
    if raw_order:
        fetch_order = merge_order(fetch_order, _parse_env_list(raw_order), label="fetch provider")
    elif providers_override:
        fetch_order = sorted(fetch_order)
    out["fetch_order"] = fetch_order
    return out


def load(
    environ: dict[str, str] | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    base = _base_config(Path(path) if path is not None else None)
    return _apply_fetch_env_overrides(base, environ or os.environ)


def reset_for_tests() -> None:
    global _FILE_CACHE
    _FILE_CACHE = None


def _resolve_capable(module: object) -> bool:
    return bool(
        callable(getattr(module, "supports", None))
        and callable(getattr(module, "discover", None))
    )


def _fetch_capable(module: object) -> bool:
    if bool(getattr(module, "PREPRINT_RESOLVER", False)):
        return True
    for attr in (
        "enabled",
        "disabled_reason",
        "candidate_items",
        "candidate_rows",
        "direct_text_items",
        "direct_text_rows",
    ):
        if callable(getattr(module, attr, None)):
            return True
    return False


def _provider_modules() -> dict[str, object]:
    try:
        from core.resolve import providers as provider_modules
    except ImportError:  # direct execution fallback
        import providers as provider_modules

    return provider_modules.get_registry()


def _module_key(module: object) -> str:
    return str(getattr(module, "__name__", "") or "").rsplit(".", 1)[-1].strip().lower()


def _module_aliases(module: object) -> set[str]:
    aliases = {_module_key(module)}
    for attr in ("NAME", "RESOLVE_NAME"):
        value = str(getattr(module, attr, "") or "").strip().lower()
        if value:
            aliases.add(value)
    return aliases


def _manifest(module: object) -> dict[str, object]:
    raw = getattr(module, "MANIFEST", None)
    return raw if isinstance(raw, dict) else {}


def _manifest_text_list(module: object, key: str, *, lowercase: bool = True) -> tuple[str, ...]:
    values = _normalize_text_list(_manifest(module).get(key), label=f"{_module_key(module)}.{key}", lowercase=lowercase)
    return tuple(values)


def _selector_matches(module: object, selector) -> bool:
    if selector is None:
        return True
    if isinstance(selector, (list, tuple, set)):
        return any(_selector_matches(module, item) for item in selector)
    normalized = str(selector or "").strip().lower()
    if not normalized or normalized in {"all", "*"}:
        return True
    if normalized in {"resolve", "resolve_capable"}:
        return _resolve_capable(module)
    if normalized in {"fetch", "fetch_capable"}:
        return _fetch_capable(module)
    return normalized in _module_aliases(module)


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(sorted(out))


def doi_prefixes_for(capability=None) -> tuple[str, ...]:
    prefixes: list[str] = []
    for module in _provider_modules().values():
        if not _selector_matches(module, capability):
            continue
        prefixes.extend(_manifest_text_list(module, "doi_prefixes"))
    return _ordered_unique(prefixes)


def official_oa_doi_prefixes() -> tuple[str, ...]:
    return doi_prefixes_for(("acl", "cvf"))


def provider_canonical_hosts(capability=None) -> tuple[str, ...]:
    hosts: list[str] = []
    for module in _provider_modules().values():
        if not _selector_matches(module, capability):
            continue
        hosts.extend(_manifest_text_list(module, "canonical_hosts"))
    return _ordered_unique(hosts)


def acl_doi_prefixes() -> tuple[str, ...]:
    return doi_prefixes_for("acl")


def acl_canonical_hosts() -> tuple[str, ...]:
    return provider_canonical_hosts("acl")


def preprint_markers() -> dict[str, tuple[str, ...]]:
    config = load(environ={})
    host_suffixes: list[str] = []
    text_markers: list[str] = []
    doi_prefixes: list[str] = []
    for module in _provider_modules().values():
        manifest = _manifest(module)
        if not manifest.get("preprint_host"):
            continue
        host_suffixes.extend(_manifest_text_list(module, "canonical_hosts"))
        text_markers.extend(_manifest_text_list(module, "host_markers"))
        doi_prefixes.extend(_manifest_text_list(module, "doi_prefixes"))
    host_suffixes.extend(config.get("preprint_hosts_extra") or [])
    text_markers.extend(config.get("preprint_markers_extra") or [])
    return {
        "host_suffixes": _ordered_unique(host_suffixes),
        "text_markers": _ordered_unique(text_markers),
        "doi_prefixes": _ordered_unique(doi_prefixes),
        "doi_patterns": _ordered_unique(list(config.get("preprint_doi_patterns_extra") or [])),
    }


def trusted_hosts() -> dict[str, object]:
    config = load(environ={})
    canonical_hosts: list[str] = []
    provider_hosts: dict[str, dict[str, tuple[str, ...]]] = {}
    for module in _provider_modules().values():
        key = _module_key(module)
        hosts = _manifest_text_list(module, "canonical_hosts")
        markers = _manifest_text_list(module, "host_markers")
        provider_hosts[key] = {
            "canonical_hosts": hosts,
            "host_markers": markers,
        }
        canonical_hosts.extend(hosts)
    canonical_hosts.extend(config.get("trusted_hosts_extra") or [])
    return {
        "canonical_hosts": _ordered_unique(canonical_hosts),
        "host_markers": _ordered_unique(list(config.get("trusted_host_markers") or [])),
        "host_tlds": _ordered_unique(list(config.get("trusted_host_tlds") or [])),
        "challenge_hosts": _ordered_unique(list(config.get("challenge_prone_hosts") or [])),
        "jmlr_hosts": _ordered_unique(list(config.get("jmlr_hosts") or [])),
        "providers": provider_hosts,
    }
