# tests/test_run_provenance.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.infra.db.repository import RunRepository


_TOOL = Path(__file__).resolve().parents[1] / "tools" / "run_provenance.py"
_SPEC = importlib.util.spec_from_file_location("run_provenance", _TOOL)
run_provenance = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(run_provenance)


class RunProvenanceTests(unittest.TestCase):
    def test_reads_persisted_revision_and_manifest_not_workspace_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = RunRepository.create(tmp, run_id="r", input_path="", input_sha256="x", accuracy="standard", style=None, model_id=None, http_profile=None, challenge_mode=None, fixture_fingerprint="f", parent_run_id="parent", run_origin="forked_from_frozen_fetch")
            repo.update_run_settings({"verify_runtime": {
                "code_revision": "run-revision", "code_dirty": True,
                "code_diff_sha256": "d" * 64, "code_snapshot_id": "e" * 64,
                "backend": None, "model": None, "reasoning": None,
                "reasoning_effort": None, "context_profile": "large",
                "max_source_chars": "", "require_fulltext": False,
                    "semantic_contract": "verify-claim-evidence-v10",
            }, "debug_mode": True, "debug_labels": ["fork:frozen_fetch_verify"]})
            repo.replace_parse_payload(
                claims=[],
                references=[{"id": "r1", "ref_number": 1, "raw_entry": "Reference"}],
                citations=[],
                manuscript_text="Complete manuscript text.",
            )
            repo.store_source_text(source_text_id="s1", ref_id="r1", identity_key="r1", tier="fulltext", origin="pdf", stored_path="a.txt", sha256="abc", char_count=12, extraction_flags=["fragmented_lines", "repeated_line"], extraction_method="pdf")
            repo._conn.execute("PRAGMA journal_mode = DELETE")
            repo.close()
            db = Path(tmp) / "run.sqlite"
            before = db.read_bytes()
            before_mtime = db.stat().st_mtime_ns
            sidecars = {suffix: (Path(str(db) + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")}
            payload = run_provenance.provenance(Path(tmp))
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(db.stat().st_mtime_ns, before_mtime)
            self.assertEqual({suffix: (Path(str(db) + suffix)).exists() for suffix in sidecars}, sidecars)
            self.assertEqual(payload["run_revision"], "run-revision")
            self.assertTrue(payload["run_code_dirty"])
            self.assertEqual(payload["run_code_diff_sha256"], "d" * 64)
            self.assertEqual(payload["run_code_snapshot_id"], "e" * 64)
            self.assertTrue(payload["debug_mode"])
            self.assertEqual(
                payload["debug_labels"], ["fork:frozen_fetch_verify"])
            self.assertIsNone(payload["input_path"])
            self.assertEqual(payload["snapshot_kind"], "frozen_fetch")
            manifest = [{"ref_id": "r1", "tier": "fulltext", "origin": "pdf", "stored_path": "a.txt", "sha256": "abc", "char_count": 12, "content_version": None, "provenance_relation": None, "extraction_method": "pdf", "extraction_flags": ["fragmented_lines", "repeated_line"]}]
            expected_manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            self.assertEqual(payload["source_manifest_sha256"], expected_manifest_hash)
            self.assertEqual(payload["source_manifest_entries"], 1)
            self.assertEqual(len(payload["source_manifest_sha256"]), 64)

            completed = subprocess.run(
                [sys.executable, str(_TOOL), str(tmp)],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(completed.stdout)["source_manifest_sha256"], expected_manifest_hash)


if __name__ == "__main__":
    unittest.main()
