#!/usr/bin/env python3
# core/fetch/extraction/ocr.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""
ocr.py — opt-in OCR for scanned PDFs with no text layer.

This is the "extra tool" for the Phase 2b parked-PDF case (see PLAYBOOK): when
fetch.py (or a user-provided PDF) yields no extractable text — typically an old
scan without an OCR layer — the source is parked, NOT discarded. On EXPLICIT user
request, this module runs OCR to turn that scan into a usable .txt the rest of the
system can verify against.

The standalone command is opt-in because OCR is slow and lossy. Fetch may also
invoke this backend for associated eligible parked scans when
``CITATION_VERIFIER_OCR_AUTO=1``; that path remains provenance-recorded.

Backends (first available wins, all declared, never silent):
  1. ocrmypdf            — system: rebuilds a text layer, --sidecar gives clean text
  2. pdftoppm + tesseract — system: render pages to images, OCR each (poppler + tesseract)
  3. rapidocr + pypdfium2 — BUNDLED: pure-pip, models included, no system binary needed
                            (pip install -r requirements-ocr.txt). This is the backend we
                            ship so OCR works out of the box without apt/system setup.
If no backend is available, raises RuntimeError telling the user what to install.

Usage:
  python run.py ocr --pdf scan.pdf --out scan.txt [--lang eng] [--dpi 300]

The resulting .txt is submitted through ``run.py tasks answer-fetch
--ocr-text-file`` for the matching parked Fetch task. This preserves origin
``ocr`` so provenance stays honest: the text was machine-OCR'd, not read from
a clean source.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import logging
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading

try:
    from core.fetch.extraction import pdf as _pdf
except ImportError:
    import pdf as _pdf

OCR_TIMEOUT = 600   # OCR is slow; allow up to 10 min per document
# Each Fetch identity batch renders at most three pages. Allow 90 seconds for
# model startup and one batch; full-document OCR keeps the ten-minute budget.
RAPIDOCR_IDENTITY_PROBE_TIMEOUT = 90
ENV_OCR_WORKERS = "CITATION_VERIFIER_OCR_WORKERS"
DEFAULT_OCR_WORKERS = 1

# OCR is deliberately scheduled independently of download workers.  In particular,
# ``fetch_fulltext`` can be called by a much larger network pool, but it cannot make
# RapidOCR model inference fan out beyond this process-wide executor.
_SCHEDULER: concurrent.futures.ThreadPoolExecutor | None = None
_SCHEDULER_LOCK = threading.Lock()
_OCR_CACHE: dict[tuple, concurrent.futures.Future] = {}
_OCR_CACHE_LOCK = threading.Lock()
# Each cache entry can hold an entire document's OCR text; bound the cache so a long
# autonomous run OCRing many scans does not grow memory without limit. Oldest entries
# are dropped first (FIFO); a dropped entry is simply recomputed if requested again.
_OCR_CACHE_MAX = 512


def _evict_ocr_cache_locked() -> None:
    """Trim the OCR cache to its bound. Caller must hold ``_OCR_CACHE_LOCK``."""
    while len(_OCR_CACHE) > _OCR_CACHE_MAX:
        oldest_key = next(iter(_OCR_CACHE))
        _OCR_CACHE.pop(oldest_key, None)


_RAPIDOCR_LOGGER_NAMES = ("rapidocr", "rapidocr_onnxruntime", "RapidOCR", "onnxruntime")
_RAPIDOCR_LOG_LOCK = threading.Lock()


@contextlib.contextmanager
def _suppress_rapidocr_info_logs():
    """Keep third-party OCR setup chatter out of the operator console."""
    with _RAPIDOCR_LOG_LOCK:
        loggers = [logging.getLogger(name) for name in _RAPIDOCR_LOGGER_NAMES]
        for logger in loggers:
            logger.setLevel(logging.WARNING)
        try:
            yield
        finally:
            for logger in loggers:
                logger.setLevel(logging.WARNING)


def _new_rapidocr_engine():
    """Load RapidOCR before suppressing the setup logs emitted by its constructor."""
    engine_class = _rapidocr_class()
    with _suppress_rapidocr_info_logs():
        if engine_class.__module__.split(".", 1)[0] == "rapidocr":
            return engine_class(params={"Global.log_level": "warning"})
        return engine_class()


