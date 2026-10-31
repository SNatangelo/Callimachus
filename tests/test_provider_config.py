# tests/test_provider_config.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
from pathlib import Path
import tempfile
import unittest
import warnings

from core.resolve import provider_config
from core.resolve import providers


class TestProviderConfig(unittest.TestCase):
    def tearDown(self):
        provider_config.reset_for_tests()

    def _write_config(self, payload: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        try:
            json.dump(payload, handle, indent=2)
        finally:
            handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_file_order_drives_resolve_and_fetch_defaults(self):
        path = self._write_config(
            {
                "resolve_order": ["crossref_metadata", "openalex"],
                "fetch_order": ["unpaywall", "openalex"],
            }
        )
        config = provider_config.load(path=path, environ={})
        self.assertEqual(config["resolve_order"], ["crossref_metadata", "openalex"])
        self.assertEqual(config["fetch_order"], ["unpaywall", "openalex"])

        registry = {
            "openalex_search": object(),
            "crossref_metadata": object(),
            "neurips_search": object(),
        }
        self.assertEqual(
            providers.merge_order(registry, config["resolve_order"]),
            ["crossref_metadata", "openalex_search", "neurips_search"],
        )

    def test_env_override_wins_over_file(self):
        path = self._write_config(
            {
                "fetch_order": ["unpaywall", "openalex", "acl"],
            }
        )
        env = {
            provider_config.ENV_FETCH_PROVIDERS: "openalex,unpaywall",
            provider_config.ENV_FETCH_PROVIDER_ORDER: "openalex,unpaywall",
        }
        config = provider_config.load(path=path, environ=env)
        self.assertEqual(config["fetch_order"], ["openalex", "unpaywall"])

    def test_missing_file_falls_back_to_current_defaults(self):
        missing = Path(tempfile.gettempdir()) / "provider-config-missing.json"
        if missing.exists():
            missing.unlink()
        config = provider_config.load(path=missing, environ={})
        self.assertEqual(config["resolve_order"], provider_config.DEFAULT_RESOLVE_ORDER)
        self.assertEqual(config["fetch_order"], provider_config.DEFAULT_FETCH_ORDER)

    def test_unknown_provider_name_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            merged = provider_config.merge_order(
                ["openalex", "unpaywall"],
                ["ghost", "openalex"],
                label="fetch provider",
            )
        self.assertEqual(merged, ["openalex", "unpaywall"])
        self.assertTrue(any("ghost" in str(item.message) for item in caught))

    def test_fetch_provider_module_uses_provider_config_order(self):
        saved_path = providers.provider_config._CONFIG_PATH
        path = self._write_config({"fetch_order": ["unpaywall", "openalex"]})
        try:
            providers.provider_config._CONFIG_PATH = Path(path)
            providers.provider_config.reset_for_tests()
            providers.reset_for_tests()
            ordered = providers.ordered_names({})
        finally:
            providers.provider_config._CONFIG_PATH = saved_path
            providers.provider_config.reset_for_tests()
            providers.reset_for_tests()
        self.assertGreaterEqual(len(ordered), 2)
        self.assertEqual(ordered[:2], ["unpaywall", "openalex"])

    def test_manifest_aggregators_rebuild_prefixed_and_hosted_sets(self):
        acl_prefixes = set(provider_config.doi_prefixes_for("acl"))
        self.assertEqual(acl_prefixes, {"10.3115/", "10.18653/"})

        official_oa_prefixes = set(provider_config.doi_prefixes_for(("acl", "cvf")))
        self.assertEqual(
            official_oa_prefixes,
            {"10.3115/", "10.18653/", "10.1109/cvpr.", "10.1109/iccv.", "10.1109/wacv."},
        )

        preprint = provider_config.preprint_markers()
        self.assertTrue(
            {
                "arxiv.org",
                "biorxiv.org",
                "medrxiv.org",
                "ssrn.com",
                "researchsquare.com",
                "preprints.org",
                "osf.io",
            }.issubset(set(preprint["host_suffixes"]))
        )
        self.assertTrue(
            {"arxiv", "biorxiv", "medrxiv", "ssrn", "preprint"}.issubset(
                set(preprint["text_markers"])
            )
        )
        self.assertEqual(len(preprint["doi_patterns"]), 7)

        trusted = provider_config.trusted_hosts()
        self.assertTrue(
            {
                "europepmc.org",
                "ebi.ac.uk",
                "ncbi.nlm.nih.gov",
                "arxiv.org",
                "openaccess.thecvf.com",
                "ssrn.com",
                "zenodo.org",
                "osf.io",
                "figshare.com",
                "biorxiv.org",
                "medrxiv.org",
                "hal.science",
                "handle.net",
            }.issubset(set(trusted["canonical_hosts"]))
        )
        self.assertEqual(set(trusted["host_markers"]), {"repository", "repo.", "eprints", "dspace"})
        self.assertEqual(
            set(trusted["jmlr_hosts"]),
            {"jmlr.org", "jmlr.csail.mit.edu"},
        )
