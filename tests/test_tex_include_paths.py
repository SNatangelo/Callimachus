#!/usr/bin/env python3
# tests/test_tex_include_paths.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import builtins
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_tex_extractor():
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "core" / "parse" / "extractors" / "tex.py"
    spec = importlib.util.spec_from_file_location(
        "tex_include_paths_under_test", source
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load TeX extractor from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tex = _load_tex_extractor()


class TexIncludePathTests(unittest.TestCase):
    def _write_document(self, root: Path, body: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        main = root / "main.tex"
        main.write_text(
            r"\begin{document}" + body + r"\end{document}", encoding="utf-8"
        )
        return main

    def _assert_rejected_before_external_open(
        self, root: Path, include: str, external: Path
    ) -> None:
        main = self._write_document(root, include)
        opened: list[str] = []
        real_open = builtins.open

        def tracked_open(file, *args, **kwargs):
            try:
                opened.append(os.path.normcase(os.path.realpath(os.fspath(file))))
            except (TypeError, ValueError):
                pass
            return real_open(file, *args, **kwargs)

        with mock.patch.object(tex, "open", new=tracked_open, create=True):
            with self.assertRaisesRegex(ValueError, "escapes document directory"):
                tex._tex_text(str(main))

        self.assertNotIn(
            os.path.normcase(os.path.realpath(external)), opened,
            "the rejected external include must not be opened",
        )

    def test_parent_traversal_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "document"
            external = base / "outside.tex"
            external.write_text("external content", encoding="utf-8")

            self._assert_rejected_before_external_open(
                root, r"\include{../outside.tex}", external
            )

    def test_absolute_prefix_sibling_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "document"
            sibling = base / "document-sibling"
            sibling.mkdir()
            external = sibling / "outside.tex"
            external.write_text("external content", encoding="utf-8")

            self._assert_rejected_before_external_open(
                root, rf"\input{{{external}}}", external
            )

    def test_symlink_escape_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "document"
            root.mkdir()
            external = base / "outside.tex"
            external.write_text("external content", encoding="utf-8")
            link = root / "escape.tex"
            try:
                link.symlink_to(external)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            self._assert_rejected_before_external_open(
                root, r"\input{escape.tex}", external
            )

    def test_in_root_nested_and_missing_includes_keep_root_relative_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "document"
            chapter_dir = root / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "chapter.tex").write_text(
                r"Chapter \input{part.tex}", encoding="utf-8"
            )
            (root / "part.tex").write_text("root-relative part", encoding="utf-8")
            (root / "appendix.tex").write_text("included appendix", encoding="utf-8")
            (chapter_dir / "part.tex").write_text(
                "chapter-relative decoy", encoding="utf-8"
            )
            main = self._write_document(
                root, r"Start \input{chapters/chapter} \include{appendix} \input{missing} End"
            )

            text, _meta = tex._tex_text(str(main))

            self.assertIn("Start", text)
            self.assertIn("Chapter", text)
            self.assertIn("root-relative part", text)
            self.assertIn("included appendix", text)
            self.assertIn("End", text)
            self.assertNotIn("chapter-relative decoy", text)
            self.assertNotIn("missing", text)


if __name__ == "__main__":
    unittest.main()