def ocr_worker_count(environ: dict | None = None) -> int:
    """Configured process-wide OCR concurrency, clamped to a safe positive value."""
    env = environ or os.environ
    try:
        return max(1, int(env.get(ENV_OCR_WORKERS, DEFAULT_OCR_WORKERS)))
    except (TypeError, ValueError):
        return DEFAULT_OCR_WORKERS


def _scheduler() -> concurrent.futures.ThreadPoolExecutor:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = concurrent.futures.ThreadPoolExecutor(
                max_workers=ocr_worker_count(), thread_name_prefix="citation-ocr")
        return _SCHEDULER


def _reset_ocr_scheduler_for_tests() -> None:
    """Reset process state for isolated tests; not part of the application API."""
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is not None:
            _SCHEDULER.shutdown(wait=True, cancel_futures=True)
        _SCHEDULER = None
    with _OCR_CACHE_LOCK:
        _OCR_CACHE.clear()


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_selection(pages: object) -> tuple[int, ...] | None:
    """Normalise one-based page selections; ``None`` means the complete PDF."""
    if pages is None:
        return None
    if isinstance(pages, range):
        values = tuple(pages)
    elif isinstance(pages, int):
        values = (pages,)
    else:
        values = tuple(int(value) for value in pages)
    if not values or any(value < 1 for value in values):
        raise ValueError("pages must contain one-based positive page numbers")
    return tuple(sorted(set(values)))


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def pdf_page_count(pdf_path: str) -> int | None:
    """Return a cheap page count when an installed PDF backend can provide it."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(pdf_path)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        pass
    try:
        import pymupdf as fitz

        doc = fitz.open(pdf_path)
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Backend 1: ocrmypdf                                                         #
# --------------------------------------------------------------------------- #

def _ocrmypdf(pdf_path: str, lang: str, pages: tuple[int, ...] | None = None) -> str | None:
    # ocrmypdf has no portable page-selection sidecar mode. Use a renderer for probes.
    if pages is not None:
        return None
    if not _have("ocrmypdf"):
        return None
    tmpdir = tempfile.mkdtemp()
    out_pdf = os.path.join(tmpdir, "ocr.pdf")
    sidecar = os.path.join(tmpdir, "sidecar.txt")
    try:
        subprocess.run(
            ["ocrmypdf", "--force-ocr", "--language", lang,
             "--sidecar", sidecar, pdf_path, out_pdf],
            check=True, capture_output=True, timeout=OCR_TIMEOUT,
        )
        if os.path.exists(sidecar):
            text = open(sidecar, encoding="utf-8", errors="replace").read()
            return text if text.strip() else None
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Backend 2: pdftoppm (poppler) + tesseract                                  #
# --------------------------------------------------------------------------- #

def _tesseract_poppler(pdf_path: str, lang: str, dpi: int,
                       pages: tuple[int, ...] | None = None) -> str | None:
    if not (_have("pdftoppm") and _have("tesseract")):
        return None
    tmpdir = tempfile.mkdtemp()
    prefix = os.path.join(tmpdir, "page")
    try:
        command = ["pdftoppm", "-png", "-r", str(dpi)]
        if pages:
            command.extend(["-f", str(min(pages)), "-l", str(max(pages))])
        subprocess.run(
            command + [pdf_path, prefix],
            check=True, capture_output=True, timeout=OCR_TIMEOUT,
        )
        rendered = sorted(f for f in os.listdir(tmpdir) if f.endswith(".png"))
        if not rendered:
            return None
        chunks: list[str] = []
        for page in rendered:
            if pages and int(page.rsplit("-", 1)[-1].split(".", 1)[0]) not in pages:
                continue
            img = os.path.join(tmpdir, page)
            res = subprocess.run(
                ["tesseract", img, "stdout", "-l", lang],
                check=True, capture_output=True, timeout=OCR_TIMEOUT,
            )
            chunks.append(res.stdout.decode("utf-8", errors="replace"))
        text = "\n\n".join(chunks)
        return text if text.strip() else None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Backend 3: rapidocr + pypdfium2  (BUNDLED, pure-pip, no system binary)      #
# --------------------------------------------------------------------------- #

def _have_rapidocr() -> bool:
    """Check bundled OCR package presence without importing its heavy runtime."""
    try:
        has_pdfium = importlib.util.find_spec("pypdfium2") is not None
        has_legacy_rapidocr = importlib.util.find_spec("rapidocr_onnxruntime") is not None
        has_rapidocr_v2 = (
            importlib.util.find_spec("rapidocr") is not None
            and importlib.util.find_spec("onnxruntime") is not None
        )
        return has_pdfium and (has_legacy_rapidocr or has_rapidocr_v2)
    except Exception:
        return False


def _rapidocr_class():
    """Load either the legacy package or its Python-3.13+ compatible successor."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        from rapidocr import RapidOCR
    return RapidOCR


