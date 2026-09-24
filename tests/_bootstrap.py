#!/usr/bin/env python3
# tests/_bootstrap.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
Shared unittest bootstrap for the split core test suite.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.parse import authoryear  # noqa: E402
import core.report as report  # noqa: E402
from core.resolve import service as resolve  # noqa: E402
from core.app import run  # noqa: E402
from core.resolve import sources  # noqa: E402
from core.parse.parse_manuscript import parse  # noqa: E402
from core.verify.claim_evidence.domain.types import Jury1Decision  # noqa: E402
from core.verify.claim_evidence.evidence.context import (  # noqa: E402
    build_effective_context,
)
from core.verify.claim_evidence.evidence.grounding import (  # noqa: E402
    ground_jury1_decision,
)


def grounded_attempt_payload(verdict, source_text):
    """Build an accepted test attempt only after final-boundary grounding passes."""
    decision = Jury1Decision(
        verdict["outcome"],
        tuple(verdict.get("quoted_passages") or ()),
        str(verdict.get("note") or "grounded test fixture"),
        verdict.get("supported_part"),
        verdict.get("incompatible_proposition"),
        verdict.get("reason"),
    )
    context = build_effective_context(
        source_text,
        mode="full_text",
        budget=len(source_text),
    )
    grounded = ground_jury1_decision(decision, source_text, context)
    return {
        **verdict,
        "quoted_passages": [quote.text for quote in grounded.quotes],
        "source_span_ids": [quote.span_id for quote in grounded.quotes],
        "source_span_source_hash": (
            grounded.quotes[0].source_hash if grounded.quotes else None
        ),
        "accepted": True,
        "passage_verified": True,
        "guard_code": "ok",
    }


class LinksDisabledMixin:
    """Force the text-only pathway (CITATION_VERIFIER_PDF_LINKS=0) for a test.

    Gold-standard PDFs (ViT, BERT, …) carry a hyperlink layer, so with links on a
    regression in the *text* pipeline could be masked by link recovery.  Text-
    pathway assertions on those PDFs must therefore run with links disabled, so
    the gold standard keeps testing the text pipeline itself.
    """

    _PDF_LINKS_ENV = "CITATION_VERIFIER_PDF_LINKS"

    def setUp(self):
        super().setUp()
        self._saved_pdf_links = os.environ.get(self._PDF_LINKS_ENV)
        os.environ[self._PDF_LINKS_ENV] = "0"

    def tearDown(self):
        if self._saved_pdf_links is None:
            os.environ.pop(self._PDF_LINKS_ENV, None)
        else:
            os.environ[self._PDF_LINKS_ENV] = self._saved_pdf_links
        super().tearDown()
