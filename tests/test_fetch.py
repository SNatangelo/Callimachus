#!/usr/bin/env python3
# tests/test_fetch.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Fetch, source provenance, and retrieval pipeline tests."""
from tests._bootstrap import *  # noqa: F401,F403
import gzip
import zlib
from pathlib import Path
from unittest import mock
from core.fetch.queue import _ocr_backends_available, _suspicious_text_identity_fail





class TestContentEncodingDecode(unittest.TestCase):
    """A declared Content-Encoding must be undone before anything reads the body.

    We never send an Accept-Encoding, so live servers answer identity — but a
    Wayback raw (``id_``) snapshot replays the ORIGINAL captured response headers
    and all, so an archived page captured with gzip arrives compressed anyway.
    Left encoded, the extractor sees binary noise and drops a usable full text.
    """

    HTML = b"<!doctype html><html><body>Clinical features of patients</body></html>"

    def _decode(self, body, encoding=None):
        from core.fetch.transport import http as fetch_http

        headers = {"Content-Encoding": encoding} if encoding is not None else {}
        return fetch_http._decode_body(body, headers)

    def test_gzip_is_decoded(self):
        self.assertEqual(self._decode(gzip.compress(self.HTML), "gzip"), self.HTML)

    def test_deflate_is_decoded(self):
        self.assertEqual(self._decode(zlib.compress(self.HTML), "deflate"), self.HTML)

    def test_raw_deflate_without_zlib_header_is_decoded(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = compressor.compress(self.HTML) + compressor.flush()
        self.assertEqual(self._decode(raw, "deflate"), self.HTML)

    def test_brotli_is_decoded(self):
        import brotli

        self.assertEqual(self._decode(brotli.compress(self.HTML), "br"), self.HTML)

    def test_stacked_encodings_are_decoded_in_reverse_order(self):
        import brotli

        encoded = brotli.compress(gzip.compress(self.HTML))
        self.assertEqual(self._decode(encoded, "gzip, br"), self.HTML)

    def test_identity_and_absent_header_pass_through(self):
        self.assertEqual(self._decode(self.HTML, "identity"), self.HTML)
        self.assertEqual(self._decode(self.HTML), self.HTML)

    def test_gzip_bytes_without_the_header_are_untouched(self):
        # A genuine .gz download declares itself in Content-Type, not here; we key
        # on the declared encoding only, never on sniffed magic bytes.
        archive = gzip.compress(self.HTML)
        self.assertEqual(self._decode(archive), archive)

    def test_unknown_encoding_fails_closed(self):
        from core.fetch.transport import http as fetch_http

        with self.assertRaises(fetch_http.ContentDecodingError) as raised:
            self._decode(self.HTML, "exotic-v9")
        self.assertEqual(raised.exception.encoding, "exotic-v9")
        self.assertEqual(raised.exception.reason_code, "content_encoding_unsupported")

    def test_corrupt_body_fails_closed(self):
        from core.fetch.transport import http as fetch_http

        with self.assertRaises(fetch_http.ContentDecodingError) as raised:
            self._decode(b"not actually gzip", "gzip")
        self.assertEqual(raised.exception.encoding, "gzip")
        self.assertEqual(raised.exception.reason_code, "content_encoding_decode_failed")

    def test_open_and_read_decodes_before_inspecting_the_body(self):
        from core.fetch.transport import http as fetch_http

        class _Response:
            status = 200
            headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
            _body = gzip.compress(TestContentEncodingDecode.HTML)
            _offset = 0

            def read(self, size=-1):
                if size < 0:
                    size = len(self._body) - self._offset
                end = min(len(self._body), self._offset + size)
                chunk = self._body[self._offset:end]
                self._offset = end
                return chunk

            def geturl(self):
                return "http://web.archive.org/web/20250126070951id_/https://example.org/a"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        result = fetch_http._open_and_read(
            lambda req, timeout=None: _Response(), object(), 5, "http://web.archive.org/x")
        self.assertEqual(result["body"], self.HTML)
        # body_head is derived from the body, so it must be readable text too.
        self.assertIn("Clinical features", result["body_head"])

    def test_open_and_read_returns_a_typed_decode_failure_without_binary_body(self):
        from core.fetch.transport import http as fetch_http

        class _Response:
            status = 200
            headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
            _body = b"not actually gzip"
            _offset = 0

            def read(self, size=-1):
                if size < 0:
                    size = len(self._body) - self._offset
                end = min(len(self._body), self._offset + size)
                chunk = self._body[self._offset:end]
                self._offset = end
                return chunk

            def geturl(self):
                return "https://example.org/corrupt"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        result = fetch_http._open_and_read(
            lambda req, timeout=None: _Response(), object(), 5, "https://example.org/corrupt")

        self.assertEqual(result["body"], b"")
        self.assertEqual(result["body_head"], "")
        self.assertEqual(
            result["content_decoding_error"],
            {
                "encoding": "gzip",
                "reason_code": "content_encoding_decode_failed",
            },
        )

    def test_open_and_read_returns_a_typed_transport_failure_without_message(self):
        from core.fetch.transport import http as fetch_http

        class PrivateTransportFailure(Exception):
            pass

        result = fetch_http._open_and_read(
            lambda req, timeout=None: (_ for _ in ()).throw(
                PrivateTransportFailure("do not persist this detail")
            ),
            object(),
            5,
            "https://example.org/unreachable",
        )

        self.assertEqual(result["status"], None)
        self.assertEqual(result["url"], None)
        self.assertEqual(result["content_type"], None)
        self.assertEqual(
            result["transport_error"],
            {"reason_code": "transport_error", "exception_type": "PrivateTransportFailure"},
        )
        self.assertNotIn("do not persist this detail", repr(result))

    def test_decode_failure_stops_before_document_and_pdf_parsing(self):
        from types import SimpleNamespace

        from core.fetch.queue import _handle_document_candidate, _handle_pdf_candidate

        def unexpected_parser(*_args, **_kwargs):
            raise AssertionError("encoded bytes reached a parser")

        deps = SimpleNamespace(decode_textual_body=unexpected_parser)
        fetched = {
            "status": 200,
            "body": b"",
            "content_type": "text/html",
            "url": "https://example.org/source",
            "content_decoding_error": {
                "encoding": "br",
                "reason_code": "content_encoding_decode_failed",
            },
        }
        for handler in (_handle_document_candidate, _handle_pdf_candidate):
            with self.subTest(handler=handler.__name__):
                failures = []
                handled = handler(
                    deps=deps,
                    ref={},
                    run_dir="",
                    resolve_result={},
                    queue=[],
                    seen=set(),
                    failures=failures,
                    candidate={"method": "reference_url", "url": fetched["url"]},
                    fetched=fetched,
                    tmp_path="unused",
                )

                self.assertIsNone(handled["return"])
                self.assertEqual(handled["attempt"]["outcome"], "content_decode_failed")
                self.assertEqual(
                    handled["attempt"]["reason_code"],
                    "content_encoding_decode_failed",
                )
                self.assertEqual(handled["attempt"]["content_encoding"], "br")
                self.assertEqual(failures[0]["reason_code"], "content_encoding_decode_failed")
                self.assertEqual(failures[0]["content_encoding"], "br")






class TestSourcesProvenance(unittest.TestCase):
    REF = {"id": "ref-1", "ref_number": 1, "doi": "10.1000/abcd.1234", "pmid": None,
           "title": None,
           "raw_entry": "Smith AB, Jones CD. Treatment effects on mortality. J Clin Med. 2018."}

    def test_corroborate_doi_and_tokens(self):
        sig, score = sources.corroborate(self.REF, "... doi:10.1000/abcd.1234 ...")
        self.assertEqual(sig, "doi")
        self.assertEqual(score, 1.0)
        sig, score = sources.corroborate(
            self.REF, "Treatment effects on mortality by Smith and Jones in clinical medicine")
        self.assertEqual(sig, "tokens")
        self.assertGreater(score, 0.4)
        _sig, score = sources.corroborate(self.REF, "astronomy telescopes galaxies nebula")
        self.assertEqual(score, 0.0)

    def test_corroborate_resolve_result_doi_in_text(self):
        ref_no_doi = {"id": "ref-2", "ref_number": 2, "doi": None, "pmid": None,
                      "raw_entry": "Smith AB. Some paper. J Med. 2019."}
        # Carries the status every real resolve result carries: a discovered DOI
        # answers for the citation only while the resolver's own answer stands.
        # This is the accepted-resolution control for the refused case below.
        rr = {"status": "resolved", "matched_title": "irrelevant",
              "doi": "10.9999/xyz.999"}
        sig, score = sources.corroborate(ref_no_doi, "text containing 10.9999/xyz.999 as found", rr)
        self.assertEqual(sig, "doi")
        self.assertEqual(score, 1.0)

    def test_corroborate_ignores_the_matched_title_of_a_refused_resolution(self):
        # The resolver returned "unverified": it does not believe this record is
        # the cited work. Borrowing its title as the identity key turned that
        # refusal into match_score 1.0 and stored a JOSS paper about a Python
        # simulation library as the full text of an IEEE paper on the Mirai
        # botnet -- the two sharing only an author surname.
        ref = {
            "id": "ref-400", "ref_number": 400, "doi": None, "pmid": None,
            "title": "on Software Sec",
            "raw_entry": (
                "See Joel Margolis, Tae (Tom) Oh, Suyash Jadhav, Young Ho Kim & Jeong "
                "Neyo Kim, An In- Depth Analysis of the Mirai Botnet, 2017 Int'l Conf. "
                "on Software Sec. & Assurance 6, 6."
            ),
        }
        wrong_paper = (
            "SimuPy: A Python framework for modeling and simulating dynamical systems. "
            "Benjamin W. L. Margolis, University of California, Davis."
        )
        rr = {
            "status": "unverified",
            "existence_confidence": "low",
            "matched_title": "SimuPy: A Python framework for modeling and simulating dynamical systems",
        }
        sig, score = sources.corroborate(ref, wrong_paper, rr)
        self.assertNotEqual(sig, "title")
        self.assertLess(score, sources.CORROBORATE_THRESHOLD)

    def test_corroborate_still_uses_the_matched_title_of_an_accepted_resolution(self):
        # Negative control: a book resolved by title carries existence_confidence
        # "low" routinely, and must keep corroborating through this path.
        ref = {
            "id": "ref-14", "ref_number": 14, "doi": None, "pmid": None,
            "title": "Deep learning",
            "raw_entry": (
                "Ian Goodfellow, Yoshua Bengio, Aaron Courville. Deep learning. "
                "MIT press Cambridge, 2016."
            ),
        }
        rr = {"status": "resolved", "existence_confidence": "low",
              "matched_title": "Deep Learning"}
        sig, score = sources.corroborate(ref, "Deep Learning -- an MIT Press book.", rr)
        self.assertEqual(sig, "title")
        self.assertEqual(score, 1.0)

    def test_corroborate_detail_names_the_branch_that_answered(self):
        # "doi" as a signal covers two different things: the citation's own
        # identifier, and one the resolver went and found. Auditing a stored
        # source needs them apart, and the manifest cannot tell them apart after
        # the fact -- its signal is a snapshot from storage time while a resolve
        # status is the final one.
        cases = [
            ({"doi": "10.1000/abcd.1234", "pmid": None, "raw_entry": "Smith. A paper. 2019."},
             "text with 10.1000/abcd.1234 in it", None, "cited_doi", None),
            ({"doi": None, "pmid": None, "raw_entry": "Smith. A paper. 2019."},
             "text with 10.9999/xyz.999 in it",
             {"status": "resolved", "doi": "10.9999/xyz.999"}, "resolver_doi", "resolved"),
            ({"doi": None, "pmid": None, "raw_entry": "Smith. A paper. 2019."},
             "Deep Learning -- an MIT Press book.",
             {"status": "resolved", "matched_title": "Deep Learning"},
             "resolver_title", "resolved"),
        ]
        for ref, text, rr, branch, status in cases:
            with self.subTest(branch=branch):
                detail = {}
                sources.corroborate(ref, text, rr, detail=detail)
                self.assertEqual(detail["branch"], branch)
                self.assertEqual(detail["resolve_status"], status)

    def test_corroborate_ignores_the_discovered_doi_of_a_refused_resolution(self):
        # Both resolver branches answer "is this the document the resolver pointed
        # at?". That is only the same question as "is this the cited work?" while
        # the resolver's own answer stands; once it has refused the record, the
        # citation's question must be answered from the citation's own tokens.
        ref = {
            "id": "ref-adam", "ref_number": 20, "doi": None, "pmid": None,
            "title": "Adam: A method for stochastic optimization",
            "raw_entry": ("Diederik P. Kingma and Jimmy Ba. Adam: A method for "
                          "stochastic optimization. ICLR, 2015."),
        }
        text = (
            "Adam: A Method for Stochastic Optimization. Diederik P. Kingma, "
            "University of Amsterdam. Jimmy Lei Ba, University of Toronto. We "
            "introduce Adam, an algorithm for first-order gradient-based "
            "optimization of stochastic objective functions. "
            "doi:10.48550/arXiv.1412.6980"
        )
        refused = {"status": "unverified", "doi": "10.48550/arXiv.1412.6980"}

        detail = {}
        sig, score = sources.corroborate(ref, text, refused, detail=detail)
        self.assertNotEqual(sig, "doi")
        self.assertEqual(detail["branch"], "cited_tokens")
        self.assertEqual(detail["resolve_status"], "unverified")
        # It still corroborates, on the citation's own words: refusing the shortcut
        # must not cost the document. Both real instances in the corpus are correct
        # papers that land here.
        self.assertGreaterEqual(score, sources.CORROBORATE_THRESHOLD)

    def test_corroborate_without_detail_is_unchanged(self):
        # The parameter is optional and descriptive: every existing caller keeps
        # working and nothing reads it back.
        sig, score = sources.corroborate(self.REF, "... doi:10.1000/abcd.1234 ...")
        self.assertEqual((sig, score), ("doi", 1.0))

    def test_corroborate_resolve_result_doi_absent_from_text(self):
        ref_no_doi = {"id": "ref-2", "ref_number": 2, "doi": None, "pmid": None,
                      "raw_entry": "Smith AB. Some paper. J Med. 2019."}
        rr = {"matched_title": "", "doi": "10.9999/xyz.999"}
        sig, score = sources.corroborate(ref_no_doi, "astronomy telescopes galaxies nebula", rr)
        self.assertEqual(score, 0.0)

    def test_corroborate_resolve_result_matched_title_high_overlap(self):
        ref_no_doi = {"id": "ref-3", "ref_number": 3, "doi": None, "pmid": None,
                      "raw_entry": "Jones XY. Neural machine translation. EMNLP. 2016."}
        # Carries the status every real resolve result carries: the matched title
        # only stands in for the citation when the resolver accepted the match.
        rr = {"status": "resolved",
              "matched_title": "Neural Machine Translation by Jointly Learning to Align and Translate"}
        text = ("Neural machine translation jointly learning align translate attention "
                "sequence encoder decoder recurrent network")
        sig, score = sources.corroborate(ref_no_doi, text, rr)
        self.assertEqual(sig, "title")
        self.assertEqual(score, 1.0)

    def test_corroborate_resolve_result_none_unchanged(self):
        sig, score = sources.corroborate(self.REF, "... doi:10.1000/abcd.1234 ...", None)
        self.assertEqual(sig, "doi")
        self.assertEqual(score, 1.0)

    def test_store_and_best_for(self):
        with tempfile.TemporaryDirectory() as run:
            sources.store_text(run, self.REF, "abstract", "crossref", "an abstract",
                               mapping="web", signal="tokens", score=0.4)
            sources.store_text(run, self.REF, "fulltext", "user", "the full text",
                               mapping="deterministic", signal="doi", score=1.0)
            man = sources.load_manifest(run)
            self.assertEqual(len(man["entries"]), 2)
            best = sources.best_for(man, "ref-1")
            self.assertEqual(best["tier"], "fulltext")        # fulltext > abstract
            self.assertTrue(os.path.exists(os.path.join(run, "sources", best["stored_as"])))

    def test_dedup_same_tier_origin(self):
        with tempfile.TemporaryDirectory() as run:
            sources.store_text(run, self.REF, "fulltext", "user", "v1")
            sources.store_text(run, self.REF, "fulltext", "user", "v2")
            man = sources.load_manifest(run)
            self.assertEqual(len(man["entries"]), 1)          # last write wins
            stored = os.path.join(run, "sources", man["entries"][0]["stored_as"])
            with open(stored) as f:
                self.assertEqual(f.read(), "v2")

    def test_concurrent_store_text_keeps_all_manifest_entries(self):
        from core.infra.db import RunRepository
        with tempfile.TemporaryDirectory() as run:
            repo = RunRepository.create(
                run,
                run_id="run-concurrent-store-text",
                input_path="C:\\paper.pdf",
                input_sha256="abc123",
                accuracy="standard",
                style=None,
                model_id=None,
                http_profile="default",
                challenge_mode="off",
                fixture_fingerprint="fixture-1",
            )
            repo.close()
            refs = [
                {
                    "id": f"ref-{idx}",
                    "ref_number": idx,
                    "doi": None,
                    "pmid": None,
                    "title": None,
                    "raw_entry": f"Entry {idx}",
                }
                for idx in range(1, 9)
            ]

            def write_one(ref):
                return sources.store_text(
                    run, ref, "fulltext", "user", f"text for {ref['id']}"
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write_one, refs))

            man = sources.load_manifest(run)
            self.assertEqual(len(man["entries"]), len(refs))
            self.assertEqual(
                {entry["ref_id"] for entry in man["entries"]},
                {ref["id"] for ref in refs},
            )