def _rapidocr_lines(output: object) -> list[str]:
    """Normalize legacy tuple output and RapidOCR 2/3 result objects."""
    result = output
    if isinstance(output, tuple) and len(output) == 2:
        result = output[0]
    txts = getattr(result, "txts", None)
    if txts:
        return [str(text) for text in txts if str(text).strip()]
    if isinstance(result, (list, tuple)):
        return [
            str(line[1])
            for line in result
            if isinstance(line, (list, tuple)) and len(line) >= 2 and str(line[1]).strip()
        ]
    return []


def _rapidocr_pypdfium2_in_process(pdf_path: str, dpi: int,
                                   pages: tuple[int, ...] | None = None) -> str | None:
    """Import, render, and run bundled OCR inside the isolated child process."""
    import numpy as np
    import pypdfium2 as pdfium

    engine = _new_rapidocr_engine()
    pdf = pdfium.PdfDocument(pdf_path)
    scale = dpi / 72.0
    chunks: list[str] = []
    try:
        for index, page in enumerate(pdf, start=1):
            if pages and index not in pages:
                continue
            bitmap = page.render(scale=scale)
            arr = np.asarray(bitmap.to_pil())
            lines = _rapidocr_lines(engine(arr))
            if lines:
                chunks.append("\n".join(lines))
    finally:
        pdf.close()
    text = "\n\n".join(chunks)
    return text if text.strip() else None


def _rapidocr_child_main(connection, worker, pdf_path: str, dpi: int,
                         pages: tuple[int, ...] | None) -> None:
    """Child entry point; send only a small status envelope plus the OCR text."""
    try:
        result = ("ok", worker(pdf_path, dpi, pages))
    except BaseException as exc:
        result = ("error", f"{type(exc).__name__}: {exc}")
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        connection.close()


def _reap_rapidocr_child(process) -> None:
    """Stop and reap a child on every exit path, including OCR timeouts."""
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if kill is not None:
            kill()
        process.join(timeout=1)
    if process.is_alive():
        raise RuntimeError("RapidOCR child process could not be stopped")
    process.close()


def _run_rapidocr_subprocess(pdf_path: str, dpi: int,
                             pages: tuple[int, ...] | None, timeout: float,
                             worker=_rapidocr_pypdfium2_in_process) -> str | None:
    """Run one RapidOCR job in a killable spawn child with a finite deadline."""
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_rapidocr_child_main,
        args=(send, worker, pdf_path, dpi, pages),
        name="citation-rapidocr",
    )
    try:
        process.start()
    except Exception as exc:
        send.close()
        receive.close()
        _reap_rapidocr_child(process)
        raise RuntimeError(f"could not start RapidOCR child: {exc}") from exc
    send.close()

    try:
        if not receive.poll(timeout):
            raise RuntimeError(f"RapidOCR timed out after {timeout:g} seconds")
        try:
            status, payload = receive.recv()
        except (EOFError, OSError) as exc:
            raise RuntimeError("RapidOCR child exited without a result") from exc

        process.join(timeout=1)
        if process.is_alive():
            raise RuntimeError("RapidOCR child did not exit after sending its result")
        if process.exitcode != 0:
            raise RuntimeError(f"RapidOCR child exited with status {process.exitcode}")
        if status != "ok":
            raise RuntimeError(f"RapidOCR failed in child process: {payload}")
        return payload
    finally:
        receive.close()
        _reap_rapidocr_child(process)


