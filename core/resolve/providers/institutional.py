#!/usr/bin/env python3
# core/resolve/providers/institutional.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Institutional / grey-literature report resolver.

A citation to an institutional report — WHO, FDA, EMA, a national agency, an
inter-governmental body — is a hard case for the scholarly resolvers: the report
itself is almost never in Crossref / OpenAlex / Europe PMC (only *derivative*
articles *about* it are, which the author-conflict guard now correctly rejects),
and the citation frequently carries no DOI and no link. The document actually
lives on the institution's own site (who.int, fda.gov, europa.eu, ...).

This resolver recovers it the only way that works: a plain web search of the
title, then a strict discriminator that accepts a result as the report ONLY when

  1. the result's host is authoritative *by class* — an authoritative TLD
     (.gov/.int/.edu/.mil, incl. multi-label like gov.uk) or a configured
     inter-governmental domain — NOT a hardcoded institution name;
  2. it is *institution-coherent*: an identity token taken FROM THE CITATION's
     own organisational author (its acronym or a significant name word) appears
     in the result host or title. We never write "who.int" in code — we match
     the citation's own token against the result;
  3. it is *title-coherent*: the result title overlaps the cited title.

Everything tunable (authoritative domain classes, organisational-author words,
report-like words, thresholds) lives in ``config/institutional_sources.json`` and
can be edited without touching this module.