class TestContentVersion(unittest.TestCase):
    """Preprint vs version-of-record labelling: the version axis, kept separate from
    identity. See gaps/report behaviour in test_run_report.TestGapsAccuracy."""

    REF = {"id": "ref-1", "ref_number": 1, "doi": "10.1000/abcd.1234",
           "title": "Treatment effects on mortality",
           "raw_entry": "Smith AB. Treatment effects on mortality. J Clin Med. 2018."}

    def test_content_version_for_maps_provider_metadata(self):
        self.assertEqual(sources.content_version_for("https://pub.example/x.pdf",
                                                     "submittedVersion"), "preprint")
        self.assertEqual(sources.content_version_for("https://pub.example/x.pdf",
                                                     "acceptedVersion"), "accepted_manuscript")
        self.assertEqual(sources.content_version_for("https://pub.example/x.pdf",
                                                     "publishedVersion"), "published")

    def test_content_version_for_detects_preprint_host(self):
        # No version metadata, but the host only ever serves preprints.
        self.assertEqual(sources.content_version_for("https://arxiv.org/pdf/2305.1.pdf"),
                         "preprint")
        self.assertEqual(sources.content_version_for("https://www.biorxiv.org/x.full.pdf"),
                         "preprint")
        self.assertEqual(sources.content_version_for("https://publisher.example/x.pdf"),
                         "published")

    def test_content_version_for_keeps_already_normalised_value(self):
        # A provider that already resolved the label must not be re-downgraded.
        self.assertEqual(sources.content_version_for("https://repo.edu/x.pdf", "preprint"),
                         "preprint")

    def test_store_text_records_preprint_and_synthesises_note(self):
        with tempfile.TemporaryDirectory() as run:
            entry = sources.store_text(run, self.REF, "fulltext", "openalex", "the text",
                                       content_version="preprint")
            self.assertEqual(entry["content_version"], "preprint")
            self.assertIn("version of record", entry["identity_note"])
            man = sources.load_manifest(run)
            self.assertEqual(man["entries"][0]["content_version"], "preprint")

    def test_infer_provenance_relation_marks_official_citation_path_copy(self):
        ref = dict(
            self.REF,
            year=2016,
            title="Neural GPUs Learn Algorithms",
            raw_entry=(
                "Lukasz Kaiser and Ilya Sutskever. Neural GPUs learn algorithms. "
                "In International Conference on Learning Representations (ICLR), 2016."
            ),
        )
        text = (
            "Published as a conference paper at ICLR 2016. Neural GPUs Learn Algorithms. "
            "This preprint describes the same work."
        )
        relation = sources.infer_provenance_relation(
            ref,
            text,
            content_version="preprint",
        )
        self.assertEqual(relation, sources.OFFICIALLY_SURFACED_COPY)

    def test_store_text_records_officially_surfaced_preprint_copy(self):
        with tempfile.TemporaryDirectory() as run:
            entry = sources.store_text(
                run,
                self.REF,
                "fulltext",
                "openalex",
                "the text",
                content_version="preprint",
                provenance_relation=sources.OFFICIALLY_SURFACED_COPY,
            )
            self.assertEqual(entry["content_version"], "preprint")
            self.assertEqual(
                entry["provenance_relation"],
                sources.OFFICIALLY_SURFACED_COPY,
            )
            self.assertIn("official citation-path copy", entry["identity_note"])
            man = sources.load_manifest(run)
            self.assertEqual(
                man["entries"][0]["provenance_relation"],
                sources.OFFICIALLY_SURFACED_COPY,
            )

    def test_store_text_published_leaves_no_version_marker(self):
        with tempfile.TemporaryDirectory() as run:
            entry = sources.store_text(run, self.REF, "fulltext", "openalex", "the text",
                                       content_version="published")
            self.assertNotIn("content_version", entry)

    def test_openalex_candidate_items_carry_submitted_version(self):
        from core.resolve.providers import openalex
        hit = {
            "open_access": {"oa_url": "https://repo.example/oa.pdf"},
            "best_oa_location": {"pdf_url": "https://repo.example/preprint.pdf",
                                 "version": "submittedVersion"},
            "locations": [],
        }
        # candidate_items derives URLs from work(); stub it to control the locations.
        orig_work = openalex.work
        try:
            openalex.work = lambda ref, **kw: hit
            items = openalex.candidate_items(
                self.REF, get_fn=lambda *a, **k: None,
                normalize_doi=lambda d: d, kind_from_url=lambda u: "pdf",
                email="x@y.edu",
            )
        finally:
            openalex.work = orig_work
        by_url = {it["url"]: it for it in items}
        self.assertEqual(by_url["https://repo.example/preprint.pdf"]["content_version"],
                         "preprint")

    def test_openalex_candidate_items_recover_idsia_pdf_from_raw_source_name(self):
        from core.resolve.providers import openalex
        hit = {
            "open_access": {},
            "best_oa_location": {},
            "locations": [
                {
                    "raw_source_name": "ftp://ftp.idsia.ch/pub/juergen/lstm.ps.gz",
                    "version": "submittedVersion",
                }
            ],
        }
        orig_work = openalex.work
        try:
            openalex.work = lambda ref, **kw: hit
            items = openalex.candidate_items(
                self.REF, get_fn=lambda *a, **k: None,
                normalize_doi=lambda d: d, kind_from_url=lambda u: "pdf",
                email="x@y.edu",
            )
        finally:
            openalex.work = orig_work
        by_url = {it["url"]: it for it in items}
        self.assertEqual(
            by_url["https://sferics.idsia.ch/pub/juergen/lstm.pdf"]["content_version"],
            "preprint",
        )

    def test_openalex_landing_to_pdf_maps_idsia_source_url(self):
        from core.resolve.providers import openalex

        self.assertEqual(
            openalex.landing_to_pdf("ftp://ftp.idsia.ch/pub/juergen/lstm.ps.gz"),
            "https://sferics.idsia.ch/pub/juergen/lstm.pdf",
        )

    def test_unpaywall_candidate_items_carry_version(self):
        from core.resolve.providers import unpaywall
        orig = unpaywall.best_oa_location
        try:
            unpaywall.best_oa_location = lambda doi, email, get_fn: {
                "url_for_pdf": "https://repo.example/sub.pdf",
                "version": "submittedVersion",
            }
            items = unpaywall.candidate_items(
                self.REF, email="x@y.edu",
                normalize_doi=lambda d: d, kind_from_url=lambda u: "pdf",
                get_fn=lambda *a, **k: None,
            )
        finally:
            unpaywall.best_oa_location = orig
        self.assertEqual(items[0]["content_version"], "preprint")

    def test_unpaywall_pdf_url_from_doi_delegates_to_best_oa_url(self):
        from core.resolve.providers import unpaywall

        orig = unpaywall.best_oa_url
        try:
            unpaywall.best_oa_url = lambda doi, email, get_fn: "https://repo.example/paper.pdf"
            self.assertEqual(
                unpaywall.pdf_url_from_doi(
                    "10.1234/example",
                    email="x@y.edu",
                    get_fn=lambda *a, **k: None,
                ),
                "https://repo.example/paper.pdf",
            )
        finally:
            unpaywall.best_oa_url = orig

    def test_try_store_fulltext_propagates_content_version(self):
        from core.fetch.storage import fetch_store
        with tempfile.TemporaryDirectory() as run:
            text = (
                "Treatment effects on mortality are evaluated in this full body text. "
                "The study reports methods, participants, outcomes, and detailed results. "
            ) * 8 + " doi:10.1000/abcd.1234"
            result = fetch_store.try_store_fulltext(
                run, self.REF, {"via": "crossref"}, text,
                method="openalex", source_ref="https://arxiv.org/pdf/x.pdf",
                content_version="preprint",
            )
            self.assertEqual(result["status"], "stored")
            self.assertEqual(result["content_version"], "preprint")
            man = sources.load_manifest(run)
            self.assertEqual(man["entries"][0]["content_version"], "preprint")

    def test_try_store_fulltext_persists_resolver_abstract_alongside_fulltext(self):
        from core.fetch.storage import fetch_store
        with tempfile.TemporaryDirectory() as run:
            text = (
                "Treatment effects on mortality are evaluated in this full body text. "
                "The study reports methods, participants, outcomes, and detailed results. "
            ) * 8 + " doi:10.1000/abcd.1234"
            result = fetch_store.try_store_fulltext(
                run,
                self.REF,
                {
                    "via": "openalex",
                    "abstract": "This study evaluates treatment effects on mortality.",
                    "abstract_via": "openalex",
                    "url": "https://openalex.org/W1234567890",
                },
                text,
                method="openalex",
                source_ref="https://example.org/fulltext.pdf",
            )
            self.assertEqual(result["status"], "stored")
            man = sources.load_manifest(run)
            entries = man["entries"]
            self.assertEqual({entry["tier"] for entry in entries}, {"fulltext", "abstract"})
            abstract_entry = next(entry for entry in entries if entry["tier"] == "abstract")
            self.assertEqual(abstract_entry["origin"], "openalex")
            self.assertEqual(abstract_entry["source_ref"], "https://openalex.org/W1234567890")
            stored = os.path.join(run, "sources", abstract_entry["stored_as"])
            with open(stored, encoding="utf-8") as handle:
                self.assertEqual(
                    handle.read(),
                    "This study evaluates treatment effects on mortality.",
                )

    def test_ref_is_preprint_detects_cited_preprints_only(self):
        # bioRxiv/medRxiv (dated and pre-2019 6-digit) and arXiv are preprints
        self.assertTrue(sources.ref_is_preprint({"doi": "10.1101/2020.03.22.002386"}))
        self.assertTrue(sources.ref_is_preprint({"doi": "10.1101/833400"}))
        self.assertTrue(sources.ref_is_preprint({"doi": "10.48550/arXiv.2305.03589"}))
        self.assertTrue(sources.ref_is_preprint({"doi": "10.2139/ssrn.123456"}))
        # 10.1101 shared with Cold Spring Harbor journals: a journal DOI is NOT a preprint
        self.assertFalse(sources.ref_is_preprint({"doi": "10.1101/gad.123456"}))
        self.assertFalse(sources.ref_is_preprint({"doi": "10.1038/s41586-020-2286-9"}))
        # resolver work type can also flag it
        self.assertTrue(sources.ref_is_preprint(
            {"doi": "10.1038/whatever"}, {"type": "posted-content"}))

    def test_preprint_identity_ok_requires_title_and_surname(self):
        ref = {"title": "Treatment effects on mortality in sepsis", "ay_surname": "Smith"}
        self.assertTrue(sources.preprint_identity_ok(
            ref, "Smith et al. Treatment effects on mortality in sepsis — full body"))
        # title matches but the known author surname is absent -> reject
        self.assertFalse(sources.preprint_identity_ok(
            ref, "Treatment effects on mortality in sepsis, full text body"))
        # a different paper (low title overlap) -> reject
        self.assertFalse(sources.preprint_identity_ok(
            ref, "Smith et al. A study of galaxies and telescopes in deep space"))

    def test_no_expected_title_is_not_a_confirmation(self):
        # With nothing to check the document against, the probe used to answer
        # "confirmed" — confirmation by absence of evidence, in a check whose job is
        # to catch a document that is not the cited work. Eleven stored sources in
        # the corpus reached that answer.
        probe = sources.document_identity_probe(
            {"id": "r", "ref_number": 1, "title": None, "raw_entry": "Anon. 2019."},
            "Some entirely unrelated document about marine sediment cores.",
            {"status": "unverified", "matched_title": None},
        )
        self.assertEqual(probe["decision"], "inconclusive")
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["reason_code"], "no_expected_title_for_front_matter_probe")

    def test_a_title_naming_a_journal_word_is_still_a_title(self):
        # "The skewness of science" was treated as titleless, because the stop list
        # that decides which words are DISTINCTIVE carries journal names — science,
        # nature, cell, lancet — and it was being asked whether a title EXISTS.
        # A real title was therefore checked against nothing.
        self.assertTrue(sources.probe_title_is_usable("The skewness of science"))
        self.assertTrue(sources.probe_title_is_usable("Nature of the cell cycle"))
        # And the genuinely unusable stay unusable.
        for unusable in (None, "", "The", "1: 95–115",
                         "https://doi.org/10.1111/ anti.12579", "doi: 10.1136/bmj.c332"):
            with self.subTest(unusable=unusable):
                self.assertFalse(sources.probe_title_is_usable(unusable))

    def test_a_usable_title_still_gets_a_real_probe(self):
        # Negative control: making the usability test kinder must not turn the probe
        # itself into a rubber stamp — a document that is not the cited work is still
        # caught, on a title the old counting would have discarded.
        probe = sources.document_identity_probe(
            {"id": "r", "ref_number": 2, "title": "The skewness of science",
             "raw_entry": "Cole S. The skewness of science. 1988."},
            "Marine sediment cores from the Weddell Sea. Introduction. Methods.",
            None,
        )
        self.assertNotEqual(probe["decision"], "confirmed")

    def test_the_resolvers_own_title_is_not_the_yardstick(self):
        # The citation carries no title we could parse, so the probe used to ask the
        # document whether it was the work the RESOLVER had gone looking for. That
        # answers "yes" exactly when the resolver picked the wrong work, and the
        # citation never votes. 182 of 1115 corpus sources were checked this way.
        ref = {"id": "r", "ref_number": 1, "title": None,
               "raw_entry": "Chen et al. 2019. 4: 64–89."}
        resolved = {"status": "resolved",
                    "matched_title": "Zoonotic Acinetobacter baumannii: the need for genomics"}
        expected, source = sources._expected_probe_title_info(ref, resolved)
        self.assertEqual(source, "citation")
        self.assertFalse(sources.probe_title_is_usable(expected))
        # A document that is a flawless copy of the resolver's pick is still not
        # evidence that it is the CITED work.
        probe = sources.document_identity_probe(
            ref,
            "Zoonotic Acinetobacter baumannii: the need for genomics. Abstract. "
            "Introduction. Methods. Results.",
            resolved,
        )
        self.assertEqual(probe["decision"], "inconclusive")
        self.assertEqual(probe["reason_code"], "no_expected_title_for_front_matter_probe")

    def test_candidate_title_must_be_localized_in_its_citation_raw_entry(self):
        ref = {
            "id": "r",
            "ref_number": 1,
            "title": None,
            "raw_entry": (
                "Doe. \\emph{Second-harmonic Bounded Rationality}. Journal. 2020."
            ),
        }
        expected, source = sources._expected_probe_title_info(
            ref,
            candidate_context={"title": "Secondharmonic Bounded Rationality"},
        )

        self.assertEqual(expected, "Second-harmonic Bounded Rationality")
        self.assertEqual(source, "citation_raw_entry")

    def test_short_candidate_title_is_citation_owned_only_when_printed_verbatim(self):
        ref = {
            "id": "r",
            "ref_number": 1,
            "title": None,
            "raw_entry": "Simon. Bounded Rationality. 1957.",
        }
        expected, source = sources._expected_probe_title_info(
            ref,
            candidate_context={"title": "Bounded Rationality"},
        )

        self.assertEqual(expected, "Bounded Rationality")
        self.assertEqual(source, "citation_raw_entry")

    def test_unanchored_candidate_title_cannot_become_the_probe_target(self):
        ref = {
            "id": "r",
            "ref_number": 1,
            "title": None,
            "raw_entry": "Chen et al. 2019. 4: 64–89.",
        }
        expected, source = sources._expected_probe_title_info(
            ref,
            candidate_context={"title": "Zoonotic Acinetobacter baumannii"},
        )

        self.assertEqual(expected, "")
        self.assertEqual(source, "citation")

    def test_candidate_prefix_does_not_displace_a_parsed_citation_title(self):
        ref = {
            "id": "r",
            "ref_number": 1,
            "title": "Grammar as a foreign language",
            "raw_entry": "Doe. Grammar as a foreign language. NIPS. 2018.",
        }
        expected, source = sources._expected_probe_title_info(
            ref,
            candidate_context={"title": "NIPS - Grammar as a foreign language"},
        )

        self.assertEqual(expected, "Grammar as a foreign language")
        self.assertEqual(source, "citation")

    def test_a_title_the_entry_prints_in_quotes_is_recovered_for_the_probe(self):
        # An entry whose title the parser missed usually still prints it, verbatim, in
        # its own text: 97 of the 119 corpus references with no usable title had it
        # there in full. Recovering it is not the substitution this file removed — it
        # is the citation's own words, so the citation still votes.
        ref = {"id": "r", "ref_number": 1, "title": None, "raw_entry": (
            "T. Umeki, O. Tadanaga, and M. Asobe, “Highly efficient wavelength "
            "converter using direct-bonded PPZnLN ridge waveguide,” IEEE J. "
            "Quantum Electron. 46, 1206 (2010).")}
        resolved = {"status": "resolved", "matched_title":
                    "Highly Efficient Wavelength Converter Using Direct-Bonded PPZnLN Ridge Waveguide"}
        expected, source = sources._expected_probe_title_info(ref, resolved)
        self.assertEqual(source, "citation_quoted")
        self.assertEqual(
            expected,
            "Highly efficient wavelength converter using direct-bonded PPZnLN ridge waveguide")

    def test_the_recovered_title_is_the_citations_and_can_contradict_the_resolver(self):
        # The point of taking the yardstick from the citation: when the resolver has
        # matched a different work, the citation's own quoted title says so. Under the
        # old fallback the document was measured against the resolver's pick and this
        # disagreement was invisible.
        ref = {"id": "r", "ref_number": 2, "title": "and Dantley, M", "raw_entry": (
            "Gooden, M.A. and Dantley, M. (2012), “Centering race in a framework "
            "for leadership preparation”, Journal of Research on Leadership "
            "Education, Vol. 7 No. 2.")}
        resolved = {"status": "resolved", "matched_title":
                    "Do You See What I See? Fostering Aspiring Leaders’ Racial Awareness"}
        expected, source = sources._expected_probe_title_info(ref, resolved)
        self.assertEqual(source, "citation_quoted")
        self.assertEqual(expected, "Centering race in a framework for leadership preparation")
        # And the resolver's document no longer passes as the cited work.
        probe = sources.document_identity_probe(
            ref,
            "Do You See What I See? Fostering Aspiring Leaders’ Racial Awareness. "
            "Abstract. Introduction.",
            resolved,
        )
        self.assertNotEqual(probe["decision"], "confirmed")

    def test_only_a_quoted_title_is_recovered_not_a_looser_guess(self):
        # The wider candidate search behind this recovery also has heuristics meant as
        # query hints. On this corpus they returned an author block for an entry that
        # quotes its title with single quotation marks, and an author block makes just
        # as confident a yardstick as a real title. Only quotable evidence is used.
        ref = {"id": "r", "ref_number": 3, "title": None, "raw_entry": (
            "B. Peter Rosendorff and Helen V. Milner, ‘The Optimal Design of "
            "International Trade Institutions’, 55 Int'l Org. 829 (2001).")}
        expected, source = sources._expected_probe_title_info(
            ref, {"status": "resolved", "matched_title":
                  "The Optimal Design of International Trade Institutions"})
        self.assertEqual(source, "citation")
        self.assertFalse(sources.probe_title_is_usable(expected))
        self.assertNotIn("Rosendorff", str(expected))

    def test_a_journal_name_left_in_the_title_field_does_not_borrow_a_title(self):
        # The commonest shape of the same fault: the parser kept the venue, not the
        # title. "Nano Lett" says nothing about which paper was cited, so it cannot
        # license using the resolver's title as the expected identity.
        for fragment in ("Nano Lett", "Br J Sports Med", "BMJ 340: c723",
                         "and deLeon, V", "1/2: 93–113"):
            with self.subTest(fragment=fragment):
                _, source = sources._expected_probe_title_info(
                    {"id": "r", "ref_number": 2, "title": fragment, "raw_entry": fragment},
                    {"status": "resolved", "matched_title":
                     "Nonlinear Refractory Plasmonics with Titanium Nitride Nanoantennas"},
                )
                self.assertEqual(source, "citation")

    def test_a_truncated_title_the_resolver_completes_still_counts(self):
        # Positive control, and the reason this is a rule and not a deletion: here the
        # citation DID vote. "Introduction to the conll-" is contained in the resolver's
        # title, so the longer form is the same work said in full, not a substitution.
        ref = {"id": "r", "ref_number": 3, "title": "Introduction to the conll-",
               "raw_entry": "Tjong Kim Sang, De Meulder. Introduction to the conll-"}
        resolved = {"status": "resolved",
                    "matched_title": "Introduction to the CoNLL-2003 shared task"}
        expected, source = sources._expected_probe_title_info(ref, resolved)
        self.assertEqual(source, "primary_resolution")
        self.assertEqual(expected, "Introduction to the CoNLL-2003 shared task")
        probe = sources.document_identity_probe(
            ref,
            "Introduction to the CoNLL-2003 shared task: language-independent named "
            "entity recognition. Tjong Kim Sang. Abstract. Introduction.",
            resolved,
        )
        self.assertEqual(probe["decision"], "confirmed")

    def test_a_leading_parenthesized_page_range_is_removed_from_the_citation_title(self):
        ref = {
            "id": "r",
            "ref_number": 25,
            "title": (
                "(2203- 2209) Randomized trials stopped early for benefit: "
                "a systematic review"
            ),
            "raw_entry": (
                "Montori VM, et al. (2203- 2209) Randomized trials stopped early "
                "for benefit: a systematic review. JAMA."
            ),
            "ay_surname": "montori",
        }
        resolved = {
            "status": "resolved",
            "matched_title": "Randomized Trials Stopped Early for Benefit",
        }

        expected, source = sources._expected_probe_title_info(ref, resolved)
        self.assertEqual(source, "citation")
        self.assertEqual(
            expected,
            "Randomized trials stopped early for benefit: a systematic review",
        )
        probe = sources.document_identity_probe(
            ref,
            "REVIEW\nRandomized Trials Stopped Early for Benefit\n"
            "A Systematic Review\nVictor M. Montori\nPaper body.",
            resolved,
        )
        self.assertEqual(probe["decision"], "confirmed")

    def test_a_leading_parenthesized_range_is_not_removed_without_title_agreement(self):
        parsed = "(1990-1994) Trends in childhood vaccination uptake"
        expected, source = sources._expected_probe_title_info(
            {
                "id": "r",
                "ref_number": 2,
                "title": parsed,
                "raw_entry": f"Smith J. {parsed}. Journal of Health.",
            },
            {
                "status": "resolved",
                "matched_title": "Unrelated adult cardiovascular outcomes",
            },
        )

        self.assertEqual(source, "citation")
        self.assertEqual(expected, parsed)

    def test_document_identity_probe_requires_title_in_front_matter(self):
        ref = {
            "raw_entry": "Smith J. Treatment effects on mortality in sepsis. J Med. 2020.",
            "title": "Treatment effects on mortality in sepsis",
        }
        ok = sources.document_identity_probe(
            ref,
            "Treatment effects on mortality in sepsis\nSmith J\nFull body text follows.",
            {"matched_title": "Treatment effects on mortality in sepsis"},
        )
        self.assertTrue(ok["ok"])

        bad = sources.document_identity_probe(
            ref,
            ("A completely different paper by Brown.\n" * 200)
            + "References\nSmith J. Treatment effects on mortality in sepsis. doi:10.1000/x",
            {"matched_title": "Treatment effects on mortality in sepsis"},
        )
        self.assertFalse(bad["ok"])
        self.assertIn("front matter", bad["reason"])

    def test_document_identity_probe_rejoins_hyphenated_title_artifacts(self):
        ref = {
            "raw_entry": (
                "Jonas Gehring, Michael Auli, David Grangier, Denis Yarats, and Yann N. Dauphin. "
                "Convolu- tional sequence to sequence learning. 2017."
            ),
            "title": "Convolu- tional sequence to sequence learning",
        }
        ok = sources.document_identity_probe(
            ref,
            (
                "Convolutional Sequence to Sequence Learning\n"
                "Jonas Gehring Michael Auli David Grangier Yann N. Dauphin\n"
                "Body text follows."
            ),
            None,
        )
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["reason"], "front_matter_identity_confirmed")

    def test_document_identity_probe_uses_complete_title_printed_in_raw_entry(self):
        title = (
            "Comparative effectiveness of physical exercise interventions for chronic "
            "non-specific neck pain"
        )
        ref = {
            "title": "Br J Sports Med",
            "raw_entry": f"de Zoete RM. {title}. Br J Sports Med. 2020.",
            "ay_surname": "zoete",
        }

        probe = sources.document_identity_probe(
            ref,
            f"{title}\nde Zoete RM\nAbstract\nSubstantive article body.",
            {"status": "resolved", "matched_title": title},
        )

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["expected_title_source"], "citation_raw_entry")

    def test_document_identity_probe_prefers_quoted_title_over_noisy_parsed_suffix(self):
        title = "Neo-Slave Narratives as Recent Interventions on Race and Freedom"
        ref = {
            "title": f"“{title}.” IOSR-JHSS, vol. 25",
            "raw_entry": f"Pun, Min. “{title}.” IOSR-JHSS, vol. 25, 2020.",
            "ay_surname": "pun",
        }

        probe = sources.document_identity_probe(
            ref,
            f"{title}\nMin Pun\nAbstract\nSubstantive article body.",
            {"status": "resolved", "matched_title": "A different resolver title"},
        )

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["expected_title_source"], "citation_quoted")

    def test_document_identity_probe_normalizes_author_apostrophe(self):
        ref = {
            "title": "Muscle Dysfunction in Cervical Spine Pain",
            "raw_entry": "O'Leary S. Muscle Dysfunction in Cervical Spine Pain. 2009.",
            "ay_surname": "o'leary",
        }

        probe = sources.document_identity_probe(
            ref,
            "Muscle Dysfunction in Cervical Spine Pain\nSHAUN O’LEARY\nAbstract\nBody.",
        )

        self.assertTrue(probe["ok"])
        self.assertTrue(probe["author_ok"])

    def test_document_identity_probe_handles_bounded_title_join_artifacts(self):
        cases = (
            (
                "Squeeze-andexcitation networks",
                "Squeeze-and-Excitation Networks",
                "Hu",
            ),
            (
                "Squeeze-and-Excitation Networks",
                "Squeeze-andexcitation networks",
                "Hu",
            ),
            (
                "Highly Efficient Wavelength Converter Using Directbonded PPZnLN Ridge Waveguide",
                "Highly Efficient Wavelength Converter Using Direct-Bonded PPZnLN Ridge Waveguide",
                "Umeki",
            ),
            (
                "Highly Efficient Wavelength Converter Using Direct-Bonded PPZnLN Ridge Waveguide",
                "Highly Efficient Wavelength Converter Using Directbonded PPZnLN Ridge Waveguide",
                "Umeki",
            ),
            (
                "Processes of retrieval from very long-term memory",
                "The Process of Retrieval from Very Long-Term Memory",
                "Williams",
            ),
            (
                "MobileNetV2: Inverted Residuals and Linear Bottlenecks",
                "MobileNetV⟦SUP:2⟧: Inverted Residuals and Linear Bottlenecks",
                "Sandler",
            ),
        )
        for cited, printed, author in cases:
            with self.subTest(cited=cited):
                probe = sources.document_identity_probe(
                    {
                        "title": cited,
                        "raw_entry": f"{author}. {cited}. 2018.",
                        "ay_surname": author.lower(),
                    },
                    f"{printed}\n{author}\nAbstract\nSubstantive body.",
                )
                self.assertTrue(probe["ok"])
                self.assertTrue(probe["author_ok"])

    def test_document_identity_probe_rejects_dispersed_title_tokens_in_front_matter(self):
        ref = {
            "raw_entry": (
                "Sepp Hochreiter, Yoshua Bengio, Paolo Frasconi, and Jürgen Schmidhuber. "
                "Gradient flow in recurrent nets: the difficulty of learning long-term dependencies, 2001."
            ),
            "title": "Gradient flow",
        }
        bad = sources.document_identity_probe(
            ref,
            (
                "Gradient Flow in Recurrent Nets\n"
                "Naresh Babu Bynagari\n"
                "Abstract In theory, recurrent networks can leverage their feedback connections.\n"
                "This review revisits several practical issues in sequence models and optimization.\n"
                "Further background text goes here to separate the leading clause from the later discussion.\n"
                "Additional survey material expands on prior work and practical limitations.\n"
                "The methodology section describes several historical approaches and empirical observations.\n"
                "A later discussion again addresses the difficulty of learning long-term dependencies.\n"
            ),
            {
                "matched_title": (
                    "Gradient Flow in Recurrent Nets: the Difficulty of Learning Long-Term Dependencies"
                )
            },
        )
        self.assertFalse(bad["ok"])
        self.assertIn("dispersed", bad["reason"])

    def test_document_identity_probe_rejects_dispersed_title_even_with_author(self):
        ref = {
            "raw_entry": "Alice Smith. Alpha Beta Gamma Delta Epsilon Zeta. 2020.",
            "title": "Alpha Beta Gamma Delta Epsilon Zeta",
        }
        bad = sources.document_identity_probe(
            ref,
            (
                "Alice Smith\n"
                + "Alpha "
                + ("filler " * 80)
                + "Beta "
                + ("filler " * 80)
                + "Gamma "
                + ("filler " * 80)
                + "Delta "
                + ("filler " * 80)
                + "Epsilon "
                + ("filler " * 80)
                + "Zeta"
            ),
            None,
        )
        self.assertFalse(bad["ok"])
        self.assertIn("dispersed", bad["reason"])

    def test_document_identity_probe_accepts_near_complete_title_with_author(self):
        ref = {
            "raw_entry": (
                "Nitish Srivastava, Geoffrey Hinton, Alex Krizhevsky, Ilya Sutskever, "
                "and Ruslan Salakhutdinov. Dropout: a simple way to prevent neural "
                "networks from overfitting. 2014."
            ),
            "title": "Dropout: a simple way to prevent neural networks from overfitting",
        }
        ok = sources.document_identity_probe(
            ref,
            (
                "Journal of Machine Learning Research 15 (2014) 1929-1958\n"
                "Dropout: A Simple Way to Prevent Neural Networks from\n"
                "Over?tting\n"
                "Nitish Srivastava\n"
                "Abstract body text follows.\n"
            ),
            {"matched_title": "Dropout: a simple way to prevent neural networks from overfitting"},
        )
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["relaxed_front_matter_match"])

    def test_first_author_surname_uses_segment_before_title(self):
        self.assertEqual(
            sources._first_author_surname({
                "title": "Xception: Deep learning with depthwise separable convolutions",
                "raw_entry": (
                    "Francois Chollet. Xception: Deep learning with depthwise separable "
                    "convolutions. arXiv preprint arXiv:1610.02357, 2016."
                ),
            }),
            "chollet",
        )
        self.assertEqual(
            sources._first_author_surname({
                "title": "Generating sequences with recurrent neural networks",
                "raw_entry": (
                    "Alex Graves. Generating sequences with recurrent neural networks. "
                    "arXiv:1308.0850, 2013."
                ),
            }),
            "graves",
        )

    def test_document_identity_probe_rejects_short_title_embedded_in_longer_title(self):
        ref = {
            "title": "Layer normalization",
            "raw_entry": (
                "Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. "
                "Layer normalization. arXiv preprint arXiv:1607.06450, 2016."
            ),
        }
        bad = sources.document_identity_probe(
            ref,
            (
                "Root Mean Square Layer Normalization\n"
                "Biao Zhang Rico Sennrich\n"
                "Abstract\n"
                "Layer normalization builds on work by Ba et al. "
                + ("This paper proposes RMSNorm. " * 40)
            ),
            None,
        )
        self.assertFalse(bad["ok"])
        self.assertIn("short expected title", bad["reason"])

    def test_content_store_reuse_skips_fulltext_with_wrong_front_matter(self):
        import shutil
        import sqlite3
        from core.fetch.storage import content_store
        from core.parse.source_text import PREPARATION_VERSION
        run = tempfile.mkdtemp(prefix="cvtest-store-")
        old_state = os.environ.get("CITATION_VERIFIER_STATE_DIR")
        try:
            os.environ["CITATION_VERIFIER_STATE_DIR"] = os.path.join(run, "state")
            ref = {
                "id": "ref-34",
                "ref_number": 34,
                "doi": "10.48550/arxiv.1503.08895",
                "title": "End-to-end memory networks",
                "year": 2015,
                "raw_entry": (
                    "Sainbayar Sukhbaatar, Arthur Szlam, Jason Weston, and Rob Fergus. "
                    "End-to-end memory networks. Advances in Neural Information Processing "
                    "Systems 28. 2015."
                ),
            }
            good_text = (
                "End-to-end Memory Networks\n"
                "Sainbayar Sukhbaatar Arthur Szlam Jason Weston Rob Fergus\n"
                + ("We introduce a memory network model trained end to end. " * 80)
            )
            bad_text = (
                "End-to-End Memory Networks: A Survey\n"
                "Raheleh Jafari Sina Razvarz Alexander Gegov\n"
                + ("This survey reviews dialog systems and neural memory models. " * 80)
            )
            good = content_store.record_parsed_text(
                run,
                ref,
                "fulltext",
                "webfetch",
                good_text,
                source_ref="https://arxiv.org/pdf/1503.08895",
                mapping="tokens",
                signal="title",
                score=1.0,
                preparation={"preparation_version": PREPARATION_VERSION},
            )
            bad = content_store.record_parsed_text(
                run,
                ref,
                "fulltext",
                "core",
                bad_text,
                source_ref="https://eprints.example.org/survey.pdf",
                mapping="tokens",
                signal="title",
                score=1.0,
                preparation={"preparation_version": PREPARATION_VERSION},
            )
            cached = content_store.find_reusable_parsed_text(run, ref, tiers=("fulltext",))
            self.assertEqual(cached["parsed_text_id"], good["parsed_text_id"])
            conn = sqlite3.connect(content_store.db_path(run))
            try:
                row = conn.execute(
                    "select active, missing from parsed_texts where parsed_text_id = ?",
                    (bad["parsed_text_id"],),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, (0, 1))
        finally:
            if old_state is None:
                os.environ.pop("CITATION_VERIFIER_STATE_DIR", None)
            else:
                os.environ["CITATION_VERIFIER_STATE_DIR"] = old_state
            shutil.rmtree(run)

    def test_document_identity_probe_rejects_short_title_without_cited_author(self):
        ref = {
            "raw_entry": (
                "Jimmy Lei Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. "
                "Layer normalization. arXiv preprint arXiv:1607.06450, 2016."
            ),
            "title": "Layer normalization",
        }
        bad = sources.document_identity_probe(
            ref,
            (
                "Root Mean Square Layer Normalization\n"
                "Biao Zhang Rico Sennrich\n"
                "Abstract Layer normalization has been successfully applied to various deep models.\n"
            ),
            {"matched_title": None},
        )
        self.assertFalse(bad["ok"])
        self.assertIn("short expected title", bad["reason"])

    def test_document_identity_probe_prefers_clean_parsed_title_over_noisy_matched_title(self):
        ref = {
            "raw_entry": (
                "Yoon Kim, Carl Denton, Luong Hoang, and Alexander M. Rush. "
                "Structured attention networks. In International Conference on "
                "Learning Representations, 2017."
            ),
            "title": "Structured attention networks",
        }
        ok = sources.document_identity_probe(
            ref,
            (
                "Structured Attention Networks\n"
                "Yoon Kim Carl Denton Luong Hoang Alexander M. Rush\n"
                "Abstract body text follows.\n"
            ),
            {"matched_title": "ICLR (Poster) - Structured Attention Networks"},
        )
        self.assertTrue(ok["ok"])

    def test_document_identity_probe_prefers_clean_matched_title_over_spaced_hyphen_parsed_title(self):
        ref = {
            "raw_entry": (
                "Minh-Thang Luong, Hieu Pham, and Christopher D Manning. "
                "Effective approaches to attention- based neural machine translation. "
                "arXiv preprint arXiv:1508.04025, 2015."
            ),
            "title": "Effective approaches to attention- based neural machine translation",
        }
        ok = sources.document_identity_probe(
            ref,
            (
                "Effective Approaches to Attention-based Neural Machine Translation\n"
                "Minh-Thang Luong Hieu Pham Christopher D. Manning\n"
                "Abstract body text follows.\n"
            ),
            {"matched_title": "Effective Approaches to Attention-based Neural Machine Translation"},
        )
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["reason"], "front_matter_identity_confirmed")

    def test_try_store_fulltext_rejects_weak_preprint_match(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-9", "ref_number": 9, "doi": "10.1101/2020.05.01.1",
               "title": "Treatment effects on mortality in sepsis", "ay_surname": "Smith"}
        with tempfile.TemporaryDirectory() as run:
            # Generic corroborate passes on title tokens, but the stricter preprint gate
            # fails (author surname absent) -> identity_mismatch, nothing stored.
            text = "Treatment effects on mortality in sepsis discussed broadly here."
            result = fetch_store.try_store_fulltext(
                run, ref, {"via": "crossref"}, text,
                method="biorxiv", source_ref="https://www.biorxiv.org/x.pdf",
                content_version="preprint")
            self.assertEqual(result["status"], "identity_mismatch")
            self.assertIn("stricter", result["reason"])
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_document_that_mentions_paper_only_in_references(self):
        from core.fetch.storage import fetch_store
        ref = {
            "id": "ref-10",
            "ref_number": 10,
            "doi": "10.1234/test",
            "title": "Treatment effects on mortality in sepsis",
            "raw_entry": "Smith J. Treatment effects on mortality in sepsis. J Med. 2020. doi:10.1234/test",
        }
        with tempfile.TemporaryDirectory() as run:
            text = (
                ("A completely different paper by Brown about imaging biomarkers.\n" * 160)
                + "References\nSmith J. Treatment effects on mortality in sepsis. doi:10.1234/test"
            )
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                {"via": "crossref_metadata", "matched_title": "Treatment effects on mortality in sepsis"},
                text,
                method="openalex",
                source_ref="https://example.org/other-paper.pdf",
            )
            self.assertEqual(result["status"], "identity_mismatch")
            self.assertIn("front matter", result["reason"])
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_non_body_material(self):
        from core.fetch.storage import fetch_store

        cases = [
            (
                "repository_cover_sheet_only",
                {
                    "id": "ref-cover",
                    "ref_number": 91,
                    "title": "Autobiographical memory and trauma in adolescents",
                    "raw_entry": (
                        "Meesters. Autobiographical memory and trauma in adolescents. 2000."
                    ),
                },
                (
                    "Autobiographical memory and trauma in adolescents\n"
                    "Citation for published version (APA).\n"
                    "Document status and date: Published.\n"
                    "Please check the document version of this publication.\n"
                    + "Repository policy and copyright information. " * 70
                    + "\nGeneral rights\nTake down policy\nDownload date: 27 Aug. 2026\n"
                ),
            ),
            (
                "book_review_not_cited_work",
                {
                    "id": "ref-book",
                    "ref_number": 379,
                    "title": "The Story of the Human Body",
                    "raw_entry": "Lieberman. The Story of the Human Body. 2013.",
                },
                (
                    "Book reviews\n223\nThe Story of the Human Body by Lieberman.\n"
                    + "The reviewer discusses this volume and several other books. " * 80
                ),
            ),
            (
                "supplementary_information_not_cited_work",
                {
                    "id": "ref-supplement",
                    "ref_number": 380,
                    "title": "A controlled trial",
                    "raw_entry": "Smith. A controlled trial. 2020.",
                },
                (
                    "SUPPLEMENTARY INFORMATION\n"
                    "Supplementary Information for A controlled trial\n"
                    + "Additional methods and tables are reported here. " * 80
                ),
            ),
            (
                "supplementary_information_not_cited_work",
                {
                    "id": "ref-supplement-split",
                    "ref_number": 381,
                    "title": "A controlled trial",
                    "raw_entry": "Smith. A controlled trial. 2020.",
                },
                (
                    "SUPPLEMENTARY INFORMATION\n"
                    "Supplementary Information for\n"
                    "A controlled trial\n"
                    + "Additional methods and tables are reported here. " * 80
                ),
            ),
        ]
        for reason_code, ref, text in cases:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as run:
                result = fetch_store.try_store_fulltext(
                    run,
                    ref,
                    {"status": "resolved", "matched_title": ref["title"]},
                    text,
                    method="metadata",
                    source_ref="https://repository.example/document.pdf",
                )
                self.assertEqual(result["status"], "identity_mismatch")
                self.assertEqual(result["reason_code"], reason_code)
                self.assertEqual(result["document_relation"]["decision"], "incompatible")
                self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_material_before_exact_acl_id_fallback(self):
        from core.fetch.storage import fetch_store

        cases = [
            (
                "supplementary_information_not_cited_work",
                "Supplementary Material:\n" + "Supplement contents. " * 160,
            ),
            (
                "book_review_not_cited_work",
                "Gotthard, Axel: Rezension über: Luca Scholz\n"
                + "Review contents. " * 160,
            ),
            (
                "access_verification_shell_not_cited_work",
                "Account help. " * 160
                + "Thank you for your patience while we verify access. "
                "If you are in Reader mode please exit and log into your Times account, "
                "or subscribe for all of The Times.",
            ),
            (
                "paginated_viewer_first_page_only",
                "Previous 1 / 9 Next\nNormal view\n" + "First page. " * 160
                + "\nSelect target paragraph3\nSave\n",
            ),
        ]
        ref = {
            "id": "ref-material-acl",
            "ref_number": 41,
            "title": "BERT: Pre-training of Deep Bidirectional Transformers",
            "raw_entry": "Devlin. BERT: Pre-training of Deep Bidirectional Transformers. 2019.",
        }
        resolve_result = {
            "status": "resolved",
            "paper_id": "D13-1170",
            "matched_title": ref["title"],
        }
        for reason_code, text in cases:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as run:
                result = fetch_store.try_store_fulltext(
                    run,
                    ref,
                    resolve_result,
                    text,
                    method="acl",
                    source_ref="https://aclanthology.org/D13-1170.Attachment.pdf",
                )
                self.assertEqual(result["status"], "identity_mismatch")
                self.assertEqual(result["reason_code"], reason_code)
                self.assertEqual(result["document_relation"]["decision"], "incompatible")

    def test_try_store_fulltext_records_inconclusive_document_relation(self):
        from core.fetch.storage import fetch_store

        ref = {
            "id": "ref-ordinary-relation",
            "ref_number": 94,
            "doi": "10.1234/ordinary-relation",
            "title": "Ordinary relation article",
            "raw_entry": "Smith. Ordinary relation article. 2020. doi:10.1234/ordinary-relation",
        }
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                {"status": "resolved", "doi": ref["doi"], "matched_title": ref["title"]},
                "Ordinary relation article\n" + "Substantive article body. " * 200,
                method="metadata",
                source_ref="https://repository.example/article.pdf",
            )
        self.assertEqual(result["status"], "stored")
        self.assertEqual(result["document_relation"]["decision"], "inconclusive")

    def test_try_store_fulltext_rejects_doi_of_hard_conflicted_metadata_candidate(self):
        """A candidate DOI cannot override conflicts against the cited work."""
        from core.fetch.storage import fetch_store

        ref = {
            "id": "ref-hard-conflict",
            "ref_number": 66,
            "title": "UNHCR notice on border violence",
            "raw_entry": "UNHCR. Notice on border violence. 2021.",
        }
        resolved = {
            "status": "unverified",
            "via": "crossref_metadata",
            "matched_title": "Asylum, Borders, and the Politics of Violence",
            "resolution_basis": "metadata_search",
            "existence_confidence": "low",
            "evidence_profile": {
                "minimum_checks_completed": False,
                "metadata_match": {
                    "author_conflict": True,
                    "year_conflict": True,
                    "metadata_conflict": True,
                    "hard_conflicts": ["author", "year"],
                },
            },
        }
        wrong_text = (
            "Asylum, Borders, and the Politics of Violence. "
            "Aradau and Canzutti describe suspicion and cruelty in asylum policy. "
        ) * 80
        with tempfile.TemporaryDirectory() as run, \
                mock.patch.object(
                    fetch_store._sources, "corroborate", return_value=("doi", 1.0)
                ), \
                mock.patch.object(
                    fetch_store._sources,
                    "document_identity_probe",
                    return_value={
                        "ok": True,
                        "decision": "confirmed",
                        "reason": "matched resolver candidate title",
                        "reason_code": "front_matter_identity_confirmed",
                    },
                ):
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                resolved,
                wrong_text,
                method="metadata",
                source_ref="https://publisher.example/wrong-candidate.pdf",
            )

            self.assertEqual(result["status"], "identity_mismatch")
            self.assertEqual(result["reason_code"], "resolve_metadata_identity_conflict")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_keeps_citation_confirmed_document_despite_resolve_conflict(self):
        from core.fetch.storage import fetch_store

        ref = {
            "id": "ref-adam",
            "ref_number": 20,
            "title": "Adam: A Method for Stochastic Optimization",
            "raw_entry": (
                "Diederik P. Kingma and Jimmy Ba. Adam: A Method for Stochastic "
                "Optimization. 2014."
            ),
            "ay_surname": "kingma",
            "year": 2014,
        }
        resolved = {
            "status": "unverified",
            "via": "openalex_search",
            "matched_title": ref["title"],
            "evidence_profile": {
                "metadata_match": {
                    "hard_conflicts": ["author", "year"],
                }
            },
        }
        text = (
            "Adam: A Method for Stochastic Optimization\n"
            "Diederik P. Kingma and Jimmy Ba\n"
            "2014\nAbstract\n"
            + "We introduce an optimization method for stochastic objectives. " * 100
        )
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                resolved,
                text,
                method="metadata",
                source_ref="https://repository.example/1412.6980v5.pdf",
                candidate_context={"title": "A conflicting resolver candidate"},
            )

            self.assertEqual(result["status"], "stored")
            self.assertEqual(
                result["identity_probe"]["expected_title_source"], "citation"
            )
            self.assertEqual(len(sources.load_manifest(run).get("entries") or []), 1)

    def test_try_store_fulltext_citation_confirmation_precedes_related_candidate_title(self):
        from core.fetch.storage import fetch_store

        ref = {
            "id": "ref-postal",
            "ref_number": 174,
            "title": "The Story of the Universal Postal Union",
            "raw_entry": (
                "Madeleine Herren. The Story of the Universal Postal Union. 2025."
            ),
            "ay_surname": "herren",
            "year": 2025,
        }
        related_title = (
            "On the 150th Anniversary of the Universal Postal Union: "
            "Revisiting an Overlooked Position"
        )
        text = (
            "The Story of the Universal Postal Union\nMadeleine Herren\n2025\n"
            "The postal union became a specialised international institution. " * 100
        )
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                {"status": "resolved", "matched_title": related_title},
                text,
                method="metadata",
                source_ref="https://museum.example/the-story-of-the-postal-union",
                candidate_context={"title": related_title},
            )

            self.assertEqual(result["status"], "stored")
            self.assertEqual(
                result["identity_probe"]["expected_title_source"], "citation"
            )

    def test_try_store_fulltext_keeps_confirmed_metadata_candidate_without_conflicts(self):
        """The conflict guard must not disable legitimate metadata recovery."""
        from core.fetch.storage import fetch_store

        ref = {
            "id": "ref-clean-metadata",
            "ref_number": 67,
            "title": "Treatment effects on mortality in sepsis",
            "raw_entry": "Smith. Treatment effects on mortality in sepsis. 2020.",
        }
        resolved = {
            "status": "resolved",
            "via": "crossref_metadata",
            "matched_title": ref["title"],
            "resolution_basis": "metadata_search",
            "existence_confidence": "medium",
            "evidence_profile": {
                "minimum_checks_completed": True,
                "metadata_match": {
                    "author_match": True,
                    "year_match": True,
                    "hard_conflicts": [],
                },
            },
        }
        text = (
            "Treatment effects on mortality in sepsis. Smith. "
            "Methods, participants, outcomes, and detailed results are reported. "
        ) * 80
        with tempfile.TemporaryDirectory() as run, \
                mock.patch.object(
                    fetch_store._sources, "corroborate", return_value=("tokens", 1.0)
                ), \
                mock.patch.object(
                    fetch_store._sources,
                    "document_identity_probe",
                    return_value={
                        "ok": True,
                        "decision": "confirmed",
                        "reason": "front matter confirmed the cited work",
                        "reason_code": "front_matter_identity_confirmed",
                    },
                ):
            result = fetch_store.try_store_fulltext(
                run,
                ref,
                resolved,
                text,
                method="metadata",
                source_ref="https://publisher.example/correct-paper.pdf",
            )

            self.assertEqual(result["status"], "stored")
            self.assertEqual(len(sources.load_manifest(run).get("entries") or []), 1)

    # -- identity-preserving redirect: a route mismatch that is a provable
    # permanent move of the same resource (e.g. a GitHub repo transfer) still
    # takes the cited-URL trust path. See core/fetch/storage/fetch_store.py's
    # `_identity_preserving_redirect` and the guard in `try_store_fulltext`.

    _WEBPAGE_BODY = ("PyTorch Image Models (timm) is a collection of image models, "
                      "layers, utilities, and scripts. " * 60).strip()

    def test_try_store_fulltext_accepts_permanent_redirect_same_last_segment(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-20", "ref_number": 20, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified", "fulltext_exists": "unknown"},
                self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://github.com/huggingface/pytorch-image-models",
                redirect_chain=[(301, "https://github.com/huggingface/pytorch-image-models")],
            )
            self.assertEqual(result["status"], "stored")
            self.assertEqual(
                result["identity_probe"]["reason_code"],
                "identity_confirmed_explicit_cited_url",
            )
            man = sources.load_manifest(run)
            entry = next(e for e in man.get("entries", []) if e.get("tier") == "fulltext")
            self.assertEqual(entry["mapping"], "cited_url")
            self.assertEqual(entry["identity_status"], "cited_url_reachable")

    def test_pipeline_injection_wrapper_forwards_redirect_chain(self):
        # The pipeline never calls fetch_store.try_store_fulltext directly: it goes
        # through the dependency-injection wrapper core.fetch._try_store_fulltext,
        # which re-forwards every parameter by name. A parameter added to the store
        # but not to the wrapper type-errors on the first real fetch while every
        # direct-call test stays green, so the wrapper is asserted here explicitly.
        import core.fetch.service as fetch_mod
        ref = {"id": "ref-27", "ref_number": 27, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_mod._try_store_fulltext(
                run, ref, {"status": "unverified", "fulltext_exists": "unknown"},
                self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://github.com/huggingface/pytorch-image-models",
                redirect_chain=[(301, "https://github.com/huggingface/pytorch-image-models")],
            )
            # Reaching "stored" proves the chain survived the hop: without forwarding
            # it the store sees None and returns cited_url_route_mismatch.
            self.assertEqual(result["status"], "stored")

    def test_try_store_fulltext_rejects_permanent_redirect_onto_site_root(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-21", "ref_number": 21, "doi": None,
               "url": "https://site.org/some/page",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://site.org/",
                redirect_chain=[(301, "https://site.org/")],
            )
            self.assertEqual(result["status"], "identity_inconclusive")
            self.assertEqual(result["reason_code"], "cited_url_route_mismatch")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_permanent_redirect_to_different_resource(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-22", "ref_number": 22, "doi": None,
               "url": "https://site.org/docs/paper-x",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://site.org/docs/paper-y",
                redirect_chain=[(301, "https://site.org/docs/paper-y")],
            )
            self.assertEqual(result["status"], "identity_inconclusive")
            self.assertEqual(result["reason_code"], "cited_url_route_mismatch")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_temporary_redirect_even_with_same_last_segment(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-23", "ref_number": 23, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://github.com/huggingface/pytorch-image-models",
                redirect_chain=[(302, "https://github.com/huggingface/pytorch-image-models")],
            )
            self.assertEqual(result["status"], "identity_inconclusive")
            self.assertEqual(result["reason_code"], "cited_url_route_mismatch")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_rejects_mixed_permanent_and_temporary_chain(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-24", "ref_number": 24, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://github.com/huggingface/pytorch-image-models",
                redirect_chain=[
                    (301, "https://github.com/intermediate/pytorch-image-models"),
                    (302, "https://github.com/huggingface/pytorch-image-models"),
                ],
            )
            self.assertEqual(result["status"], "identity_inconclusive")
            self.assertEqual(result["reason_code"], "cited_url_route_mismatch")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_route_mismatch_unchanged_without_a_chain(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-25", "ref_number": 25, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        for chain in (None, []):
            with self.subTest(redirect_chain=chain):
                with tempfile.TemporaryDirectory() as run:
                    result = fetch_store.try_store_fulltext(
                        run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                        method="reference_url",
                        source_ref="https://github.com/huggingface/pytorch-image-models",
                        redirect_chain=chain,
                    )
                    self.assertEqual(result["status"], "identity_inconclusive")
                    self.assertEqual(result["reason_code"], "cited_url_route_mismatch")
                    self.assertEqual(sources.load_manifest(run).get("entries"), [])

    def test_try_store_fulltext_exact_route_match_still_stored_unaffected(self):
        from core.fetch.storage import fetch_store
        ref = {"id": "ref-26", "ref_number": 26, "doi": None,
               "url": "https://github.com/rwightman/pytorch-image-models",
               "title": None, "source_type": "webpage"}
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, self._WEBPAGE_BODY,
                method="reference_url",
                source_ref="https://github.com/rwightman/pytorch-image-models",
            )
            self.assertEqual(result["status"], "stored")
            man = sources.load_manifest(run)
            entry = next(e for e in man.get("entries", []) if e.get("tier") == "fulltext")
            self.assertEqual(entry["identity_status"], "cited_url_reachable")

    def test_try_store_fulltext_article_ref_unaffected_by_identity_preserving_redirect(self):
        from core.fetch.storage import fetch_store
        ref = {
            "id": "ref-27", "ref_number": 27, "doi": None,
            "url": "https://example.org/articles/my-paper",
            "title": "A Distinctive Study About Something Specific",
            "source_type": "article",
        }
        text = ("This document discusses a completely unrelated topic in depth. " * 60).strip()
        with tempfile.TemporaryDirectory() as run:
            result = fetch_store.try_store_fulltext(
                run, ref, {"status": "unverified"}, text,
                method="reference_url",
                source_ref="https://example.org/journal/my-paper",
                # Same shape that would qualify under the web-citation exception
                # (permanent, not the root, same last segment) — but this ref
                # requires identity, so the exception must never engage.
                redirect_chain=[(301, "https://example.org/journal/my-paper")],
            )
            self.assertNotEqual(result["status"], "stored")
            self.assertNotEqual(result.get("reason_code"), "cited_url_route_mismatch")
            self.assertEqual(sources.load_manifest(run).get("entries"), [])






# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tests: fetch.py  (no network â€” mock _get / Unpaywall / pdf.extract)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€






# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tests: resolve.py â€” OpenLibrary book resolver
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€



# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tests: parse_manuscript stop on bibliography-only (no claims)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€




