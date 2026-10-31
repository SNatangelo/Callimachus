#!/usr/bin/env python3
# tests/test_report_html_assets.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""External locale and theme asset validation for the human HTML report."""
from tests._bootstrap import *  # noqa: F401,F403

import copy
import shutil
from pathlib import Path

from core.report.human.assets import AssetValidationError, default_asset_root, load_asset_catalog


class TestHumanReportAssets(unittest.TestCase):
    def test_defaults_and_discovery_are_stable(self):
        first = load_asset_catalog()
        second = load_asset_catalog()

        self.assertEqual(first.default_locale, "en")
        self.assertEqual(first.default_theme, "callimachus")
        self.assertEqual(tuple(first.locales), ("en", "it"))
        self.assertEqual(tuple(first.themes), ("callimachus", "high_contrast"))
        self.assertEqual(first, second)
        self.assertIn("--cv-nav-bg", first.theme_tokens)
        self.assertIn("--cv-surface-soft", first.theme_tokens)
        self.assertIn("--cv-positive-soft", first.theme_tokens)
        self.assertIn("--cv-shadow-large", first.theme_tokens)
        self.assertIn("--cv-serif-family", first.theme_tokens)
        self.assertIn("--cv-hero-angle", first.theme_tokens)
        self.assertEqual(first.locales["it"].messages["count.items"], {"one": "{count} elemento", "other": "{count} elementi"})

    def test_external_valid_assets_extend_catalog_without_code_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "assets")
            shutil.copytree(default_asset_root(), root)
            with open(os.path.join(root, "locales", "fr.json"), encoding="utf-8", mode="w") as stream:
                french = copy.deepcopy(json.loads(Path(root, "locales", "en.json").read_text(encoding="utf-8")))
                french.update({"language_tag": "fr", "native_name": "Français"})
                json.dump(french, stream)
            with open(os.path.join(root, "locales", "ar.json"), encoding="utf-8", mode="w") as stream:
                arabic = copy.deepcopy(json.loads(Path(root, "locales", "en.json").read_text(encoding="utf-8")))
                arabic.update({"language_tag": "ar", "direction": "rtl", "native_name": "العربية"})
                arabic["messages"]["count.items"] = {
                    "zero": "{count}", "one": "{count}", "two": "{count}",
                    "few": "{count}", "many": "{count}", "other": "{count}",
                }
                json.dump(arabic, stream)
            with open(os.path.join(root, "themes", "paper.json"), encoding="utf-8", mode="w") as stream:
                theme = copy.deepcopy(json.loads(Path(root, "themes", "callimachus.json").read_text(encoding="utf-8")))
                theme.update({"id": "paper", "label_key": "theme.callimachus"})
                json.dump(theme, stream)

            catalog = load_asset_catalog(root)

        self.assertEqual(tuple(catalog.locales), ("ar", "en", "fr", "it"))
        self.assertEqual(tuple(catalog.themes), ("callimachus", "high_contrast", "paper"))

    def test_missing_or_invalid_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "assets")
            shutil.copytree(default_asset_root(), root)
            os.remove(os.path.join(root, "locales", "en.json"))
            with self.assertRaises(AssetValidationError):
                load_asset_catalog(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "assets")
            shutil.copytree(default_asset_root(), root)
            manifest_path = os.path.join(root, "manifest.json")
            with open(manifest_path, encoding="utf-8") as stream:
                manifest = json.load(stream)
            manifest["default_theme"] = "../unsafe"
            with open(manifest_path, encoding="utf-8", mode="w") as stream:
                json.dump(manifest, stream)
            with self.assertRaises(AssetValidationError):
                load_asset_catalog(root)

    def test_locale_parity_and_theme_safety_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "assets")
            shutil.copytree(default_asset_root(), root)
            locale_path = os.path.join(root, "locales", "it.json")
            with open(locale_path, encoding="utf-8") as stream:
                italian = json.load(stream)
            italian["messages"].pop("nav.claims")
            with open(locale_path, encoding="utf-8", mode="w") as stream:
                json.dump(italian, stream)
            with self.assertRaises(AssetValidationError):
                load_asset_catalog(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = os.path.join(temporary, "assets")
            shutil.copytree(default_asset_root(), root)
            theme_path = os.path.join(root, "themes", "callimachus.json")
            with open(theme_path, encoding="utf-8") as stream:
                theme = json.load(stream)
            theme["tokens"]["--cv-text"] = "red; background: black"
            with open(theme_path, encoding="utf-8", mode="w") as stream:
                json.dump(theme, stream)
            with self.assertRaises(AssetValidationError):
                load_asset_catalog(root)

    def test_reader_first_shell_uses_external_theme_tokens(self):
        stylesheet = (default_asset_root() / "app.css").read_text(encoding="utf-8")
        for hook in (
            ".cv-sidebar", ".cv-topbar", ".cv-hero", ".cv-drawer",
            ".cv-audit", ".cv-assessment-green", ".cv-assessment-yellow",
            ".cv-assessment-red", ".cv-hero[data-assessment=", "@media print",
            ".cv-debug-banner", ".cv-metric-link",
        ):
            self.assertIn(hook, stylesheet)
        for token in (
            "--cv-nav-bg", "--cv-surface-bg", "--cv-positive", "--cv-warning",
            "--cv-negative", "--cv-serif-family", "--cv-shadow-large",
            "--cv-hero-angle",
        ):
            self.assertIn(token, stylesheet)
        self.assertNotRegex(stylesheet, r"#[0-9a-fA-F]{3,8}")
        self.assertNotIn("135deg", stylesheet)
        self.assertEqual(stylesheet.count("linear-gradient(var(--cv-hero-angle)"), 4)
        self.assertIn(".cv-filter-field { display: grid; gap: 4px; }", stylesheet)
        self.assertIn("backdrop-filter: blur(12px)", stylesheet)
        self.assertIn(".cv-metric.cv-unresolved { border-top: 3px solid var(--cv-purple); }", stylesheet)
        self.assertIn(".cv-metric.cv-related, .cv-metric.cv-off_topic", stylesheet)
        self.assertIn(".cv-pair-bar .cv-related, .cv-pair-bar .cv-off_topic", stylesheet)
        self.assertIn(".cv-legend-dot.cv-related, .cv-legend-dot.cv-off_topic", stylesheet)
        self.assertIn("height: 100dvh", stylesheet)
        self.assertIn("-webkit-overflow-scrolling: touch", stylesheet)
        self.assertIn("env(safe-area-inset-bottom)", stylesheet)
        self.assertIn(".cv-supports { --cv-semantic-color: var(--cv-positive);", stylesheet)
        self.assertIn(".cv-partial { --cv-semantic-color: var(--cv-warning);", stylesheet)
        self.assertIn(".cv-unresolved, .cv-no_outcome, .cv-non_decidable { --cv-semantic-color: var(--cv-purple);", stylesheet)
        self.assertIn(".cv-contradicts { --cv-semantic-color: var(--cv-negative);", stylesheet)
        self.assertIn(".cv-verdict-card { border-top: 3px solid var(--cv-semantic-color", stylesheet)
        self.assertIn(".cv-reason-block", stylesheet)
        self.assertIn(".cv-jury2-rejected-list", stylesheet)
        self.assertIn(".cv-verdict-attribution", stylesheet)
        self.assertIn(".cv-comparison-grid", stylesheet)
        self.assertIn(".cv-triage-row.cv-elevated", stylesheet)
        self.assertIn(".cv-badge.cv-high_fabrication_suspicion", stylesheet)
        self.assertIn(".cv-badge.cv-elevated_bibliographic_suspicion", stylesheet)

    def test_callimachus_brand_palette_is_blue_and_semantic_colors_stay_distinct(self):
        catalog = load_asset_catalog()
        tokens = catalog.themes["callimachus"].tokens

        self.assertEqual(tokens["--cv-accent"], "#2457a6")
        self.assertEqual(tokens["--cv-accent-soft"], "#e7eef8")
        self.assertEqual(tokens["--cv-accent-alt"], "#1c4485")
        self.assertEqual(tokens["--cv-focus"], "#2e6fd0")
        self.assertEqual(tokens["--cv-positive"], "#14785b")
        self.assertEqual(tokens["--cv-positive-soft"], "#e7f5ef")
        self.assertEqual(tokens["--cv-warning"], "#a66a00")
        self.assertEqual(tokens["--cv-warning-soft"], "#fff3d8")
        self.assertEqual(tokens["--cv-negative"], "#b13d45")
        self.assertEqual(tokens["--cv-negative-soft"], "#fdecef")
        self.assertEqual(tokens["--cv-neutral"], "#5d6b78")
        self.assertEqual(tokens["--cv-neutral-soft"], "#edf1f4")
        self.assertEqual(tokens["--cv-purple"], "#6853b8")
        self.assertEqual(tokens["--cv-purple-soft"], "#eeeafd")
        self.assertNotEqual(tokens["--cv-accent"], tokens["--cv-positive"])

        stylesheet = (default_asset_root() / "app.css").read_text(encoding="utf-8")
        self.assertIn("outline: 3px solid var(--cv-focus)", stylesheet)
        self.assertIn("border-color: var(--cv-accent-alt)", stylesheet)
