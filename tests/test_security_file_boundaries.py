# tests/test_security_file_boundaries.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused stdlib regressions for symlink and tracked-path boundaries."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _temporary_workspace() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(
        dir=globals().get("SECURITY_TEST_SCRATCH", ROOT), prefix="security-file-boundaries-",
    )


def _load_library_files():
    source_path = ROOT / "core" / "app" / "library" / "importer.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    importer = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LibraryImporter"
    )
    files_method = next(
        node for node in importer.body
        if isinstance(node, ast.FunctionDef) and node.name == "_files"
    )
    files_method.decorator_list = []
    link_helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_is_link_or_reparse"
    )
    isolated = ast.Module(body=[link_helper, files_method], type_ignores=[])
    namespace = {
        "Path": Path,
        "os": os,
        "stat": stat,
        "_REPARSE_POINT": getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        "parse_extract": SimpleNamespace(supported_extensions=lambda: (".pdf", ".txt")),
        "io_path": lambda path: os.fspath(path),
        "logical_path": lambda path, strict=False: Path(path).resolve(strict=strict),
    }
    exec(compile(ast.fix_missing_locations(isolated), str(source_path), "exec"), namespace)
    return namespace["_files"]




def _fake_git(root: Path, tracked: list[str]):
    encoded = b"\0".join(path.encode("utf-8") for path in tracked) + b"\0"

    def run(argv, **_kwargs):
        if argv[1] == "rev-parse":
            return SimpleNamespace(stdout=str(root))
        return SimpleNamespace(stdout=encoded)

    return run


class LibraryImportBoundaryTests(unittest.TestCase):
    def test_folder_scan_skips_file_and_directory_links_but_explicit_files_work(self):
        discover = _load_library_files()
        with _temporary_workspace() as temporary:
            base = Path(temporary)
            selected = base / "incoming"
            selected.mkdir()
            ordinary = selected / "ordinary.txt"
            ordinary.write_text("inside", encoding="utf-8")
            outside_file = base / "outside.pdf"
            outside_file.write_bytes(b"external file bytes")
            outside_directory = base / "outside-directory"
            outside_directory.mkdir()
            (outside_directory / "nested.pdf").write_bytes(b"nested external bytes")
            file_link = selected / "linked.pdf"
            directory_link = selected / "linked-directory"
            try:
                file_link.symlink_to(outside_file)
                directory_link.symlink_to(outside_directory, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            self.assertEqual(discover([str(selected)]), [ordinary.resolve()])
            self.assertEqual(discover([str(ordinary)]), [ordinary.resolve()])
            # Selecting a file path explicitly keeps the existing direct-file behavior.
            self.assertEqual(discover([str(file_link)]), [outside_file.resolve()])

    def test_inspect_never_hashes_or_copies_external_folder_links(self):
        from unittest.mock import Mock
        source_path=ROOT/"core/app/library/importer.py"
        tree=ast.parse(source_path.read_text(encoding="utf-8"))
        importer=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="LibraryImporter")
        method=next(n for n in importer.body if isinstance(n,ast.FunctionDef) and n.name=="inspect")
        hash_file=Mock(side_effect=AssertionError("external file was hashed"))
        copy_file=Mock(side_effect=AssertionError("external file was copied"))
        scope={"Path":Path,"io_path":os.fspath,"_sha256":hash_file,
               "shutil":SimpleNamespace(copyfile=copy_file),"_json_write":Mock()}
        module=ast.Module(body=ast.parse("from __future__ import annotations").body+[method],type_ignores=[])
        exec(compile(module,str(source_path),"exec"),scope)
        with _temporary_workspace() as temporary:
            base=Path(temporary);folder=base/"incoming";folder.mkdir()
            outside=base/"outside.pdf";outside.write_bytes(b"dummy external content")
            try:
                (folder/"linked.pdf").symlink_to(outside)
            except (NotImplementedError,OSError) as exc:
                self.skipTest(str(exc))
            instance=SimpleNamespace(_files=_load_library_files(),session_id="dummy",
                _session_dir=lambda _:base/"session",_session_path=lambda _:base/"review.json")
            self.assertEqual(scope["inspect"](instance,[str(folder)]),[])
            hash_file.assert_not_called();copy_file.assert_not_called()




if __name__ == "__main__":
    unittest.main()
