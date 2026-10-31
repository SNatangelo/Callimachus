#!/usr/bin/env python3
# tests/test_resolve_golden.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Golden characterization tests for the current resolver contract."""

from __future__ import annotations

import copy
import importlib
import io
import json
import os
from pathlib import Path
import re
import sys
import types
import unittest
import urllib.error


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "resolve_golden" / "cases.json"
)

_MODULE_PREFIXES = (
    "core.resolve",
    "core.resolve.providers",
)
_MODULE_EXACT = (
    "resolve",
    "core.resolve.sources",
    "sources",
    "core.resolve.retraction_watch",
    "retraction_watch",
)


def load_cases() -> list[dict]:
    with _FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _fixture_http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "fixture error", {}, io.BytesIO(b""))


def _fixture_body(rule: dict) -> str:
    if "json" in rule:
        return json.dumps(rule["json"])
    if "text" in rule:
        return rule["text"]
    raise AssertionError(f"Fixture rule missing body payload: {rule!r}")


def _install_sources_stub() -> None:
    stop_words = set(
        (
            "the and for with from that this study journal vol volume pp page pages "
            "doi http https www org com pubmed pmid abstract et al eds editor edition press "
            "springer elsevier wiley nature science lancet bmj jama cell proc natl acad sci "
            "new york london received accepted published available online article review"
        ).split()
    )
    word_re = re.compile(r"[a-zA-Zà-ÿ]{3,}")
    normalized_versions = {
        "submittedversion": "preprint",
        "acceptedversion": "accepted",
        "publishedversion": "published",
        "vor": "published",
    }
    preprint_hosts = ("arxiv.org", "biorxiv.org", "medrxiv.org", "sferics.idsia.ch")

    def _norm(text: str) -> str:
        normalized = str(text or "").lower().replace("\u00ad", "")
        return re.sub(r"(?<=\w)-\s+(?=\w)", "", normalized)

    def _tokens(text: str) -> set[str]:
        return {word for word in word_re.findall(_norm(text)) if word not in stop_words}

    def _title_key(text: str | None) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))

    def _first_author_surname(ref: dict) -> str | None:
        surname = str(ref.get("ay_surname") or "").strip().lower()
        if surname:
            return surname
        raw_entry = ref.get("raw_entry") or ""
        if not raw_entry:
            return None
        clean = re.sub(r"[{}]", "", raw_entry)
        clean = re.sub(r"\\[a-zA-Z]+\s*\{\}", "", clean)
        clean = re.sub(r"\\[a-zA-Z]+", "", clean)
        title = str(ref.get("title") or "").strip()
        if title:
            title_pattern = re.escape(title)
            title_pattern = re.sub(r"\\\s+", r"\\s+", title_pattern)
            match = re.search(title_pattern, clean, flags=re.IGNORECASE)
            if match and match.start() > 0:
                clean = clean[:match.start()].strip(" .")
        match = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+)\s+[A-Z]{2,4}\b", clean)
        if match:
            return match.group(1).lower()
        match = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+)\s+[A-Z]\b", clean)
        if match:
            return match.group(1).lower()
        match = re.match(r"\s*([A-Z][^\W\d_][\w'’.-]+),\s*[A-Z]", clean)
        if match:
            return match.group(1).lower()
        match = re.match(r"\s*[A-Z][^\W\d_][\w'’.-]+(?:\s+[A-Z]\.)+\s+([A-Z][^\W\d_][\w'’.-]+)", clean)
        if match:
            return match.group(1).lower()
        first_author = re.split(r"\s+(?:and|&)\s+|,|;", clean, maxsplit=1)[0].strip()
        tokens = re.findall(r"[A-Z][^\W\d_][\w'’.-]*", first_author)
        if len(tokens) >= 2:
            if re.fullmatch(r"[A-Z]{2,4}", tokens[1]):
                return tokens[0].lower()
            return tokens[-1].lower()
        return None

    def content_version_for(url: str | None, version: str | None = None) -> str:
        key = re.sub(r"[^a-z]", "", str(version or "").lower())
        if key in normalized_versions:
            return normalized_versions[key]
        lower_url = str(url or "").lower()
        if any(host in lower_url for host in preprint_hosts):
            return "preprint"
        return "published"

    stub = types.ModuleType("core.resolve.sources")
    stub._norm = _norm
    stub._tokens = _tokens
    stub._STOP = stop_words
    stub._title_key = _title_key
    stub._first_author_surname = _first_author_surname
    stub.content_version_for = content_version_for
    sys.modules["core.resolve.sources"] = stub
    sys.modules["sources"] = stub
    core_pkg = sys.modules.get("core")
    if core_pkg is not None:
        core_pkg.sources = stub


def _install_retraction_stub() -> None:
    stub = types.ModuleType("core.resolve.retraction_watch")
    stub.load = lambda _email=None: None
    stub.is_retracted = lambda _doi=None: False
    sys.modules["core.resolve.retraction_watch"] = stub
    sys.modules["retraction_watch"] = stub
    core_pkg = sys.modules.get("core")
    if core_pkg is not None:
        core_pkg.retraction_watch = stub


