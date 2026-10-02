#!/usr/bin/env python3
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Public report redaction must remove hidden source prose while retaining verdicts."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from redact_report_html import RedactionError, main, redact_html  # noqa: E402


SECRET = "Unpublished source passage " * 12
CLAIM = "The manuscript makes this claim."
QUOTE = "The brief source excerpt supporting the verdict."
MARKER = "(Hammond and Youngs 2011); (Santtila et al. 2007); " * 4
RESOLVER_BODY = '{"journal":"metadata","count":42}'


def _report():
    payload = {
        "projection": {
            "schema_version": 1,
            "manuscript": {"title": "Example", "input_path": "private/input.pdf",
                           "title_identity": {"attempts": [{"abstract": SECRET}]}},
            "claims": [{"focus_text": CLAIM, "claim": {"sentence": CLAIM, "marker_raw": MARKER,
                                                      "context_window": SECRET}}],
            "sources": [{"reference": {"id": "R1", "title": "A source"},
                         "resolve": {"abstract": SECRET, "attempts": [{"abstract": SECRET}],
                                     "evidence_profile": {"resolver_coverage": {"payloads": [{
                                         "body": RESOLVER_BODY,
                                         "sha256": hashlib.sha256(RESOLVER_BODY.encode()).hexdigest(),
                                         "media_type": "application/json",
                                     }]}}},
                         "fetch_attempts": [{"trace": {"body_head": SECRET}, "outcome": "ok"}]}],
            "pairs": [{"claim_id": "C1", "ref_id": "R1",
                       "decision": {"explanation": "The quotation supports the claim.", "evidence": [QUOTE]},
                       "verification": {"evidence": [{"text": QUOTE}]},
                       "logical_requests": [{"stage": "jury1", "payload": {"source_spans": [{"text": SECRET}],
                                                                         "claim_context": SECRET,
                                                                         "citation_marker": MARKER}}],
                       "candidates": [{"candidate_id": "X1", "evidence": [SECRET], "grounded": [{"text": SECRET}], "explanation": SECRET, "outcome_fields": {"supported_part": SECRET}}],
                       "candidate_events": [{"event_type": "created", "payload": {"reason": SECRET}}],
                       "dispatch_events": [{"event_type": "complete", "payload": {"answer": SECRET, "technical_result": "answer_received"}}]}],
        },
        "render_metadata": {"projection_sha256": "original-only"},
    }
    return (
        '<!doctype html><html><body><script id="cv-data" type="application/json">'
        + json.dumps(payload)
        + '</script><script>window.reportViewer = true;</script></body></html>\n'
        + '<!-- citation-verifier-human-report-seal data=YWJj -->\n'
    ).encode("utf-8")


class TestPublicHtmlRedaction(unittest.TestCase):
    def test_redacts_hidden_prose_but_keeps_verdict_excerpts_and_provenance(self):
        original = _report()
        public, manifest = redact_html(original, timestamp="2026-09-27T00:00:00+00:00")
        text = public.decode("utf-8")
        data = json.loads(text.split('<script id="cv-data" type="application/json">', 1)[1].split("</script>", 1)[0])

        self.assertNotIn(SECRET, text)
        self.assertNotIn("private/input.pdf", text)
        self.assertNotIn("citation-verifier-human-report-seal", text)
        self.assertNotIn("Public redacted copy", text)
        self.assertIn(CLAIM, text)
        self.assertIn(QUOTE, text)
        self.assertIn(MARKER, text)
        self.assertNotIn(RESOLVER_BODY, text)
        self.assertEqual(data["projection"]["sources"][0]["resolve"]["evidence_profile"]
                         ["resolver_coverage"]["payloads"][0]["sha256"],
                         hashlib.sha256(RESOLVER_BODY.encode()).hexdigest())
        self.assertNotIn("body", data["projection"]["sources"][0]["resolve"]
                         ["evidence_profile"]["resolver_coverage"]["payloads"][0])
        self.assertNotIn("abstract", data["projection"]["manuscript"]
                         ["title_identity"]["attempts"][0])
        self.assertEqual(manifest["tool"], "core.report.human.redaction")
        self.assertEqual(manifest["tool_version"],
                         (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip())
        self.assertEqual(data["projection"]["pairs"][0]["logical_requests"][0]["stage"], "jury1")
        self.assertEqual(data["projection"]["pairs"][0]["candidates"][0]["candidate_id"], "X1")
        self.assertEqual(data["projection"]["pairs"][0]["dispatch_events"][0]["payload"],
                         {"technical_result": "answer_received"})
        self.assertEqual(data["render_metadata"], {"publication_redaction": data["publication_redaction"]})
        self.assertEqual(manifest["original_html_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest["public_html_sha256"], hashlib.sha256(public).hexdigest())
        self.assertEqual(manifest["redacted_fields"]["pairs.logical_requests.payload"], 1)
        self.assertEqual(manifest["redacted_source_ref_ids"], ["R1"])
        self.assertTrue(manifest["original_seal_present"])
        self.assertIn(SECRET.encode(), original)

    def test_unsealed_site_copy_is_recorded_and_invalid_formats_fail_closed(self):
        original = _report()
        unsealed = original.split(b"<!-- citation-verifier-human-report-seal", 1)[0]
        public, manifest = redact_html(unsealed, timestamp="2026-09-27T00:00:00+00:00")
        self.assertFalse(manifest["original_seal_present"])
        self.assertNotIn(SECRET.encode(), public)
        with self.assertRaisesRegex(RedactionError, "malformed original report seal"):
            redact_html(original.replace(b"data=YWJj", b"data=broken!"))
        with self.assertRaisesRegex(RedactionError, "unsupported report projection schema"):
            redact_html(original.replace(b'"schema_version": 1', b'"schema_version": 2'))
        with self.assertRaisesRegex(RedactionError, "non-preview, non-debug"):
            redact_html(original.replace(b'"render_metadata":', b'"preview": true, "render_metadata":'))
        with self.assertRaisesRegex(RedactionError, "non-preview, non-debug"):
            redact_html(original.replace(b'"schema_version": 1', b'"schema_version": 1, "execution": {"debug_mode": true}'))

    def test_crlf_seal_does_not_leave_a_stray_carriage_return(self):
        public, _ = redact_html(_report().replace(b"\n", b"\r\n"),
                                timestamp="2026-09-27T00:00:00+00:00")
        self.assertTrue(public.endswith(b"</html>"))

    def test_resolver_payload_without_matching_digest_fails_closed(self):
        with self.assertRaisesRegex(RedactionError, "resolver coverage payload digest mismatch"):
            redact_html(_report().replace(
                hashlib.sha256(RESOLVER_BODY.encode()).hexdigest().encode(), b"0" * 64,
            ))

    def test_cli_writes_new_html_and_matching_manifest_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            original = Path(directory) / "original.html"
            public = Path(directory) / "public.html"
            original.write_bytes(_report())
            self.assertEqual(main([str(original), "--output", str(public)]), 0)
            manifest = json.loads(public.with_suffix(".html.redaction.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["public_html_sha256"], hashlib.sha256(public.read_bytes()).hexdigest())
            self.assertEqual(main([str(original), "--output", str(public)]), 1)
            self.assertEqual(original.read_bytes(), _report())