Honesty: a report recovered here is the primary document, but its identity was
established by web search of a non-scholarly, link-less citation — not by a
strong identifier or a scholarly index. The result is therefore marked
``resolution_basis="institutional_web"`` with medium existence confidence, and it
carries ``identity_basis="web_search"`` so the fetch stage stores the text as
``externally_corroborated_text`` (capped reliability — never a fully-green claim).
"""

from __future__ import annotations

import functools
import html
import json
import os
import re
import urllib.parse

NAME = "institutional_search"
RESOLVE_NAME = "institutional_search"
MANIFEST = {"origin": "websearch"}
# Invoked only through a dedicated last-resort gate in core.resolve (never adopted
# by the optional metadata stage), so an institutional web copy can add a
# resolution but never displace an authoritative index match.
OPTIONAL_STAGE = False

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "institutional_sources.json")

ENV_SEARCH_URL = "CITATION_VERIFIER_SEARCH_URL"
_DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={q}"

# Words that never carry organisational identity (skipped when deriving an acronym
# or matching significant name words for coherence).
_NAME_STOPWORDS = frozenset({
    "of", "and", "the", "for", "on", "in", "to", "a", "an", "de", "del", "della",
    "di", "la", "le", "les", "des", "du", "el", "und", "der", "die", "das",
})

_RESULT_A = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
#  Citation-side detection                                                     #
# --------------------------------------------------------------------------- #

def _author_block(ref: dict) -> str:
    """The leading author segment of the citation (before the year / first stop)."""
    raw = str(ref.get("raw_entry") or "").strip()
    if not raw:
        return ""
    # Cut at the first year, or the first sentence stop, whichever comes first.
    cut = len(raw)
    year = re.search(r"\(?(1[89]\d{2}|20\d{2})\)?", raw)
    if year:
        cut = min(cut, year.start())
    stop = re.search(r"\.\s", raw)
    if stop:
        cut = min(cut, stop.start())
    return raw[:cut].strip(" .,")


def _org_acronyms(text: str, cfg: dict) -> list[str]:
    """Organisational acronyms in an author block, WITHOUT mistaking Vancouver
    author initials ("Smith JM, Jones KL") for one. An institutional citation
    leads with the body ("WHO. ...", "EMA: ...") or spells it out with a
    parenthetical acronym ("World Health Organization (WHO)"); author initials do
    neither (a surname always precedes them)."""
    lo = cfg.get("acronym_min_len", 2)
    hi = cfg.get("acronym_max_len", 7)
    out: list[str] = []
    lead = re.match(r"\s*([A-Z][A-Z&]{%d,%d})\b" % (max(lo - 1, 0), hi - 1), text)
    if lead:
        out.append(lead.group(1))
    out.extend(re.findall(r"\(([A-Z][A-Z&]{%d,%d})\)" % (max(lo - 1, 0), hi - 1), text))
    cleaned = [t.replace("&", "").lower() for t in out]
    return [t for t in cleaned if lo <= len(t) <= hi]


def _significant_words(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]+", text)
    return [w.lower() for w in words if w.lower() not in _NAME_STOPWORDS]


def _derived_acronym(text: str) -> str | None:
    """Initials of the significant name words (World Health Organization -> who)."""
    initials = "".join(w[0] for w in _significant_words(text))
    return initials.lower() if len(initials) >= 2 else None


def _identity_tokens(ref: dict, cfg: dict) -> list[str]:
    """Tokens that should recur in the real report's host/title if it is the same
    institution: explicit acronyms, a derived acronym, and significant name words."""
    block = _author_block(ref)
    spelled = re.sub(r"\([^)]*\)", " ", block)  # drop a parenthetical acronym before deriving one
    tokens: list[str] = []
    tokens.extend(_org_acronyms(block, cfg))
    derived = _derived_acronym(spelled)
    if derived:
        tokens.append(derived)
    tokens.extend(w for w in _significant_words(spelled) if len(w) >= 4)
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in tokens:
        if tok and tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def _is_organisational_author(ref: dict, cfg: dict) -> bool:
    block = _author_block(ref)
    if not block:
        return False
    low = block.lower()
    if any(re.search(r"(?<!\w)" + re.escape(str(word).strip().lower()) + r"(?!\w)", low)
           for word in cfg.get("org_author_words", [])):
        return True
    return bool(_org_acronyms(block, cfg))


def _looks_report_like(ref: dict, cfg: dict) -> bool:
    blob = " ".join(str(ref.get(k) or "") for k in ("title", "raw_entry", "source_type")).lower()
    return any(word in blob for word in cfg.get("report_like_words", []))


def is_institutional_report(ref: dict) -> bool:
    """A citation whose author is an organisation (report-like content strengthens,
    but an organisational author alone qualifies — WHO/FDA cites are often bare)."""
    cfg = _config()
    if not _is_organisational_author(ref, cfg):
        return False
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref) or ref.get("title")
    if not title or len(_significant_words(title)) < cfg.get("min_title_words", 3):
        # A bare organisational author with no distinctive title is not actionable,
        # unless the entry clearly reads as a report.
        return _looks_report_like(ref, cfg)
    return True


def supports(ref: dict) -> bool:
    if ref.get("url"):
        return False
    return is_institutional_report(ref)


# --------------------------------------------------------------------------- #
#  Web search + result discrimination                                         #
# --------------------------------------------------------------------------- #

def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _real_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.endswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return href


def _search(query: str, cfg: dict, get_fn=None) -> list[dict] | None:
    return _search_with_provenance(query, cfg, get_fn=get_fn)[0]


def _search_with_provenance(query: str, cfg: dict, get_fn=None) -> tuple[list[dict] | None, dict]:
    """Run one web search.

    Returns the results as ``[{"url","title"}]``, an empty list when the engine
    answered but knows of nothing, or ``None`` when it refused to answer at all.
    That last distinction matters: DuckDuckGo soft-blocks an automated client with
    an anti-bot interstitial served as **HTTP 202** carrying no results, and an
    engine that would not talk to us is not evidence that the report is absent.
    Never raises.

    Without ``get_fn`` the query goes through ``core.search``, which tries the
    configured backends in order (an API first, HTML scraping only as a tail) and
    preserves exactly this list/None distinction. ``get_fn`` stays as the single
    injected seam: tests drive the legacy HTML path through it without touching
    the network or needing any API key.
    """
    if not query:
        return [], {"outcome": "answered", "selected_backend": None, "attempts": []}
    if get_fn is None:
        from core.search import router as _search_router
        return _search_router.search_with_provenance(query, max_results=cfg.get("max_results", 6))
    template = os.environ.get(ENV_SEARCH_URL) or _DEFAULT_SEARCH_URL
    url = template.format(q=urllib.parse.quote(query))
    def status_or_none(value):
        try:
            code = int(value)
        except (TypeError, ValueError):
            return None
        return code if 100 <= code <= 599 else None
    try:
        status, body = get_fn(url, accept="text/html,*/*")
    except Exception:
        return None, {"outcome": "refused", "selected_backend": None, "attempts": [{
            "backend": "legacy_html", "outcome": "refused", "transport_outcome": "network_error",
        }]}
    if not 200 <= int(status or 0) < 300 or int(status or 0) == 202:
        observed_status = status_or_none(status)
        return None, {"outcome": "refused", "selected_backend": None, "attempts": [{
            "backend": "legacy_html", "outcome": "refused", "transport_outcome": "http_error", "http_status": observed_status,
        }]}
    if not body:
        # A 2xx with nothing in it is still an answer, not a refusal.
        return [], {"outcome": "answered", "selected_backend": "legacy_html", "attempts": [{
            "backend": "legacy_html", "outcome": "answered", "transport_outcome": "response", "http_status": int(status),
        }]}
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    out: list[dict] = []
    seen: set[str] = set()
    for href, title_frag in _RESULT_A.findall(text):
        real = _real_url(href)
        if not real or real in seen:
            continue
        seen.add(real)
        out.append({"url": real, "title": _clean(title_frag)})
        if len(out) >= cfg.get("max_results", 6):
            break
    return out, {"outcome": "answered", "selected_backend": "legacy_html", "attempts": [{
        "backend": "legacy_html", "outcome": "answered", "transport_outcome": "response", "http_status": int(status),
    }]}


def _host_labels(url: str) -> list[str]:
    host = urllib.parse.urlparse(url).netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return [lbl for lbl in host.split(".") if lbl]


def _is_authoritative(url: str, cfg: dict) -> bool:
    labels = _host_labels(url)
    if not labels:
        return False
    tld_labels = {t.lstrip(".").lower() for t in cfg.get("authoritative_tlds", [])}
    if tld_labels & set(labels):
        return True
    host = ".".join(labels)
    for pattern in cfg.get("authoritative_domain_patterns", []):
        pat = str(pattern).strip().lower().lstrip(".")
        if host == pat or host.endswith("." + pat):
            return True
    return False


def _coherence_tier(tokens: list[str], result: dict) -> str | None:
    """Strength of the citation<->result institution match, or None:
      "host"  — an identity token is a host label (who.int for a WHO cite): strong,
                pins the document to the institution's own domain;
      "title" — a token recurs only in the result title: weak corroboration, used
                only when no host-coherent result exists (many bodies sit on a
                shared domain, e.g. europa.eu, where the acronym is not a label)."""
    if not tokens:
        return None
    labels = set(_host_labels(result.get("url", "")))
    if any(tok in labels for tok in tokens):
        return "host"
    title_words = set(_significant_words(result.get("title") or ""))
    if any(tok in title_words for tok in tokens):
        return "title"
    return None


def _title_coherent(cited_title: str, result: dict, cfg: dict) -> bool:
    """Directional containment: how much of the cited title's significant wording
    recurs in the result title. Robust to the institution suffix web results carry
    (e.g. "... - European Commission"), which a symmetric overlap would penalise."""
    cited = _significant_words(cited_title)
    result_words = set(_significant_words(result.get("title") or ""))
    if not cited or not result_words:
        return False
    hit = sum(1 for w in cited if w in result_words)
    return (hit / len(cited)) >= cfg.get("title_overlap_min", 0.6)


def _content_type(url: str) -> str:
    return "pdf" if url.lower().split("?")[0].endswith(".pdf") else "html"


def _choose(ref: dict, results: list[dict], cfg: dict) -> dict | None:
    """Pick the report. A candidate must be authoritative AND title-coherent AND
    institution-coherent; a host-level institution match is preferred over a
    title-only one, so who.int wins over a cdc.gov page that merely names the WHO."""
    from core.resolve import service as resolve_mod

    cited_title = resolve_mod._article_title_candidate(ref) or ref.get("title") or ""
    tokens = _identity_tokens(ref, cfg)
    host_match = None
    title_match = None
    for result in results:
        if not _is_authoritative(result.get("url", ""), cfg):
            continue
        if not _title_coherent(cited_title, result, cfg):
            continue
        tier = _coherence_tier(tokens, result)
        if tier == "host" and host_match is None:
            host_match = result
        elif tier == "title" and title_match is None:
            title_match = result
    return host_match or title_match


def _build_query(ref: dict, cfg: dict) -> str:
    from core.resolve import service as resolve_mod

    title = resolve_mod._article_title_candidate(ref) or ref.get("title") or ""
    parts = [f'"{title}"'] if title else []
    block = _author_block(ref)
    if block:
        parts.append(block)
    year = ref.get("year")
    if year:
        parts.append(str(year))
    return " ".join(parts).strip()


def discover(ref: dict, get_fn=None) -> dict | None:
    from core.resolve import service as resolve_mod

    cfg = _config()
    if not is_institutional_report(ref):
        return None
    title = resolve_mod._article_title_candidate(ref) or ref.get("title")
    if not title:
        return {"status": "unverified", "via": NAME,
                "reason": "no usable title for institutional web lookup"}

    query = _build_query(ref, cfg)
    results, provenance = _search_with_provenance(query, cfg, get_fn=get_fn)
    from core.resolve import transport_telemetry
    transport_telemetry.record_institutional_search(query, provenance, results or [])
    if results is None:
        # The engine refused (anti-bot interstitial, rate limit, network). That is
        # a statement about us, not about the report — report it as retryable so it
        # never masquerades as "this report does not exist".
        return {"status": "unresolved", "via": NAME,
                "reason": "web search engine did not answer (blocked or rate-limited)"}
    if not results:
        return {"status": "unverified", "via": NAME,
                "reason": "no web results for institutional report",
                "resolution_basis": "institutional_web", "existence_confidence": "low"}

    chosen = _choose(ref, results, cfg)
    if chosen is None:
        return {"status": "unverified", "via": NAME,
                "reason": "no authoritative, institution-coherent web result",
                "resolution_basis": "institutional_web", "existence_confidence": "low"}

    url = chosen["url"]
    return {
        "status": "resolved",
        "via": NAME,
        "matched_title": chosen.get("title") or title,
        "reason": "institutional report recovered from an authoritative web source",
        "resolution_basis": "institutional_web",
        # Web-recovered, non-scholarly, link-less: never a strong identifier, so
        # confidence is deliberately capped below "high".
        "existence_confidence": "medium",
        # Consumed by the fetch stage: store the retrieved text as
        # externally_corroborated_text so the claim can never be fully green.
        "identity_basis": "web_search",
        "retracted": False,
        "fulltext_exists": True,
        "oa_status": "open",
        "work_type": "report",
        "fulltext_links": [{"url": url, "content_type": _content_type(url)}],
    }