def _matches_module_scope(name: str) -> bool:
    if name in _MODULE_EXACT:
        return True
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _MODULE_PREFIXES)


def _snapshot_scoped_modules() -> dict[str, object]:
    return {
        name: module
        for name, module in list(sys.modules.items())
        if _matches_module_scope(name)
    }


def _pop_scoped_modules() -> None:
    for name in list(sys.modules):
        if _matches_module_scope(name):
            sys.modules.pop(name, None)


def _restore_scoped_modules(saved_modules: dict[str, object]) -> None:
    _pop_scoped_modules()
    sys.modules.update(saved_modules)
    core_pkg = sys.modules.get("core")
    if core_pkg is None:
        return
    saved_resolve = saved_modules.get("core.resolve")
    if saved_resolve is None:
        if hasattr(core_pkg, "resolve"):
            delattr(core_pkg, "resolve")
    else:
        core_pkg.resolve = saved_resolve
    saved_providers = saved_modules.get("core.resolve.providers")
    if saved_providers is None:
        if hasattr(core_pkg, "providers"):
            delattr(core_pkg, "providers")
    else:
        core_pkg.providers = saved_providers
    saved_sources = saved_modules.get("core.resolve.sources")
    if saved_sources is None:
        if hasattr(core_pkg, "sources"):
            delattr(core_pkg, "sources")
    else:
        core_pkg.sources = saved_sources
    saved_rw = saved_modules.get("core.resolve.retraction_watch")
    if saved_rw is None:
        if hasattr(core_pkg, "retraction_watch"):
            delattr(core_pkg, "retraction_watch")
    else:
        core_pkg.retraction_watch = saved_rw


def _load_resolve_module():
    _pop_scoped_modules()
    _install_sources_stub()
    _install_retraction_stub()
    return importlib.import_module("core.resolve.service")


def run_case(case: dict) -> dict:
    saved_modules = _snapshot_scoped_modules()
    resolve_mod = _load_resolve_module()
    acl_module = resolve_mod._resolver_registry()["acl_search"]

    saved_get = resolve_mod._get
    saved_load = resolve_mod._rw.load
    saved_is_retracted = resolve_mod._rw.is_retracted
    saved_acl_tree = acl_module._ACL_XML_TREE_CACHE
    saved_acl_cache = dict(acl_module._ACL_ANTHOLOGY_CACHE)
    saved_semantic = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    saved_lens = os.environ.get("LENS_API_KEY")
    saved_google = os.environ.get("GOOGLE_BOOKS_API_KEY")

    retracted_dois = {str(value).lower() for value in case.get("retracted_dois", [])}

    def fake_get(url, accept="application/json"):
        for rule in case.get("responses", []):
            if rule["match"] in url:
                if "http_error" in rule:
                    raise _fixture_http_error(url, int(rule["http_error"]))
                return 200, _fixture_body(rule)
        raise AssertionError(f"Unexpected URL for golden fixture {case['name']}: {url}")

    try:
        os.environ.pop("SEMANTIC_SCHOLAR_API_KEY", None)
        os.environ.pop("LENS_API_KEY", None)
        os.environ.pop("GOOGLE_BOOKS_API_KEY", None)
        resolve_mod._get = fake_get
        resolve_mod._rw.load = lambda _email: None
        resolve_mod._rw.is_retracted = lambda doi: str(doi or "").lower() in retracted_dois
        acl_module._ACL_XML_TREE_CACHE = None
        acl_module._ACL_ANTHOLOGY_CACHE = {}
        return resolve_mod.resolve(copy.deepcopy(case["ref"]))
    finally:
        resolve_mod._get = saved_get
        resolve_mod._rw.load = saved_load
        resolve_mod._rw.is_retracted = saved_is_retracted
        acl_module._ACL_XML_TREE_CACHE = saved_acl_tree
        acl_module._ACL_ANTHOLOGY_CACHE = saved_acl_cache
        if saved_semantic is None:
            os.environ.pop("SEMANTIC_SCHOLAR_API_KEY", None)
        else:
            os.environ["SEMANTIC_SCHOLAR_API_KEY"] = saved_semantic
        if saved_lens is None:
            os.environ.pop("LENS_API_KEY", None)
        else:
            os.environ["LENS_API_KEY"] = saved_lens
        if saved_google is None:
            os.environ.pop("GOOGLE_BOOKS_API_KEY", None)
        else:
            os.environ["GOOGLE_BOOKS_API_KEY"] = saved_google
        _restore_scoped_modules(saved_modules)


def _normalized_output(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    normalized = copy.deepcopy(payload)
    normalized.pop("checked_at", None)
    return normalized


class TestResolveGolden(unittest.TestCase):
    def test_resolve_matches_golden_snapshots(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                self.assertIsNotNone(case.get("expected"), "Missing expected snapshot")
                actual = _normalized_output(run_case(case))
                expected = _normalized_output(case["expected"])
                self.assertEqual(actual, expected)