def _rapidocr_pypdfium2(pdf_path: str, dpi: int,
                        pages: tuple[int, ...] | None = None) -> str | None:
    """Self-contained bundled backend, isolated from the caller by spawn."""
    timeout = (
        RAPIDOCR_IDENTITY_PROBE_TIMEOUT if pages is not None else OCR_TIMEOUT
    )
    return _run_rapidocr_subprocess(pdf_path, dpi, pages, timeout)


# --------------------------------------------------------------------------- #
#  Public API                                                                  #
# --------------------------------------------------------------------------- #

def available_backends() -> list[str]:
    backends = []
    if _have("ocrmypdf"):
        backends.append("ocrmypdf")
    if _have("pdftoppm") and _have("tesseract"):
        backends.append("tesseract+poppler")
    if _have_rapidocr():
        backends.append("rapidocr+pypdfium2")
    return backends


def _ocr_pdf_uncached(pdf_path: str, lang: str, dpi: int,
                      pages: tuple[int, ...] | None, backend: str) -> tuple[str, str]:
    """Run one job on an OCR worker. ``backend`` is a stable cache dimension."""
    backends = available_backends()
    if backend != "auto":
        if backend not in backends:
            raise RuntimeError(f"requested OCR backend is unavailable: {backend}")
        backends = [backend]
    if not backends:
        bundled_install = (
            "pip install pypdfium2 rapidocr onnxruntime"
            if sys.version_info >= (3, 13) else "pip install -r requirements-ocr.txt")
        raise RuntimeError("no OCR backend available. Install the bundled (pure-pip, no system setup) stack:\n"
                           f"  {bundled_install}")
    runners = {
        "ocrmypdf": lambda: _ocrmypdf(pdf_path, lang, pages),
        "tesseract+poppler": lambda: _tesseract_poppler(pdf_path, lang, dpi, pages),
        "rapidocr+pypdfium2": lambda: _rapidocr_pypdfium2(pdf_path, dpi, pages),
    }
    for name in backends:
        text = runners[name]()
        if text:
            return text, name
    selection = "all pages" if pages is None else f"pages {','.join(map(str, pages))}"
    raise RuntimeError(f"OCR produced no usable text ({selection}; backends tried: {', '.join(backends)})")


def ocr_pdf(pdf_path: str, lang: str = "eng", dpi: int = 300, *,
            pages: object = None, backend: str = "auto", retry: bool = False) -> tuple[str, str]:
    """OCR a scanned PDF to text. Returns (text, method).

    Tries system backends first (faster), then the bundled pure-pip backend.

    Raises:
      FileNotFoundError — pdf_path does not exist
      RuntimeError      — no OCR backend available, or all backends failed
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    selection = _page_selection(pages)
    key = (_file_hash(pdf_path), lang, int(dpi), selection, backend)
    with _OCR_CACHE_LOCK:
        future = None if retry else _OCR_CACHE.get(key)
        if future is None:
            future = _scheduler().submit(_ocr_pdf_uncached, pdf_path, lang, int(dpi), selection, backend)
            _OCR_CACHE[key] = future
            _evict_ocr_cache_locked()
    # A failed Future intentionally remains cached: unattended fetch retries must
    # not repeatedly spend OCR time. Pass retry=True for a deliberate retry.
    return future.result()


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="scanned PDF to OCR")
    ap.add_argument("--out", required=True, help="destination .txt")
    ap.add_argument("--lang", default="eng", help="tesseract language(s), e.g. 'eng' or 'eng+ita'")
    ap.add_argument("--dpi", type=int, default=300, help="render DPI (tesseract+poppler backend)")
    args = ap.parse_args()

    try:
        text, method = ocr_pdf(args.pdf, lang=args.lang, dpi=args.dpi)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Quality gate: an OCR result below threshold is a failure, declared not buried.
    if not _pdf._quality(text):
        print(f"ERROR: OCR output failed the quality gate (len={len(text)}, "
              f"method={method}); likely a poor scan. Provide a transcribed .txt.",
              file=sys.stderr)
        sys.exit(3)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    import json
    print(json.dumps({"ok": True, "method": method, "chars": len(text),
                      "out": args.out}, ensure_ascii=False))


if __name__ == "__main__":
    main()
